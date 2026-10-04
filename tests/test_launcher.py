import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_portable_path_and_working_defaults(self):
        with tempfile.TemporaryDirectory(prefix="monitor test ") as directory:
            root = Path(directory)
            project = root / "project with spaces"
            project.mkdir()
            shutil.copyfile(ROOT / "run-xvfb.sh", project / "run-xvfb.sh")
            binary = root / "bin"
            binary.mkdir()
            fake = binary / "xvfb-run"
            fake.write_text(f"#!{sys.executable}\nimport os,sys,json\nprint(json.dumps({{'args':sys.argv[1:],'cwd':os.getcwd(),'env':{{k:os.getenv(k) for k in ['HEADLESS','CODEX_POLL_SECONDS','CLAUDE_REFRESH_SECONDS','PROVIDER_FALLBACK_SECONDS']}}}}))\n")
            fake.chmod(0o700)
            # Keep the pre-fix launcher from starting a real display during RED.
            (binary / "Xvfb").write_text("#!/bin/sh\nexit 0\n")
            (binary / "Xvfb").chmod(0o700)
            env = {"PATH": str(binary) + os.pathsep + os.defpath,
                   "HOME": str(root), "HEADLESS": "1"}
            result = subprocess.run(["sh", str(project / "run-xvfb.sh")],
                                    env=env, cwd=root, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["cwd"], str(project))
            self.assertEqual(data["env"], {"HEADLESS": "0", "CODEX_POLL_SECONDS": "5",
                                          "CLAUDE_REFRESH_SECONDS": "5", "PROVIDER_FALLBACK_SECONDS": "300"})
            self.assertEqual(data["args"][-2:], [str(project / ".venv/bin/python"), str(project / "app.py")])
            self.assertIn("-a", data["args"])


if __name__ == "__main__":
    unittest.main()
