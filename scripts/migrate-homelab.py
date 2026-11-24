#!/usr/bin/env python3
"""Inspect existing deployments and encrypt legacy configuration without changing it.

Imports and host captures default to dry-run. This tool never deploys, moves data,
rotates credentials, edits firewall rules, or stops containers. See docs/MIGRATION.md.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


class MigrationError(Exception):
    pass


def inside(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root):
        raise MigrationError('A configured path escapes its source/destination root')
    return path


def exclusive_write(path, content):
    """Publish privately and atomically, refusing existing files even on a race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.migration-', delete=False) as output:
            temporary = Path(output.name)
            output.write(content.encode())
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def read_literal(path):
    with path.open('r', encoding='utf-8', newline='') as source:
        return source.read()


def parse_dotenv_text(text):
    """Parse a conservative dotenv subset without running shell or interpolation."""
    values = {}
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise MigrationError(f'Unsupported dotenv syntax at line {binding.original.line}')
        if binding.key is None:
            continue
        if not KEY.fullmatch(binding.key) or binding.value is None:
            raise MigrationError(f'Unsupported dotenv key/value at line {binding.original.line}')
        if binding.key in values:
            raise MigrationError(f'Duplicate dotenv key {binding.key}')
        # Quoted literals are safe; expansion semantics differ between dotenv tools.
        # Reject ambiguous non-single-quoted dollar syntax rather than guessing values.
        assignment = binding.original.string.strip()
        if assignment.startswith('export '):
            assignment = assignment[7:].lstrip()
        raw = assignment.split('=', 1)[1].lstrip()
        if not raw.startswith("'") and '$' in binding.value:
            raise MigrationError(f'Potential dotenv interpolation in {binding.key}; provide a reviewed single-quoted literal')
        if raw.startswith('"') and re.search(r'\\[abfv]', raw):
            raise MigrationError(f'Unsupported dotenv escape in {binding.key}; review the literal value')
        values[binding.key] = binding.value
    return values


def read_dotenv(path):
    return parse_dotenv_text(path.read_text())


def import_values(source, entry):
    migration = entry.get('migration', {})
    values = {}
    for name in migration.get('env_files', []):
        incoming = read_dotenv(inside(source, name))
        if values.keys() & incoming.keys():
            raise MigrationError('Duplicate keys across migration source files; resolve them explicitly')
        values.update(incoming)
    for key, name in migration.get('file_values', {}).items():
        if key in values:
            raise MigrationError(f'Duplicate file/environment key {key}')
        values[key] = read_literal(inside(source, name))
    for target, aliases in migration.get('aliases', {}).items():
        if target not in values:
            found = [values[key] for key in aliases if values.get(key)]
            if len(set(found)) > 1:
                raise MigrationError(f'Ambiguous legacy aliases for {target}')
            if found:
                values[target] = found[0]
    for key in set(entry.get('required', [])) | set(entry.get('files', {})):
        if not values.get(key):
            raise MigrationError(f'Missing required source value {key}')
    if not values:
        raise MigrationError('No migration source values were found')
    for key, value in values.items():
        if not KEY.fullmatch(key) or key.startswith('COMPOSE_'):
            raise MigrationError('Unsupported variable name; keep Compose project settings in the deployment plan')
        if entry.get('env_file') and key not in entry.get('file_only', []) and any(c in value for c in '\r\n\0'):
            raise MigrationError(f'Multiline value {key} needs an explicit file mapping before import')
    return values


def encrypt(root, relative, values, apply):
    target = inside(root, relative)
    if target.exists() or target.is_symlink():
        raise MigrationError('Encrypted destination already exists; edit it explicitly with SOPS instead')
    if not apply:
        print(f'DRY RUN: validated {len(values)} values for {relative}; no files changed')
        return
    result = subprocess.run(['sops', 'encrypt', '--input-type', 'json', '--output-type', 'json',
                             '--filename-override', relative, '/dev/stdin'],
                            input=json.dumps(values), capture_output=True, text=True, cwd=root, check=False)
    if result.returncode:
        raise MigrationError('Encryption failed; check SOPS recipients/configuration (no output published)')
    encrypted = json.loads(result.stdout)
    if not isinstance(encrypted.get('sops'), dict) or set(encrypted) - {'sops'} != set(values):
        raise MigrationError('Encryption output is missing expected SOPS metadata/keys')
    if any(not isinstance(encrypted[key], str) or (encrypted[key] != '' and not encrypted[key].startswith('ENC[')) for key in values):
        raise MigrationError('Encryption output contains a plaintext value; refusing publication')
    exclusive_write(target, result.stdout)
    print(f'Created encrypted file {relative}; source files were left unchanged')


HOST_FILES = ('etc/wireguard/*.conf', 'etc/docker/daemon.json', 'etc/sysctl.conf',
              'etc/sysctl.d/*.conf', 'etc/systemd/resolved.conf', 'etc/systemd/resolved.conf.d/*.conf',
              'etc/NetworkManager/conf.d/*.conf', 'etc/netplan/*.yaml', 'etc/network/interfaces',
              'etc/ufw/user.rules', 'etc/ufw/user6.rules', 'etc/nftables.conf')


def capture_host(system):
    values = {}
    paths = {}
    for pattern in HOST_FILES:
        for path in sorted(system.glob(pattern)):
            if not path.is_file():
                continue
            relative = str(path.relative_to(system))
            inside(system, relative)
            key = 'FILE_' + re.sub(r'[^A-Za-z0-9]', '_', relative).upper()
            if key in values:
                raise MigrationError('Host configuration names collide; capture the conflicting files separately')
            values[key] = read_literal(path)
            paths[key] = '/' + relative
    if not values:
        raise MigrationError('No supported host configuration files found')
    values['SOURCE_PATHS_JSON'] = json.dumps(paths, sort_keys=True)
    return values


DOCKER_FORMAT = '{"name":{{json .Name}},"image":{{json .Image}},"configured_image":{{json .Config.Image}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},"config_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},"mounts":{{json .Mounts}},"port_bindings":{{json .HostConfig.PortBindings}},"networks":{{json .NetworkSettings.Networks}},"running":{{json .State.Running}}}'


def audit(root, source, host, output):
    if output.resolve().is_relative_to(root) or output.resolve().is_relative_to(source):
        raise MigrationError('Audit report must be outside repository/source trees')
    if not source.is_dir():
        raise MigrationError('Source root must be an existing local directory on the audited host')
    report = {'host': host, 'executing_hostname': socket.gethostname(), 'source_root': str(source), 'containers': [], 'network': {}, 'errors': []}
    try:
        ids = subprocess.run(['docker','ps','-aq'],capture_output=True,text=True,check=True).stdout.split()
        for identifier in ids:
            response = subprocess.run(['docker','inspect','--format',DOCKER_FORMAT,identifier],capture_output=True,text=True,check=True)
            item = json.loads(response.stdout)
            # Defense in depth: never persist arbitrary Docker environment/labels/config.
            report['containers'].append({key:item.get(key) for key in ('name','image','configured_image','project','service','working_dir','config_files','mounts','port_bindings','networks','running')})
    except (OSError, subprocess.CalledProcessError, ValueError):
        report['errors'].append('Docker inventory incomplete; rerun with appropriate daemon access')
    for name, command in {'addresses':['ip','-json','address'], 'routes':['ip','-json','route','show','table','all'],
                          'rules':['ip','-json','rule'], 'wireguard_interfaces':['wg','show','interfaces'],
                          'ipv4_forwarding':['sysctl','-n','net.ipv4.ip_forward'], 'ipv6_forwarding':['sysctl','-n','net.ipv6.conf.all.forwarding']}.items():
        try:
            response = subprocess.run(command,capture_output=True,text=True,check=True)
            report['network'][name] = response.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            report['errors'].append(f'{name} not captured; inspect manually')
    exclusive_write(output, json.dumps(report,indent=2)+'\n')
    print(f'Created private audit report ({len(report["containers"])} containers, {len(report["errors"])} incomplete checks); no host changes made')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    sub=parser.add_subparsers(dest='action',required=True)
    for name in ('import-secrets','capture-host','audit'):
        command=sub.add_parser(name)
        command.add_argument('--host',required=True)
        if name=='import-secrets':
            command.add_argument('--stack',required=True)
        if name in ('import-secrets','audit'):
            command.add_argument('--source-root',type=Path,required=True)
        if name=='capture-host':
            command.add_argument('--system-root',type=Path,default=Path('/'))
            command.add_argument('--runtime-firewall',choices=('iptables','nftables'))
        if name=='audit':
            command.add_argument('--output',type=Path,required=True)
        else:
            command.add_argument('--apply',action='store_true')
    args=parser.parse_args();root=args.root.resolve()
    try:
        manifest=json.loads((root/'shared/secrets/deployments.json').read_text())
        if args.host not in manifest:
            raise MigrationError('Unknown host')
        if args.action=='audit':
            audit(root,args.source_root.resolve(),args.host,args.output)
        elif args.action=='capture-host':
            values = capture_host(args.system_root.resolve())
            if args.runtime_firewall:
                commands = {'LIVE_IPTABLES_SAVE':['iptables-save'], 'LIVE_IP6TABLES_SAVE':['ip6tables-save']} if args.runtime_firewall == 'iptables' else {'LIVE_NFT_RULESET':['nft','list','ruleset']}
                for key, command in commands.items():
                    response = subprocess.run(command,capture_output=True,text=True,check=False)
                    if response.returncode:
                        raise MigrationError('Runtime firewall capture failed; no encrypted output published')
                    values[key] = response.stdout
            encrypt(root,f'secrets/{args.host}/host-config.enc.json',values,args.apply)
        else:
            if args.stack not in manifest[args.host]:
                raise MigrationError('Unknown stack')
            entry=manifest[args.host][args.stack]
            encrypt(root,entry['encrypted_file'],import_values(args.source_root.resolve(),entry),args.apply)
        return 0
    except MigrationError as error:
        print(f'Error: {error}',file=sys.stderr)
    except (OSError,ValueError,KeyError,TypeError):
        print('Error: migration input, permissions, encryption, or destination invalid; no sources were modified',file=sys.stderr)
    return 1


if __name__=='__main__':
    sys.exit(main())
