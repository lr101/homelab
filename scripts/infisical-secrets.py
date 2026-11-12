#!/usr/bin/env python3
"""Synchronize scoped Infisical secrets and deploy Compose without shell expansion.

Bootstrap credentials stay outside Infisical; see docs/INFISICAL.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


class SyncError(Exception):
    """An actionable error that contains no secret values."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected endpoint.


class Client:
    def __init__(self, config):
        self.url = config['url'].rstrip('/')
        parsed = urlsplit(self.url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise SyncError('Infisical URL must be an HTTPS origin without credentials or a path')
        self.config = config
        self.opener = build_opener(NoRedirect())
        path = Path(config['client_secret_file'])
        if path.stat().st_mode & 0o077:
            raise SyncError('Machine identity secret file must have mode 0600 or stricter')
        secret = path.read_text().strip()
        if not secret:
            raise SyncError('Machine identity secret is empty')
        response = self.request('/api/v1/auth/universal-auth/login', {
            'clientId': config['client_id'], 'clientSecret': secret})
        self.token = response.get('accessToken')
        if not isinstance(self.token, str) or not self.token:
            raise SyncError('Infisical login did not return an access token')

    def request(self, path, payload=None):
        headers = {'Accept': 'application/json'}
        if hasattr(self, 'token'):
            headers['Authorization'] = f'Bearer {self.token}'
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.url + path, headers=headers,
                          data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise SyncError(f'Infisical request failed (HTTP {error.code}); local files unchanged') from None
        except (URLError, TimeoutError, ValueError):
            raise SyncError('Infisical connection or JSON response failed; local files unchanged') from None

    def fetch(self, path):
        query = urlencode({'projectId': self.config['project_id'],
                           'environment': self.config['environment'], 'secretPath': path,
                           'recursive': 'false', 'includeImports': 'false',
                           'includePersonalOverrides': 'false', 'expandSecretReferences': 'true'})
        response = self.request('/api/v4/secrets?' + query)
        values = {}
        if not isinstance(response.get('secrets'), list):
            raise SyncError('Infisical response has no secrets list')
        for item in response['secrets']:
            key, value = item.get('secretKey'), item.get('secretValue')
            if not isinstance(key, str) or not KEY.fullmatch(key) or key.startswith('COMPOSE_'):
                raise SyncError('Infisical response has an unsupported variable name')
            if key in values or not isinstance(value, str) or item.get('secretValueHidden'):
                raise SyncError(f'Infisical returned a duplicate, hidden, or invalid value for {key}')
            values[key] = value
        if not values:
            raise SyncError(f'Infisical folder {path} is empty; local files unchanged')
        return values


def destination(root, name):
    path = root / name
    if Path(name).is_absolute() or not path.resolve().is_relative_to(root):
        raise SyncError('Secret destination points outside the repository')
    return path


def private_write(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.name == '.secrets':
        target.parent.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.infisical-', delete=False) as output:
            temporary = Path(output.name)
            output.write(content.encode())
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def sync(root, config, entries):
    client = Client(config)
    pending = {}
    for entry in entries.values():
        values = client.fetch(entry['secret_path'])
        for key in set(entry.get('required', [])) | set(entry.get('files', {})):
            if not values.get(key):
                raise SyncError(f'Required secret {key} is missing or empty in {entry["secret_path"]}')
        outputs = {}
        if entry.get('env_file'):
            exported = {key: value for key, value in values.items() if key not in entry.get('file_only', [])}
            if any('\n' in value or '\r' in value or '\0' in value for value in exported.values()):
                raise SyncError('A multiline environment value requires file delivery (files/file_only in the manifest)')
            # Compose format: raw; do not quote, interpolate, or execute these values.
            outputs[entry['env_file']] = ''.join(f'{key}={value}\n' for key, value in sorted(exported.items()))
        for key, target in entry.get('files', {}).items():
            outputs[target] = values[key]
        for name, content in outputs.items():
            target = destination(root, name)
            if target in pending:
                raise SyncError('Duplicate secret destination in deployment manifest')
            pending[target] = content
    # Fetch and validate every selected folder before modifying any local files.
    for target, content in pending.items():
        private_write(target, content)
    print(f'Synchronized {len(pending)} private files; no containers restarted')


def cached_environment(root, entry):
    env = dict(os.environ)
    if entry.get('env_file'):
        path = destination(root, entry['env_file'])
        if path.stat().st_mode & 0o077:
            raise SyncError('Cached environment file must have mode 0600 or stricter')
        values = {}
        for line in path.read_text().splitlines():
            if not line:
                continue
            key, separator, value = line.partition('=')
            if not separator or not KEY.fullmatch(key) or key.startswith('COMPOSE_') or key in values:
                raise SyncError('Invalid cached secret environment file')
            values[key] = value
        env.update(values)
        for key in entry.get('required', []):
            if key not in entry.get('file_only', []) and not values.get(key):
                raise SyncError(f'Required cached secret {key} is missing or empty')
    for name in entry.get('files', {}).values():
        path = destination(root, name)
        if not path.is_file() or not path.stat().st_size or path.stat().st_mode & 0o077:
            raise SyncError('A required cached secret file is missing, empty, or not private')
    return env


def bootstrap(root):
    path = destination(root, 'dell/infisical/.env')
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {'ENCRYPTION_KEY': secrets.token_hex(16),
              'AUTH_SECRET': base64.b64encode(secrets.token_bytes(32)).decode(),
              'INFISICAL_DB_PASSWORD': secrets.token_hex(32)}
    # Exclusive creation: losing/replacing the encryption key makes existing data unreadable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    print('Created private Dell Infisical bootstrap file; back it up separately before deployment')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--config', type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('bootstrap')
    for name in ('sync', 'compose'):
        command = commands.add_parser(name)
        command.add_argument('--host', required=True)
        command.add_argument('--stack', required=name == 'compose')
        if name == 'compose':
            command.add_argument('--cached', action='store_true', help='Explicit offline operation using previously synchronized files')
            command.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.action == 'bootstrap':
            bootstrap(root)
            return 0
        manifest = json.loads((root / 'shared/infisical/deployments.json').read_text())
        config_path = args.config or Path(f'/etc/homelab/infisical-{args.host}.json')
        entries = manifest.get(args.host, {})
        if not entries:
            raise SyncError('Unknown host identity')
        if args.stack:
            if args.stack not in entries:
                raise SyncError('Unknown stack for this host identity')
            selected = {}
            def include(name):
                if name in selected:
                    return
                if name not in entries:
                    raise SyncError('Unknown stack dependency in manifest')
                selected[name] = entries[name]
                for dependency in entries[name].get('dependencies', []):
                    include(dependency)
            include(args.stack)
            entries = selected
        if not getattr(args, 'cached', False):
            config = json.loads(config_path.read_text())
            if config.get('host') != args.host:
                raise SyncError('Machine identity does not match the requested host')
            sync(root, config, entries)
        if args.action == 'compose':
            entry = entries[args.stack]
            if not entry.get('compose'):
                raise SyncError('This entry only synchronizes files; it is not a Compose stack')
            arguments = args.arguments
            if arguments[:1] == ['--']:
                arguments = arguments[1:]
            if not arguments or ('config' in arguments and '--quiet' not in arguments and '-q' not in arguments):
                raise SyncError('Supply a Compose command; use config --quiet to avoid disclosing secrets')
            directory = destination(root, entry['directory'])
            command = ['docker', 'compose']
            for file in entry['compose']:
                command.extend(['-f', file])
            for related in entries.values():
                cached_environment(root, related)
            return subprocess.run(command + arguments, cwd=directory,
                                  env=cached_environment(root, entry), check=False).returncode
        return 0
    except SyncError as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('Error: configuration, local file access, or deployment failed; check private config and permissions', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
