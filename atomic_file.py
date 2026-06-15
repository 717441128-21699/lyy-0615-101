"""
崩溃安全的原子文件写入工具

采用经典的"写临时文件 + fsync + 原子重命名 + 父目录 fsync"方案，
确保在任意时刻断电/崩溃，文件要么是旧内容，要么是完整的新内容，
绝不会出现半写的损坏状态。

跨平台支持：Linux / macOS / Windows
"""

import os
import sys
import tempfile
import argparse
from pathlib import Path
from typing import Union, Optional, NamedTuple, List


__all__ = [
    'atomic_write',
    'atomic_write_read',
    'check_crash_safety',
    'AtomicWriteResult',
    'CheckResult',
    'FsyncStatus',
    'main',
]


class FsyncStatus:
    NOT_ATTEMPTED = 'not_attempted'
    SUCCESS = 'success'
    FAILED_DEGRADED = 'failed_degraded'
    UNSUPPORTED_PLATFORM = 'unsupported_platform'


class AtomicWriteResult(NamedTuple):
    """
    原子写入的结果，让调用者能精确知道每一步是否成功。

    Fields:
        success: 写入是否成功（目标文件内容已更新）
        fully_crash_safe: 是否完成了全部 fsync 步骤，提供完整崩溃安全承诺
        temp_file_fsync: 临时文件 fsync 的状态
        dir_fsync: 父目录 fsync 的状态
        renamed: rename 是否已经完成（区分 rename 前后的失败）
        warnings: 非致命警告信息列表
        target_path: 最终写入的目标文件绝对路径
    """
    success: bool
    fully_crash_safe: bool
    temp_file_fsync: str
    dir_fsync: str
    renamed: bool
    warnings: List[str]
    target_path: Path

    @property
    def is_degraded(self) -> bool:
        return self.success and not self.fully_crash_safe


class CheckResult(NamedTuple):
    """
    --check / --dry-run 预检结果。

    Fields:
        target_path: 目标文件绝对路径
        parent_dir: 父目录绝对路径
        parent_dir_exists: 父目录是否存在
        same_filesystem: 临时文件与目标是否在同一文件系统（None=无法判断）
        dir_fsync_supported: 父目录是否支持 fsync（None=未测试）
        dir_fsync_error: 如果 dir_fsync 测试失败，记录错误信息
        would_be_fully_crash_safe: 如果真正写入，能否达到完全崩溃安全
        warnings: 警告信息
    """
    target_path: Path
    parent_dir: Path
    parent_dir_exists: bool
    same_filesystem: Optional[bool]
    dir_fsync_supported: Optional[bool]
    dir_fsync_error: Optional[str]
    would_be_fully_crash_safe: bool
    warnings: List[str]


def _fsync_dir(dir_path: Union[str, Path]) -> str:
    """
    对目录执行 fsync，确保目录项持久化到磁盘。

    Returns:
        FsyncStatus 字符串

    Raises:
        OSError: fsync 失败时抛出
    """
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
    """
    预检目标路径的崩溃安全能力，不真正写入文件。

    检测：
    1. 父目录是否存在
    2. 临时文件与目标是否在同一文件系统
    3. 父目录是否支持 fsync

    Returns:
        CheckResult
    """
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

    算法步骤（每一步都防御特定的崩溃场景）：
    1. 解析目标路径，确保父目录存在
    2. 在目标文件 **同一目录** 下创建临时文件（保证同一文件系统，rename 才能原子）
    3. 将用户数据写入临时文件
    4. 对临时文件执行 fsync ──► 防御：写后 rename 前崩溃，临时文件数据丢失
    5. 关闭临时文件
    6. 执行原子 rename（同一文件系统内是原子的）──► 防御：rename 中途崩溃导致文件损坏
    7. 对父目录执行 fsync ──► 防御：rename 后目录项未持久化导致新文件"消失"

    Raises:
        OSError: 底层文件操作失败时抛出。
            - rename 之前的异常：目标文件不变（仍是旧内容）
            - rename 之后目录 fsync 的异常：
              strict=True 或 allow_degraded=False 时抛出，
              此时目标文件**已经被替换为新内容**，但目录项可能未持久化到磁盘。
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
        fd, tmp_path = tempfile.mkstemp(
            suffix=temp_suffix,
            prefix=temp_prefix,
            dir=str(parent_dir),
        )

        _verify_same_filesystem(tmp_path, path, warnings)

        try:
            with os.fdopen(fd, 'wb') as f:
                fd = None
                f.write(data_bytes)
                f.flush()
                temp_fsync_status = _fsync_file(f.fileno())
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            raise

        if permissions is not None:
            try:
                os.chmod(tmp_path, permissions)
            except NotImplementedError:
                warnings.append('chmod not supported on this platform, permissions not set')

        os.replace(tmp_path, str(path))
        tmp_path = None
        renamed = True

        try:
            dir_fsync_status = _fsync_dir(parent_dir)
        except OSError as e:
            if strict:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed (strict mode): {e}. '
                    f'IMPORTANT: The target file has ALREADY been replaced with new content. '
                    f'However, the directory entry update may not be persisted to disk yet. '
                    f'If power fails now, the file could revert to old content or disappear. '
                    f'Missing crash-safety step: directory fsync (step 7 of 7).'
                )
                raise
            elif not allow_degraded:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed (degraded mode not allowed): {e}. '
                    f'IMPORTANT: The target file has ALREADY been replaced with new content. '
                    f'However, the directory entry update may not be persisted to disk yet. '
                    f'If power fails now, the file could revert to old content or disappear. '
                    f'Missing crash-safety step: directory fsync (step 7 of 7).'
                )
                raise
            else:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed: {e}. '
                    f'The target file has been replaced with new content, '
                    f'but the directory entry update may not be persisted to disk. '
                    f'If power fails now, the file could revert to old content or disappear. '
                    f'Missing crash-safety step: directory fsync (step 7 of 7).'
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
        )

    except Exception:
        if renamed:
            pass
        elif tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    finally:
        if not renamed and tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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


EXIT_SUCCESS = 0
EXIT_WRITE_FAILED = 1
EXIT_DEGRADED_SUCCESS = 2
EXIT_INVALID_ARGS = 3
EXIT_POST_RENAME_FAILURE = 4


def main(argv: Optional[List[str]] = None) -> int:
    """
    命令行入口。

    退出码：
        0: 成功且完整崩溃安全
        1: 写入失败（目标文件未被修改）
        2: 写入成功但降级（部分 fsync 失败，崩溃安全不完整）
        3: 参数错误
        4: 写入部分完成（rename 已执行但目录 fsync 失败，strict/no-degraded 模式下报错）
           目标文件已被替换为新内容，但目录项可能未持久化
    """
    parser = argparse.ArgumentParser(
        prog='atomic-write',
        description='Crash-safe atomic file writer using temp-file + fsync + rename pattern',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Exit codes:
  0  Success, fully crash-safe (all fsync steps completed)
  1  Write failed BEFORE rename (target file UNCHANGED, still has old content)
  2  Write succeeded but DEGRADED (directory fsync failed, partial crash-safety)
  3  Invalid arguments
  4  Write PARTIALLY completed: rename done but directory fsync failed
     in strict/no-degraded mode. Target file HAS new content but directory
     entry may not be persisted. Power loss could revert to old content.

Examples:
  atomic-write config.json --text '{"key": "value"}'
  atomic-write data.bin --file /tmp/new_data.bin
  cat large_file.txt | atomic-write output.txt --file -
  atomic-write important.txt --text "hello" --strict
  atomic-write --check /path/to/target.txt
  atomic-write --dry-run config.json
        ''',
    )

    parser.add_argument(
        'target',
        nargs='?',
        help='Target file path to write atomically',
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        '--text',
        help='Text content to write (encoded as UTF-8 by default)',
    )
    input_group.add_argument(
        '--file',
        help='Source file path to read content from. Use "-" for stdin.',
    )
    input_group.add_argument(
        '--check',
        action='store_true',
        help='Check crash-safety capabilities for target path without writing. '
             'Tests directory fsync support and filesystem compatibility.',
    )
    input_group.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate a write to target path without actually writing. '
             'Reports same checks as --check plus whether the write would succeed.',
    )

    parser.add_argument(
        '--encoding',
        default='utf-8',
        help='Encoding for --text mode (default: utf-8)',
    )
    parser.add_argument(
        '--binary',
        action='store_true',
        help='Read/write in binary mode (no encoding). Only valid with --file, not --text.',
    )
    parser.add_argument(
        '--permissions',
        type=lambda x: int(x, 8),
        default=None,
        help='File permissions in octal, e.g. 644',
    )
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Strict mode: any fsync failure causes hard error instead of degraded success',
    )
    parser.add_argument(
        '--no-degraded',
        action='store_true',
        help='Do not allow degraded success; treat directory fsync failure as hard error',
    )
    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Suppress non-error output',
    )

    args = parser.parse_args(argv)

    if args.check or args.dry_run:
        if args.target is None:
            print('Error: target path is required for --check / --dry-run', file=sys.stderr)
            return EXIT_INVALID_ARGS
        return _run_check(args)

    if args.target is None:
        print('Error: target path is required', file=sys.stderr)
        return EXIT_INVALID_ARGS

    if args.text is None and args.file is None:
        print('Error: one of --text, --file, --check, or --dry-run is required', file=sys.stderr)
        return EXIT_INVALID_ARGS

    allow_degraded = not args.no_degraded

    if args.binary and args.text is not None:
        print('Error: --binary cannot be used with --text (text input is always encoded). Use --file with --binary instead.', file=sys.stderr)
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
    except OSError as e:
        is_post_rename = (
            isinstance(e, OSError)
            and hasattr(e, 'args')
            and 'Directory fsync' in str(e) or 'FlushFileBuffers' in str(e) or 'fsync' in str(e).lower()
        )
        if is_post_rename and (args.strict or not allow_degraded):
            print(
                f'Error: Directory fsync failed after rename: {e}',
                file=sys.stderr,
            )
            print(
                'IMPORTANT: The target file HAS been replaced with new content, '
                'but the directory entry update may not be persisted to disk. '
                'If power fails now, the file could revert to old content or disappear. '
                'Missing crash-safety step: directory fsync.',
                file=sys.stderr,
            )
            return EXIT_POST_RENAME_FAILURE
        else:
            print(f'Error: Atomic write failed before rename: {e}', file=sys.stderr)
            print('The target file is unchanged (still contains old content).', file=sys.stderr)
            return EXIT_WRITE_FAILED

    if result.success:
        if not args.quiet:
            print(f'Wrote to {result.target_path}')
            print(f'  Temp file fsync: {result.temp_file_fsync}')
            print(f'  Directory fsync: {result.dir_fsync}')
            print(f'  Fully crash-safe: {"YES" if result.fully_crash_safe else "NO"}')
            for w in result.warnings:
                print(f'  WARNING: {w}', file=sys.stderr)

        if result.fully_crash_safe:
            return EXIT_SUCCESS
        else:
            print(
                'DEGRADED: Write succeeded but full crash-safety NOT guaranteed. '
                'See warnings above.',
                file=sys.stderr,
            )
            return EXIT_DEGRADED_SUCCESS
    else:
        print('Error: Write reported failure without exception', file=sys.stderr)
        return EXIT_WRITE_FAILED


def _run_check(args) -> int:
    check = check_crash_safety(args.target)

    if args.quiet:
        if check.would_be_fully_crash_safe:
            return EXIT_SUCCESS
        return EXIT_DEGRADED_SUCCESS

    print(f'Crash-safety check for: {check.target_path}')
    print(f'  Parent directory: {check.parent_dir}')
    print(f'  Parent directory exists: {"YES" if check.parent_dir_exists else "NO (will be created on write)"}')

    if check.same_filesystem is None:
        print('  Same filesystem: UNKNOWN (could not determine)')
    elif check.same_filesystem:
        print('  Same filesystem: YES (rename will be atomic)')
    else:
        print('  Same filesystem: NO (rename will be copy+unlink, NOT atomic!)')

    if check.dir_fsync_supported is None:
        print('  Directory fsync: NOT TESTED (parent dir does not exist)')
    elif check.dir_fsync_supported:
        print('  Directory fsync: SUPPORTED')
    else:
        print(f'  Directory fsync: NOT SUPPORTED ({check.dir_fsync_error})')

    if check.would_be_fully_crash_safe:
        print('  Crash-safety level: FULL (rename atomic + directory entry persisted)')
    else:
        missing = []
        if not check.parent_dir_exists:
            missing.append('parent directory must be created first')
        if check.same_filesystem is not True:
            missing.append('rename will NOT be atomic (different filesystems)')
        if check.dir_fsync_supported is not True:
            missing.append('directory fsync not supported (directory entry may not persist after rename)')
        print(f'  Crash-safety level: DEGRADED')
        print(f'  Missing guarantees: {"; ".join(missing)}')

    for w in check.warnings:
        print(f'  WARNING: {w}', file=sys.stderr)

    if args.dry_run:
        print()
        if check.would_be_fully_crash_safe:
            print('Dry-run result: Write would be FULLY crash-safe (exit code 0)')
        elif check.dir_fsync_supported is False or check.same_filesystem is False:
            print('Dry-run result: Write would succeed in DEGRADED mode (exit code 2)')
        else:
            print('Dry-run result: Write would likely succeed but crash-safety is uncertain')

    if check.would_be_fully_crash_safe:
        return EXIT_SUCCESS
    return EXIT_DEGRADED_SUCCESS


if __name__ == '__main__':
    sys.exit(main())
