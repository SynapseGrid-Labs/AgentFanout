"""Clean-room children must not inherit group-writable clone permissions."""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cleanroom_test_modes", ROOT / "scripts/cleanroom-test.py")
cleanroom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanroom)


@unittest.skipUnless(os.name == "posix", "Clean-room installation is POSIX-only")
class CleanroomModeTests(unittest.TestCase):
    def test_child_restricts_inherited_group_writable_umask_without_changing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            prior = os.umask(0o002)
            try:
                cleanroom._run([sys.executable, "-c", "from pathlib import Path; Path('trusted.json').write_text('{}')"],
                               cwd=Path(directory))
                mode = stat.S_IMODE((Path(directory) / "trusted.json").stat().st_mode)
                self.assertEqual(mode, 0o644)
                parent_current = os.umask(0o002)
                self.assertEqual(parent_current, 0o002)
            finally:
                os.umask(prior)
