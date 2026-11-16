"""Keep deployed secret inputs connected to their host's synchronization plan."""
import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_every_managed_compose_input_has_a_sync_destination(self):
        manifest = json.loads((ROOT / 'shared/secrets/deployments.json').read_text())
        outputs = set()
        for host, entries in manifest.items():
            for stack, entry in entries.items():
                self.assertTrue(entry['encrypted_file'].startswith(f'secrets/{host}/'), f'{host}/{stack}: cross-host encrypted input')
                self.assertTrue(entry['encrypted_file'].endswith('.enc.json'), f'{host}/{stack}: encrypted JSON required')
                for dependency in entry.get('dependencies', []):
                    self.assertIn(dependency, entries, f'{host}/{stack}: missing dependency')
                targets = list(entry.get('files', {}).values())
                if entry.get('env_file'):
                    targets.append(entry['env_file'])
                for target in targets:
                    self.assertTrue((ROOT / target).resolve().is_relative_to(ROOT), target)
                outputs.update(targets)
                for compose in entry.get('compose', []):
                    self.assertTrue((ROOT / entry['directory'] / compose).is_file(), f'{host}/{stack}: {compose}')
        for path in [*ROOT.rglob('docker-compose.yml'), *ROOT.rglob('docker-compose.yaml')]:
            data = yaml.safe_load(path.read_text())
            targets = []
            # These are independently recovered host credentials, not application secrets.
            host_mounts = {
                '~/.docker/config.json',  # Registry login for Watchtower.
                '~/.ssh',               # Backup transport identity.
                './rclone.conf',        # Backup transport configuration.
            }
            for service in data.get('services', {}).values():
                for mount in service.get('volumes', []):
                    source = mount.split(':', 1)[0] if isinstance(mount, str) else mount.get('source', '')
                    if source in host_mounts:
                        continue
                    name = Path(source).name.lower()
                    if any(term in name for term in ('secret', 'token', 'credential')):
                        targets.append(source)
                env_files = service.get('env_file', [])
                if isinstance(env_files, str):
                    env_files = [env_files]
                for item in env_files:
                    name = item if isinstance(item, str) else item['path']
                    if Path(name).name.startswith('.secrets'):
                        self.assertEqual(item.get('format'), 'raw', str(path))
                        targets.append(name)
            for secret in data.get('secrets', {}).values():
                if Path(secret.get('file', '')).parts[:1] == ('.secrets',):
                    targets.append(secret['file'])
            for target in targets:
                relative = str((path.parent / target).relative_to(ROOT))
                self.assertIn(relative, outputs, f'{path}: no sync output for {target}')

    def test_committed_secret_documents_contain_only_ciphertext(self):
        for path in (ROOT / 'secrets').rglob('*.enc.json'):
            document = json.loads(path.read_text())
            self.assertIsInstance(document.get('sops'), dict, f'{path}: missing SOPS metadata')
            self.assertTrue(document['sops'].get('mac'), f'{path}: missing integrity metadata')
            values = {key: value for key, value in document.items() if key != 'sops'}
            self.assertTrue(values, f'{path}: no encrypted secret values')
            for key, value in values.items():
                self.assertIsInstance(value, str, f'{path}: {key} must be an encrypted string')
                self.assertTrue(value.startswith('ENC[') and value.endswith(']'), f'{path}: plaintext value for {key}')
