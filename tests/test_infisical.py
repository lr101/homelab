"""Exercise secret sync against a local TLS API, including failure and escaping."""
import http.server
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/infisical-secrets.py'


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls = tempfile.TemporaryDirectory()
        cert = Path(cls.tls.name) / 'cert.pem'
        key = Path(cls.tls.name) / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.cert = cert
        cls.values = {}
        cls.requests = []
        cls.fail_path = None

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                cls.requests.append((self.path, payload))
                if self.path != '/api/v1/auth/universal-auth/login' or payload != {
                    'clientId': 'host-client', 'clientSecret': 'machine-secret'}:
                    self.send_error(403)
                    return
                self.reply({'accessToken': 'test-access-token', 'expiresIn': 60,
                            'accessTokenMaxTTL': 60, 'tokenType': 'Bearer'})

            def do_GET(self):
                from urllib.parse import urlsplit, parse_qs
                query = parse_qs(urlsplit(self.path).query)
                cls.requests.append((self.path, self.headers.get('Authorization')))
                if query.get('secretPath') == [cls.fail_path]:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'NEVER-LOG-THIS-SECRET')
                    return
                if not self.path.startswith('/api/v4/secrets?') or self.headers.get('Authorization') != 'Bearer test-access-token':
                    self.send_error(403)
                    return
                self.reply({'secrets': [{'secretKey': key, 'secretValue': value,
                                       'secretValueHidden': False, 'secretPath': query['secretPath'][0]}
                                      for key, value in cls.values.items()], 'imports': []})

            def reply(self, payload):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        cls.server = http.server.ThreadingHTTPServer(('localhost', 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tls.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'dell/app').mkdir(parents=True)
        (self.root / 'shared/infisical').mkdir(parents=True)
        secret = self.root / 'client-secret'
        secret.write_text('machine-secret\n')
        secret.chmod(0o600)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'host': 'dell', 'url': f'https://localhost:{self.server.server_port}',
            'project_id': 'project-id', 'environment': 'prod', 'client_id': 'host-client',
            'client_secret_file': str(secret)}))
        self.manifest = {'dell': {'app': {'directory': 'dell/app', 'secret_path': '/dell/app',
            'env_file': 'dell/app/.secrets.env', 'required': ['PASSWORD'],
            'files': {'DOCUMENT': 'dell/app/.secrets/document.json'}, 'file_only': ['DOCUMENT'],
            'compose': ['docker-compose.yml']}}}
        self.write_manifest()
        type(self).values = {'PASSWORD': 'pa$$word${HOME} # a\\b\'"', 'DOCUMENT': '{\n"key": "value"\n}'}
        type(self).requests = []
        type(self).fail_path = None

    def write_manifest(self):
        (self.root / 'shared/infisical/deployments.json').write_text(json.dumps(self.manifest))

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
            '--config', str(self.config), *args], capture_output=True, text=True,
            env={**os.environ, 'SSL_CERT_FILE': str(self.cert)})

    def test_sync_preserves_literal_values_and_writes_private_files(self):
        result = self.run_script('sync', '--host', 'dell')
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / 'dell/app/.secrets.env'
        self.assertEqual(target.read_text(), 'PASSWORD=pa$$word${HOME} # a\\b\'"\n')
        document = self.root / 'dell/app/.secrets/document.json'
        self.assertEqual(document.read_text(), '{\n"key": "value"\n}')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(document.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('machine-secret', result.stdout + result.stderr)
        self.assertNotIn('test-access-token', result.stdout + result.stderr)

    def test_missing_required_value_preserves_existing_files(self):
        target = self.root / 'dell/app/.secrets.env'
        target.write_text('PASSWORD=last-good\n')
        type(self).values.pop('PASSWORD')
        result = self.run_script('sync', '--host', 'dell')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PASSWORD', result.stderr)
        self.assertEqual(target.read_text(), 'PASSWORD=last-good\n')
        self.assertFalse((self.root / 'dell/app/.secrets/document.json').exists())

    def test_later_api_failure_does_not_publish_partial_host_update(self):
        self.manifest['dell']['second'] = {**self.manifest['dell']['app'], 'secret_path': '/denied',
            'env_file': 'dell/second/.secrets.env', 'files': {}}
        self.write_manifest()
        type(self).fail_path = '/denied'
        result = self.run_script('sync', '--host', 'dell')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'dell/app/.secrets.env').exists())
        self.assertIn('403', result.stderr)
        self.assertNotIn('NEVER-LOG-THIS-SECRET', result.stdout + result.stderr)

    def test_host_identity_cannot_sync_another_host(self):
        self.manifest['medion'] = self.manifest['dell']
        self.write_manifest()
        result = self.run_script('sync', '--host', 'medion')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('identity', result.stderr)
        self.assertEqual(self.requests, [])

    def test_multiline_environment_values_require_file_delivery(self):
        type(self).values['PASSWORD'] = 'line1\nline2'
        result = self.run_script('sync', '--host', 'dell')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('multiline', result.stderr)
        self.assertFalse((self.root / 'dell/app/.secrets.env').exists())

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / 'dell/app/.secrets').symlink_to(outside, target_is_directory=True)
            result = self.run_script('sync', '--host', 'dell')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('outside', result.stderr)
            self.assertFalse((Path(outside) / 'document.json').exists())

    def test_bootstrap_is_private_and_refuses_to_replace_keys(self):
        result = self.run_script('bootstrap')
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / 'dell/infisical/.env'
        first = target.read_bytes()
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        result = self.run_script('bootstrap')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), first)

    def fake_docker(self):
        directory = self.root / 'bin'
        directory.mkdir()
        executable = directory / 'docker'
        executable.write_text('#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\nPath("captured.json").write_text(json.dumps({"password": os.environ["PASSWORD"], "args": sys.argv[1:], "token": os.environ.get("INFISICAL_TOKEN")}))\n')
        executable.chmod(0o700)
        return directory

    def test_compose_cached_preserves_literals_without_network(self):
        result = self.run_script('sync', '--host', 'dell')
        self.assertEqual(result.returncode, 0, result.stderr)
        type(self).requests = []
        directory = self.fake_docker()
        from unittest.mock import patch
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
            result = self.run_script('compose', '--host', 'dell', '--stack', 'app', '--cached', '--', 'up', '-d')
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads((self.root / 'dell/app/captured.json').read_text())
        self.assertEqual(captured['password'], 'pa$$word${HOME} # a\\b\'"')
        self.assertEqual(captured['args'], ['compose', '-f', 'docker-compose.yml', 'up', '-d'])
        self.assertIsNone(captured['token'])
        self.assertEqual(self.requests, [])

    def test_compose_synchronizes_related_file_inputs(self):
        self.manifest['dell']['app']['dependencies'] = ['extra']
        self.manifest['dell']['extra'] = {'directory': 'dell/app', 'secret_path': '/dell/extra',
            'files': {'DOCUMENT': 'dell/app/.secrets/extra.json'}}
        self.write_manifest()
        directory = self.fake_docker()
        from unittest.mock import patch
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
            result = self.run_script('compose', '--host', 'dell', '--stack', 'app', '--', 'up', '-d')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'dell/app/.secrets/extra.json').read_text(), '{\n"key": "value"\n}')
