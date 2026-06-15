"""
崩溃安全的原子文件写入工具

采用经典的"写临时文件 + fsync + 原子重命名 + 父目录 fsync"方案，
确保在任意时刻断电/崩溃，文件要么是旧内容，要么是完整的新内容，
绝不会出现半写的损坏状态。
"""

import os
import sys
import tempfile
import errno
from pathlib import Path
from typing import Union, Optional


def _fsync_dir(dir_path: Union[str, Path]) -> None:
    """
    对目录执行 fsync，确保目录项（包括重命名操作）持久化到磁盘。

    在 POSIX 系统上直接对目录 fd 调用 fsync。
    在 Windows 上，目录 fsync 需要通过 CreateFile 打开目录后调用 FlushFileBuffers，
    Python 的 os.fsync 在 Windows 上不支持目录句柄，因此采用兼容处理：
    Windows 的 MoveFileEx 在 NTFS 上本身是事务性的，这里尽量做最佳努力。
    """
    dir_path = str(dir_path)
    if not os.path.isdir(dir_path):
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    if sys.platform == 'win32':
        try:
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
                return
            try:
                FlushFileBuffers(hdir)
            finally:
                CloseHandle(hdir)
        except Exception:
            pass
    else:
        fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(
    path: Union[str, Path],
    data: Union[bytes, str],
    mode: str = 'wb',
    encoding: Optional[str] = None,
    permissions: Optional[int] = None,
    temp_suffix: str = '.tmp',
    temp_prefix: str = '.~',
) -> None:
    """
    崩溃安全地原子写入文件。

    算法步骤（每一步都防御特定的崩溃场景）：
    1. 解析目标路径，确保父目录存在
    2. 在目标文件 **同一目录** 下创建临时文件（保证同一文件系统）
    3. 将用户数据写入临时文件
    4. 对临时文件执行 fsync ──► 防御：写后 rename 前崩溃，临时文件数据丢失
    5. 关闭临时文件
    6. 执行原子 rename（同一文件系统内是原子的）──► 防御：rename 中途崩溃导致文件损坏
    7. 对父目录执行 fsync ──► 防御：rename 后目录项未持久化导致新文件"消失"

    Args:
        path: 目标文件路径
        data: 要写入的数据，bytes 或 str
        mode: 写入模式，'wb' 或 'w'；默认根据 data 类型推断
        encoding: 当 data 为 str 时使用的编码，默认 utf-8
        permissions: 新建文件的权限（如 0o644），None 则使用系统默认
        temp_suffix: 临时文件名后缀
        temp_prefix: 临时文件名前缀

    Raises:
        OSError: 底层文件操作失败时抛出，此时保证目标文件不变（仍是旧内容）
    """
    path = Path(path).resolve()
    parent_dir = path.parent

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
    try:
        fd, tmp_path = tempfile.mkstemp(
            suffix=temp_suffix,
            prefix=temp_prefix,
            dir=str(parent_dir),
        )

        try:
            with os.fdopen(fd, 'wb') as f:
                fd = None
                f.write(data_bytes)
                f.flush()
                os.fsync(f.fileno())
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
                pass

        os.replace(tmp_path, str(path))
        tmp_path = None

        _fsync_dir(parent_dir)

    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
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
