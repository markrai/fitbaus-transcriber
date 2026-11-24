import os
from pathlib import Path
import pty
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "installation"
        self.install.mkdir()
        shutil.copyfile(SCRIPT, self.install / "install.sh")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        self.running = self.root / "running"
        self.env = {**os.environ, "PATH": str(self.bin) + ":" + os.environ["PATH"],
                    "FAKE_LOG": str(self.log), "FAKE_RUNNING": str(self.running),
                    "TRANSCRIBER_TOKEN": "override-must-not-be-used"}
        self.executable("curl", """#!/bin/bash
set -eu
while [[ $# -gt 0 ]]; do
  if [[ $1 == -o ]]; then
    printf 'services: {}\\n' > "$2"
    exit
  fi
  shift
done
exit 1
""")
        self.executable("docker", """#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$FAKE_LOG"
if [[ $1 == info ]]; then echo linux/x86_64; exit; fi
if [[ $1 == pull ]]; then exit; fi
if [[ $1 == image ]]; then
  printf 'ghcr.io/markrai/fitbaus-transcriber@sha256:%064d\\n' 1
  exit
fi
shift
if [[ $1 == version ]]; then exit; fi
[[ ! -v TRANSCRIBER_TOKEN ]]
while [[ $1 == --* || $1 == -f ]]; do shift 2; done
case "$1" in
  config) exit ;;
  run)
    if [[ " $* " == *" --entrypoint tar "* ]]; then
      [[ ! -e "$FAKE_RUNNING" ]]
      [[ ${FAIL_BACKUP:-0} == 0 ]] || exit 2
      tar -cf - --files-from /dev/null
    else
      [[ ${FAIL_PREFLIGHT:-0} == 0 ]] || exit 2
    fi ;;
  up) touch "$FAKE_RUNNING" ;;
  stop) rm -f "$FAKE_RUNNING" ;;
  ps) if [[ -e "$FAKE_RUNNING" ]]; then echo container; fi ;;
  exec) if [[ " $* " == *" pair "* || $* == *" pair" ]]; then echo ABCD-EFGH; fi ;;
esac
""")

    def executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o700)

    def run_script(self, *args, answers=None, extra=None):
        env = {**self.env, **(extra or {})}
        command = ["bash", str(self.install / "install.sh"), *args]
        if answers is None:
            return subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
        master, slave = pty.openpty()
        try:
            with os.fdopen(os.dup(master), "wb", buffering=0) as terminal:
                process = subprocess.Popen(command, env=env, stdin=slave, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                terminal.write(answers.encode())
                out, err = process.communicate(timeout=15)
            return subprocess.CompletedProcess(command, process.returncode, out, err)
        finally:
            os.close(master)
            os.close(slave)

    def setup_install(self):
        result = self.run_script("install", "v0.1.0", answers="http://192.168.1.20:13378\n192.168.1.30\n\ny\nn\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.install / ".env"
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        settings = dict(line.split("=", 1) for line in env.read_text().splitlines())
        self.assertRegex(settings["TRANSCRIBER_TOKEN"], r"^[a-f0-9]{64}$")
        self.assertNotIn(settings["TRANSCRIBER_TOKEN"], result.stdout + result.stderr + self.log.read_text())
        self.assertIn("ABCD-EFGH", result.stdout)
        self.assertIn("@sha256:", settings["TRANSCRIBER_IMAGE"])
        return settings

    def test_install_and_upgrade_preserve_configuration_and_make_backup(self):
        before = self.setup_install()
        result = self.run_script("upgrade", "v0.2.0", answers="y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = dict(line.split("=", 1) for line in (self.install / ".env").read_text().splitlines())
        self.assertEqual(after.pop("FITBAUS_RELEASE_VERSION"), "v0.2.0")
        before.pop("FITBAUS_RELEASE_VERSION")
        self.assertEqual(before, after)
        backups = list((self.install / "backups").glob("*/data.tar"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(self.running.exists())

    def test_failed_backup_restarts_original_and_never_switches_configuration(self):
        self.setup_install()
        old = (self.install / ".env").read_bytes()
        result = self.run_script("upgrade", "v0.2.0", answers="y\n", extra={"FAIL_BACKUP": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.running.exists())
        self.assertEqual((self.install / ".env").read_bytes(), old)
        self.assertFalse(list((self.install / "backups").glob("*/data.tar")))

    def test_failed_candidate_preflight_does_not_stop_service(self):
        self.setup_install()
        old = (self.install / ".env").read_bytes()
        result = self.run_script("upgrade", "v0.2.0", answers="y\n", extra={"FAIL_PREFLIGHT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.running.exists())
        self.assertEqual((self.install / ".env").read_bytes(), old)
        self.assertFalse((self.install / "backups").exists())

    def test_refuses_existing_source_install_and_public_http(self):
        (self.install / ".env").write_text("existing")
        result = self.run_script("install", "v0.1.0", answers="\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.install / ".env").read_text(), "existing")
        (self.install / ".env").unlink()
        result = self.run_script("install", "v0.1.0", answers="http://8.8.8.8:13378\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.install / ".env").exists())
