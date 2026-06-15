"""
原子文件写入工具的测试用例

注意：真正的"崩溃"场景（断电、内核崩溃）无法通过单元测试验证，
这里验证的是正常流程、并发写入、异常回滚等可测试的方面。
"""

import os
import sys
import shutil
import tempfile
import threading
import time
import io
import unittest
from pathlib import Path
from unittest import mock

import atomic_file
from atomic_file import (
    atomic_write,
    atomic_write_read,
    main,
    FsyncStatus,
    AtomicWriteResult,
    EXIT_SUCCESS,
    EXIT_WRITE_FAILED,
    EXIT_DEGRADED_SUCCESS,
    EXIT_INVALID_ARGS,
)


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='atomic_test_')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ========== 基础读写测试 ==========

    def test_basic_write_bytes(self):
        target = os.path.join(self.test_dir, 'test.bin')
        data = b'\x00\x01\x02\x03\xff\xfe'
        result = atomic_write(target, data)
        self.assertTrue(result.success)
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_basic_write_str(self):
        target = os.path.join(self.test_dir, 'test.txt')
        text = 'Hello, 原子写入！'
        result = atomic_write(target, text)
        self.assertTrue(result.success)
        content = atomic_write_read(target)
        self.assertEqual(content, text)

    def test_fully_crash_safe_with_mocked_fsync(self):
        target = os.path.join(self.test_dir, 'fsafe.txt')
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.return_value = FsyncStatus.SUCCESS
            result = atomic_write(target, 'data')
            self.assertIsInstance(result, AtomicWriteResult)
            self.assertEqual(result.temp_file_fsync, FsyncStatus.SUCCESS)
            self.assertEqual(result.dir_fsync, FsyncStatus.SUCCESS)
            self.assertEqual(result.target_path, Path(target).resolve())
            self.assertEqual(result.warnings, [])
            self.assertFalse(result.is_degraded)
            self.assertTrue(result.fully_crash_safe)

    # ========== 覆盖写测试 ==========

    def test_overwrite_existing_file_preserves_old_on_failure(self):
        target = os.path.join(self.test_dir, 'overwrite.txt')
        old_data = 'OLD CONTENT - very important data that must survive failures'
        atomic_write(target, old_data)
        self.assertEqual(atomic_write_read(target), old_data)

        original_fdopen = os.fdopen
        call_flag = [0]

        def failing_fdopen_after_mkstemp(*args, **kwargs):
            call_flag[0] += 1
            if call_flag[0] == 1:
                raise IOError('Simulated disk full during temp file write')
            return original_fdopen(*args, **kwargs)

        with mock.patch.object(os, 'fdopen', side_effect=failing_fdopen_after_mkstemp):
            with self.assertRaises(IOError):
                atomic_write(target, 'NEW CONTENT that should never appear')

        self.assertEqual(atomic_write_read(target), old_data)

    def test_overwrite_existing_file_success(self):
        target = os.path.join(self.test_dir, 'overwrite_success.txt')
        old_data = 'OLD CONTENT'
        atomic_write(target, old_data)
        self.assertEqual(atomic_write_read(target), old_data)

        new_data = 'NEW CONTENT, completely replaced'
        result = atomic_write(target, new_data)
        self.assertTrue(result.success)
        self.assertEqual(atomic_write_read(target), new_data)

    # ========== 子目录测试 ==========

    def test_write_to_nonexistent_subdir(self):
        target = os.path.join(self.test_dir, 'a', 'b', 'c', 'deep.txt')
        data = 'deeply nested file'
        result = atomic_write(target, data)
        self.assertTrue(result.success)
        self.assertEqual(atomic_write_read(target), data)

    # ========== 权限测试 ==========

    def test_permissions_set(self):
        if sys.platform == 'win32':
            self.skipTest('Windows 不支持 Unix 权限测试')
        target = os.path.join(self.test_dir, 'perm.txt')
        atomic_write(target, 'data', permissions=0o600)
        mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    # ========== 失败回滚测试 ==========

    def test_exception_on_write_failure_preserves_old(self):
        target = os.path.join(self.test_dir, 'safe.txt')
        old_data = 'ORIGINAL DATA THAT MUST SURVIVE'
        atomic_write(target, old_data)

        new_target = os.path.join(self.test_dir, 'fail_target.txt')
        original_fdopen = os.fdopen
        call_flag = [0]

        def failing_fdopen(*args, **kwargs):
            call_flag[0] += 1
            if call_flag[0] == 1:
                raise IOError('Simulated disk failure - cannot open temp file for writing')
            return original_fdopen(*args, **kwargs)

        with mock.patch.object(os, 'fdopen', side_effect=failing_fdopen):
            with self.assertRaises(IOError):
                atomic_write(new_target, 'new data that will fail')

        self.assertEqual(atomic_write_read(target), old_data)
        self.assertFalse(os.path.exists(new_target))

    def test_no_temp_files_left_after_success(self):
        target = os.path.join(self.test_dir, 'clean.txt')
        atomic_write(target, 'content')
        entries = list(os.listdir(self.test_dir))
        self.assertEqual(entries, ['clean.txt'])

    def test_no_temp_files_left_after_write_error(self):
        target = os.path.join(self.test_dir, 'fail.txt')

        original_fdopen = os.fdopen
        call_flag = [0]

        def failing_fdopen(*args, **kwargs):
            call_flag[0] += 1
            if call_flag[0] == 1:
                raise IOError('Simulated disk failure mid-write')
            return original_fdopen(*args, **kwargs)

        with mock.patch.object(os, 'fdopen', side_effect=failing_fdopen):
            with self.assertRaises(IOError):
                atomic_write(target, 'some data')

        entries = list(os.listdir(self.test_dir))
        for entry in entries:
            full = os.path.join(self.test_dir, entry)
            if entry.startswith('.~') or entry.endswith('.tmp'):
                self.fail(f'Temporary file left behind: {entry}')

    def test_no_temp_files_left_after_mid_write_failure(self):
        target = os.path.join(self.test_dir, 'fail2.txt')

        class FailingBytesIO:
            def __init__(self, raw):
                self._raw = raw
                self._wrote = False

            def write(self, data):
                if not self._wrote:
                    self._wrote = True
                    half = len(data) // 2
                    if half > 0:
                        self._raw.write(data[:half])
                        raise IOError('Simulated disk full during mid-write')
                return self._raw.write(data)

            def flush(self):
                return self._raw.flush()

            def fileno(self):
                return self._raw.fileno()

            def close(self):
                return self._raw.close()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self._raw.__exit__(*args)

        original_fdopen = os.fdopen

        def mock_fdopen(*args, **kwargs):
            wrapper = original_fdopen(*args, **kwargs)
            return FailingBytesIO(wrapper)

        with mock.patch.object(os, 'fdopen', side_effect=mock_fdopen):
            with self.assertRaises(IOError):
                atomic_write(target, b'A' * 100000)

        entries = list(os.listdir(self.test_dir))
        tmp_found = [e for e in entries if e.startswith('.~') or e.endswith('.tmp')]
        self.assertEqual(tmp_found, [], f'Temp files leaked: {tmp_found}')

    # ========== 并发写入测试 ==========

    def test_concurrent_writes(self):
        target = os.path.join(self.test_dir, 'concurrent.txt')
        atomic_write(target, 'INITIAL')

        NUM_WRITERS = 5
        WRITES_PER_WRITER = 30
        errors = []
        writes_done = [0]
        lock = threading.Lock()

        def writer(writer_id):
            for i in range(WRITES_PER_WRITER):
                attempt = 0
                while attempt < 5:
                    try:
                        payload = f'WRITER_{writer_id}_SEQ_{i}_' + 'X' * 100
                        atomic_write(target, payload)
                        with lock:
                            writes_done[0] += 1
                        break
                    except PermissionError:
                        attempt += 1
                        time.sleep(0.01)
                    except Exception as e:
                        errors.append(e)
                        break

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(NUM_WRITERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f'Errors during concurrent writes: {errors}')
        self.assertEqual(writes_done[0], NUM_WRITERS * WRITES_PER_WRITER)

        content = atomic_write_read(target)
        self.assertTrue(
            content.startswith('WRITER_'),
            f'Final content corrupted, does not match any known write pattern: {content[:50]}'
        )

    # ========== 大文件 & 编码测试 ==========

    def test_large_file_write(self):
        target = os.path.join(self.test_dir, 'large.bin')
        size = 2 * 1024 * 1024
        data = os.urandom(size)
        result = atomic_write(target, data)
        self.assertTrue(result.success)
        with open(target, 'rb') as f:
            read_back = f.read()
        self.assertEqual(read_back, data)
        self.assertEqual(len(read_back), size)

    def test_encoding(self):
        target = os.path.join(self.test_dir, 'encoding.txt')
        text = '测试中文编码 特殊字符'
        atomic_write(target, text, encoding='gbk')
        with open(target, 'r', encoding='gbk') as f:
            self.assertEqual(f.read(), text)

    def test_read_as_bytes(self):
        target = os.path.join(self.test_dir, 'readas.bin')
        binary = b'\xde\xad\xbe\xef'
        atomic_write(target, binary)
        result = atomic_write_read(target, read_encoding=None)
        self.assertIsInstance(result, bytes)
        self.assertEqual(result, binary)

    # ========== fsync 调用测试 ==========

    def test_directory_fsync_is_called(self):
        target = os.path.join(self.test_dir, 'fsync_check.txt')
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.return_value = FsyncStatus.SUCCESS
            atomic_write(target, 'hello')
            mock_fsync.assert_called_once()
            args, _ = mock_fsync.call_args
            self.assertEqual(Path(args[0]).resolve(), Path(self.test_dir).resolve())

    # ========== 降级模式测试 ==========

    def test_dir_fsync_failure_returns_degraded_result(self):
        target = os.path.join(self.test_dir, 'degraded.txt')
        old_data = 'OLD CONTENT'
        atomic_write(target, old_data)

        new_data = 'NEW CONTENT'
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.side_effect = OSError(123, 'Simulated FlushFileBuffers failure')
            result = atomic_write(target, new_data, allow_degraded=True)

        self.assertTrue(result.success)
        self.assertFalse(result.fully_crash_safe)
        self.assertTrue(result.is_degraded)
        self.assertEqual(result.dir_fsync, FsyncStatus.FAILED_DEGRADED)
        self.assertEqual(result.temp_file_fsync, FsyncStatus.SUCCESS)
        self.assertTrue(any('DEGRADED' in w for w in result.warnings))
        self.assertEqual(atomic_write_read(target), new_data)

    def test_dir_fsync_failure_with_no_degraded_raises(self):
        target = os.path.join(self.test_dir, 'no_degraded.txt')
        old_data = 'OLD CONTENT'
        atomic_write(target, old_data)

        new_data = 'NEW CONTENT'
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.side_effect = OSError(123, 'Simulated FlushFileBuffers failure')
            with self.assertRaises(OSError):
                atomic_write(target, new_data, allow_degraded=False)

        self.assertEqual(atomic_write_read(target), new_data)

    def test_dir_fsync_failure_with_strict_raises(self):
        target = os.path.join(self.test_dir, 'strict.txt')
        old_data = 'OLD CONTENT'
        atomic_write(target, old_data)

        new_data = 'NEW CONTENT'
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.side_effect = OSError(123, 'Simulated FlushFileBuffers failure')
            with self.assertRaises(OSError):
                atomic_write(target, new_data, strict=True, allow_degraded=True)

        self.assertEqual(atomic_write_read(target), new_data)

    def test_temp_fsync_failure_before_rename_raises_and_preserves_old(self):
        target = os.path.join(self.test_dir, 'temp_fsync_fail.txt')
        old_data = 'OLD CONTENT'
        atomic_write(target, old_data)

        new_data = 'NEW CONTENT'
        with mock.patch.object(atomic_file, '_fsync_file') as mock_fsync:
            mock_fsync.side_effect = OSError(99, 'Simulated disk error on fsync')
            with self.assertRaises(OSError):
                atomic_write(target, new_data)

        self.assertEqual(atomic_write_read(target), old_data)
        entries = os.listdir(self.test_dir)
        tmp_found = [e for e in entries if e.startswith('.~') or e.endswith('.tmp')]
        self.assertEqual(tmp_found, [], 'Temp file leaked after temp fsync failure')

    # ========== 跨文件系统检测测试 ==========

    def test_cross_filesystem_warning(self):
        target = os.path.join(self.test_dir, 'cross_fs.txt')

        with mock.patch.object(atomic_file, '_verify_same_filesystem') as mock_verify:
            def fake_verify(tmp_path, target_path, warnings):
                warnings.append(
                    'WARNING: Temporary file and target file are on different filesystems'
                )
            mock_verify.side_effect = fake_verify

            result = atomic_write(target, 'data')
            self.assertTrue(result.success)
            self.assertTrue(any('different filesystems' in w for w in result.warnings))

    # ========== 命令行入口测试 ==========

    def test_cli_text_write_success(self):
        target = os.path.join(self.test_dir, 'cli_text.txt')
        argv = [target, '--text', 'Hello CLI world']
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.return_value = FsyncStatus.SUCCESS
            exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertEqual(atomic_write_read(target), 'Hello CLI world')

    def test_cli_file_write_success(self):
        source = os.path.join(self.test_dir, 'source.txt')
        with open(source, 'w', encoding='utf-8') as f:
            f.write('Content from source file')
        target = os.path.join(self.test_dir, 'cli_file.txt')
        argv = [target, '--file', source]
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.return_value = FsyncStatus.SUCCESS
            exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertEqual(atomic_write_read(target), 'Content from source file')

    def test_cli_nonexistent_source_file(self):
        target = os.path.join(self.test_dir, 'cli_not_exist.txt')
        argv = [target, '--file', '/path/that/does/not/exist']
        exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_INVALID_ARGS)

    def test_cli_stdin_write(self):
        target = os.path.join(self.test_dir, 'cli_stdin.txt')
        argv = [target, '--file', '-']
        stdin_content = 'Content from stdin'
        with mock.patch.object(sys, 'stdin', io.StringIO(stdin_content)):
            with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
                mock_fsync.return_value = FsyncStatus.SUCCESS
                exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        self.assertEqual(atomic_write_read(target), stdin_content)

    def test_cli_binary_mode(self):
        target = os.path.join(self.test_dir, 'cli_binary.bin')
        source = os.path.join(self.test_dir, 'binary_source.bin')
        data = b'\xde\xad\xbe\xef\x00\xff'
        with open(source, 'wb') as f:
            f.write(data)
        argv = [target, '--file', source, '--binary']
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.return_value = FsyncStatus.SUCCESS
            exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_cli_binary_with_text_rejected(self):
        target = os.path.join(self.test_dir, 'should_fail.bin')
        argv = [target, '--text', 'some text', '--binary']
        exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_INVALID_ARGS)
        self.assertFalse(os.path.exists(target))

    def test_cli_write_failure_exit_code(self):
        target = os.path.join(self.test_dir, 'cli_fail.txt')

        original_fdopen = os.fdopen
        call_flag = [0]

        def failing_fdopen(*args, **kwargs):
            call_flag[0] += 1
            if call_flag[0] == 1:
                raise IOError('Simulated disk failure')
            return original_fdopen(*args, **kwargs)

        argv = [target, '--text', 'should fail']
        with mock.patch.object(os, 'fdopen', side_effect=failing_fdopen):
            exit_code = main(argv)

        self.assertEqual(exit_code, EXIT_WRITE_FAILED)
        self.assertFalse(os.path.exists(target))

    def test_cli_degraded_success_exit_code(self):
        target = os.path.join(self.test_dir, 'cli_degraded.txt')

        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.side_effect = OSError(123, 'Simulated fsync failure')
            argv = [target, '--text', 'degraded content']
            exit_code = main(argv)

        self.assertEqual(exit_code, EXIT_DEGRADED_SUCCESS)
        self.assertEqual(atomic_write_read(target), 'degraded content')

    def test_cli_no_degraded_flag(self):
        target = os.path.join(self.test_dir, 'cli_no_degraded.txt')

        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.side_effect = OSError(123, 'Simulated fsync failure')
            argv = [target, '--text', 'should fail hard', '--no-degraded']
            exit_code = main(argv)

        self.assertEqual(exit_code, EXIT_WRITE_FAILED)
        self.assertEqual(atomic_write_read(target), 'should fail hard')

    def test_cli_strict_flag(self):
        target = os.path.join(self.test_dir, 'cli_strict.txt')

        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            mock_fsync.side_effect = OSError(123, 'Simulated fsync failure')
            argv = [target, '--text', 'strict mode', '--strict']
            exit_code = main(argv)

        self.assertEqual(exit_code, EXIT_WRITE_FAILED)
        self.assertEqual(atomic_write_read(target), 'strict mode')

    def test_cli_quiet_flag(self):
        target = os.path.join(self.test_dir, 'cli_quiet.txt')
        argv = [target, '--text', 'quiet output', '--quiet']

        with mock.patch('builtins.print') as mock_print:
            with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
                mock_fsync.return_value = FsyncStatus.SUCCESS
                exit_code = main(argv)

        self.assertEqual(exit_code, EXIT_SUCCESS)

    def test_cli_degraded_on_real_platform(self):
        """在真实环境下测试，不 mock fsync，接受可能的降级结果"""
        target = os.path.join(self.test_dir, 'cli_real.txt')
        argv = [target, '--text', 'real platform test', '--quiet']
        exit_code = main(argv)
        self.assertIn(exit_code, [EXIT_SUCCESS, EXIT_DEGRADED_SUCCESS])
        self.assertEqual(atomic_write_read(target), 'real platform test')

    def test_cli_invalid_args_missing_input(self):
        target = os.path.join(self.test_dir, 'cli_invalid.txt')
        argv = [target]
        with self.assertRaises(SystemExit):
            main(argv)

    def test_cli_mutually_exclusive_args(self):
        target = os.path.join(self.test_dir, 'cli_excl.txt')
        argv = [target, '--text', 'foo', '--file', 'bar']
        with self.assertRaises(SystemExit):
            main(argv)

    def test_cli_permissions_flag(self):
        if sys.platform == 'win32':
            self.skipTest('Windows 不支持 Unix 权限测试')
        target = os.path.join(self.test_dir, 'cli_perm.txt')
        argv = [target, '--text', 'permission test', '--permissions', '640']
        exit_code = main(argv)
        self.assertEqual(exit_code, EXIT_SUCCESS)
        mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o640)

    # ========== 旧文件保留综合测试 ==========

    def test_failure_at_every_step_preserves_old_file(self):
        """在 rename 前的每一步都模拟失败，验证旧文件始终不变"""
        target = os.path.join(self.test_dir, 'old_preserve.txt')
        old_data = 'THE ORIGINAL CONTENT - must never be lost'
        atomic_write(target, old_data)

        failure_points = [
            ('os.fdopen', os, 'fdopen'),
            ('atomic_file._fsync_file', atomic_file, '_fsync_file'),
        ]

        for name, module, attr in failure_points:
            original = getattr(module, attr)
            call_flag = [0]

            def make_failing(orig, flag):
                def failing(*args, **kwargs):
                    flag[0] += 1
                    if flag[0] == 1:
                        raise IOError(f'Simulated failure at {name}')
                    return orig(*args, **kwargs)
                return failing

            with mock.patch.object(module, attr, side_effect=make_failing(original, call_flag)):
                try:
                    atomic_write(target, 'NEW CONTENT THAT SHOULD NOT APPEAR')
                except IOError:
                    pass
                else:
                    self.fail(f'Expected IOError when failing at {name}')

            self.assertEqual(
                atomic_write_read(target),
                old_data,
                f'Old file was modified when failing at {name}'
            )

            entries = os.listdir(self.test_dir)
            tmp_found = [e for e in entries if e.startswith('.~') or e.endswith('.tmp')]
            self.assertEqual(tmp_found, [], f'Temp files leaked when failing at {name}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
