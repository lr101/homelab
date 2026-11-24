#!/usr/bin/env python3
"""Decrypt SOPS/age deployment secrets into private files; see docs/SECRETS.md."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


class SyncError(Exception):
    """An actionable error that contains no secret values."""


def decrypt(root, entry):
    source = destination(root, entry['encrypted_file'])
    result = subprocess.run(['sops', 'decrypt', '--output-type', 'json', str(source)],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise SyncError('SOPS decryption failed; local files unchanged (check the age key and encrypted file)')
    try:
        values = json.loads(result.stdout)
    except ValueError:
        raise SyncError('SOPS plaintext must be a JSON object of string values') from None
    if not isinstance(values, dict) or not values:
        raise SyncError('SOPS plaintext must be a nonempty JSON object')
    for key, value in values.items():
        if not KEY.fullmatch(key) or key.startswith('COMPOSE_') or not isinstance(value, str):
            raise SyncError('SOPS plaintext has an unsupported key or non-string value')
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
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.sops-', delete=False) as output:
            temporary = Path(output.name)
            output.write(content.encode())
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def sync(root, entries):
    pending = {}
    for entry in entries.values():
        values = decrypt(root, entry)
        for key in set(entry.get('required', [])) | set(entry.get('files', {})):
            if not values.get(key):
                raise SyncError(f'Required secret {key} is missing or empty in {entry["encrypted_file"]}')
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
    # Decrypt and validate every selected file before modifying any local files.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('sync', 'compose', 'template'):
        command = commands.add_parser(name)
        command.add_argument('--host', required=True)
        command.add_argument('--stack', required=name in ('compose', 'template'))
        if name == 'compose':
            command.add_argument('--cached', action='store_true', help='Explicit offline operation using previously synchronized files')
            command.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        manifest = json.loads((root / 'shared/secrets/deployments.json').read_text())
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
        if args.action == 'template':
            entry = entries[args.stack]
            keys = set(entry.get('required', [])) | set(entry.get('files', {}))
            print(json.dumps({key: '' for key in sorted(keys)}, indent=2))
            return 0
        if not getattr(args, 'cached', False):
            sync(root, entries)
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
