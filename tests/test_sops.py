"""Test decrypt-before-publish behavior without needing real deployment keys."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/sops-secrets.py'


class SopsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'shared/secrets').mkdir(parents=True)
        (self.root / 'secrets/dell').mkdir(parents=True)
        (self.root / 'dell/app').mkdir(parents=True)
        self.manifest = {'dell': {'app': {'directory': 'dell/app',
            'encrypted_file': 'secrets/dell/app.enc.json',
            'env_file': 'dell/app/.secrets.env', 'required': ['TEST_VALUE'],
            'files': {'DOCUMENT': 'dell/app/.secrets/document.json'}, 'file_only': ['DOCUMENT'],
            'compose': ['docker-compose.yml']}}}
        self.write_manifest()
        # Only the fake sops executable reads this plaintext fixture, outside the repository.
        self.values = {'TEST_VALUE': 'test$${HOME} # literal\\value', 'DOCUMENT': '{\n"test": true\n}'}
        self.source = self.root / 'secrets/dell/app.enc.json'
        self.source.write_text(json.dumps(self.values))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'sops'
        fake.write_text('#!/usr/bin/env python3\nimport pathlib, sys\np = pathlib.Path(sys.argv[-1])\nif "FAIL" in p.read_text():\n print("PRIVATE-ERROR", file=sys.stderr)\n sys.exit(1)\nprint(p.read_text())\n')
        fake.chmod(0o700)

    def write_manifest(self):
        (self.root / 'shared/secrets/deployments.json').write_text(json.dumps(self.manifest))

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root), *args],
            capture_output=True, text=True, env={**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH']})

    def test_decryption_materializes_private_literal_files(self):
        result = self.run_script('sync', '--host', 'dell')
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.root / 'dell/app/.secrets.env'
        self.assertEqual(env.read_text(), 'TEST_VALUE=test$${HOME} # literal\\value\n')
        document = self.root / 'dell/app/.secrets/document.json'
        self.assertEqual(document.read_text(), '{\n"test": true\n}')
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(document.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.values['TEST_VALUE'], result.stdout + result.stderr)

    def test_failed_decryption_keeps_all_previous_outputs(self):
        self.manifest['dell']['second'] = {'directory': 'dell/app', 'encrypted_file': 'secrets/dell/second.enc.json',
            'env_file': 'dell/app/.second.env'}
        self.write_manifest()
        (self.root / 'secrets/dell/second.enc.json').write_text('FAIL')
        target = self.root / 'dell/app/.secrets.env'
        target.write_text('last good')
        result = self.run_script('sync', '--host', 'dell')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('decryption failed', result.stderr)
        self.assertEqual(target.read_text(), 'last good')
        self.assertFalse((self.root / 'dell/app/.secrets/document.json').exists())
        self.assertNotIn('PRIVATE-ERROR', result.stderr)

    def test_missing_required_value_is_not_published(self):
        self.source.write_text(json.dumps({'DOCUMENT': 'text'}))
        result = self.run_script('sync', '--host', 'dell')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('TEST_VALUE', result.stderr)
        self.assertFalse((self.root / 'dell/app/.secrets.env').exists())

    def test_multiline_environment_requires_file_delivery(self):
        self.values['TEST_VALUE'] = 'one\ntwo'
        self.source.write_text(json.dumps(self.values))
        result = self.run_script('sync', '--host', 'dell')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('multiline', result.stderr)

    def test_symlink_cannot_write_outside_repo(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / 'dell/app/.secrets').symlink_to(outside, target_is_directory=True)
            result = self.run_script('sync', '--host', 'dell')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('outside', result.stderr)
            self.assertFalse((Path(outside) / 'document.json').exists())

    def test_template_contains_required_and_file_keys_without_values(self):
        result = self.run_script('template', '--host', 'dell', '--stack', 'app')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'TEST_VALUE': '', 'DOCUMENT': ''})

    def test_cached_compose_needs_no_sops_and_preserves_values(self):
        self.assertEqual(self.run_script('sync', '--host', 'dell').returncode, 0)
        (self.bin / 'sops').unlink()
        docker = self.bin / 'docker'
        docker.write_text('#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\nPath("capture.json").write_text(json.dumps([sys.argv[1:], os.environ["TEST_VALUE"]]))\n')
        docker.chmod(0o700)
        result = self.run_script('compose', '--host', 'dell', '--stack', 'app', '--cached', '--', 'up', '-d')
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads((self.root / 'dell/app/capture.json').read_text())
        self.assertEqual(captured, [['compose', '-f', 'docker-compose.yml', 'up', '-d'], 'test$${HOME} # literal\\value'])
