#!/usr/bin/env python3
"""Controller-side SSH migration tool for inspecting, encrypting, adopting, and rolling back stacks.

Runs on an operator workstation/controller with Git checkout, Python, SOPS/age, and SSH access.
Remote hosts need only Python 3 + Docker/Compose (plus optional passwordless sudo for host capture).
No Git checkout, SOPS binary, private age keys, or Python packages are installed on remote hosts.
See docs/MIGRATION.md.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import subprocess
import sys
import zlib

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
REMOTE_AGENT_PATH = SCRIPTS_DIR / 'ssh_remote_agent.py'
OVERRIDE_FILENAME = '.homelab-adopt.override.yml'
SSH_TARGET_RE = re.compile(r'[A-Za-z0-9._@:\[\]-]+\Z')
VAR_EXPR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?(?::\?[^}]*|\?[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)')


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location('migrate_homelab', SCRIPTS_DIR / 'migrate-homelab.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('Unable to load scripts/migrate-homelab.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MIGRATE = _load_migrate_module()
HOST_FILES = MIGRATE.HOST_FILES
KEY = MIGRATE.KEY


class SshMigrationError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')


def redact(text: str, secrets: set[str]) -> str:
    if not text or not secrets:
        return text
    out = text
    for val in sorted((s for s in secrets if s and len(s) >= 3), key=len, reverse=True):
        out = out.replace(val, '[REDACTED]')
    return out


def validate_ssh_target(target: str) -> str:
    if not target or target.startswith('-') or not SSH_TARGET_RE.fullmatch(target):
        raise SshMigrationError('Invalid SSH target; must not start with "-" or contain whitespace/shell characters')
    return target


def validate_remote_path(path_str: str, label: str = 'Remote path') -> str:
    if not path_str or any(ord(ch) < 32 or ord(ch) == 127 for ch in path_str):
        raise SshMigrationError(f'{label} contains invalid control characters')
    return path_str


def inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise SshMigrationError(f'Configured path escapes root: {relative}')
    return path


def ensure_outside_repo(root: Path, target: Path, label: str) -> Path:
    resolved = target.resolve()
    if resolved.is_relative_to(root.resolve()):
        raise SshMigrationError(f'{label} must be placed outside the repository tree')
    return resolved


def exclusive_write_private(path: Path, content: str) -> None:
    try:
        MIGRATE.exclusive_write(path, content)
    except FileExistsError as exc:
        raise SshMigrationError(f'Refusing to overwrite existing file: {path}') from exc


def build_remote_command(use_sudo: bool = False) -> str:
    agent_bytes = REMOTE_AGENT_PATH.read_bytes()
    payload = base64.b64encode(zlib.compress(agent_bytes, level=9)).decode('ascii')
    bootstrap = 'import base64,zlib,sys;exec(zlib.decompress(base64.b64decode(sys.argv[1])))'
    cmd = f"python3 -c {shlex.quote(bootstrap)} {payload}"
    if use_sudo:
        return f'sudo -n -- {cmd}'
    return cmd


class SshClient:
    def __init__(
        self,
        target: str,
        ssh_bin: str = 'ssh',
        ssh_config: str | None = None,
        ssh_identity: str | None = None,
        ssh_port: int | None = None,
        connect_timeout: int = 15,
    ):
        self.target = validate_ssh_target(target)
        self.ssh_bin = ssh_bin
        self.ssh_config = ssh_config
        self.ssh_identity = ssh_identity
        self.ssh_port = ssh_port
        self.connect_timeout = connect_timeout

    def call(self, request: dict, use_sudo: bool = False, secrets: set[str] | None = None) -> dict:
        secret_set = secrets or set()
        cmd = [
            self.ssh_bin,
            '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'PasswordAuthentication=no',
            '-o', f'ConnectTimeout={self.connect_timeout}',
        ]
        if self.ssh_config:
            cmd.extend(['-F', self.ssh_config])
        if self.ssh_identity:
            cmd.extend(['-i', self.ssh_identity])
        if self.ssh_port:
            cmd.extend(['-p', str(self.ssh_port)])
        cmd.extend(['--', self.target, build_remote_command(use_sudo=use_sudo)])

        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise SshMigrationError(f'Failed executing SSH client ({self.ssh_bin}): {exc.strerror or exc}') from exc

        if proc.returncode != 0:
            err = redact((proc.stderr or proc.stdout or '').strip(), secret_set)
            hint = ' (check passwordless sudo configuration on remote host)' if use_sudo else ''
            raise SshMigrationError(f'SSH remote execution failed (exit {proc.returncode}){hint}: {err or "no output"}')

        try:
            response = json.loads(proc.stdout)
        except ValueError as exc:
            raise SshMigrationError('Remote SSH helper returned non-JSON output; verify remote python3 and login shell banners') from exc

        if not isinstance(response, dict):
            raise SshMigrationError('Remote SSH helper returned unexpected response shape')
        if not response.get('ok'):
            err = redact(str(response.get('error', 'unknown remote error')), secret_set)
            raise SshMigrationError(err)
        return response


def load_manifest(root: Path) -> tuple[dict, str]:
    manifest_path = root / 'shared/secrets/deployments.json'
    raw = manifest_path.read_bytes()
    return json.loads(raw.decode('utf-8')), sha256_bytes(raw)


def discover_host_stacks(root: Path, manifest: dict, host: str) -> dict[str, dict]:
    if host not in manifest:
        raise SshMigrationError(f'Unknown host "{host}" in shared/secrets/deployments.json')
    entries = manifest[host]
    catalog: dict[str, dict] = {}
    managed_dirs: set[str] = set()
    for name, entry in entries.items():
        managed_dirs.add(entry['directory'])
        kind = 'managed' if entry.get('compose') else 'dependency'
        catalog[name] = {**entry, 'name': name, 'kind': kind}

    host_dir = root / host
    if host_dir.is_dir():
        for child in sorted(host_dir.iterdir()):
            if not child.is_dir():
                continue
            rel_dir = f'{host}/{child.name}'
            if rel_dir in managed_dirs:
                continue
            compose_files = [
                fname for fname in ('docker-compose.yml', 'docker-compose.yaml')
                if (child / fname).is_file()
            ]
            if compose_files:
                stack_name = child.name
                if stack_name in catalog:
                    continue
                catalog[stack_name] = {
                    'name': stack_name,
                    'kind': 'credentialless',
                    'directory': rel_dir,
                    'compose': [compose_files[0]],
                    'dependencies': [],
                    'required': [],
                    'files': {},
                    'file_only': [],
                    'encrypted_file': None,
                }
    return catalog


def expand_manifest_selection(entries: dict, requested: list[str] | None, all_stacks: bool) -> list[str]:
    if all_stacks:
        names = list(entries.keys())
    else:
        if not requested:
            raise SshMigrationError('Specify --stack <name> (repeatable) or --all-stacks')
        names = list(requested)
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise SshMigrationError(f'Cyclic stack dependency involving {name}')
        if name not in entries:
            raise SshMigrationError(f'Unknown stack "{name}" for host')
        visiting.add(name)
        for dep in entries[name].get('dependencies', []):
            visit(dep)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for n in names:
        visit(n)
    return ordered


def sops_encrypt_values(root: Path, relative: str, values: dict[str, str], apply: bool, secrets: set[str], skip_existing: bool = False) -> str:
    target = inside(root, relative)
    if target.exists() or target.is_symlink():
        if skip_existing:
            print(f'SKIPPED (already exists): {relative}')
            return 'skipped_existing'
        raise SshMigrationError(f'Encrypted destination already exists ({relative}); edit it explicitly with SOPS or pass --skip-existing')
    if not apply:
        print(f'DRY RUN: validated {len(values)} values for {relative}; no files changed')
        return 'dry_run'
    proc = subprocess.run(
        ['sops', 'encrypt', '--input-type', 'json', '--output-type', 'json', '--filename-override', relative, '/dev/stdin'],
        input=json.dumps(values),
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    if proc.returncode != 0:
        err = redact((proc.stderr or '').strip(), secrets)
        raise SshMigrationError(f'SOPS encryption failed for {relative}; check recipients/config (no output published): {err}')
    try:
        encrypted = json.loads(proc.stdout)
    except ValueError as exc:
        raise SshMigrationError(f'SOPS encryption output for {relative} is not valid JSON') from exc
    if not isinstance(encrypted.get('sops'), dict) or set(encrypted) - {'sops'} != set(values):
        raise SshMigrationError(f'SOPS encryption output for {relative} is missing expected metadata/keys')
    if any(not isinstance(encrypted[k], str) or (encrypted[k] != '' and not encrypted[k].startswith('ENC[')) for k in values):
        raise SshMigrationError(f'SOPS encryption output for {relative} contains plaintext; refusing publication')
    exclusive_write_private(target, proc.stdout)
    print(f'Created encrypted file {relative} (mode 0600); remote sources were left unchanged')
    return 'created'


def sops_decrypt_entry(root: Path, entry: dict, secrets: set[str]) -> tuple[dict[str, str], str]:
    rel = entry['encrypted_file']
    source = inside(root, rel)
    if not source.is_file():
        raise SshMigrationError(f'Encrypted secret file is missing: {rel}; run import-secrets --apply first')
    enc_sha = sha256_file(source)
    proc = subprocess.run(
        ['sops', 'decrypt', '--output-type', 'json', str(source)],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    if proc.returncode != 0:
        err = redact((proc.stderr or '').strip(), secrets)
        raise SshMigrationError(f'SOPS decryption failed for {rel}; verify SOPS_AGE_KEY_FILE/key access: {err}')
    try:
        values = json.loads(proc.stdout)
    except ValueError as exc:
        raise SshMigrationError(f'Decrypted SOPS payload for {rel} is not valid JSON') from exc
    if not isinstance(values, dict):
        raise SshMigrationError(f'Decrypted SOPS payload for {rel} must be a JSON object')
    for k, v in values.items():
        if not KEY.fullmatch(k) or k.startswith('COMPOSE_') or not isinstance(v, str):
            raise SshMigrationError(f'SOPS plaintext in {rel} has an unsupported key or non-string value')
        if v:
            secrets.add(v)
    for k in set(entry.get('required', [])) | set(entry.get('files', {})):
        if not values.get(k):
            raise SshMigrationError(f'Required secret {k} is missing or empty in {rel}')
    return values, enc_sha


def render_secret_outputs(entry: dict, values: dict[str, str]) -> dict[str, tuple[bytes, str]]:
    """Return {repo_rel_path: (raw_bytes, kind)} matching scripts/sops-secrets.py semantics."""
    outputs: dict[str, tuple[bytes, str]] = {}
    if entry.get('env_file'):
        exported = {k: v for k, v in values.items() if k not in entry.get('file_only', [])}
        if any('\n' in v or '\r' in v or '\0' in v for v in exported.values()):
            raise SshMigrationError(f'Multiline environment value in {entry["encrypted_file"]} requires file_only delivery')
        env_text = ''.join(f'{k}={v}\n' for k, v in sorted(exported.items()))
        outputs[entry['env_file']] = (env_text.encode('utf-8'), 'env_file')
    for key, target in entry.get('files', {}).items():
        outputs[target] = (values[key].encode('utf-8'), 'secret_file')
    return outputs


def interpolate_value(expr: str, env_map: dict[str, str]) -> str:
    def _replace(match: re.Match) -> str:
        name = match.group(1) or match.group(4)
        op = match.group(2)
        default = match.group(3)
        if name in env_map:
            val = env_map[name]
            if op == ':-' and val == '':
                return default or ''
            return val
        if op in (':-', '-'):
            return default or ''
        return match.group(0)
    return VAR_EXPR_RE.sub(_replace, expr)


def normalize_image_ref(ref: str) -> str:
    s = ref.strip()
    if s.startswith('docker.io/'):
        s = s[len('docker.io/'):]
    if s.startswith('library/'):
        s = s[len('library/'):]
    return s


def load_merged_compose(root: Path, entry: dict) -> tuple[dict, dict, list[str]]:
    """Load and lightly merge compose files + referenced extends files for inspection."""
    stack_dir = inside(root, entry['directory'])
    services: dict[str, dict] = {}
    volumes: dict[str, dict | None] = {}
    extra_compose_files: list[str] = []

    def merge_service(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if k == 'volumes' and isinstance(v, list):
                dst['volumes'] = list(dst.get('volumes', [])) + list(v)
            elif k == 'environment' and isinstance(v, dict) and isinstance(dst.get('environment'), dict):
                dst['environment'] = {**dst['environment'], **v}
            elif k == 'depends_on' and isinstance(v, dict) and isinstance(dst.get('depends_on'), dict):
                dst['depends_on'] = {**dst['depends_on'], **v}
            else:
                dst[k] = v

    for cf in entry.get('compose', []):
        cpath = inside(stack_dir, cf)
        data = yaml.safe_load(cpath.read_text(encoding='utf-8')) or {}
        for vname, vcfg in (data.get('volumes') or {}).items():
            volumes[vname] = vcfg
        for sname, scfg in (data.get('services') or {}).items():
            scfg = dict(scfg or {})
            ext = scfg.get('extends')
            if isinstance(ext, dict) and ext.get('file'):
                ext_rel = ext['file']
                if ext_rel not in extra_compose_files:
                    extra_compose_files.append(ext_rel)
                ext_path = inside(stack_dir, ext_rel)
                ext_data = yaml.safe_load(ext_path.read_text(encoding='utf-8')) or {}
                ext_svc = (ext_data.get('services') or {}).get(ext.get('service', sname), {})
                base = dict(ext_svc or {})
                merge_service(base, scfg)
                scfg = base
            target_svc = services.setdefault(sname, {})
            merge_service(target_svc, scfg)
    return services, volumes, extra_compose_files


def collect_staged_repo_files(root: Path, stack_entry: dict, dep_entries: list[dict], secret_output_rels: set[str]) -> list[str]:
    """Return explicitly allowlisted repo-relative files to stage for a stack (never data dirs!)."""
    stack_dir = inside(root, stack_entry['directory'])
    services, _, extra_compose_files = load_merged_compose(root, stack_entry)
    rel_files: set[str] = set()

    def add_rel(path_in_repo: Path) -> None:
        if not path_in_repo.is_file():
            return
        rel = str(path_in_repo.relative_to(root.resolve()))
        if rel in secret_output_rels:
            return
        if Path(rel).name in ('.env', '.secrets.env') or '.secrets' in Path(rel).parts:
            return
        rel_files.add(rel)

    for cf in list(stack_entry.get('compose', [])) + extra_compose_files:
        add_rel(inside(stack_dir, cf))

    # Inspect relative bind mounts and seccomp files referenced by Compose
    candidate_rel_refs: list[str] = []
    for scfg in services.values():
        for vol in scfg.get('volumes') or []:
            src = vol.split(':', 1)[0] if isinstance(vol, str) else (vol.get('source') or '')
            if src.startswith('./'):
                candidate_rel_refs.append(src[2:])
        for opt in scfg.get('security_opt') or []:
            if isinstance(opt, str) and opt.startswith('seccomp=./'):
                candidate_rel_refs.append(opt[len('seccomp=./'):])

    for ref in candidate_rel_refs:
        if ref.startswith('.secrets') or ref in ('.env', '.secrets.env'):
            continue
        candidate = stack_dir / ref
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root.resolve()):
            continue
        if resolved.is_file():
            add_rel(resolved)
        elif resolved.is_dir():
            for item in sorted(resolved.rglob('*')):
                if item.is_file():
                    add_rel(item)

    # For backup-credentials dependency, also stage tracked <host>/.autorestic.yml if present
    for dep in dep_entries:
        dep_dir = inside(root, dep['directory'])
        autorestic = dep_dir / '.autorestic.yml'
        if autorestic.is_file():
            add_rel(autorestic)

    return sorted(rel_files)


def parse_compose_mounts(scfg: dict, declared_volumes: set[str], remote_stack_dir: str, remote_home: str, env_map: dict[str, str]) -> list[dict]:
    parsed = []
    for raw in scfg.get('volumes') or []:
        if isinstance(raw, str):
            parts = raw.split(':')
            if len(parts) < 2:
                continue
            raw_src, raw_dst = parts[0], parts[1]
            src_interp = interpolate_value(raw_src, env_map)
            dst_interp = interpolate_value(raw_dst, env_map)
            if src_interp in declared_volumes and not src_interp.startswith(('/', './', '../', '~')):
                parsed.append({'type': 'volume', 'volume_key': src_interp, 'destination': dst_interp})
            else:
                if src_interp.startswith('~/'):
                    resolved_src = posixpath.normpath(posixpath.join(remote_home, src_interp[2:]))
                elif src_interp.startswith('/'):
                    resolved_src = posixpath.normpath(src_interp)
                else:
                    resolved_src = posixpath.normpath(posixpath.join(remote_stack_dir, src_interp))
                parsed.append({'type': 'bind', 'source': resolved_src, 'raw_source': raw_src, 'destination': dst_interp})
        elif isinstance(raw, dict):
            mtype = raw.get('type', 'bind')
            raw_src = str(raw.get('source') or '')
            raw_dst = str(raw.get('target') or '')
            src_interp = interpolate_value(raw_src, env_map)
            dst_interp = interpolate_value(raw_dst, env_map)
            if mtype == 'volume':
                parsed.append({'type': 'volume', 'volume_key': src_interp, 'destination': dst_interp})
            elif mtype == 'bind':
                if src_interp.startswith('~/'):
                    resolved_src = posixpath.normpath(posixpath.join(remote_home, src_interp[2:]))
                elif src_interp.startswith('/'):
                    resolved_src = posixpath.normpath(src_interp)
                else:
                    resolved_src = posixpath.normpath(posixpath.join(remote_stack_dir, src_interp))
                parsed.append({'type': 'bind', 'source': resolved_src, 'raw_source': raw_src, 'destination': dst_interp})
    return parsed


def detect_oneshot_services(services: dict[str, dict]) -> list[str]:
    flagged: set[str] = set()
    for sname, scfg in services.items():
        restart = scfg.get('restart')
        if restart is False or str(restart).strip().lower() == 'no':
            flagged.add(sname)
        deps = scfg.get('depends_on')
        if isinstance(deps, dict):
            for dep_name, dep_cfg in deps.items():
                if isinstance(dep_cfg, dict) and dep_cfg.get('condition') == 'service_completed_successfully':
                    flagged.add(dep_name)
    return sorted(flagged)


def compute_remote_fingerprint(containers: list[dict], pinned_volumes: dict[str, str], remote_file_hashes: dict[str, str | None]) -> str:
    norm_containers = sorted(
        [
            {
                'name': c.get('name'),
                'project': c.get('project'),
                'service': c.get('service'),
                'image': c.get('image'),
                'configured_image': c.get('configured_image'),
                'working_dir': c.get('working_dir'),
                'mounts': sorted(
                    [
                        {
                            'Type': m.get('Type'),
                            'Name': m.get('Name'),
                            'Source': m.get('Source'),
                            'Destination': m.get('Destination'),
                        }
                        for m in (c.get('mounts') or [])
                    ],
                    key=lambda x: (str(x.get('Destination')), str(x.get('Source'))),
                ),
                'running': c.get('running'),
            }
            for c in containers
        ],
        key=lambda x: (str(x.get('project')), str(x.get('service')), str(x.get('name'))),
    )
    payload = {
        'containers': norm_containers,
        'pinned_volumes': pinned_volumes,
        'remote_file_hashes': remote_file_hashes,
    }
    return sha256_bytes(canonical_json_bytes(payload))


def git_repo_revision(root: Path) -> tuple[str | None, bool | None]:
    if not (root / '.git').exists():
        return None, None
    head = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=False)
    status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], capture_output=True, text=True, check=False)
    if head.returncode != 0:
        return None, None
    return head.stdout.strip(), bool(status.stdout.strip())


def compute_plan_digest(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != 'plan_digest'}
    return sha256_bytes(canonical_json_bytes(body))


def cmd_audit(root: Path, manifest: dict, args: argparse.Namespace) -> int:
    if args.host not in manifest:
        raise SshMigrationError(f'Unknown host "{args.host}"')
    output = ensure_outside_repo(root, args.output, 'Audit report')
    probe_paths: set[str] = set()
    for entry in manifest[args.host].values():
        mig = entry.get('migration', {})
        probe_paths.update(mig.get('env_files', []))
        probe_paths.update(mig.get('file_values', {}).values())
    ssh = SshClient(args.ssh_target, args.ssh_bin, args.ssh_config, args.ssh_identity, args.ssh_port, args.connect_timeout)
    resp = ssh.call({
        'action': 'audit',
        'source_root': validate_remote_path(args.source_root, 'Source root'),
        'probe_paths': sorted(probe_paths),
        'docker_sudo': args.docker_sudo,
    })
    report = {
        'host': args.host,
        'ssh_target': args.ssh_target,
        'executing_hostname': resp.get('hostname'),
        'source_root': resp.get('source_root_realpath'),
        'compose_version': resp.get('compose_version'),
        'containers': resp.get('containers', []),
        'volumes': resp.get('volumes', {}),
        'networks': resp.get('networks', []),
        'network': resp.get('network', {}),
        'probed_files': resp.get('probed_files', {}),
        'errors': resp.get('errors', []),
    }
    exclusive_write_private(output, json.dumps(report, indent=2) + '\n')
    print(
        f'Created private SSH audit report {output} '
        f'({len(report["containers"])} containers, {len(report["volumes"])} volumes, {len(report["errors"])} incomplete checks); '
        'no remote changes made'
    )
    return 0


def cmd_capture_host(root: Path, manifest: dict, args: argparse.Namespace) -> int:
    if args.host not in manifest:
        raise SshMigrationError(f'Unknown host "{args.host}"')
    ssh = SshClient(args.ssh_target, args.ssh_bin, args.ssh_config, args.ssh_identity, args.ssh_port, args.connect_timeout)
    resp = ssh.call(
        {
            'action': 'capture_host',
            'system_root': validate_remote_path(args.system_root, 'System root'),
            'patterns': list(HOST_FILES),
            'runtime_firewall': args.runtime_firewall,
        },
        use_sudo=args.sudo,
    )
    unreadable = resp.get('unreadable', [])
    if unreadable:
        joined = ', '.join(unreadable)
        if not args.skip_unreadable:
            raise SshMigrationError(
                f'Permission denied reading root-only host file(s): {joined}. '
                'Rerun with --sudo (requires passwordless sudo) or opt out explicitly with --skip-unreadable.'
            )
        print(f'WARNING: skipping unreadable host file(s) due to --skip-unreadable: {joined}', file=sys.stderr)

    values: dict[str, str] = {}
    paths: dict[str, str] = {}
    secrets: set[str] = set()
    for rel, b64 in sorted(resp.get('files', {}).items()):
        key = 'FILE_' + re.sub(r'[^A-Za-z0-9]', '_', rel).upper()
        if key in values:
            raise SshMigrationError(f'Host configuration names collide ({key}); capture conflicting files separately')
        try:
            text = base64.b64decode(b64).decode('utf-8')
        except UnicodeDecodeError as exc:
            raise SshMigrationError(f'Host config file /{rel} is not valid UTF-8') from exc
        values[key] = text
        paths[key] = '/' + rel
        if text:
            secrets.add(text)
    if not values:
        raise SshMigrationError('No supported host configuration files found on remote host')
    values['SOURCE_PATHS_JSON'] = json.dumps(paths, sort_keys=True)
    if unreadable:
        values['SKIPPED_UNREADABLE_PATHS_JSON'] = json.dumps(unreadable)

    for key, b64 in sorted(resp.get('runtime_firewall_outputs', {}).items()):
        text = base64.b64decode(b64).decode('utf-8')
        values[key] = text
        if text:
            secrets.add(text)

    sops_encrypt_values(
        root,
        f'secrets/{args.host}/host-config.enc.json',
        values,
        args.apply,
        secrets,
        skip_existing=args.skip_existing,
    )
    return 0


def cmd_import_secrets(root: Path, manifest: dict, args: argparse.Namespace) -> int:
    if args.host not in manifest:
        raise SshMigrationError(f'Unknown host "{args.host}"')
    entries = manifest[args.host]
    selected = expand_manifest_selection(entries, args.stack, args.all_stacks)
    ssh = SshClient(args.ssh_target, args.ssh_bin, args.ssh_config, args.ssh_identity, args.ssh_port, args.connect_timeout)

    deployed_dirs: set[str] = set()
    source_root_real = args.source_root
    if args.all_stacks and args.skip_not_deployed:
        audit_resp = ssh.call({
            'action': 'audit',
            'source_root': validate_remote_path(args.source_root, 'Source root'),
            'probe_paths': [],
            'docker_sudo': args.docker_sudo,
        })
        source_root_real = audit_resp.get('source_root_realpath', args.source_root)
        for c in audit_resp.get('containers', []):
            wdir = c.get('working_dir')
            if wdir:
                deployed_dirs.add(posixpath.normpath(wdir))

    needed_paths: set[str] = set()
    for name in selected:
        mig = entries[name].get('migration', {})
        needed_paths.update(mig.get('env_files', []))
        needed_paths.update(mig.get('file_values', {}).values())

    read_resp = ssh.call({
        'action': 'read_files',
        'source_root': validate_remote_path(args.source_root, 'Source root'),
        'paths': sorted(needed_paths),
    })
    remote_files = read_resp.get('files', {})
    secrets: set[str] = set()
    prepared: list[tuple[str, dict, dict[str, str]]] = []
    skipped_not_deployed: list[str] = []

    for name in selected:
        entry = entries[name]
        mig = entry.get('migration', {})
        env_files = mig.get('env_files', [])
        file_values = mig.get('file_values', {})
        all_sources = list(env_files) + list(file_values.values())

        statuses = {p: remote_files.get(p, {'status': 'missing'}) for p in all_sources}
        for p, info in statuses.items():
            if info['status'] == 'escape_error':
                raise SshMigrationError(f'Legacy source path escapes source root: {p}')
            if info['status'] == 'permission_denied':
                raise SshMigrationError(f'Permission denied reading legacy source {p} on remote host; check SSH user permissions')
            if info['status'] == 'not_a_file':
                raise SshMigrationError(f'Legacy source path is not a regular file: {p}')

        missing = [p for p, info in statuses.items() if info['status'] == 'missing']
        if missing:
            stack_remote_dir = posixpath.normpath(posixpath.join(source_root_real, entry['directory']))
            has_containers = stack_remote_dir in deployed_dirs
            if args.all_stacks and args.skip_not_deployed and len(missing) == len(all_sources) and not has_containers:
                skipped_not_deployed.append(name)
                print(f'SKIPPED (not deployed): {args.host}/{name}')
                continue
            raise SshMigrationError(
                f'Missing migration source file(s) for {args.host}/{name}: {", ".join(missing)}'
            )

        values: dict[str, str] = {}
        for rel in env_files:
            raw_bytes = base64.b64decode(statuses[rel]['b64'])
            try:
                text = raw_bytes.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise SshMigrationError(f'Legacy dotenv file {rel} is not valid UTF-8') from exc
            try:
                incoming = MIGRATE.parse_dotenv_text(text)
            except MIGRATE.MigrationError as exc:
                raise SshMigrationError(f'{rel}: {exc}') from exc
            if values.keys() & incoming.keys():
                raise SshMigrationError(f'Duplicate keys across migration source files for {name}')
            values.update(incoming)

        for key, rel in file_values.items():
            if key in values:
                raise SshMigrationError(f'Duplicate file/environment key {key} in {name}')
            raw_bytes = base64.b64decode(statuses[rel]['b64'])
            try:
                values[key] = raw_bytes.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise SshMigrationError(f'Legacy file input {rel} is not valid UTF-8') from exc

        for target_key, aliases in mig.get('aliases', {}).items():
            if target_key not in values:
                found = [values[k] for k in aliases if values.get(k)]
                if len(set(found)) > 1:
                    raise SshMigrationError(f'Ambiguous legacy aliases for {target_key} in {name}')
                if found:
                    values[target_key] = found[0]

        for k in set(entry.get('required', [])) | set(entry.get('files', {})):
            if not values.get(k):
                raise SshMigrationError(f'Missing required source value {k} for {args.host}/{name}')
        if not values:
            raise SshMigrationError(f'No migration source values found for {args.host}/{name}')
        for k, v in values.items():
            if v:
                secrets.add(v)
            if not KEY.fullmatch(k) or k.startswith('COMPOSE_'):
                raise SshMigrationError(f'Unsupported variable name in {name}; keep Compose project settings in the plan')
            if entry.get('env_file') and k not in entry.get('file_only', []) and any(c in v for c in '\r\n\0'):
                raise SshMigrationError(f'Multiline value {k} in {name} requires file_only delivery')

        prepared.append((name, entry, values))

    # Validate everything before encrypting/publishing any files
    for _, entry, values in prepared:
        sops_encrypt_values(
            root,
            entry['encrypted_file'],
            values,
            args.apply,
            secrets,
            skip_existing=args.skip_existing,
        )
    return 0


def cmd_prepare(root: Path, manifest: dict, args: argparse.Namespace) -> int:
    if args.audit_output:
        audit_out = ensure_outside_repo(root, args.audit_output, 'Audit report')
        if args.apply:
            audit_args = argparse.Namespace(
                host=args.host,
                ssh_target=args.ssh_target,
                source_root=args.source_root,
                output=audit_out,
                docker_sudo=args.docker_sudo,
                ssh_bin=args.ssh_bin,
                ssh_config=args.ssh_config,
                ssh_identity=args.ssh_identity,
                ssh_port=args.ssh_port,
                connect_timeout=args.connect_timeout,
            )
            cmd_audit(root, manifest, audit_args)
        else:
            print(f'DRY RUN: would write private SSH audit report to {audit_out}')

    if not args.skip_host_config:
        cap_args = argparse.Namespace(
            host=args.host,
            ssh_target=args.ssh_target,
            system_root=args.system_root,
            sudo=args.sudo,
            runtime_firewall=args.runtime_firewall,
            skip_unreadable=args.skip_unreadable,
            skip_existing=args.skip_existing,
            apply=args.apply,
            ssh_bin=args.ssh_bin,
            ssh_config=args.ssh_config,
            ssh_identity=args.ssh_identity,
            ssh_port=args.ssh_port,
            connect_timeout=args.connect_timeout,
        )
        cmd_capture_host(root, manifest, cap_args)

    imp_args = argparse.Namespace(
        host=args.host,
        ssh_target=args.ssh_target,
        source_root=args.source_root,
        stack=args.stack,
        all_stacks=args.all_stacks,
        skip_not_deployed=args.skip_not_deployed,
        skip_existing=args.skip_existing,
        docker_sudo=args.docker_sudo,
        apply=args.apply,
        ssh_bin=args.ssh_bin,
        ssh_config=args.ssh_config,
        ssh_identity=args.ssh_identity,
        ssh_port=args.ssh_port,
        connect_timeout=args.connect_timeout,
    )
    cmd_import_secrets(root, manifest, imp_args)
    return 0


def cmd_plan(root: Path, manifest: dict, manifest_sha: str, args: argparse.Namespace) -> int:
    plan_path = ensure_outside_repo(root, args.plan, 'Adoption plan')
    catalog = discover_host_stacks(root, manifest, args.host)

    if args.all_stacks:
        selected_names = [name for name, item in catalog.items() if item['kind'] in ('managed', 'credentialless')]
    else:
        if not args.stack:
            raise SshMigrationError('Specify --stack <name> (repeatable) or --all-stacks')
        selected_names = []
        for name in args.stack:
            if name not in catalog:
                raise SshMigrationError(f'Unknown stack "{name}" for host "{args.host}"')
            if catalog[name]['kind'] == 'dependency':
                raise SshMigrationError(f'"{name}" is a file-only dependency; select its parent Compose stack instead')
            if name not in selected_names:
                selected_names.append(name)

    secrets: set[str] = set()
    # Pre-decrypt managed stacks + dependencies on controller (in memory only)
    stack_secret_state: dict[str, dict] = {}
    probe_rel_paths: set[str] = set()

    for name in selected_names:
        entry = catalog[name]
        dep_names: list[str] = []
        if entry['kind'] == 'managed':
            all_names = expand_manifest_selection(manifest[args.host], [name], False)
            dep_names = [d for d in all_names if d != name]
        dep_entries = [manifest[args.host][d] for d in dep_names]

        secret_inputs = []
        secret_outputs: dict[str, tuple[bytes, str]] = {}
        primary_env_map: dict[str, str] = {}
        legacy_rels: set[str] = set()

        if entry['kind'] == 'managed':
            for sub_name in dep_names + [name]:
                sub_entry = manifest[args.host][sub_name]
                vals, enc_sha = sops_decrypt_entry(root, sub_entry, secrets)
                secret_inputs.append({
                    'stack': sub_name,
                    'encrypted_file': sub_entry['encrypted_file'],
                    'encrypted_sha256': enc_sha,
                })
                rendered = render_secret_outputs(sub_entry, vals)
                for rel_out, payload in rendered.items():
                    if rel_out in secret_outputs:
                        raise SshMigrationError(f'Duplicate secret output destination: {rel_out}')
                    secret_outputs[rel_out] = payload
                if sub_name == name:
                    primary_env_map = {k: v for k, v in vals.items() if k not in sub_entry.get('file_only', [])}
                mig = sub_entry.get('migration', {})
                legacy_rels.update(mig.get('env_files', []))
                legacy_rels.update(mig.get('file_values', {}).values())

        staged_repo_rels = collect_staged_repo_files(root, entry, dep_entries, set(secret_outputs.keys()))
        probe_rel_paths.update(staged_repo_rels)
        probe_rel_paths.update(secret_outputs.keys())
        probe_rel_paths.update(legacy_rels)
        probe_rel_paths.add(f'{entry["directory"]}/{OVERRIDE_FILENAME}')

        stack_secret_state[name] = {
            'entry': entry,
            'dep_names': dep_names,
            'dep_entries': dep_entries,
            'secret_inputs': secret_inputs,
            'secret_outputs': secret_outputs,
            'primary_env_map': primary_env_map,
            'staged_repo_rels': staged_repo_rels,
            'legacy_rels': sorted(legacy_rels),
        }

    ssh = SshClient(args.ssh_target, args.ssh_bin, args.ssh_config, args.ssh_identity, args.ssh_port, args.connect_timeout)
    audit_resp = ssh.call(
        {
            'action': 'audit',
            'source_root': validate_remote_path(args.source_root, 'Source root'),
            'probe_paths': sorted(probe_rel_paths),
            'docker_sudo': args.docker_sudo,
        },
        secrets=secrets,
    )
    if not audit_resp.get('compose_version'):
        raise SshMigrationError('Docker Compose v2 is not available on the remote host')

    remote_root_real = posixpath.normpath(audit_resp['source_root_realpath'])
    remote_home = posixpath.normpath(audit_resp['home_dir'])
    all_containers = audit_resp.get('containers', [])
    remote_volumes = audit_resp.get('volumes', {})
    remote_images = audit_resp.get('images', {})
    probed_files = audit_resp.get('probed_files', {})

    # Collect bind mount host paths to verify existence on remote
    proposed_mounts_by_stack: dict[str, dict[str, list[dict]]] = {}
    extra_check_paths: set[str] = set()
    for name in selected_names:
        st = stack_secret_state[name]
        entry = st['entry']
        services, declared_vols, _ = load_merged_compose(root, entry)
        remote_stack_dir = posixpath.normpath(posixpath.join(remote_root_real, entry['directory']))
        per_svc: dict[str, list[dict]] = {}
        for sname, scfg in services.items():
            mounts = parse_compose_mounts(scfg, set(declared_vols.keys()), remote_stack_dir, remote_home, st['primary_env_map'])
            per_svc[sname] = mounts
            for m in mounts:
                if m['type'] == 'bind':
                    extra_check_paths.add(m['source'])
        proposed_mounts_by_stack[name] = per_svc

    checked_paths: dict[str, dict] = {}
    if extra_check_paths:
        chk_resp = ssh.call(
            {
                'action': 'audit',
                'source_root': validate_remote_path(args.source_root, 'Source root'),
                'probe_paths': [],
                'check_paths': sorted(extra_check_paths),
                'docker_sudo': args.docker_sudo,
            },
            secrets=secrets,
        )
        checked_paths = chk_resp.get('checked_paths', {})

    planned_stacks: dict[str, dict] = {}
    skipped_stacks: list[str] = []

    for name in selected_names:
        st = stack_secret_state[name]
        entry = st['entry']
        services, declared_vols, _ = load_merged_compose(root, entry)
        remote_stack_dir = posixpath.normpath(posixpath.join(remote_root_real, entry['directory']))

        # Check for containers at this exact working_dir
        matched = [
            c for c in all_containers
            if c.get('working_dir') and posixpath.normpath(c['working_dir']) == remote_stack_dir
        ]

        # Detect directory drift / container_name collisions elsewhere
        expected_cnames = {
            '/' + str(scfg['container_name']).lstrip('/')
            for scfg in services.values()
            if scfg.get('container_name')
        }
        for c in all_containers:
            if c in matched:
                continue
            cname = '/' + str(c.get('name', '')).lstrip('/')
            if cname in expected_cnames:
                raise SshMigrationError(
                    f'Container {cname} for stack "{name}" is running from a different directory '
                    f'({c.get("working_dir")}) than expected ({remote_stack_dir}); refusing ambiguous adoption'
                )

        if not matched:
            if args.all_stacks and args.skip_not_deployed:
                skipped_stacks.append(name)
                print(f'SKIPPED (not deployed): {args.host}/{name}')
                continue
            raise SshMigrationError(
                f'Stack "{name}" has no audited containers at {remote_stack_dir}; '
                'in-place adoption only applies to deployed stacks'
            )

        # Refuse automatic cutover for stacks with one-shot schema migration services!
        oneshots = detect_oneshot_services(services)
        if oneshots:
            raise SshMigrationError(
                f'Stack "{name}" defines one-shot schema migration service(s) ({", ".join(oneshots)}); '
                'refusing automatic adoption so migrations are never executed unattended. '
                'See docs/MIGRATION.md for manual adoption steps.'
            )

        projects = {c.get('project') for c in matched}
        if None in projects or '' in projects or len(projects) != 1:
            raise SshMigrationError(
                f'Stack "{name}" has ambiguous or missing Compose project labels ({sorted(str(p) for p in projects)})'
            )
        project_name = next(iter(projects))

        by_service: dict[str, dict] = {}
        for c in matched:
            svc = c.get('service')
            if not svc:
                raise SshMigrationError(f'Container {c.get("name")} in stack "{name}" lacks com.docker.compose.service label')
            if svc in by_service:
                raise SshMigrationError(f'Multiple containers found for service {name}/{svc}; refusing ambiguous adoption')
            by_service[svc] = c

        # Detect removed or newly added services
        removed_svcs = sorted(set(by_service.keys()) - set(services.keys()))
        if removed_svcs:
            raise SshMigrationError(
                f'Stack "{name}" has running service(s) missing from repo Compose ({", ".join(removed_svcs)}); '
                'refusing silent service removal'
            )
        new_svcs = sorted(set(services.keys()) - set(by_service.keys()))
        if new_svcs and not args.allow_new_services:
            raise SshMigrationError(
                f'Stack "{name}" introduces new service(s) not currently deployed ({", ".join(new_svcs)}); '
                'review first and rerun with --allow-new-services once their images are locally cached'
            )

        # Image identity & drift validation
        pinned_images: dict[str, str] = {}
        image_drift_pinned: list[str] = []
        service_records: dict[str, dict] = {}

        for sname, scfg in services.items():
            if sname in new_svcs:
                raw_img = interpolate_value(str(scfg.get('image', '')), st['primary_env_map'])
                if not raw_img:
                    raise SshMigrationError(f'New service {name}/{sname} requires a concrete image (builds are not run during adoption)')
                service_records[sname] = {'new_service': True, 'repo_image': raw_img}
                continue
            audited = by_service[sname]
            img_id = audited.get('image')
            if not img_id or not remote_images.get(img_id, {}).get('exists'):
                raise SshMigrationError(f'Audited running image ID for {name}/{sname} is not available locally on remote host')
            audited_cfg_img = str(audited.get('configured_image') or '')
            if 'build' in scfg and not scfg.get('image'):
                pinned_images[sname] = img_id
            else:
                repo_img = interpolate_value(str(scfg.get('image') or ''), st['primary_env_map'])
                if normalize_image_ref(repo_img) != normalize_image_ref(audited_cfg_img):
                    if not args.pin_running_images:
                        raise SshMigrationError(
                            f'Unreviewed image/schema change for {name}/{sname}: '
                            f'running "{audited_cfg_img}" vs repo "{repo_img}". '
                            'Review schema compatibility or pass --pin-running-images to lock to the running image.'
                        )
                    image_drift_pinned.append(sname)
                pinned_images[sname] = img_id
            service_records[sname] = {
                'container_name': audited.get('name'),
                'image_id': img_id,
                'configured_image': audited_cfg_img,
                'running': bool(audited.get('running')),
            }

        # Mount & named volume identity validation
        secret_remote_paths = {
            posixpath.normpath(posixpath.join(remote_root_real, rel))
            for rel in st['secret_outputs']
        }
        staged_remote_paths = {
            posixpath.normpath(posixpath.join(remote_root_real, rel))
            for rel in st['staged_repo_rels']
        }
        pinned_volumes: dict[str, str] = {}

        for sname, scfg in services.items():
            proposed = proposed_mounts_by_stack[name][sname]
            prop_by_dst = {posixpath.normpath(m['destination']): m for m in proposed}

            if sname in by_service:
                for am in by_service[sname].get('mounts') or []:
                    dst = posixpath.normpath(str(am.get('Destination') or ''))
                    if dst.startswith('/run/secrets/'):
                        continue
                    atype = am.get('Type')
                    pm = prop_by_dst.get(dst)
                    if atype == 'volume':
                        vname = am.get('Name')
                        if not pm or pm['type'] != 'volume':
                            raise SshMigrationError(
                                f'Incompatible mount for {name}/{sname}:{dst}: audited container uses named volume "{vname}" '
                                'but repo Compose does not preserve a named volume at that destination'
                            )
                        if vname not in remote_volumes:
                            raise SshMigrationError(
                                f'Audited named volume "{vname}" for {name}/{sname} not found in Docker volume list; '
                                'refusing to risk creating an empty volume'
                            )
                        vkey = pm['volume_key']
                        if vkey in pinned_volumes and pinned_volumes[vkey] != vname:
                            raise SshMigrationError(f'Conflicting named volume bindings for volume key "{vkey}" in stack "{name}"')
                        pinned_volumes[vkey] = vname
                    elif atype == 'bind':
                        asrc = posixpath.normpath(str(am.get('Source') or ''))
                        if pm and pm['type'] == 'bind' and pm['source'] in secret_remote_paths:
                            # Intentional SOPS secret file migration (e.g. legacy token -> .secrets/token)
                            continue
                        if not pm or pm['type'] != 'bind':
                            raise SshMigrationError(
                                f'Incompatible mount for {name}/{sname}:{dst}: audited container uses bind mount "{asrc}" '
                                'but repo Compose does not preserve it'
                            )
                        psrc = pm['source']
                        chk = checked_paths.get(psrc, {})
                        psrc_real = posixpath.normpath(chk.get('realpath', psrc))
                        if psrc != asrc and psrc_real != asrc:
                            raise SshMigrationError(
                                f'Bind mount drift for {name}/{sname}:{dst}: audited source "{asrc}" vs proposed "{psrc}"'
                            )

            # Verify no proposed named volume is unpinned (would create a new empty volume!)
            for pm in proposed:
                if pm['type'] == 'volume':
                    vkey = pm['volume_key']
                    if vkey not in pinned_volumes:
                        raise SshMigrationError(
                            f'Named volume "{vkey}" in {name}/{sname} does not match any audited existing Docker volume; '
                            'refusing to create a new empty data volume'
                        )
                elif pm['type'] == 'bind':
                    psrc = pm['source']
                    if psrc in secret_remote_paths or psrc in staged_remote_paths:
                        continue
                    # Also allow parent directory of a staged file (e.g., ./config when ./config/app.conf is staged)
                    if any(sp.startswith(psrc + '/') for sp in staged_remote_paths | secret_remote_paths):
                        continue
                    if not checked_paths.get(psrc, {}).get('exists'):
                        raise SshMigrationError(
                            f'Proposed bind mount source "{psrc}" for {name}/{sname}:{pm["destination"]} '
                            'does not exist on remote host; refusing to auto-create an empty directory'
                        )

        # Build Compose adoption override pinning exact running images & external named volumes
        override_doc: dict = {}
        if pinned_images:
            override_doc['services'] = {s: {'image': img} for s, img in sorted(pinned_images.items())}
        if pinned_volumes:
            override_doc['volumes'] = {
                vkey: {'external': True, 'name': vname}
                for vkey, vname in sorted(pinned_volumes.items())
            }
        override_yaml = '# Generated by scripts/ssh-migrate.py to preserve running image and volume identities.\n' + (
            yaml.safe_dump(override_doc, sort_keys=False) if override_doc else '{}\n'
        )
        override_bytes = override_yaml.encode('utf-8')

        # Build staged_files manifest (non-secret repo files) + secret_outputs hashes (NO plaintext!)
        staged_files_plan = []
        remote_file_hashes: dict[str, str | None] = {}
        for rel in st['staged_repo_rels']:
            local_file = inside(root, rel)
            expected_sha = sha256_file(local_file)
            pinfo = probed_files.get(rel, {})
            if pinfo.get('permission_denied'):
                raise SshMigrationError(f'Permission denied probing remote file {rel}')
            before_sha = pinfo.get('sha256') if pinfo.get('exists') else None
            remote_file_hashes[rel] = before_sha
            action = 'unchanged' if before_sha == expected_sha else ('update' if before_sha else 'create')
            staged_files_plan.append({
                'repo_rel': rel,
                'remote_path': posixpath.normpath(posixpath.join(remote_root_real, rel)),
                'expected_sha256': expected_sha,
                'remote_before_sha256': before_sha,
                'mode': local_file.stat().st_mode & 0o777,
                'action': action,
            })

        secret_outputs_plan = []
        for rel, (raw_bytes, kind) in sorted(st['secret_outputs'].items()):
            pinfo = probed_files.get(rel, {})
            if pinfo.get('permission_denied'):
                raise SshMigrationError(f'Permission denied probing remote secret target {rel}')
            before_sha = pinfo.get('sha256') if pinfo.get('exists') else None
            remote_file_hashes[rel] = before_sha
            secret_outputs_plan.append({
                'repo_rel': rel,
                'remote_path': posixpath.normpath(posixpath.join(remote_root_real, rel)),
                'expected_sha256': sha256_bytes(raw_bytes),
                'remote_before_sha256': before_sha,
                'mode': 0o600,
                'kind': kind,
            })

        # Archive legacy files that exist on remote and are NOT overwritten in-place by new outputs
        new_output_rels = set(st['secret_outputs'].keys()) | set(st['staged_repo_rels'])
        legacy_to_archive = []
        for rel in st['legacy_rels']:
            if rel in new_output_rels:
                continue
            pinfo = probed_files.get(rel, {})
            if pinfo.get('permission_denied'):
                raise SshMigrationError(f'Permission denied probing legacy file {rel}')
            if pinfo.get('exists'):
                remote_file_hashes[rel] = pinfo.get('sha256')
                legacy_to_archive.append({
                    'repo_rel': rel,
                    'remote_path': posixpath.normpath(posixpath.join(remote_root_real, rel)),
                    'remote_before_sha256': pinfo.get('sha256'),
                })

        # Capture original compose files from audited labels (for rollback)
        orig_cf_set: list[str] = []
        for c in matched:
            for item in str(c.get('config_files') or '').split(','):
                item = item.strip()
                if item and item not in orig_cf_set:
                    orig_cf_set.append(item)
        if not orig_cf_set:
            orig_cf_set = [posixpath.normpath(posixpath.join(remote_stack_dir, cf)) for cf in entry['compose']]

        fingerprint = compute_remote_fingerprint(matched, pinned_volumes, remote_file_hashes)
        primary_env_remote = (
            posixpath.normpath(posixpath.join(remote_root_real, entry['env_file']))
            if entry.get('env_file')
            else None
        )

        planned_stacks[name] = {
            'stack': name,
            'kind': entry['kind'],
            'directory': entry['directory'],
            'dependencies': st['dep_names'],
            'compose_files': entry['compose'],
            'original_compose_files': orig_cf_set,
            'project_name': project_name,
            'remote_stack_dir': remote_stack_dir,
            'primary_env_file': primary_env_remote,
            'services': service_records,
            'expected_running_services': sorted(s for s, info in service_records.items() if info.get('running')),
            'pinned_images': pinned_images,
            'image_drift_pinned': image_drift_pinned,
            'pinned_volumes': pinned_volumes,
            'override_filename': OVERRIDE_FILENAME,
            'override_yaml': override_yaml,
            'override_sha256': sha256_bytes(override_bytes),
            'staged_files': staged_files_plan,
            'secret_inputs': st['secret_inputs'],
            'secret_outputs': secret_outputs_plan,
            'legacy_files_to_archive': legacy_to_archive,
            'remote_file_hashes': remote_file_hashes,
            'remote_state_fingerprint': fingerprint,
        }

    if not planned_stacks:
        raise SshMigrationError('No deployed stacks matched selection; nothing to plan')

    now = datetime.now(timezone.utc)
    git_head, git_dirty = git_repo_revision(root)
    plan_id = f'{args.host}-{now.strftime("%Y%m%dT%H%M%SZ")}-{sha256_bytes(canonical_json_bytes(planned_stacks))[:8]}'
    rollback_base = args.rollback_base or posixpath.join(remote_home, '.homelab-migration-rollback')
    rollback_dir = posixpath.normpath(posixpath.join(rollback_base, args.host, plan_id))

    plan = {
        'plan_version': 1,
        'plan_id': plan_id,
        'created_at': now.isoformat(),
        'host': args.host,
        'ssh_target': args.ssh_target,
        'remote_root': remote_root_real,
        'rollback_dir': rollback_dir,
        'repo_git_head': git_head,
        'repo_git_dirty': git_dirty,
        'manifest_sha256': manifest_sha,
        'stack_order': list(planned_stacks.keys()),
        'skipped_not_deployed': skipped_stacks,
        'stacks': planned_stacks,
    }
    plan['plan_digest'] = compute_plan_digest(plan)
    exclusive_write_private(plan_path, json.dumps(plan, indent=2) + '\n')
    print(f'Created private adoption plan {plan_path} (mode 0600, {len(planned_stacks)} stack(s)); no remote changes made')
    for sname, sp in planned_stacks.items():
        changes = [f'{f["action"]}:{f["repo_rel"]}' for f in sp['staged_files'] if f['action'] != 'unchanged']
        print(
            f'  - {sname} [{sp["kind"]}]: project={sp["project_name"]}, '
            f'pinned_volumes={len(sp["pinned_volumes"])}, secret_outputs={len(sp["secret_outputs"])}, '
            f'archived_legacy={len(sp["legacy_files_to_archive"])}, file_changes={changes or ["none"]}'
        )
    return 0


def cmd_apply(root: Path, manifest: dict, manifest_sha: str, args: argparse.Namespace) -> int:
    # Gate 1: Load plan & verify integrity digest BEFORE anything else
    if not args.plan.is_file():
        raise SshMigrationError(f'Plan file not found: {args.plan}')
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    recorded_digest = plan.get('plan_digest')
    if not recorded_digest or compute_plan_digest(plan) != recorded_digest:
        raise SshMigrationError('Plan digest verification failed; plan file was modified or corrupted after generation')

    # Gate 2: Require explicit operator acknowledgement & evidence of a tested application-data backup
    if not args.confirm_backup_tested or not (args.backup_evidence or '').strip():
        raise SshMigrationError(
            'Refusing cutover without tested application-data backup confirmation. '
            'Configuration rollback does not replace DB dumps. Pass both --confirm-backup-tested and --backup-evidence "<snapshot/restore details>".'
        )
    backup_evidence = args.backup_evidence.strip()

    host = plan['host']
    if host not in manifest:
        raise SshMigrationError(f'Plan host "{host}" does not exist in deployment manifest')

    # Gate 3: Verify controller repo revision, manifest, and staged file hashes (reject stale plans!)
    if plan.get('manifest_sha256') != manifest_sha:
        raise SshMigrationError('Deployment manifest drifted since plan was generated; rerun plan')
    cur_head, _ = git_repo_revision(root)
    if plan.get('repo_git_head') and cur_head != plan['repo_git_head']:
        raise SshMigrationError(
            f'Repository Git revision drifted since plan ({plan["repo_git_head"][:12]} -> {str(cur_head)[:12]}); rerun plan'
        )

    secrets: set[str] = set()
    # Gate 4: Validate & decrypt ALL selected stacks on controller BEFORE making any remote changes!
    stack_payloads: dict[str, dict] = {}
    all_probe_rels: set[str] = set()
    all_check_images: set[str] = set()
    all_check_volumes: set[str] = set()
    all_writable_dirs: set[str] = set()

    for sname in plan['stack_order']:
        sp = plan['stacks'][sname]
        staged_payload = []

        # Verify every non-secret repo file hash matches plan
        for fitem in sp['staged_files']:
            local_file = inside(root, fitem['repo_rel'])
            if not local_file.is_file():
                raise SshMigrationError(f'Repo file missing (drift) since plan: {fitem["repo_rel"]}')
            raw = local_file.read_bytes()
            if sha256_bytes(raw) != fitem['expected_sha256']:
                raise SshMigrationError(f'Repo file drifted since plan: {fitem["repo_rel"]}; rerun plan')
            staged_payload.append({
                'remote_path': fitem['remote_path'],
                'b64': base64.b64encode(raw).decode('ascii'),
                'mode': fitem['mode'],
                'is_secret': False,
            })

        # Re-decrypt SOPS inputs on controller and verify hashes match plan
        decrypted_outputs: dict[str, bytes] = {}
        for sinput in sp.get('secret_inputs', []):
            sub_entry = manifest[host][sinput['stack']]
            vals, enc_sha = sops_decrypt_entry(root, sub_entry, secrets)
            if enc_sha != sinput['encrypted_sha256']:
                raise SshMigrationError(f'Encrypted secret file drifted since plan: {sinput["encrypted_file"]}; rerun plan')
            for rel_out, (raw_bytes, _) in render_secret_outputs(sub_entry, vals).items():
                decrypted_outputs[rel_out] = raw_bytes

        for soutput in sp.get('secret_outputs', []):
            rel = soutput['repo_rel']
            if rel not in decrypted_outputs:
                raise SshMigrationError(f'Expected secret output missing after decryption: {rel}')
            raw = decrypted_outputs[rel]
            if sha256_bytes(raw) != soutput['expected_sha256']:
                raise SshMigrationError(f'Rendered secret output drifted since plan: {rel}; rerun plan')
            staged_payload.append({
                'remote_path': soutput['remote_path'],
                'b64': base64.b64encode(raw).decode('ascii'),
                'mode': 0o600,
                'is_secret': True,
            })

        all_probe_rels.update(sp['remote_file_hashes'].keys())
        all_check_images.update(sp['pinned_images'].values())
        for svc_info in sp.get('services', {}).values():
            if svc_info.get('new_service') and svc_info.get('repo_image'):
                all_check_images.add(svc_info['repo_image'])
        all_check_volumes.update(sp['pinned_volumes'].values())
        all_writable_dirs.add(sp['remote_stack_dir'])

        stack_payloads[sname] = {
            'staged_files': staged_payload,
            'override_b64': base64.b64encode(sp['override_yaml'].encode('utf-8')).decode('ascii'),
        }

    # Gate 5: Remote read-only preflight verifying zero remote drift across ALL stacks before ANY mutation!
    ssh_target = args.ssh_target or plan['ssh_target']
    ssh = SshClient(ssh_target, args.ssh_bin, args.ssh_config, args.ssh_identity, args.ssh_port, args.connect_timeout)
    pre = ssh.call(
        {
            'action': 'preflight',
            'source_root': plan['remote_root'],
            'rollback_dir': plan['rollback_dir'],
            'probe_paths': sorted(all_probe_rels),
            'check_images': sorted(all_check_images),
            'check_volumes': sorted(all_check_volumes),
            'writable_dirs': sorted(all_writable_dirs),
            'docker_sudo': args.docker_sudo,
        },
        secrets=secrets,
    )
    if pre.get('missing_images'):
        raise SshMigrationError(f'Pinned image(s) missing on remote host: {", ".join(pre["missing_images"])}')
    if pre.get('missing_volumes'):
        raise SshMigrationError(f'Pinned Docker volume(s) missing on remote host: {", ".join(pre["missing_volumes"])}')
    if pre.get('unwritable_dirs'):
        raise SshMigrationError(f'Remote stack directory not writable: {", ".join(pre["unwritable_dirs"])}')
    if pre.get('rollback_exists'):
        raise SshMigrationError(f'Rollback directory already exists ({plan["rollback_dir"]}); refusing to reuse plan ID')
    if not pre.get('rollback_writable'):
        raise SshMigrationError(f'Remote rollback parent directory is not writable: {plan["rollback_dir"]}')

    probed_now = pre.get('probed_files', {})
    containers_now = pre.get('containers', [])
    for sname in plan['stack_order']:
        sp = plan['stacks'][sname]
        matched_now = [
            c for c in containers_now
            if c.get('working_dir') and posixpath.normpath(c['working_dir']) == sp['remote_stack_dir']
        ]
        cur_hashes: dict[str, str | None] = {}
        for rel in sp['remote_file_hashes']:
            info = probed_now.get(rel, {})
            cur_hashes[rel] = info.get('sha256') if info.get('exists') else None
        cur_fp = compute_remote_fingerprint(matched_now, sp['pinned_volumes'], cur_hashes)
        if cur_fp != sp['remote_state_fingerprint']:
            raise SshMigrationError(
                f'Remote state drifted for stack "{sname}" since plan was generated; rerun plan before applying'
            )

    # Phase 6: Apply each stack sequentially; automatically rollback ALL touched stacks on ANY failure!
    applied_order: list[str] = []
    for sname in plan['stack_order']:
        sp = plan['stacks'][sname]
        payload = stack_payloads[sname]
        applied_order.append(sname)
        try:
            resp = ssh.call(
                {
                    'action': 'apply_stack',
                    'plan_id': plan['plan_id'],
                    'host': host,
                    'backup_evidence': backup_evidence,
                    'rollback_dir': plan['rollback_dir'],
                    'stack': sname,
                    'remote_root': plan['remote_root'],
                    'remote_stack_dir': sp['remote_stack_dir'],
                    'project_name': sp['project_name'],
                    'compose_files': sp['compose_files'],
                    'original_compose_files': sp['original_compose_files'],
                    'override_filename': sp['override_filename'],
                    'override_b64': payload['override_b64'],
                    'staged_files': payload['staged_files'],
                    'archive_legacy_files': [item['remote_path'] for item in sp['legacy_files_to_archive']],
                    'primary_env_file': sp['primary_env_file'],
                    'expected_running_services': sp['expected_running_services'],
                    'docker_sudo': args.docker_sudo,
                },
                secrets=secrets,
            )
            print(f'Adopted stack {host}/{sname} (project={sp["project_name"]}, verified={resp.get("containers_verified", 0)} container(s))')
        except SshMigrationError as apply_err:
            print(f'ERROR during apply of {host}/{sname}: {redact(str(apply_err), secrets)}', file=sys.stderr)
            print(f'Initiating automatic best-effort rollback for stack(s): {", ".join(reversed(applied_order))}', file=sys.stderr)
            try:
                rb_resp = ssh.call(
                    {
                        'action': 'rollback',
                        'rollback_dir': plan['rollback_dir'],
                        'stacks': list(reversed(applied_order)),
                        'force_compose_restore': False,
                        'docker_sudo': args.docker_sudo,
                    },
                    secrets=secrets,
                )
                print(
                    f'Automatic rollback completed for {len(rb_resp.get("results", {}))} stack(s). '
                    'NOTE: Configuration/files were restored; if any service wrote to a database, restore from your tested application-data backup.',
                    file=sys.stderr,
                )
            except SshMigrationError as rb_err:
                print(f'WARNING: Automatic rollback reported errors: {redact(str(rb_err), secrets)}', file=sys.stderr)
                print(f'Run manual recovery: python3 scripts/ssh-migrate.py rollback --plan {args.plan}', file=sys.stderr)
            raise SshMigrationError(f'Apply failed on stack "{sname}"; automatic rollback executed') from apply_err

    print(
        f'Adoption complete for {len(applied_order)} stack(s) on {host}. '
        f'Protected rollback state retained at {plan["rollback_dir"]}.'
    )
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if args.plan:
        if not args.plan.is_file():
            raise SshMigrationError(f'Plan file not found: {args.plan}')
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        ssh_target = args.ssh_target or plan['ssh_target']
        rollback_dir = args.rollback_dir or plan['rollback_dir']
    else:
        if not args.ssh_target or not args.rollback_dir:
            raise SshMigrationError('Specify --plan <plan.json> or both --ssh-target and --rollback-dir')
        ssh_target = args.ssh_target
        rollback_dir = args.rollback_dir

    ssh = SshClient(ssh_target, args.ssh_bin, args.ssh_config, args.ssh_identity, args.ssh_port, args.connect_timeout)
    resp = ssh.call({
        'action': 'rollback',
        'rollback_dir': rollback_dir,
        'stacks': args.stack or None,
        'force_compose_restore': True,
        'docker_sudo': args.docker_sudo,
    })
    for sname, res in resp.get('results', {}).items():
        if res.get('skipped'):
            print(f'Rollback {sname}: skipped ({res.get("reason")})')
        else:
            print(
                f'Rollback {sname}: restored={len(res.get("restored_files", []))} file(s), '
                f'removed={len(res.get("removed_files", []))} file(s), '
                f'compose_restored={res.get("compose_restored")}'
            )
    print(
        'Rollback finished. NOTE: File/Compose rollback does not undo database writes; '
        'restore from your tested application-data backup if writes or schema changes occurred.'
    )
    return 0


def add_ssh_args(parser: argparse.ArgumentParser, target_required: bool = True) -> None:
    parser.add_argument('--ssh-target', required=target_required, help='SSH host alias or user@host from SSH config')
    parser.add_argument('--ssh-bin', default=os.environ.get('SSH_BIN', 'ssh'), help='SSH client executable (default: ssh)')
    parser.add_argument('--ssh-config', help='Optional explicit SSH config file (-F)')
    parser.add_argument('--ssh-identity', help='Optional explicit SSH identity file (-i)')
    parser.add_argument('--ssh-port', type=int, help='Optional SSH port (-p)')
    parser.add_argument('--connect-timeout', type=int, default=15, help='SSH connect timeout in seconds')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='Local controller repository root')
    sub = parser.add_subparsers(dest='action', required=True)

    p_audit = sub.add_parser('audit', help='Read-only remote audit of Docker stacks, volumes, networking, and legacy files')
    p_audit.add_argument('--host', required=True)
    p_audit.add_argument('--source-root', required=True, help='Existing deployment directory on remote host')
    p_audit.add_argument('--output', type=Path, required=True, help='Private audit JSON path outside the repo')
    p_audit.add_argument('--docker-sudo', action='store_true', help='Prefix remote docker invocations with sudo -n')
    add_ssh_args(p_audit)

    p_cap = sub.add_parser('capture-host', help='Fetch allowlisted host/WireGuard/DNS/firewall config over SSH and encrypt locally with SOPS')
    p_cap.add_argument('--host', required=True)
    p_cap.add_argument('--system-root', default='/', help='Remote system root (default: /)')
    p_cap.add_argument('--sudo', action='store_true', help='Use passwordless sudo -n on remote host for root-only files/firewall')
    p_cap.add_argument('--runtime-firewall', choices=('iptables', 'nftables'))
    p_cap.add_argument('--skip-unreadable', action='store_true', help='Explicit opt-out to skip unreadable root-only files instead of failing')
    p_cap.add_argument('--skip-existing', action='store_true', help='Skip if encrypted host-config file already exists')
    p_cap.add_argument('--apply', action='store_true', help='Encrypt and publish secrets/<host>/host-config.enc.json (default: dry-run)')
    add_ssh_args(p_cap)

    p_imp = sub.add_parser('import-secrets', help='Fetch legacy stack secrets over SSH and encrypt locally with SOPS')
    p_imp.add_argument('--host', required=True)
    p_imp.add_argument('--source-root', required=True)
    p_imp.add_argument('--stack', action='append', default=[], help='Stack to import (repeatable; dependencies included automatically)')
    p_imp.add_argument('--all-stacks', action='store_true', help='Import all manifest stacks for this host')
    p_imp.add_argument('--skip-not-deployed', action='store_true', help='With --all-stacks, skip stacks with neither containers nor legacy source files')
    p_imp.add_argument('--skip-existing', action='store_true', help='Skip stacks whose encrypted file already exists')
    p_imp.add_argument('--docker-sudo', action='store_true')
    p_imp.add_argument('--apply', action='store_true', help='Encrypt and publish .enc.json files (default: dry-run)')
    add_ssh_args(p_imp)

    p_prep = sub.add_parser('prepare', help='Combined read-only capture phase: audit + capture-host + import-secrets')
    p_prep.add_argument('--host', required=True)
    p_prep.add_argument('--source-root', required=True)
    p_prep.add_argument('--system-root', default='/')
    p_prep.add_argument('--audit-output', type=Path, help='Private audit JSON path outside the repo')
    p_prep.add_argument('--stack', action='append', default=[])
    p_prep.add_argument('--all-stacks', action='store_true')
    p_prep.add_argument('--skip-host-config', action='store_true')
    p_prep.add_argument('--sudo', action='store_true')
    p_prep.add_argument('--docker-sudo', action='store_true')
    p_prep.add_argument('--runtime-firewall', choices=('iptables', 'nftables'))
    p_prep.add_argument('--skip-unreadable', action='store_true')
    p_prep.add_argument('--skip-not-deployed', action='store_true')
    p_prep.add_argument('--skip-existing', action='store_true')
    p_prep.add_argument('--apply', action='store_true')
    add_ssh_args(p_prep)

    p_plan = sub.add_parser('plan', help='Audit remote host, validate identity/mounts/images/secrets, and write a hashed adoption plan')
    p_plan.add_argument('--host', required=True)
    p_plan.add_argument('--source-root', required=True)
    p_plan.add_argument('--plan', type=Path, required=True, help='Private plan JSON path outside the repo')
    p_plan.add_argument('--stack', action='append', default=[], help='Stack to adopt (secret-managed or credentialless; repeatable)')
    p_plan.add_argument('--all-stacks', action='store_true', help='Plan all deployed stacks for this host')
    p_plan.add_argument('--skip-not-deployed', action='store_true', help='With --all-stacks, skip stacks not currently deployed')
    p_plan.add_argument('--pin-running-images', action='store_true', help='Allow pinning audited running images when repo image tag differs')
    p_plan.add_argument('--allow-new-services', action='store_true', help='Allow newly introduced Compose services whose images exist locally')
    p_plan.add_argument('--rollback-base', help='Override remote base directory for protected rollback snapshots')
    p_plan.add_argument('--docker-sudo', action='store_true')
    add_ssh_args(p_plan)

    p_apply = sub.add_parser('apply', help='Execute an approved adoption plan over SSH with drift checks, backup gate, and automatic rollback')
    p_apply.add_argument('--plan', type=Path, required=True, help='Path to approved plan JSON')
    p_apply.add_argument('--confirm-backup-tested', action='store_true', help='Confirm a tested application-data backup exists prior to cutover')
    p_apply.add_argument('--backup-evidence', default='', help='Operator reference/evidence of the tested application-data backup')
    p_apply.add_argument('--docker-sudo', action='store_true')
    add_ssh_args(p_apply, target_required=False)

    p_rb = sub.add_parser('rollback', help='Restore protected rollback copies and previous Compose definitions over SSH')
    p_rb.add_argument('--plan', type=Path, help='Plan JSON created before apply')
    p_rb.add_argument('--rollback-dir', help='Explicit remote rollback snapshot directory')
    p_rb.add_argument('--stack', action='append', default=[], help='Optional stack subset to roll back')
    p_rb.add_argument('--docker-sudo', action='store_true')
    add_ssh_args(p_rb, target_required=False)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.action == 'rollback':
            return cmd_rollback(args)
        manifest, manifest_sha = load_manifest(root)
        if args.action == 'audit':
            return cmd_audit(root, manifest, args)
        if args.action == 'capture-host':
            return cmd_capture_host(root, manifest, args)
        if args.action == 'import-secrets':
            return cmd_import_secrets(root, manifest, args)
        if args.action == 'prepare':
            return cmd_prepare(root, manifest, args)
        if args.action == 'plan':
            return cmd_plan(root, manifest, manifest_sha, args)
        if args.action == 'apply':
            return cmd_apply(root, manifest, manifest_sha, args)
        raise SshMigrationError(f'Unknown action: {args.action}')
    except SshMigrationError as err:
        print(f'Error: {err}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f'Error: migration operation failed ({type(err).__name__}); verify configuration and permissions', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
