"""Check that systemd accepts the generated Docker updater service, including an install path with spaces."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ticket_pilot.docker_update_manager import systemd_unit


class SystemdUnitTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and shutil.which("systemd-analyze"), "requires systemd-analyze")
    def test_generated_unit_passes_systemd_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tp install 100%"
            root.mkdir()
            service = Path(directory) / "ticket-pilot-docker-updater.service"
            service.write_text(systemd_unit(root, [sys.executable, "-m", "ticket_pilot.docker_update_manager", "run"],
                                            {"PATH": os.environ["PATH"]}))
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            result = subprocess.run(["systemd-analyze", "--user", "verify", str(service)], capture_output=True,
                                    text=True, env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)}, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
