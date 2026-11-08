"""扫描与清理逻辑：纯函数 + QThread worker。

判重 / 孤儿目录检测 / 安装信息采集等核心逻辑抽成纯函数，与 Qt 解耦，便于无 GUI 测试；
QThread 只负责调用它们并发射信号。

安全性设计：
- 卸载残留只识别「不在任何已安装软件目录下」的安装/卸载器文件，以及「疑似残留目录」；
- 疑似残留目录默认不勾选（recommended=False），只有用户手动勾选后才会被「清理选中项」删除；
- 扫描与删除都会跳过系统目录（Windows、WindowsApps 等），并在删除前做保护性校验。
"""
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from PySide6.QtCore import QThread, Signal

# 垃圾文件扩展名
JUNK_EXTS = {'.tmp', '.log', '.bak', '.old', '.chk', '.gid', '.dmp',
             '.cache', '.thumbcache', '.etl', '.blf', '.regtrans-ms'}

# 整盘遍历时跳过的系统目录（小写全路径，硬编码仅针对系统盘）
SKIP_DIRS = {
    'c:\\windows\\winsxs', 'c:\\windows\\system32', 'c:\\windows\\syswow64',
    'c:\\$recycle.bin', 'c:\\system volume information',
    'c:\\programdata\\microsoft\\search', 'c:\\perflogs'
}

DUP_SIZE_THRESHOLD = 1024   # 仅对大于该字节数的文件做内容哈希判重
BATCH_SIZE = 2000           # 单次信号批量大小
CLEAN_BATCH_SIZE = 200      # 清理时删除/失败信号的批量阈值

# 重复文件判重漏斗参数
SAMPLE_SIZE = 16 * 1024             # 每个抽样段 16KB（头/中/尾各一段）
SMALL_FILE_LIMIT = 3 * SAMPLE_SIZE  # <=48KB：三段抽样已铺满全文，直接全文哈希一次
HASH_CHUNK_SIZE = 2 * 1024 * 1024   # 全量哈希分块：兼顾停止响应与循环开销
MAX_INFLIGHT = 64                   # 在途哈希任务上限，避免大分组一次性创建海量 Future
PROGRESS_POLL = 0.2                 # 协调线程轮询停止标志的间隔（秒）
PHASE1_MAX_FILES = 2048             # 阶段 1 每批文件数：跨大小组并行，同时限制摘要内存

SCAN_CATEGORIES = ("uninstall", "duplicate", "empty")  # 可单独扫描的分类

# 高置信度的「安装/卸载器」文件名（视为卸载残留）
INSTALLER_NAMES = {"uninstall.exe", "uninstaller.exe", "setup.exe", "installer.exe"}

# 孤儿目录检测时跳过的一级目录名（小写）
ORPHAN_EXCLUSIONS = {
    "common files", "internet explorer", "windowsapps", "modifiablewindowsapps",
    "windows defender", "windows mail", "windows media player", "windows nt",
    "windows photo viewer", "windows portable devices", "windows security",
    "windows sidebar", "windows powershell", "windows powerpack",
    "windows multimedia platform", "windows kits", "reference assemblies",
    "msbuild", "microsoft", "microsoft shared", "microsoft.net", "microsoft sdk",
    "microsoft sdks", "microsoft help", "microsoft edge", "microsoft edgecore",
    "microsoftonedrive", "onedrive", "microsoft office", "microsoft visual studio",
    "microsoft sql server", "xbox", "dotnet",
}


def _norm(path):
    return os.path.normcase(os.path.normpath(path.rstrip("\\/")))


def _is_protected(path_norm, protected_set):
    """path_norm 是否位于 protected_set 中任一目录之下（含相等）。逐级向上查父目录。"""
    if not protected_set:
        return False
    d = path_norm
    while True:
        if d in protected_set:
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _overlaps(dir_norm, dirs):
    """dir_norm 与 dirs 中任意目录相同、互为祖先/后代。"""
    for d in dirs:
        if dir_norm == d or dir_norm.startswith(d + os.sep) or d.startswith(dir_norm + os.sep):
            return True
    return False


def file_hash(path, chunk_size=HASH_CHUNK_SIZE, *, size=None, should_stop=None):
    """全量 blake2b 摘要（判重用，非安全用途）。

    size 给定时在读取前后复核文件尺寸；返回 None 表示被停止、文件在读取
    期间发生变化或不可读。缓冲区按文件大小分配，避免小文件也初始化数 MB。
    """
    h = hashlib.blake2b()
    try:
        with open(path, 'rb') as f:
            st = os.fstat(f.fileno())
            if size is not None and st.st_size != size:
                return None
            mtime = st.st_mtime_ns
            buf = bytearray(max(1, min(chunk_size, st.st_size)))
            view = memoryview(buf)
            total = 0
            while True:
                if should_stop is not None and should_stop():
                    return None
                n = f.readinto(view)
                if not n:
                    break
                h.update(view[:n])
                total += n
            st2 = os.fstat(f.fileno())
            if total != st.st_size:            # 读取期间增长或截断
                return None
            if size is not None and st2.st_size != size:
                return None
            if st2.st_mtime_ns != mtime:       # 读取期间被改写
                return None
    except OSError:
        return None
    return h.digest()


def _sample_ranges(size, sample=SAMPLE_SIZE):
    """头/中/尾三段抽样区间，返回升序、合并后的 (start, length)。"""
    if size <= sample:
        return [(0, size)]
    mid = max(0, min(size // 2 - sample // 2, size - sample))
    ranges = sorted([(0, sample), (mid, sample), (size - sample, sample)])
    merged = []
    for start, length in ranges:
        end = start + length
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(s, e - s) for s, e in merged]


def sample_hash(path, size, sample=SAMPLE_SIZE, should_stop=None):
    """对文件头/中/尾抽样做 blake2b，作为判重的廉价预筛签名。

    返回 None 表示被停止、尺寸/mtime 变化或不可读。抽样只是预筛，
    幸存的组仍必须经过 file_hash 全量确认，绝不会仅凭抽样判定重复。
    """
    try:
        with open(path, 'rb') as f:
            st = os.fstat(f.fileno())
            if st.st_size != size:
                return None
            mtime = st.st_mtime_ns
            h = hashlib.blake2b()
            for start, length in _sample_ranges(size, sample):
                if should_stop is not None and should_stop():
                    return None
                f.seek(start)
                remaining = length
                while remaining:
                    if should_stop is not None and should_stop():
                        return None
                    chunk = f.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        return None
                    h.update(chunk)
                    remaining -= len(chunk)
            st2 = os.fstat(f.fileno())
            if st2.st_size != size or st2.st_mtime_ns != mtime:
                return None
    except OSError:
        return None
    return h.digest()


def _phase1_digest(path, size, should_stop=None):
    """阶段 1 摘要：小文件直接全文哈希（结果即最终结果），大文件头/中/尾抽样。"""
    if size <= SMALL_FILE_LIMIT:
        return file_hash(path, size=size, should_stop=should_stop)
    return sample_hash(path, size=size, should_stop=should_stop)


def quick_hash(path, head_tail=4096):
    """legacy：首尾各 head_tail 字节的旧预筛签名（已由 sample_hash 取代）。"""
    h = hashlib.blake2b()
    with open(path, 'rb') as f:
        h.update(f.read(head_tail))
        f.seek(0, os.SEEK_END)
        if f.tell() > head_tail:
            f.seek(-head_tail, os.SEEK_END)
            h.update(f.read(head_tail))
    return h.digest()


def dir_size(path):
    """递归统计目录总大小（跳过符号链接，容错）。"""
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += dir_size(entry.path)
            except OSError:
                continue
    except OSError:
        pass
    return total


def _extract_dir(value):
    """从注册表字符串值里提取一个绝对目录（InstallLocation/DisplayIcon/UninstallString）。"""
    if not value:
        return None
    s = str(value).strip().strip('"')
    if not s:
        return None
    # DisplayIcon 可能是 "C:\\path\\app.exe,0"
    s = s.split(",", 1)[0].strip().strip('"')
    # UninstallString 可能是 "C:\\path\\unins000.exe" /S 或 MsiExec.exe /I{...}
    if not os.path.isabs(s):
        return None
    if os.path.isdir(s):
        return _norm(s)
    d = os.path.dirname(s)
    if os.path.isdir(d):
        return _norm(d)
    return None


def collect_installed_dirs():
    """枚举卸载注册表项，返回所有能定位到磁盘目录的「已安装软件目录」规范化集合。"""
    dirs = set()
    try:
        import winreg
    except ImportError:
        return dirs
    uninstall_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for up in uninstall_paths:
            try:
                key = winreg.OpenKey(hive, up)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, sub_name) as sub:
                            for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    v, _ = winreg.QueryValueEx(sub, val_name)
                                except OSError:
                                    continue
                                d = _extract_dir(v)
                                if d:
                                    dirs.add(d)
                    except OSError:
                        continue
    return dirs


def collect_app_paths_dirs():
    """枚举注册表 App Paths，返回被注册的可执行文件所在目录（可能仍在使用）。"""
    dirs = set()
    try:
        import winreg
    except ImportError:
        return dirs
    app_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
    ]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for ap in app_paths:
            try:
                key = winreg.OpenKey(hive, ap)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, sub_name) as sub:
                            v, _ = winreg.QueryValueEx(sub, None)
                    except OSError:
                        continue
                    d = _extract_dir(v)
                    if d:
                        dirs.add(d)
    return dirs


def collect_service_dirs():
    """枚举系统服务 ImagePath，返回服务二进制所在目录（仍在运行）。"""
    dirs = set()
    try:
        import winreg
    except ImportError:
        return dirs
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services")
    except OSError:
        return dirs
    with key:
        i = 0
        while True:
            try:
                sub_name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(key, sub_name) as sub:
                    v, _ = winreg.QueryValueEx(sub, "ImagePath")
            except OSError:
                continue
            d = _extract_dir(v)
            if d:
                dirs.add(d)
    return dirs


def program_roots_for_drive(drive_path):
    """返回位于所扫描磁盘上的「程序安装根目录」列表。"""
    drive = _norm(drive_path)
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env)
        if v and _norm(v).startswith(drive + os.sep):
            roots.append(v)
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        lp = os.path.join(lad, "Programs")
        if _norm(lp).startswith(drive + os.sep):
            roots.append(lp)
    return roots


def _orphan_dirs_in_roots(roots, installed_dirs, in_use_dirs):
    orphans = []
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            if name.lower() in ORPHAN_EXCLUSIONS:
                continue
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            norm = _norm(full)
            if any(norm == p or p.startswith(norm + os.sep) for p in installed_dirs):
                continue
            # 排除仍被系统引用（App Paths / 服务 ImagePath）的在用目录
            if _overlaps(norm, in_use_dirs):
                continue
            orphans.append(full)
    return orphans


def find_orphan_dirs(drive_path, installed_dirs, in_use_dirs):
    """在一级目录层面查找「疑似卸载残留目录」：安装根下既不属于任何已安装程序、
    也不被系统（App Paths / 服务）引用的目录。"""
    return _orphan_dirs_in_roots(program_roots_for_drive(drive_path), installed_dirs, in_use_dirs)


def is_installer_leftover(filename):
    """判断文件名是否为高置信度的安装/卸载器（卸载残留特征）。"""
    lower = filename.lower()
    if lower in INSTALLER_NAMES:
        return True
    return lower.startswith("unins") and lower.endswith((".exe", ".dat", ".msg"))


def _walk_dirs(top, skip_dirs, should_stop):
    """用 os.scandir 栈式 DFS 遍历目录树，产出 (dirpath, dir_entries, file_entries, has_other)。

    相比 os.walk：DirEntry 自带 FindFirstFile 缓存的元数据，后续可用
    entry.stat(follow_symlinks=False) 零系统调用取文件大小，省掉逐文件 os.stat。
    不跟随符号链接/联接（is_symlink 先行过滤，避免循环）；skip_dirs（小写全路径）
    中的目录不进入。has_other 表示目录里存在符号链接等非文件/非目录条目，
    用于「空目录」判定时不被误判为空。
    """
    stack = [top]
    while stack:
        if should_stop and should_stop():
            break
        root = stack.pop()
        try:
            with os.scandir(root) as it:
                entries = list(it)
        except (PermissionError, OSError):
            continue

        dir_entries = []
        file_entries = []
        has_other = False
        for entry in entries:
            try:
                if entry.is_symlink():
                    has_other = True          # 符号链接/联接：不跟随、不扫描
                elif entry.is_dir(follow_symlinks=False):
                    dir_entries.append(entry)
                elif entry.is_file(follow_symlinks=False):
                    file_entries.append(entry)
                else:
                    has_other = True
            except OSError:
                has_other = True

        yield root, dir_entries, file_entries, has_other

        for d in dir_entries:
            if should_stop and should_stop():
                break
            if d.path.lower() not in skip_dirs:
                stack.append(d.path)


def _iter_hashed(executor, tasks, fn, should_stop, max_inflight=MAX_INFLIGHT):
    """有界并发执行哈希，按完成顺序产出 (path, size, digest|None)。

    tasks 是 (path, size) 可迭代对象；在途任务数始终不超过 max_inflight，
    因此单个大小组即使有数万文件也不会一次性创建海量 Future。
    """
    pending = {}                      # Future -> (path, size)
    it = iter(tasks)
    try:
        while True:
            while len(pending) < max_inflight:
                if should_stop():
                    return
                try:
                    path, size = next(it)
                except StopIteration:
                    break
                fut = executor.submit(fn, path, size=size, should_stop=should_stop)
                pending[fut] = (path, size)

            if not pending:
                return

            done, _ = wait(pending, timeout=PROGRESS_POLL,
                           return_when=FIRST_COMPLETED)
            if not done:
                continue

            for fut in done:
                path, size = pending.pop(fut)
                try:
                    digest = fut.result()
                except Exception:
                    # 哈希函数已把 OSError 降级为 None；其余异常同样跳过该项
                    digest = None
                yield path, size, digest

            if should_stop():
                return
    finally:
        for fut in pending:
            fut.cancel()              # 只能取消尚未开始的任务


def _iter_duplicate_rows(paths, size, protected):
    """把一组全量内容相同的文件展开为 (path, size, recommended)。

    保留路径最短的一份；受保护副本优先保留且一律 recommended=False。
    完成顺序不确定，先按 (长度, 路径) 排序，保证保留结果确定。
    """
    ordered = sorted(paths, key=lambda p: (len(p), p))
    pro, unpro = [], []
    for p in ordered:
        (pro if _is_protected(_norm(p), protected) else unpro).append(p)

    if pro:
        keep = pro[0]
        yield (keep, size, False)
        for p in unpro:
            yield (p, size, True)          # 未受保护副本可删
        for p in pro:
            if p != keep:
                yield (p, size, False)     # 其余受保护副本不推荐删除
    else:
        keep = unpro[0]
        yield (keep, size, False)
        for p in unpro:
            if p != keep:
                yield (p, size, True)


def find_duplicates(size_groups, progress_cb=None, should_stop=None,
                    protected=None, max_workers=8, status_cb=None):
    """对 {size: [paths]} 分组做「抽样预筛 + 全量确认」两阶段判重。

    第 1 阶段（进度 0-40%）：
    - <=48KB 的文件直接做一次全文哈希，结果即最终结果；
    - 更大的文件做头/中/尾各 16KB 抽样哈希，只有摘要相同的组进入第 2 阶段。
    第 2 阶段（进度 40-99%）：对抽样幸存组做全量 blake2b，按字节数推进进度。

    生成器：逐项产出 (path, size, recommended)；同一内容的一组里保留路径最短
    的一份（recommended=False），其余标记为推荐删除。受保护目录（protected）下的
    副本一律 recommended=False，只推荐删除未受保护的副本。
    """
    protected = protected or set()
    candidates = [(s, ps) for s, ps in size_groups.items() if len(ps) >= 2]
    total_files = sum(len(ps) for _, ps in candidates)
    if not total_files:
        if progress_cb:
            progress_cb(100)
        return

    executor = ThreadPoolExecutor(max_workers=max_workers)
    aborted = False

    def stopping():
        return aborted or (should_stop is not None and should_stop())

    files_done = 0
    last_pct = -1

    def emit_pct(pct):
        nonlocal last_pct
        pct = max(0, min(100, pct))
        if pct > last_pct:
            last_pct = pct
            if progress_cb:
                progress_cb(pct)

    sample_groups = []        # 抽样幸存且组内 >=2 的 (size, paths)

    def phase1_batch_rows(batch):
        """哈希一批完整的大小组；小文件组直接产出，大文件组留给阶段 2。"""
        nonlocal files_done
        tasks = ((p, size) for size, paths in batch for p in paths)
        groups = {}
        for path, size, digest in _iter_hashed(
                executor, tasks, _phase1_digest, stopping):
            files_done += 1
            emit_pct((files_done * 40) // total_files)
            if digest is not None:
                groups.setdefault((size, digest), []).append(path)
        for (size, _digest), group in groups.items():
            if len(group) < 2:
                continue
            if size <= SMALL_FILE_LIMIT:
                # 小文件：该摘要已是全文摘要，可直接判定
                yield from _iter_duplicate_rows(group, size, protected)
            else:
                sample_groups.append((size, group))

    try:
        # ---------- 阶段 1：大小分组 + 抽样预筛（0-40%） ----------
        # 按文件数分批（而非逐个大小组）提交，让小分组的任务也能填满线程池。
        if status_cb:
            status_cb(f"正在抽样预筛 {total_files} 个候选文件...")
        batch, batch_files = [], 0
        for size, paths in candidates:
            if stopping():
                break
            batch.append((size, paths))
            batch_files += len(paths)
            if batch_files >= PHASE1_MAX_FILES:
                yield from phase1_batch_rows(batch)
                if stopping():
                    break
                batch, batch_files = [], 0
        if batch and not stopping():
            yield from phase1_batch_rows(batch)

        # ---------- 阶段 2：全量内容确认（40-99%） ----------
        if not stopping():
            emit_pct(40)
            total_bytes = sum(size * len(group) for size, group in sample_groups)
            bytes_done = 0
            if sample_groups and status_cb:
                suspect_count = sum(len(g) for _, g in sample_groups)
                status_cb(f"正在全量校验 {suspect_count} 个疑似重复文件...")
            # 所有幸存组一次性提交，避免小组只有 2 个任务时线程池空转。
            tasks = ((p, size) for size, group in sample_groups for p in group)
            full_groups = {}
            for path, size, digest in _iter_hashed(
                    executor, tasks, file_hash, stopping):
                bytes_done += size
                if total_bytes:
                    emit_pct(40 + (bytes_done * 59) // total_bytes)
                if digest is not None:
                    full_groups.setdefault((size, digest), []).append(path)
            # 即使中途停止，已拿到完整全文摘要的组仍是真实重复项，可以安全产出
            for (size, _digest), group in full_groups.items():
                if len(group) >= 2:
                    yield from _iter_duplicate_rows(group, size, protected)
    finally:
        aborted = True
        executor.shutdown(wait=True, cancel_futures=True)

    emit_pct(100)


def protected_roots():
    """返回禁止删除的保护目录（系统目录等）的规范化集合。"""
    roots = []
    sr = os.environ.get("SystemRoot")
    if sr:
        roots.append(_norm(sr))
    pf = os.environ.get("ProgramFiles")
    if pf:
        roots.append(_norm(os.path.join(pf, "WindowsApps")))
        roots.append(_norm(os.path.join(pf, "ModifiableWindowsApps")))
    return roots


def collect_protected_dirs():
    """返回禁止删除的目录集合：系统目录 + 已安装程序目录 + 在用目录。"""
    dirs = set(protected_roots())
    dirs |= collect_installed_dirs()
    dirs |= collect_app_paths_dirs()
    dirs |= collect_service_dirs()
    return dirs


class ScanWorker(QThread):
    phase = Signal(str)              # walking / hashing / done
    status = Signal(str)
    progress = Signal(int)           # 哈希阶段 0-100
    batch_found = Signal(str, list)  # category, [(ftype, path, size, recommended)]
    finished = Signal(dict)

    def __init__(self, drive_path, categories=None):
        super().__init__()
        self.drive_path = drive_path
        if categories is None:
            self.categories = set(SCAN_CATEGORIES)
        else:
            self.categories = set(categories) & set(SCAN_CATEGORIES)
            if not self.categories:  # 空集合按全量处理，避免空转
                self.categories = set(SCAN_CATEGORIES)
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        cats = self.categories
        counts = {"uninstall": 0, "duplicate": 0, "empty": 0}

        # 阶段 1：注册表/保护目录 + 孤儿目录
        # 孤儿目录始终检测：protected 依赖注册表集合，且 orphan_dirs 会并入 skip_dirs，
        # 必须与全量扫描保持一致，单类扫描结果才是全量扫描的子集。
        self.phase.emit("walking")
        self.status.emit("正在准备扫描...")
        installed_dirs = collect_installed_dirs()
        in_use_dirs = collect_app_paths_dirs() | collect_service_dirs()
        protected = set(protected_roots()) | installed_dirs | in_use_dirs
        orphan_dirs = find_orphan_dirs(self.drive_path, installed_dirs, in_use_dirs)
        if "uninstall" in cats and orphan_dirs:
            items = [("卸载残留目录（疑似）", d, dir_size(d), False) for d in orphan_dirs]
            counts["uninstall"] += len(items)
            self.batch_found.emit("uninstall", items)

        # 扫描时跳过的目录：系统目录 + 当前磁盘的回收站/系统卷信息等 + 疑似残留目录
        skip_dirs = set(SKIP_DIRS) | {d.lower() for d in orphan_dirs}
        drive = self.drive_path
        for name in ("$Recycle.Bin", "System Volume Information",
                     "$Windows.~BT", "$Windows.~WS", "PerfLogs", "Recovery"):
            skip_dirs.add(os.path.join(drive, name).lower())
        sr = os.environ.get("SystemRoot")
        if sr:
            skip_dirs.add(sr.rstrip("\\/").lower())
        pf = os.environ.get("ProgramFiles")
        if pf:
            skip_dirs.add(os.path.join(pf, "WindowsApps").lower())
            skip_dirs.add(os.path.join(pf, "ModifiableWindowsApps").lower())

        size_groups = {}
        buf = {"uninstall": [], "duplicate": [], "empty": []}
        processed = 0
        dup_candidates = 0

        def status_text():
            if "uninstall" in cats and "empty" in cats:
                return f"正在扫描卸载残留... 残留 {counts['uninstall']} | 空 {counts['empty']}"
            if cats == {"duplicate"}:
                return f"正在扫描重复文件... 判重候选 {dup_candidates}"
            if cats == {"uninstall"}:
                return f"正在扫描卸载残留... 残留 {counts['uninstall']}"
            if cats == {"empty"}:
                return f"正在扫描空文件或空文件夹... 空 {counts['empty']}"
            return "正在扫描..."

        # 阶段 2：主遍历（scandir 缓存元数据，省去逐文件 stat）
        should_stop = lambda: not self._is_running
        for root, dir_entries, file_entries, has_other in _walk_dirs(
                self.drive_path, skip_dirs, should_stop):
            processed += 1

            if "empty" in cats and not file_entries and not dir_entries and not has_other:
                rec = not _is_protected(_norm(root), protected)
                buf["empty"].append(("空文件夹", root, 0, rec))
                counts["empty"] += 1

            for entry in file_entries:
                filepath = entry.path
                file = entry.name
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue

                if size == 0:
                    if "empty" in cats:
                        rec = not _is_protected(_norm(filepath), protected)
                        buf["empty"].append(("空文件", filepath, 0, rec))
                        counts["empty"] += 1
                    continue

                if "uninstall" in cats:
                    ext = os.path.splitext(file)[1].lower()
                    if (is_installer_leftover(file)
                            and not _overlaps(_norm(root), installed_dirs)):
                        rec = not _is_protected(_norm(filepath), protected)
                        buf["uninstall"].append(("卸载残留", filepath, size, rec))
                        counts["uninstall"] += 1
                    elif ext in JUNK_EXTS:
                        rec = not _is_protected(_norm(filepath), protected)
                        label = f"{ext.upper().replace('.', '')} 临时文件"
                        buf["uninstall"].append((label, filepath, size, rec))
                        counts["uninstall"] += 1

                if "duplicate" in cats and size > DUP_SIZE_THRESHOLD:
                    size_groups.setdefault(size, []).append(filepath)
                    dup_candidates += 1

            for cat in buf:
                if len(buf[cat]) >= BATCH_SIZE:
                    self.batch_found.emit(cat, buf[cat])
                    buf[cat] = []

            if self._is_running and processed % 200 == 0:
                self.status.emit(status_text())

        for cat in buf:
            if buf[cat]:
                self.batch_found.emit(cat, buf[cat])

        # 阶段 3：内容哈希判重（仅选中 duplicate 且未在遍历阶段被停止时执行）
        if "duplicate" in cats and self._is_running:
            self.phase.emit("hashing")
            dup_buf = []
            for path, size, recommended in find_duplicates(
                    size_groups,
                    progress_cb=self.progress.emit,
                    should_stop=lambda: not self._is_running,
                    protected=protected,
                    status_cb=self.status.emit):
                dup_buf.append(("重复文件", path, size, recommended))
                counts["duplicate"] += 1
                if len(dup_buf) >= BATCH_SIZE:
                    self.batch_found.emit("duplicate", dup_buf)
                    dup_buf = []

            if dup_buf:
                self.batch_found.emit("duplicate", dup_buf)

        self.phase.emit("done")
        self.finished.emit(counts)


class CleanWorker(QThread):
    progress = Signal(int)        # 0-100
    deleted = Signal(list)        # 批量：成功移入回收站的路径
    failed = Signal(list)         # 批量：(path, size) 无法移入回收站的项
    finished = Signal(int, int)   # 成功数, 失败数

    def __init__(self, items):
        super().__init__()
        self.items = list(items)  # [(path, size), ...]
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        from send2trash import send2trash
        protected = collect_protected_dirs()
        total = len(self.items)
        ok = 0
        fail = 0
        done = 0
        deleted_batch = []
        failed_items = []          # [(path, size), ...]
        last_pct = -1

        def emit_progress():
            nonlocal last_pct
            pct = int(done / total * 100) if total else 100
            if pct != last_pct:
                last_pct = pct
                self.progress.emit(pct)

        # 1) 保护目录项直接跳过（不碰 Shell，最快）
        movable = []
        for p, s in self.items:
            if _is_protected(_norm(p), protected):
                fail += 1
                done += 1
                failed_items.append((p, s))
            else:
                movable.append((p, s))

        # 2) 分批移入回收站：一次 Shell 操作移一批，摊薄每文件开销
        for i in range(0, len(movable), CLEAN_BATCH_SIZE):
            if not self._is_running:
                break
            batch = movable[i:i + CLEAN_BATCH_SIZE]
            paths = [p for p, _ in batch]
            try:
                send2trash(paths)
            except Exception:
                # 整批失败（可能部分成功）：对仍存在的项逐文件重试
                for p, s in batch:
                    if not os.path.exists(p):
                        continue
                    try:
                        send2trash(p)
                    except Exception:
                        pass

            # 以 os.path.exists 为唯一判定：仍在 → 未移入回收站；消失 → 成功
            for p, s in batch:
                if os.path.exists(p):
                    fail += 1
                    failed_items.append((p, s))
                else:
                    ok += 1
                    deleted_batch.append(p)
                done += 1

            if len(deleted_batch) >= CLEAN_BATCH_SIZE:
                self.deleted.emit(deleted_batch)
                deleted_batch = []
            if len(failed_items) >= CLEAN_BATCH_SIZE:
                self.failed.emit(failed_items)
                failed_items = []
            emit_progress()

        if deleted_batch:
            self.deleted.emit(deleted_batch)
        if failed_items:
            self.failed.emit(failed_items)
        self.finished.emit(ok, fail)
