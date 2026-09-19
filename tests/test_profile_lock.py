"""A crashed scheduled run must not strand the profile lock."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from profile_lock import acquire


class ProfileLockTests(unittest.TestCase):
    def test_other_process_is_rejected_and_abrupt_exit_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'profile'
            source = str(Path(__file__).resolve().parents[1])
            code = (f'import sys; sys.path.insert(0,{source!r}); from pathlib import Path; '
                    'from profile_lock import acquire; import time; '
                    f'held=acquire(Path({str(target)!r})); held.__enter__(); '
                    'print("acquired",flush=True); time.sleep(45)')
            process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'acquired')
                with self.assertRaises(RuntimeError):
                    with acquire(target):
                        pass
                with acquire(Path(tmp)/'other-profile'):
                    pass
            finally:
                process.kill()
                process.communicate(timeout=10)
            with acquire(target):
                pass


if __name__ == '__main__':
    unittest.main()
