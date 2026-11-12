"""Regression tests: failed database dumps must never replace a usable backup."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'shared/backup/hooks'
sys.path.insert(0, str(HOOKS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f'{name}-hook.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackupTests(unittest.TestCase):
    def exercise_dump(self, kind, rc):
        module = load(kind)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'database.dump'
            target.write_bytes(b'last good backup')
            docker = Path(directory) / 'docker'
            docker.write_text('#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\nPath(os.environ["ARGS_FILE"]).write_text(json.dumps([sys.argv[1:], os.environ.get("MYSQL_PWD")]))\nsys.stdout.buffer.write(b"new dump" if os.environ["DUMP_RC"] == "0" else b"partial dump")\nsys.exit(int(os.environ["DUMP_RC"]))\n')
            docker.chmod(0o700)
            args_file = Path(directory) / 'args.json'
            output = io.StringIO()
            with patch.dict(os.environ, {'PATH': directory + os.pathsep + os.environ['PATH'],
                                         'ARGS_FILE': str(args_file), 'DUMP_RC': str(rc)}), contextlib.redirect_stdout(output):
                if kind == 'mariadb':
                    result = module.dump_db('database', 'root', 'sensitive-password', 'app', target)
                else:
                    result = module.dump_db('database', 'postgres', 'app', target)
            command, password = json.loads(args_file.read_text())
            docker.unlink()
            args_file.unlink()
            self.assertEqual(result, rc)
            self.assertEqual(target.read_bytes(), b'new dump' if rc == 0 else b'last good backup')
            self.assertEqual(list(Path(directory).iterdir()), [target])
            if kind == 'mariadb':
                self.assertNotIn('sensitive-password', output.getvalue())
                self.assertNotIn('sensitive-password', ' '.join(command))
                self.assertIn('mariadb-dump', command)
                self.assertEqual(password, 'sensitive-password')
            if rc == 0:
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_mariadb_failure_preserves_previous_dump(self):
        self.exercise_dump('mariadb', 1)

    def test_mariadb_success_is_private_and_does_not_log_credentials(self):
        self.exercise_dump('mariadb', 0)

    def test_postgres_failure_preserves_previous_dump(self):
        self.exercise_dump('postgres', 1)

    def test_postgres_success_is_private(self):
        self.exercise_dump('postgres', 0)

    def test_nextcloud_password_and_quoted_env_values(self):
        module = load('mariadb')
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('NEXTCLOUD_DB_ROOT_PASSWORD="a password"\n')
            self.assertEqual(module.load_env(env_file)['NEXTCLOUD_DB_ROOT_PASSWORD'], 'a password')


if __name__ == '__main__':
    unittest.main()

class PasswordFileTests(unittest.TestCase):
    def test_mariadb_cli_reads_password_file_without_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password = root / 'root_password'
            password.write_text('literal$secret\'"\\value\n')
            docker = root / 'docker'
            docker.write_text('#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\nif sys.argv[1] == "ps": print("database")\nelse:\n Path(os.environ["CAPTURE_FILE"]).write_text(os.environ["MYSQL_PWD"])\n print("dump")\n')
            docker.chmod(0o700)
            result = __import__('subprocess').run([sys.executable, str(HOOKS / 'mariadb-hook.py'),
                'database', 'app', '--password-file', str(password), '--backup-dir', str(root / 'dumps')],
                capture_output=True, text=True, env={**os.environ,
                    'PATH': str(root) + os.pathsep + os.environ['PATH'], 'CAPTURE_FILE': str(root / 'captured')})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'captured').read_text(), 'literal$secret\'"\\value')
            self.assertNotIn('literal$secret', result.stdout + result.stderr)
