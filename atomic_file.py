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
import errno
import argparse
from pathlib import Path
from typing import Union, Optional, NamedTuple, List


__all__ = [
    'atomic_write',
    'atomic_write_read',
    'AtomicWriteResult',
    'FsyncStatus',
    'main',
]


class FsyncStatus:
    """fsync 操作的状态枚举"""
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
        warnings: 非致命警告信息列表
        target_path: 最终写入的目标文件绝对路径
    """
    success: bool
    fully_crash_safe: bool
    temp_file_fsync: str
    dir_fsync: str
    warnings: List[str]
    target_path: Path

    @property
    def is_degraded(self) -> bool:
        """是否以降级模式成功（写入成功但未完全保障崩溃安全）"""
        return self.success and not self.fully_crash_safe


def _fsync_dir(dir_path: Union[str, Path]) -> str:
    """
    对目录执行 fsync，确保目录项（包括重命名操作）持久化到磁盘。

    **不再静默吞掉错误**：
    - POSIX 系统：直接对目录 fd 调用 fsync，失败抛异常
    - Windows 系统：通过 CreateFile + FlushFileBuffers 实现，失败抛异常
      而不是 pass。让上层决定如何处理降级。

    Returns:
        FsyncStatus 字符串

    Raises:
        OSError: fsync 失败时抛出，由调用者决定是否降级处理
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
    """
    对文件 fd 执行 fsync，确保数据和元数据持久化。

    Returns:
        FsyncStatus 字符串

    Raises:
        OSError: fsync 失败时抛出
    """
    os.fsync(fd)
    return FsyncStatus.SUCCESS


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
       ★ 关键前提：临时文件与目标文件必须在同一挂载点
       ★ 跨文件系统的 rename 不是原子的（会退化成 copy+unlink）
    3. 将用户数据写入临时文件
    4. 对临时文件执行 fsync ──► 防御：写后 rename 前崩溃，临时文件数据丢失
    5. 关闭临时文件
    6. 执行原子 rename（同一文件系统内是原子的）──► 防御：rename 中途崩溃导致文件损坏
    7. 对父目录执行 fsync ──► 防御：rename 后目录项未持久化导致新文件"消失"

    Args:
        path: 目标文件路径
        data: 要写入的数据，bytes 或 str
        encoding: 当 data 为 str 时使用的编码，默认 utf-8
        permissions: 新建文件的权限（如 0o644），None 则使用系统默认
        temp_suffix: 临时文件名后缀
        temp_prefix: 临时文件名前缀
        allow_degraded: 如果目录 fsync 失败（比如 Windows 下某些 FS 不支持），
            是否允许以降级模式返回（写入成功但不保证完全崩溃安全）。
            True 则返回降级结果；False 则抛出异常。
        strict: 严格模式。如果为 True，任何 fsync 失败都直接抛出异常，不回退。
            此参数优先级高于 allow_degraded。

    Returns:
        AtomicWriteResult: 包含每一步的成功状态、是否完全崩溃安全、警告信息

    Raises:
        OSError: 底层文件操作失败时抛出。
            - 在 rename 之前发生的异常：保证目标文件不变（仍是旧内容）
            - 在 rename 之后但目录 fsync 之前的异常：
              * 如果 strict=True：抛出异常，此时目标文件已经更新但可能未完全持久化
              * 如果 strict=False 且 allow_degraded=True：返回降级结果，不抛出
              * 如果 strict=False 且 allow_degraded=False：抛出异常
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
                warnings.append(f'chmod not supported on this platform, permissions not set')

        os.replace(tmp_path, str(path))
        tmp_path = None

        try:
            dir_fsync_status = _fsync_dir(parent_dir)
        except OSError as e:
            if strict:
                warnings.append(f'Directory fsync failed: {e}')
                raise
            elif not allow_degraded:
                warnings.append(f'Directory fsync failed and degraded mode not allowed: {e}')
                raise
            else:
                dir_fsync_status = FsyncStatus.FAILED_DEGRADED
                warnings.append(
                    f'Directory fsync failed with error: {e}. '
                    f'Write succeeded but crash-safety is DEGRADED. '
                    f'In case of power failure immediately after this call, '
                    f'the file may appear to revert to the old content or disappear.'
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
            warnings=warnings,
            target_path=path,
        )

    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _verify_same_filesystem(tmp_path: str, target_path: Path, warnings: List[str]) -> None:
    """
    验证临时文件与目标文件是否在同一文件系统。
    跨文件系统 rename 不是原子的，需要发出警告。
    """
    try:
        tmp_stat = os.stat(tmp_path)
        target_stat = os.stat(str(target_path)) if target_path.exists() else os.stat(str(target_path.parent))
        if tmp_stat.st_dev != target_stat.st_dev:
            warnings.append(
                f'WARNING: Temporary file and target file are on different filesystems '
                f'(st_dev={tmp_stat.st_dev} vs {target_stat.st_dev}). '
                f'os.replace() will NOT be atomic and will fall back to copy+unlink, '
                f'which means a mid-operation crash can leave a corrupted file. '
                f'For true atomicity, ensure temp dir and target are on the same mount point.'
            )
    except OSError:
        pass


def atomic_write_read(
    path: Union[str, Path],
    read_encoding: Optional[str] = 'utf-8',
) -> Union[bytes, str]:
    """
    读取文件的便捷函数（配合 atomic_write 使用）。

    Args:
        path: 文件路径
        read_encoding: 如果指定则返回 str，否则返回 bytes

    Returns:
        文件内容，bytes 或 str
    """
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


def main(argv: Optional[List[str]] = None) -> int:
    """
    命令行入口：
        atomic-write <target_path> --text "content to write"
        atomic-write <target_path> --file <source_file>
        atomic-write <target_path> --file -   # 从 stdin 读取

    退出码：
        0: 成功且完整崩溃安全
        1: 写入失败（目标文件未被修改）
        2: 写入成功但降级（部分 fsync 失败，崩溃安全不完整）
        3: 参数错误
    """
    parser = argparse.ArgumentParser(
        prog='atomic-write',
        description='Crash-safe atomic file writer using temp-file + fsync + rename pattern',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Exit codes:
  0  Success, fully crash-safe
  1  Write failed (target file unchanged)
  2  Write succeeded but DEGRADED (partial fsync failure, not fully crash-safe)
  3  Invalid arguments

Examples:
  atomic-write config.json --text '{"key": "value"}'
  atomic-write data.bin --file /tmp/new_data.bin
  cat large_file.txt | atomic-write output.txt --file -
  atomic-write important.txt --text "hello" --strict
        ''',
    )
    parser.add_argument(
        'target',
        help='Target file path to write atomically',
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        '--text',
        help='Text content to write (encoded as UTF-8 by default)',
    )
    input_group.add_argument(
        '--file',
        help='Source file path to read content from. Use "-" for stdin.',
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
        help='Strict mode: any fsync failure causes hard error (exit 1) instead of degraded success',
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
        print(f'Error: Atomic write failed: {e}', file=sys.stderr)
        print(f'Note: Target file should be unchanged (still contains old content)', file=sys.stderr)
        return EXIT_WRITE_FAILED

    if result.success:
        if not args.quiet:
            print(f'Wrote {len(data)} bytes to {result.target_path}')
            print(f'  Temp file fsync: {result.temp_file_fsync}')
            print(f'  Directory fsync: {result.dir_fsync}')
            print(f'  Fully crash-safe: {"YES" if result.fully_crash_safe else "NO"}')
            for w in result.warnings:
                print(f'  WARNING: {w}', file=sys.stderr)

        if result.fully_crash_safe:
            return EXIT_SUCCESS
        else:
            print(
                'DEGRADED MODE: Write succeeded but full crash-safety NOT guaranteed. '
                'See warnings above.',
                file=sys.stderr,
            )
            return EXIT_DEGRADED_SUCCESS
    else:
        print('Error: Write reported failure without exception', file=sys.stderr)
        return EXIT_WRITE_FAILED


if __name__ == '__main__':
    sys.exit(main())
