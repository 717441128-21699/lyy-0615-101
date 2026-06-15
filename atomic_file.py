"""
崩溃安全的原子文件写入工具

采用经典的"写临时文件 + fsync + 原子重命名 + 父目录 fsync"方案，
确保在任意时刻断电/崩溃，文件要么是旧内容，要么是完整的新内容，
绝不会出现半写的损坏状态。

跨平台支持：Linux / macOS / Windows

子命令：
  atomic-write write    标准原子写入（默认子命令，可省略）
  atomic-write doctor   诊断当前系统/目录的崩溃安全能力
  atomic-write batch    从 JSON 清单批量写入多个目标文件
"""

import os
import sys
import json
import time
import tempfile
import argparse
import stat
from pathlib import Path
from typing import Union, Optional, NamedTuple, List, Dict, Any, Iterator, Tuple


__all__ = [
    'atomic_write',
    'atomic_write_read',
    'check_crash_safety',
    'batch_write',
    'doctor',
    'AtomicWriteResult',
    'CheckResult',
    'DoctorResult',
    'BatchResult',
    'BatchItemResult',
    'FsyncStatus',
    'FailPhase',
    'AtomicWriteError',
    'main',
]


class FsyncStatus:
    NOT_ATTEMPTED = 'not_attempted'
    SUCCESS = 'success'
    FAILED_DEGRADED = 'failed_degraded'
    UNSUPPORTED_PLATFORM = 'unsupported_platform'


class FailPhase:
    """原子写入失败的阶段枚举"""
    BEFORE_RENAME = 'before_rename'   # rename 之前失败 → 目标文件未修改
    AFTER_RENAME = 'after_rename'     # rename 完成后目录 fsync 失败 → 内容已替换


class AtomicWriteError(OSError):
    """
    原子写入失败的自定义异常。
    带有 phase 字段，调用者可精确区分 rename 前/后失败。
    """
    def __init__(
        self,
        phase: str,
        message: str,
        target_path: Optional[Path] = None,
        inner_error: Optional[BaseException] = None,
    ) -> None:
        super().__init__(1, message)
        self.phase = phase
        self.target_path = target_path
        self.inner_error = inner_error

    @property
    def target_modified(self) -> bool:
        """rename 是否已经完成（目标文件是否已被修改）"""
        return self.phase == FailPhase.AFTER_RENAME


class AtomicWriteResult(NamedTuple):
    success: bool
    fully_crash_safe: bool
    temp_file_fsync: str
    dir_fsync: str
    renamed: bool
    warnings: List[str]
    target_path: Path
    bytes_written: int = 0

    @property
    def is_degraded(self) -> bool:
        return self.success and not self.fully_crash_safe


class CheckResult(NamedTuple):
    target_path: Path
    parent_dir: Path
    parent_dir_exists: bool
    same_filesystem: Optional[bool]
    dir_fsync_supported: Optional[bool]
    dir_fsync_error: Optional[str]
    would_be_fully_crash_safe: bool
    warnings: List[str]


class DoctorCapability(NamedTuple):
    name: str
    ok: bool
    detail: str


class DoctorResult(NamedTuple):
    directory: Path
    capabilities: List[DoctorCapability]
    all_ok: bool
    summary: str
    warnings: List[str]


class BatchItemResult(NamedTuple):
    target: str
    status: str               # 'success' | 'degraded' | 'failed'
    fully_crash_safe: bool
    bytes_written: int
    error: Optional[str]
    target_modified: bool
    details: Dict[str, str]


class BatchResult(NamedTuple):
    total: int
    succeeded: int
    degraded: int
    failed: int
    items: List[BatchItemResult]

    @property
    def all_ok(self) -> bool:
        return self.failed == 0 and self.degraded == 0


def _fsync_dir(dir_path: Union[str, Path]) -> str:
    dir_path = str(dir_path)
    if not os.path.isdir(dir_path):
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    if sys.platform == 'win32':
        import ctypes
        import ctypes.wintypes

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        INVALID_HANDLE_VALUE = -1

        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.HANDLE,
        ]
        CreateFileW.restype = ctypes.wintypes.HANDLE

        CloseHandle = ctypes.windll.kernel32.CloseHandle
        CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        CloseHandle.restype = ctypes.wintypes.BOOL

        FlushFileBuffers = ctypes.windll.kernel32.FlushFileBuffers
        FlushFileBuffers.argtypes = [ctypes.wintypes.HANDLE]
        FlushFileBuffers.restype = ctypes.wintypes.BOOL

        GetLastError = ctypes.windll.kernel32.GetLastError
        GetLastError.argtypes = []
        GetLastError.restype = ctypes.wintypes.DWORD

        hdir = CreateFileW(
            dir_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if hdir == INVALID_HANDLE_VALUE:
            err = GetLastError()
            raise OSError(err, f'CreateFileW failed for directory: {dir_path}, error code: {err}')
        try:
            ret = FlushFileBuffers(hdir)
            if ret == 0:
                err = GetLastError()
                raise OSError(err, f'FlushFileBuffers failed for directory: {dir_path}, error code: {err}')
        finally:
            CloseHandle(hdir)
        return FsyncStatus.SUCCESS
    else:
        fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return FsyncStatus.SUCCESS


def _fsync_file(fd: int) -> str:
    os.fsync(fd)
    return FsyncStatus.SUCCESS


def _check_same_filesystem(tmp_path: str, target_path: Path) -> Optional[bool]:
    try:
        tmp_stat = os.stat(tmp_path)
        target_stat = os.stat(str(target_path)) if target_path.exists() else os.stat(str(target_path.parent))
        return tmp_stat.st_dev == target_stat.st_dev
    except OSError:
        return None


def _verify_same_filesystem(tmp_path: str, target_path: Path, warnings: List[str]) -> None:
    result = _check_same_filesystem(tmp_path, target_path)
    if result is False:
        try:
            tmp_dev = os.stat(tmp_path).st_dev
            tgt_dev = os.stat(str(target_path)).st_dev if target_path.exists() else os.stat(str(target_path.parent)).st_dev
            warnings.append(
                f'Temporary file and target file are on different filesystems '
                f'(st_dev={tmp_dev} vs {tgt_dev}). '
                f'os.replace() will NOT be atomic and will fall back to copy+unlink, '
                f'which means a mid-operation crash can leave a corrupted file. '
                f'For true atomicity, ensure temp dir and target are on the same mount point.'
            )
        except OSError:
            warnings.append(
                'Unable to verify filesystem compatibility. '
                'If temp and target are on different filesystems, rename will not be atomic.'
            )


def check_crash_safety(target: Union[str, Path]) -> CheckResult:
    target = Path(target).resolve()
    parent_dir = target.parent
    warnings: List[str] = []

    parent_exists = parent_dir.exists()
    same_fs: Optional[bool] = None
    dir_fsync_ok: Optional[bool] = None
    dir_fsync_err: Optional[str] = None

    if parent_exists:
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix='.tmp',
                prefix='.~check_',
                dir=str(parent_dir),
            )
            try:
                same_fs = _check_same_filesystem(tmp_path, target)
                if same_fs is False:
                    try:
                        tmp_dev = os.stat(tmp_path).st_dev
                        tgt_dev = os.stat(str(target)).st_dev if target.exists() else os.stat(str(parent_dir)).st_dev
                        warnings.append(
                            f'Different filesystems (st_dev={tmp_dev} vs {tgt_dev}). '
                            f'Rename will NOT be atomic; mid-crash can corrupt the file.'
                        )
                    except OSError:
                        warnings.append('Different filesystems detected. Rename will not be atomic.')
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except OSError as e:
            warnings.append(f'Cannot create temp file in parent directory: {e}')
            same_fs = None

        try:
            _fsync_dir(parent_dir)
            dir_fsync_ok = True
        except OSError as e:
            dir_fsync_ok = False
            dir_fsync_err = str(e)
            warnings.append(
                f'Directory fsync not supported: {e}. '
                f'After rename, if power fails before the OS flushes the directory entry, '
                f'the file may revert to old content or disappear. '
                f'Write will succeed but operate in DEGRADED mode.'
            )
    else:
        warnings.append(f'Parent directory does not exist: {parent_dir} (will be auto-created on write)')

    fully_safe = (
        parent_exists
        and same_fs is True
        and dir_fsync_ok is True
    )

    return CheckResult(
        target_path=target,
        parent_dir=parent_dir,
        parent_dir_exists=parent_exists,
        same_filesystem=same_fs,
        dir_fsync_supported=dir_fsync_ok,
        dir_fsync_error=dir_fsync_err,
        would_be_fully_crash_safe=fully_safe,
        warnings=warnings,
    )


def doctor(directory: Optional[Union[str, Path]] = None) -> DoctorResult:
    """
    诊断当前系统/目录的崩溃安全能力。
    适合在 CI 中运行，返回值 all_ok 可用于 CI 判定。
    """
    if directory is None:
        directory = Path.cwd()
    directory = Path(directory).resolve()

    sample_target = directory / '.atomic_write_doctor_test.tmp'
    warnings: List[str] = []
    caps: List[DoctorCapability] = []

    check = check_crash_safety(sample_target)

    caps.append(DoctorCapability(
        name='Parent directory exists',
        ok=check.parent_dir_exists,
        detail=f'{check.parent_dir}' if check.parent_dir_exists else f'Missing: {check.parent_dir}',
    ))

    if check.same_filesystem is None:
        caps.append(DoctorCapability(
            name='Same-filesystem atomic rename',
            ok=False,
            detail='UNKNOWN (could not create temp file for verification)',
        ))
    elif check.same_filesystem:
        caps.append(DoctorCapability(
            name='Same-filesystem atomic rename',
            ok=True,
            detail='PASS: rename() will be atomic within the directory',
        ))
    else:
        caps.append(DoctorCapability(
            name='Same-filesystem atomic rename',
            ok=False,
            detail='FAIL: temp and target are on different filesystems, rename will be copy+unlink (not atomic)',
        ))

    if check.dir_fsync_supported is None:
        caps.append(DoctorCapability(
            name='Directory fsync (entry persistence)',
            ok=False,
            detail='NOT TESTED (parent directory does not exist)',
        ))
    elif check.dir_fsync_supported:
        caps.append(DoctorCapability(
            name='Directory fsync (entry persistence)',
            ok=True,
            detail='PASS: directory entries will be persisted to disk after rename',
        ))
    else:
        caps.append(DoctorCapability(
            name='Directory fsync (entry persistence)',
            ok=False,
            detail=(
                f'FAIL: {check.dir_fsync_error}. '
                f'Writes will operate in DEGRADED mode - directory entries may not persist across crashes.'
            ),
        ))

    stdin_ok = True
    try:
        if hasattr(sys.stdin, 'fileno'):
            try:
                sys.stdin.fileno()
                stdin_detail = 'Available (can use --file -)'
            except OSError:
                stdin_detail = 'Unavailable (e.g. piped input not connected) -- stdin fsync unavailable'
                stdin_ok = True
    except Exception as e:
        stdin_detail = f'Uncertain: {e}'
        stdin_ok = True

    caps.append(DoctorCapability(
        name='Stdin reading support',
        ok=stdin_ok,
        detail=stdin_detail,
    ))

    temp_dir_ok = True
    try:
        tfd, tpath = tempfile.mkstemp(dir=str(directory))
        try:
            os.close(tfd)
        finally:
            os.unlink(tpath)
        temp_detail = f'Can create temp files in {directory}'
    except OSError as e:
        temp_dir_ok = False
        temp_detail = f'Cannot create temp files in {directory}: {e}'

    caps.append(DoctorCapability(
        name='Temp file creation',
        ok=temp_dir_ok,
        detail=temp_detail,
    ))

    all_ok = all(c.ok for c in caps)

    if all_ok:
        summary = 'All crash-safety capabilities available. Writes will be FULLY crash-safe.'
    else:
        bad = [c.name for c in caps if not c.ok]
        summary = (
            f'Degraded mode expected. Missing/failing capabilities: {", ".join(bad)}. '
            f'Writes will still be atomic (no corruption) but full crash-safety is not guaranteed.'
        )

    warnings.extend(check.warnings)

    return DoctorResult(
        directory=directory,
        capabilities=caps,
        all_ok=all_ok,
        summary=summary,
        warnings=warnings,
    )


def atomic_write(
    path: Union[str, Path],
    data: Union[bytes, str],
    encoding: Optional[str] = None,
    permissions: Optional[int] = None,
    temp_suffix: str = '.tmp',
    temp_prefix: str = '.~',
    allow_degraded: bool = True,
    strict: bool = False,
) -> AtomicWriteResult:
    """
    崩溃安全地原子写入文件。

    Raises:
        AtomicWriteError: 分为两种 phase:
            - FailPhase.BEFORE_RENAME: rename 前失败 → 目标文件未被修改
            - FailPhase.AFTER_RENAME: rename 后目录 fsync 失败 → 内容已替换，仅目录项 fsync 缺失
    """
    path = Path(path).resolve()
    parent_dir = path.parent
    warnings: List[str] = []

    if not parent_dir.exists():
        parent_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(data, str):
        if encoding is None:
            encoding = 'utf-8'
        data_bytes = data.encode(encoding)
    else:
        data_bytes = data

    fd = None
    tmp_path = None
    temp_fsync_status = FsyncStatus.NOT_ATTEMPTED
    dir_fsync_status = FsyncStatus.NOT_ATTEMPTED
    renamed = False

    try:
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=temp_suffix,
                prefix=temp_prefix,
                dir=str(parent_dir),
            )
        except OSError as e:
            raise AtomicWriteError(
                phase=FailPhase.BEFORE_RENAME,
                message=f'Failed to create temporary file: {e}',
                target_path=path,
                inner_error=e,
            ) from e

        _verify_same_filesystem(tmp_path, path, warnings)

        try:
            with os.fdopen(fd, 'wb') as f:
                fd = None
                f.write(data_bytes)
                f.flush()
                try:
                    temp_fsync_status = _fsync_file(f.fileno())
                except OSError as e:
                    raise AtomicWriteError(
                        phase=FailPhase.BEFORE_RENAME,
                        message=f'Failed to fsync temporary file (data may not be persisted): {e}',
                        target_path=path,
                        inner_error=e,
                    ) from e
        except AtomicWriteError:
            raise
        except OSError as e:
            raise AtomicWriteError(
                phase=FailPhase.BEFORE_RENAME,
                message=f'Failed to write/flush temporary file: {e}',
                target_path=path,
                inner_error=e,
            ) from e
        except Exception as e:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            raise AtomicWriteError(
                phase=FailPhase.BEFORE_RENAME,
                message=f'Failed during temp file write: {e}',
                target_path=path,
                inner_error=e,
            ) from e

        if permissions is not None:
            try:
                os.chmod(tmp_path, permissions)
            except NotImplementedError:
                warnings.append('chmod not supported on this platform, permissions not set')

        try:
            os.replace(tmp_path, str(path))
            tmp_path = None
            renamed = True
        except OSError as e:
            raise AtomicWriteError(
                phase=FailPhase.BEFORE_RENAME,
                message=f'Failed to rename temp file to target: {e}',
                target_path=path,
                inner_error=e,
            ) from e

        try:
            dir_fsync_status = _fsync_dir(parent_dir)
        except OSError as e:
            if strict:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed (strict mode): {e}. '
                    f'Target file has ALREADY been replaced with new content. '
                    f'Missing crash-safety step: directory fsync.'
                )
                raise AtomicWriteError(
                    phase=FailPhase.AFTER_RENAME,
                    message=(
                        f'Directory fsync failed (strict mode): {e}. '
                        f'Target file has ALREADY been replaced with new content, '
                        f'but directory entry update may not be persisted to disk yet. '
                        f'If power fails now, the file could revert to old content or disappear.'
                    ),
                    target_path=path,
                    inner_error=e,
                ) from e
            elif not allow_degraded:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed (degraded mode not allowed): {e}. '
                    f'Target file has ALREADY been replaced with new content.'
                )
                raise AtomicWriteError(
                    phase=FailPhase.AFTER_RENAME,
                    message=(
                        f'Directory fsync failed (no-degraded mode): {e}. '
                        f'Target file has ALREADY been replaced with new content, '
                        f'but directory entry update may not be persisted to disk yet.'
                    ),
                    target_path=path,
                    inner_error=e,
                ) from e
            else:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed: {e}. '
                    f'Target file has been replaced with new content, '
                    f'but the directory entry update may not be persisted to disk.'
                )

        fully_safe = (
            temp_fsync_status == FsyncStatus.SUCCESS
            and dir_fsync_status == FsyncStatus.SUCCESS
        )

        return AtomicWriteResult(
            success=True,
            fully_crash_safe=fully_safe,
            temp_file_fsync=temp_fsync_status,
            dir_fsync=dir_fsync_status,
            renamed=renamed,
            warnings=warnings,
            target_path=path,
            bytes_written=len(data_bytes),
        )

    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
        if not renamed and tmp_path is not None and os.path.exists(tmp_path):
            for _ in range(3):
                try:
                    os.unlink(tmp_path)
                    break
                except OSError:
                    time.sleep(0.01)


def atomic_write_read(
    path: Union[str, Path],
    read_encoding: Optional[str] = 'utf-8',
) -> Union[bytes, str]:
    path = Path(path).resolve()
    if read_encoding is not None:
        with open(path, 'r', encoding=read_encoding) as f:
            return f.read()
    else:
        with open(path, 'rb') as f:
            return f.read()


def _validate_permissions(perms: Optional[int]) -> None:
    if perms is None:
        return
    if perms < 0 or perms > 0o777:
        raise ValueError(f'Permissions must be between 0 and 0777, got: {oct(perms)}')


def batch_write(
    manifest: Union[str, Path, List[Dict[str, Any]]],
    allow_degraded: bool = True,
    strict: bool = False,
) -> BatchResult:
    """
    从清单批量写入多个目标文件。

    manifest 格式（列表或指向 JSON 文件的路径）：
        [
            {"target": "config.json", "text": "data"},
            {"target": "data.bin", "file": "/tmp/source.bin", "binary": true},
            {"target": "a.txt", "text": "hello", "encoding": "gbk", "permissions": 384}
        ]

    每个条目字段：
        target (str):        目标路径 (必需)
        text (str):          直接写入文本
        file (str):          从文件读取内容
        encoding (str):      编码，默认 utf-8
        binary (bool):       二进制模式
        permissions (int):   权限（八进制数值，如 0o644 = 420）

    行为：
        - 每个目标独立执行原子写入
        - 某一项失败不影响已成功的项
        - 返回 BatchResult 汇总所有结果
    """
    if isinstance(manifest, (str, Path)):
        manifest_path = Path(manifest).resolve()
        with open(manifest_path, 'r', encoding='utf-8') as mf:
            items = json.load(mf)
        if not isinstance(items, list):
            raise ValueError('Batch manifest must be a JSON array of objects')
    elif isinstance(manifest, list):
        items = manifest
    else:
        raise TypeError('manifest must be a list or a path to JSON file')

    results: List[BatchItemResult] = []
    succeeded = 0
    degraded = 0
    failed = 0

    for idx, item in enumerate(items):
        target = item.get('target')
        if not target:
            failed += 1
            results.append(BatchItemResult(
                target=f'<item #{idx}>',
                status='failed',
                fully_crash_safe=False,
                bytes_written=0,
                error=f'Missing "target" field in manifest entry #{idx}',
                target_modified=False,
                details={},
            ))
            continue

        has_text = 'text' in item
        has_file = 'file' in item
        if has_text == has_file:
            failed += 1
            results.append(BatchItemResult(
                target=target,
                status='failed',
                fully_crash_safe=False,
                bytes_written=0,
                error=f'Entry must contain exactly one of "text" or "file"',
                target_modified=False,
                details={},
            ))
            continue

        encoding = item.get('encoding', None if item.get('binary') else 'utf-8')
        is_binary = bool(item.get('binary', False))
        permissions = item.get('permissions', None)

        try:
            if permissions is not None:
                if not isinstance(permissions, int):
                    raise ValueError(f'permissions must be integer, got {type(permissions)}')
                _validate_permissions(permissions)

            if has_text:
                data = item['text']
                if not isinstance(data, str):
                    raise ValueError(f'"text" field must be string')
                if is_binary:
                    raise ValueError('binary mode cannot be used with text input')
                result = atomic_write(
                    target, data, encoding=encoding,
                    permissions=permissions,
                    allow_degraded=allow_degraded, strict=strict,
                )
            else:
                src = item['file']
                if not isinstance(src, str):
                    raise ValueError(f'"file" field must be string path')
                mode = 'rb' if is_binary else 'r'
                kwargs = {} if is_binary else {'encoding': encoding}
                with open(src, mode, **kwargs) as sf:
                    data = sf.read()
                enc = None if is_binary else encoding
                result = atomic_write(
                    target, data, encoding=enc,
                    permissions=permissions,
                    allow_degraded=allow_degraded, strict=strict,
                )

            if result.is_degraded:
                degraded += 1
                status = 'degraded'
            else:
                succeeded += 1
                status = 'success'

            results.append(BatchItemResult(
                target=target,
                status=status,
                fully_crash_safe=result.fully_crash_safe,
                bytes_written=result.bytes_written,
                error=None,
                target_modified=True,
                details={
                    'temp_file_fsync': result.temp_file_fsync,
                    'dir_fsync': result.dir_fsync,
                    **{f'warning_{i}': w for i, w in enumerate(result.warnings)},
                },
            ))

        except AtomicWriteError as e:
            failed += 1
            results.append(BatchItemResult(
                target=target,
                status='failed',
                fully_crash_safe=False,
                bytes_written=0,
                error=f'{e.phase}: {str(e.args[1]) if len(e.args) > 1 else str(e)}',
                target_modified=e.target_modified,
                details={'fail_phase': e.phase},
            ))
        except Exception as e:
            failed += 1
            results.append(BatchItemResult(
                target=target,
                status='failed',
                fully_crash_safe=False,
                bytes_written=0,
                error=f'{type(e).__name__}: {e}',
                target_modified=False,
                details={},
            ))

    return BatchResult(
        total=len(items),
        succeeded=succeeded,
        degraded=degraded,
        failed=failed,
        items=results,
    )


EXIT_SUCCESS = 0
EXIT_WRITE_FAILED = 1
EXIT_DEGRADED_SUCCESS = 2
EXIT_INVALID_ARGS = 3
EXIT_POST_RENAME_FAILURE = 4
EXIT_PARTIAL_BATCH_FAILURE = 5
EXIT_TOTAL_BATCH_FAILURE = 6


def main(argv: Optional[List[str]] = None) -> int:
    """
    命令行入口。

    子命令:
        write     标准原子写入（默认）
        doctor    诊断当前/指定目录的崩溃安全能力
        batch     从 JSON 清单批量写入多个文件

    退出码:
        0 成功, fully crash-safe
        1 写入失败 BEFORE rename (目标文件未修改)
        2 写入成功但 DEGRADED (directory fsync failed)
        3 参数错误
        4 写入部分完成: rename done 但 dir fsync failed (strict/no-degraded)
        5 批量写入: 部分成功 (CI 应视为失败)
        6 批量写入: 全部失败
    """
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    KNOWN_SUBCOMMANDS = {'write', 'w', 'doctor', 'batch'}

    if argv:
        first = argv[0]
        if first in KNOWN_SUBCOMMANDS:
            pass
        elif first.startswith('-'):
            if first in ('--help', '-h', '--version'):
                pass
            else:
                return _cmd_write(argv)
        else:
            return _cmd_write(argv)

    parser = argparse.ArgumentParser(
        prog='atomic-write',
        description='Crash-safe atomic file writer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument('--help', action='help', help='show this help message and exit')
    parser.add_argument('--version', action='version', version='atomic-write 1.2.0')

    sub = parser.add_subparsers(dest='command')

    p_write = sub.add_parser('write', help='Atomically write a single file', aliases=['w'])
    _add_write_args(p_write)

    p_doctor = sub.add_parser('doctor', help='Diagnose crash-safety capabilities of a directory')
    p_doctor.add_argument('directory', nargs='?', default=None,
                          help='Directory to diagnose (default: current working directory)')
    p_doctor.add_argument('--quiet', '-q', action='store_true',
                          help='Print no details, use exit code for CI')
    p_doctor.add_argument('--json', action='store_true', help='Output JSON machine-readable result')

    p_batch = sub.add_parser('batch', help='Write multiple files from a JSON manifest')
    p_batch.add_argument('manifest', help='Path to JSON manifest file')
    p_batch.add_argument('--strict', action='store_true',
                         help='Treat directory fsync failure as hard error per file')
    p_batch.add_argument('--no-degraded', action='store_true',
                         help='Do not allow degraded success per file')
    p_batch.add_argument('--quiet', '-q', action='store_true',
                         help='Only print summary line')
    p_batch.add_argument('--json', action='store_true',
                         help='Output JSON result instead of human-readable summary')

    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit as e:
        return EXIT_INVALID_ARGS if e.code == 2 else 0

    if args.command is None:
        if not argv or argv == ['--help'] or argv == ['-h']:
            parser.print_help()
            return EXIT_INVALID_ARGS
        if argv == ['--version']:
            parser._print_message(f'atomic-write 1.2.0\n', sys.stdout)
            return 0
        return _cmd_write(argv)

    if args.command in ('write', 'w'):
        return _cmd_write(argv[1:])
    if args.command == 'doctor':
        return _cmd_doctor(args)
    if args.command == 'batch':
        return _cmd_batch(args)

    parser.print_help()
    return EXIT_INVALID_ARGS


def _add_write_args(p: argparse.ArgumentParser) -> None:
    p.add_argument('target', help='Target file path to write atomically')
    g_input = p.add_mutually_exclusive_group()
    g_input.add_argument('--text', help='Text content to write')
    g_input.add_argument('--file', help='Source file to read, or "-" for stdin')
    p.add_argument('--check', action='store_true',
                   help='Check crash-safety without writing')
    p.add_argument('--dry-run', action='store_true',
                   help='Simulate a full write (validates input) without modifying target')
    p.add_argument('--encoding', default='utf-8', help='Text encoding (default: utf-8)')
    p.add_argument('--binary', action='store_true',
                   help='Binary mode with --file (no encoding)')
    p.add_argument('--permissions', type=lambda x: int(x, 8), default=None,
                   help='File permissions in octal, e.g. 644')
    p.add_argument('--strict', action='store_true')
    p.add_argument('--no-degraded', action='store_true')
    p.add_argument('--quiet', '-q', action='store_true')


def _cmd_write(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog='atomic-write write', add_help=False)
    _add_write_args(parser)
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_INVALID_ARGS if e.code == 2 else 0

    if args.check or args.dry_run:
        return _cmd_dry_run(args)

    return _cmd_write_execute(args)


def _cmd_dry_run(args) -> int:
    target = args.target
    check = check_crash_safety(target)

    input_checks: List[Tuple[str, bool, str]] = []
    permissions_ok: Optional[bool] = None
    permissions_error: Optional[str] = None

    if args.text is not None:
        if args.binary:
            input_checks.append(('Input --text with --binary', False,
                                 'Cannot use --binary with --text'))
        else:
            try:
                encoded = args.text.encode(args.encoding if args.encoding else 'utf-8')
                input_checks.append((
                    f'Input --text (encoding={args.encoding})',
                    True,
                    f'OK: {len(encoded)} bytes would be written',
                ))
            except (UnicodeEncodeError, LookupError) as e:
                input_checks.append((f'Input --text encoding', False, f'Encoding error: {e}'))

    elif args.file is not None:
        source = args.file
        if source == '-':
            mode_desc = 'binary' if args.binary else f'text ({args.encoding})'
            input_checks.append((
                f'Input stdin ({mode_desc})',
                True,
                f'Would read from stdin in {mode_desc} mode',
            ))
        elif not os.path.isfile(source):
            input_checks.append((f'Input file: {source}', False, 'File does not exist'))
        else:
            try:
                size = os.path.getsize(source)
                input_checks.append((
                    f'Input file: {source}',
                    True,
                    f'Exists, size={size} bytes, binary={args.binary}, encoding={args.encoding}',
                ))
            except OSError as e:
                input_checks.append((f'Input file: {source}', False, f'Stat error: {e}'))

    if args.permissions is not None:
        try:
            _validate_permissions(args.permissions)
            permissions_ok = True
            permissions_error = None
        except ValueError as e:
            permissions_ok = False
            permissions_error = str(e)

    if not args.quiet:
        print(f'Dry-run for target: {Path(target).resolve()}')
        print(f'  [Crash safety]')
        print(f'    Parent dir exists  : {"YES" if check.parent_dir_exists else "NO (would be created)"}')
        if check.same_filesystem is None:
            fs_line = 'UNKNOWN'
        elif check.same_filesystem:
            fs_line = 'YES (atomic rename)'
        else:
            fs_line = 'NO → rename will be copy+unlink (NOT ATOMIC!)'
        print(f'    Same filesystem    : {fs_line}')
        if check.dir_fsync_supported is None:
            df_line = 'NOT TESTED'
        elif check.dir_fsync_supported:
            df_line = 'SUPPORTED'
        else:
            df_line = f'NOT SUPPORTED → {check.dir_fsync_error}'
        print(f'    Directory fsync    : {df_line}')

        print(f'  [Input validation]')
        for name, ok, detail in input_checks:
            tag = '[OK]' if ok else '[FAIL]'
            print(f'    {tag} {name}: {detail}')

        if args.permissions is not None:
            if permissions_ok:
                print(f'    [OK] Permissions      : {oct(args.permissions)}')
            else:
                print(f'    [FAIL] Permissions      : {permissions_error}')

        print(f'  [Prediction]')
        missing = []
        if not check.parent_dir_exists:
            missing.append('parent dir creation')
        if check.same_filesystem is False:
            missing.append('atomic rename')
        if check.dir_fsync_supported is False:
            missing.append('dir fsync → degraded mode')

        all_input_ok = all(ok for _, ok, _ in input_checks) and permissions_ok is not False

        if not all_input_ok:
            outcome = 'FAIL (invalid input or permissions)'
            exit_code = EXIT_INVALID_ARGS
        elif check.would_be_fully_crash_safe:
            outcome = 'FULLY crash-safe write (exit 0)'
            exit_code = EXIT_SUCCESS
        elif missing:
            outcome = f'DEGRADED success (exit 2), missing: {"; ".join(missing)}'
            exit_code = EXIT_DEGRADED_SUCCESS
        else:
            outcome = 'DEGRADED success (exit 2)'
            exit_code = EXIT_DEGRADED_SUCCESS

        print(f'    Write outcome      : {outcome}')
        print(f'    Target file        : WOULD NOT be modified')

        for w in check.warnings:
            print(f'    WARNING: {w}', file=sys.stderr)

        if args.dry_run and all_input_ok:
            print()
            print('Dry-run complete. No files were modified.')
    else:
        all_input_ok = all(ok for _, ok, _ in input_checks) and permissions_ok is not False
        if not all_input_ok:
            exit_code = EXIT_INVALID_ARGS
        elif check.would_be_fully_crash_safe:
            exit_code = EXIT_SUCCESS
        else:
            exit_code = EXIT_DEGRADED_SUCCESS

    return exit_code


def _cmd_write_execute(args) -> int:
    if args.target is None:
        print('Error: target path is required', file=sys.stderr)
        return EXIT_INVALID_ARGS
    if args.text is None and args.file is None:
        print('Error: one of --text or --file is required', file=sys.stderr)
        return EXIT_INVALID_ARGS

    allow_degraded = not args.no_degraded

    if args.binary and args.text is not None:
        print('Error: --binary cannot be used with --text. Use --file with --binary instead.', file=sys.stderr)
        return EXIT_INVALID_ARGS

    try:
        if args.text is not None:
            data = args.text
            encoding = args.encoding
        else:
            source = args.file
            if source == '-':
                if args.binary:
                    data = sys.stdin.buffer.read()
                else:
                    data = sys.stdin.read()
            else:
                if not os.path.isfile(source):
                    print(f'Error: Source file does not exist: {source}', file=sys.stderr)
                    return EXIT_INVALID_ARGS
                mode = 'rb' if args.binary else 'r'
                kwargs = {} if args.binary else {'encoding': args.encoding}
                with open(source, mode, **kwargs) as f:
                    data = f.read()
            encoding = None if args.binary else args.encoding
    except Exception as e:
        print(f'Error reading input: {e}', file=sys.stderr)
        return EXIT_INVALID_ARGS

    try:
        result = atomic_write(
            args.target,
            data,
            encoding=encoding,
            permissions=args.permissions,
            allow_degraded=allow_degraded,
            strict=args.strict,
        )
    except AtomicWriteError as e:
        if e.phase == FailPhase.AFTER_RENAME:
            print(
                f'Error (AFTER rename): {e.args[1] if len(e.args) > 1 else str(e)}',
                file=sys.stderr,
            )
            print(
                'IMPORTANT: The target file HAS been replaced with new content, '
                'but directory entry fsync failed.',
                file=sys.stderr,
            )
            print(
                'Temporary file fsync succeeded (data blocks are on disk). '
                'Risk: if power fails NOW, directory entry may revert, '
                'causing the file to appear as old content or disappear.',
                file=sys.stderr,
            )
            return EXIT_POST_RENAME_FAILURE
        else:
            print(
                f'Error (BEFORE rename): {e.args[1] if len(e.args) > 1 else str(e)}',
                file=sys.stderr,
            )
            print(
                'Target file was NOT modified (still contains old content).',
                file=sys.stderr,
            )
            return EXIT_WRITE_FAILED
    except OSError as e:
        print(f'Error: Atomic write failed: {e}', file=sys.stderr)
        print('Target file should be unchanged.', file=sys.stderr)
        return EXIT_WRITE_FAILED

    if result.success:
        if not args.quiet:
            print(f'Wrote {result.bytes_written} bytes → {result.target_path}')
            print(f'  Temp file fsync : {result.temp_file_fsync}')
            print(f'  Directory fsync : {result.dir_fsync}')
            print(f'  Fully crash-safe: {"YES" if result.fully_crash_safe else "NO"}')
            for w in result.warnings:
                print(f'  WARNING: {w}', file=sys.stderr)

        if result.fully_crash_safe:
            return EXIT_SUCCESS
        else:
            if not args.quiet:
                print(
                    'DEGRADED: Write succeeded but full crash-safety NOT guaranteed.',
                    file=sys.stderr,
                )
            return EXIT_DEGRADED_SUCCESS

    print('Error: Write reported failure without exception', file=sys.stderr)
    return EXIT_WRITE_FAILED


def _cmd_doctor(args) -> int:
    directory = args.directory or Path.cwd()
    result = doctor(directory)

    if args.json:
        out = {
            'directory': str(result.directory),
            'all_ok': result.all_ok,
            'summary': result.summary,
            'capabilities': [c._asdict() for c in result.capabilities],
            'warnings': result.warnings,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif not args.quiet:
        print(f'Crash-safety diagnosis for: {result.directory}')
        print(f'Platform: {sys.platform}')
        print()
        for cap in result.capabilities:
            tag = '[OK]' if cap.ok else '[FAIL]'
            print(f'{tag} {cap.name}: {cap.detail}')
        print()
        print(f'Overall: {result.summary}')
        if result.warnings:
            print()
            for w in result.warnings:
                print(f'WARNING: {w}', file=sys.stderr)

    if result.all_ok:
        return EXIT_SUCCESS
    return EXIT_DEGRADED_SUCCESS


def _cmd_batch(args) -> int:
    allow_degraded = not args.no_degraded

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f'Error: Manifest file does not exist: {manifest_path}', file=sys.stderr)
        return EXIT_INVALID_ARGS

    try:
        result = batch_write(
            manifest_path,
            allow_degraded=allow_degraded,
            strict=args.strict,
        )
    except (ValueError, json.JSONDecodeError) as e:
        print(f'Error: Invalid manifest: {e}', file=sys.stderr)
        return EXIT_INVALID_ARGS

    if args.json:
        out = {
            'total': result.total,
            'succeeded': result.succeeded,
            'degraded': result.degraded,
            'failed': result.failed,
            'items': [item._asdict() for item in result.items],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif not args.quiet:
        print(f'Batch result: total={result.total}  succeeded={result.succeeded}  '
              f'degraded={result.degraded}  failed={result.failed}')
        for item in result.items:
            tag_map = {'success': '[OK]', 'degraded': '[DEG]', 'failed': '[ERR]'}
            tag = tag_map[item.status]
            size_info = f'{item.bytes_written}B' if item.bytes_written else '0B'
            safe_info = 'safe' if item.fully_crash_safe else (
                'degraded' if item.status == 'degraded' else 'N/A'
            )
            base = f'  {tag} [{item.status:8s}] {size_info:>8s} [{safe_info:>8s}] → {item.target}'
            print(base)
            if item.error:
                print(f'       error: {item.error}')
                if item.target_modified:
                    print(f'       NOTE: target HAS been modified (post-rename failure)')
                else:
                    print(f'       NOTE: target was NOT modified')

    if result.failed == 0 and result.degraded == 0:
        return EXIT_SUCCESS
    if result.failed == result.total and result.total > 0:
        return EXIT_TOTAL_BATCH_FAILURE
    return EXIT_PARTIAL_BATCH_FAILURE


if __name__ == '__main__':
    sys.exit(main())
