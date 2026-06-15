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
import unittest
from pathlib import Path
from unittest import mock

import atomic_file
from atomic_file import atomic_write, atomic_write_read


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='atomic_test_')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_basic_write_bytes(self):
        target = os.path.join(self.test_dir, 'test.bin')
        data = b'\x00\x01\x02\x03\xff\xfe'
        atomic_write(target, data)
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), data)

    def test_basic_write_str(self):
        target = os.path.join(self.test_dir, 'test.txt')
        text = 'Hello, 原子写入！'
        atomic_write(target, text)
        content = atomic_write_read(target)
        self.assertEqual(content, text)

    def test_overwrite_existing_file(self):
        target = os.path.join(self.test_dir, 'overwrite.txt')
        old_data = 'OLD CONTENT, very important data that must be preserved on failure'
        atomic_write(target, old_data)
        self.assertEqual(atomic_write_read(target), old_data)

        new_data = 'NEW CONTENT, replacing the old'
        atomic_write(target, new_data)
        self.assertEqual(atomic_write_read(target), new_data)

    def test_write_to_nonexistent_subdir(self):
        target = os.path.join(self.test_dir, 'a', 'b', 'c', 'deep.txt')
        data = 'deeply nested file'
        atomic_write(target, data)
        self.assertEqual(atomic_write_read(target), data)

    def test_permissions_set(self):
        if sys.platform == 'win32':
            self.skipTest('Windows 不支持 Unix 权限测试')
        target = os.path.join(self.test_dir, 'perm.txt')
        atomic_write(target, 'data', permissions=0o600)
        mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o600)

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
                        result = self._raw.write(data[:half])
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

    def test_large_file_write(self):
        target = os.path.join(self.test_dir, 'large.bin')
        size = 2 * 1024 * 1024
        data = os.urandom(size)
        atomic_write(target, data)
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

    def test_directory_fsync_is_called(self):
        target = os.path.join(self.test_dir, 'fsync_check.txt')
        with mock.patch.object(atomic_file, '_fsync_dir') as mock_fsync:
            atomic_write(target, 'hello')
            mock_fsync.assert_called_once()
            args, _ = mock_fsync.call_args
            self.assertEqual(Path(args[0]).resolve(), Path(self.test_dir).resolve())


if __name__ == '__main__':
    unittest.main(verbosity=2)
