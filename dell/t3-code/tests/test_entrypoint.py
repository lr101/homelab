"""Exercise the real privilege drop in a disposable chroot; no Docker needed.

Run as root: python3 -m unittest discover -s dell/t3-code/tests -v
Only PostgreSQL and gh are stand-ins; shell, UID changes, permissions, and exec
use the host's actual binaries. No host services or persisted directories change.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ENTRYPOINT = Path(__file__).resolve().parents[1] / 'entrypoint.sh'


@unittest.skipUnless(os.geteuid() == 0 and shutil.which('chroot'), 'requires root and chroot')
class EntrypointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        for name in ('sh', 'id', 'setpriv', 'env', 'awk'):
            source = Path(shutil.which(name))
            self.copy(source, Path('/bin' if name == 'sh' else '/usr/bin') / name)
            result = subprocess.run(['ldd', str(source)], capture_output=True, text=True, check=True)
            for library in re.findall(r'(/[^\s()]+)', result.stdout):
                self.copy(Path(library), Path(library))
        self.write('/etc/passwd', 'root:x:0:0::/root:/bin/sh\nnode:x:1000:1000::/home/node:/bin/sh\n')
        self.write('/etc/group', 'root:x:0:\nnode:x:1000:\n')
        self.write('/etc/nsswitch.conf', 'passwd: files\ngroup: files\n')
        for directory in ('/home/node/.t3', '/home/node/.codex', '/home/node/.codex_p', '/workspace', '/opt/mise'):
            target = self.root / directory.lstrip('/')
            target.mkdir(parents=True, exist_ok=True)
            os.chown(target, 1000, 1000)
        self.write('/events', '', mode=0o666)
        self.copy(ENTRYPOINT, Path('/entrypoint.sh'))
        self.script('pg_isready', 'exit 0')
        self.script('pg_lsclusters', 'echo "17 main"')
        self.script('pg_ctlcluster', 'echo "postgres:$(id -u)" >> /events')
        self.script('gh', 'echo "gh:$(id -u)" >> /events')
        self.write('/workspace/bootstrap-codex.sh', '#!/bin/sh\necho "bootstrap:$(id -u):$HOME" >> /events\n', mode=0o755)

    def copy(self, source, destination):
        target = self.root / str(destination).lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=True)

    def write(self, path, text, mode=0o644):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(mode)

    def script(self, name, body):
        self.write('/usr/bin/' + name, '#!/bin/sh\n' + body + '\n', mode=0o755)

    def run_entrypoint(self, *args, **overrides):
        env = {'PATH': '/usr/bin:/bin', **overrides}
        return subprocess.run([shutil.which('chroot'), str(self.root), '/entrypoint.sh', *args],
                              env={**os.environ, **env}, capture_output=True, text=True)

    def events(self):
        return (self.root / 'events').read_text()

    def test_command_and_bootstrap_run_as_node(self):
        result = self.run_entrypoint('/usr/bin/id', '-u')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1000')
        self.assertEqual(self.events(), 'bootstrap:1000:/home/node\ngh:1000\n')

    def test_initializes_postgres_as_root_before_dropping_privileges(self):
        self.script('pg_isready', '[ -f /pg-ready ]')
        self.script('pg_ctlcluster', 'echo "postgres:$(id -u):$*" >> /events\n: > /pg-ready')
        result = self.run_entrypoint('/usr/bin/id', '-u')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1000')
        self.assertTrue(self.events().startswith('postgres:0:--skip-systemctl-redirect 17 main start\n'))

    def test_refuses_disabled_browser_sandbox(self):
        result = self.run_entrypoint('/usr/bin/id', '-u', T3CODE_SERVER_BROWSER_SANDBOX='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sandboxing must remain enabled', result.stderr)
        self.assertEqual(self.events(), '')

    def test_reports_unwritable_persisted_state(self):
        os.chown(self.root / 'home/node/.t3', 0, 0)
        result = self.run_entrypoint('/usr/bin/id', '-u')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('/home/node/.t3 must be writable by node', result.stderr)
        self.assertEqual(self.events(), '')

    def test_bootstrap_failure_prevents_server_start(self):
        self.write('/workspace/bootstrap-codex.sh', '#!/bin/sh\nexit 42\n', mode=0o755)
        result = self.run_entrypoint('/usr/bin/id', '-u')
        self.assertEqual(result.returncode, 42)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.events(), '')

    def test_missing_optional_bootstrap_and_command_arguments(self):
        (self.root / 'workspace/bootstrap-codex.sh').unlink()
        result = self.run_entrypoint('/bin/sh', '-c', 'printf "%s" "$1"', 'test', 'value with spaces')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'value with spaces')
        self.assertEqual(self.events(), 'gh:1000\n')

    def test_postgres_start_failure_prevents_bootstrap(self):
        self.script('pg_isready', 'exit 1')
        self.script('pg_ctlcluster', 'exit 43')
        result = self.run_entrypoint('/usr/bin/id', '-u')
        self.assertEqual(result.returncode, 43)
        self.assertEqual(self.events(), '')


if __name__ == '__main__':
    unittest.main()
