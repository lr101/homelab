#!/usr/bin/env python3
"""Stdlib-only remote helper executed over SSH by scripts/ssh-migrate.py.

Receives a single JSON request on stdin and emits a single JSON response on stdout.
Compatible with Python 3.8+ (stdlib only) so Synology DSM 7.x and standard Linux hosts work
without Git, SOPS, private age keys, or third-party Python packages.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time

KEY_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
STACK_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
PROJECT_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
SERVICE_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
FILENAME_RE = re.compile(r'[A-Za-z0-9._-]+\Z')

DOCKER_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"configured_image":{{json .Config.Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '"config_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
    '"mounts":{{json .Mounts}},"port_bindings":{{json .HostConfig.PortBindings}},'
    '"networks":{{json .NetworkSettings.Networks}},'
    '"running":{{json .State.Running}},"restarting":{{json .State.Restarting}},'
    '"exit_code":{{json .State.ExitCode}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}""{{end}}}'
)

CONTAINER_FIELDS = (
    'id', 'name', 'image', 'configured_image', 'project', 'service',
    'working_dir', 'config_files', 'mounts', 'port_bindings', 'networks',
    'running', 'restarting', 'exit_code', 'health',
)

SUDO_COMPOSE_EXEC = (
    'import json,os,sys;'
    'p=json.load(sys.stdin);'
    'env=dict(os.environ);'
    'home=p.get("home");\n'
    'if home: env["HOME"]=home\n'
    'for k,v in (p.get("env_updates") or {}).items(): env[k]=v\n'
    'fd=os.open(os.devnull,os.O_RDONLY);os.dup2(fd,0);os.close(fd);'
    'os.execvpe(p["argv"][0],p["argv"],env)'
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def redact(text: str, secrets: set[str]) -> str:
    if not text or not secrets:
        return text
    result = text
    for secret in sorted((s for s in secrets if s and len(s) >= 3), key=len, reverse=True):
        result = result.replace(secret, '[REDACTED]')
    return result


def is_compose_definition_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith('docker-compose')
        or name.startswith('compose.')
        or name == 'compose.yml'
        or name == 'compose.yaml'
        or name.startswith('.homelab-')
    )


def harvest_secret_values(raw: bytes, secrets: set[str]) -> None:
    """Extract plausible secret values from dotenv/INI/YAML/raw secret files for redaction."""
    if not raw or len(raw) > 262144:
        return
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return
    stripped_whole = text.strip()
    if stripped_whole and '\n' not in stripped_whole and len(stripped_whole) >= 3:
        secrets.add(stripped_whole)
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if '=' in s:
            _, _, val = s.partition('=')
            val_s = val.strip()
            if len(val_s) >= 3:
                secrets.add(val_s)
            if len(val_s) >= 2 and val_s[0] == val_s[-1] and val_s[0] in ("'", '"'):
                inner = val_s[1:-1]
                if len(inner) >= 3:
                    secrets.add(inner)
        elif ':' in s:
            _, _, val = s.partition(':')
            val_s = val.strip()
            if len(val_s) >= 3:
                secrets.add(val_s)
            if len(val_s) >= 2 and val_s[0] == val_s[-1] and val_s[0] in ("'", '"'):
                inner = val_s[1:-1]
                if len(inner) >= 3:
                    secrets.add(inner)


def _is_within(child: Path, parent: Path) -> bool:
    """Python 3.8-compatible containment check (avoids Path.is_relative_to)."""
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False


def validate_identifier(value: str, regex: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not value or not regex.fullmatch(value) or value in ('.', '..'):
        raise ValueError(f'Invalid {label}: {value!r}')
    return value


def resolve_inside(root: Path, rel_or_abs: str, *, reject_symlinks: bool = False) -> Path:
    if not isinstance(rel_or_abs, str) or not rel_or_abs or '\0' in rel_or_abs:
        raise ValueError(f'Invalid path: {rel_or_abs!r}')
    raw = Path(rel_or_abs)
    if '..' in raw.parts:
        raise ValueError(f'Path traversal ("..") is not allowed: {rel_or_abs}')

    root_norm = Path(os.path.normpath(str(root)))
    root_real = root.resolve()

    if raw.is_absolute():
        raw_norm = Path(os.path.normpath(str(raw)))
        if _is_within(raw_norm, root_real):
            lexical = raw_norm
        elif _is_within(raw_norm, root_norm):
            lexical = root_real / raw_norm.relative_to(root_norm)
        else:
            # Still resolve to check if it legitimately aliases root_real via parent symlink
            resolved_abs = raw.resolve(strict=False)
            if not _is_within(resolved_abs, root_real):
                raise ValueError(f'Path escapes allowed root: {rel_or_abs}')
            lexical = resolved_abs
    else:
        lexical = Path(os.path.normpath(str(root_real / raw)))

    if not _is_within(lexical, root_real):
        raise ValueError(f'Path escapes allowed root: {rel_or_abs}')

    # Walk every component strictly beneath root_real to catch symlinks (including dangling)
    rel_parts = lexical.relative_to(root_real).parts
    if reject_symlinks:
        cur = root_real
        for part in rel_parts:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError(f'Refusing symlink path inside allowed root: {rel_or_abs}')

    resolved = lexical.resolve(strict=False)
    if not _is_within(resolved, root_real):
        raise ValueError(f'Path escapes allowed root: {rel_or_abs}')
    return lexical if reject_symlinks else resolved


def ensure_disjoint_rollback_dir(rollback_dir: Path, remote_root: Path) -> Path:
    rb_res = rollback_dir.resolve(strict=False)
    root_res = remote_root.resolve(strict=False)
    if _is_within(rb_res, root_res) or _is_within(root_res, rb_res):
        raise ValueError('Rollback directory must lie outside remote_root')
    return rb_res


def docker_cmd(docker_sudo: bool, *args: str) -> list[str]:
    prefix = ['sudo', '-n', '--', 'docker'] if docker_sudo else ['docker']
    return prefix + list(args)


def probe_sudo_home() -> tuple[str | None, str | None]:
    proc = subprocess.run(
        ['sudo', '-n', '--', 'python3', '-c', 'import os; print(os.environ.get("HOME", ""))'],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None, (
            'sudo -n -- python3 failed; --docker-sudo requires passwordless sudo for docker and python3 '
            '(same as capture-host --sudo)'
        )
    return proc.stdout.strip() or None, None


def run_compose(
    docker_sudo: bool,
    compose_args: list[str],
    cwd: str,
    env_updates: dict[str, str] | None = None,
    compose_home: str | None = None,
) -> subprocess.CompletedProcess:
    """Execute docker compose safely without leaking secrets on argv or losing them to sudo env_reset."""
    updates = dict(env_updates or {})
    if docker_sudo:
        payload = json.dumps({
            'argv': ['docker', 'compose'] + list(compose_args),
            'env_updates': updates,
            'home': compose_home,
        })
        return subprocess.run(
            ['sudo', '-n', '--', 'python3', '-c', SUDO_COMPOSE_EXEC],
            input=payload,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    env = dict(os.environ)
    if compose_home:
        env['HOME'] = compose_home
    env.update(updates)
    return subprocess.run(
        ['docker', 'compose'] + list(compose_args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def atomic_write_bytes(target: Path, data: bytes, mode: int, private_parents: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if private_parents or target.parent.name == '.secrets':
        target.parent.chmod(0o700)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.homelab-mig-', delete=False) as out:
            tmp_path = Path(out.name)
            os.chmod(tmp_path, mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp_path, mode)
        tmp_path.replace(target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def inspect_image_ids(docker_sudo: bool, refs: list[str]) -> dict[str, str | None]:
    resolved: dict[str, str | None] = {}
    for ref in refs:
        if not ref or ref.startswith('-'):
            resolved[ref] = None
            continue
        proc = subprocess.run(
            docker_cmd(docker_sudo, 'image', 'inspect', '--format', '{{.Id}}', '--', ref),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            val = proc.stdout.strip()
            resolved[ref] = val or None
        else:
            resolved[ref] = None
    return resolved


def run_docker_audit(docker_sudo: bool) -> dict:
    result: dict = {
        'compose_version': None,
        'sudo_home_dir': None,
        'containers': [],
        'images': {},
        'volumes': {},
        'networks': [],
        'errors': [],
    }
    if docker_sudo:
        sudo_home, sudo_err = probe_sudo_home()
        result['sudo_home_dir'] = sudo_home
        if sudo_err:
            result['errors'].append(sudo_err)

    ver = subprocess.run(docker_cmd(docker_sudo, 'compose', 'version'), capture_output=True, text=True, check=False)
    if ver.returncode == 0:
        result['compose_version'] = ver.stdout.strip()
    else:
        result['errors'].append('docker compose version failed; check Docker Compose v2 installation')

    ps = subprocess.run(docker_cmd(docker_sudo, 'ps', '-aq'), capture_output=True, text=True, check=False)
    if ps.returncode != 0:
        result['errors'].append('Docker inventory incomplete; rerun with appropriate daemon access')
        return result

    image_ids: set[str] = set()
    for cid in ps.stdout.split():
        insp = subprocess.run(
            docker_cmd(docker_sudo, 'inspect', '--format', DOCKER_FORMAT, '--', cid),
            capture_output=True,
            text=True,
            check=False,
        )
        if insp.returncode != 0:
            result['errors'].append(f'Failed to inspect container {cid[:12]}')
            continue
        try:
            item = json.loads(insp.stdout)
        except ValueError:
            result['errors'].append(f'Invalid inspect JSON for container {cid[:12]}')
            continue
        # Defense in depth: copy strictly allowlisted keys only (never Config.Env or full Labels)
        safe = {k: item.get(k) for k in CONTAINER_FIELDS}
        if not safe.get('id'):
            safe['id'] = cid
        result['containers'].append(safe)
        if safe.get('image'):
            image_ids.add(safe['image'])

    for img_id in sorted(image_ids):
        img_insp = subprocess.run(
            docker_cmd(docker_sudo, 'image', 'inspect', '--format', '{{json .RepoDigests}}', '--', img_id),
            capture_output=True,
            text=True,
            check=False,
        )
        if img_insp.returncode == 0:
            try:
                result['images'][img_id] = {'exists': True, 'repo_digests': json.loads(img_insp.stdout)}
            except ValueError:
                result['images'][img_id] = {'exists': True, 'repo_digests': []}
        else:
            result['images'][img_id] = {'exists': False, 'repo_digests': []}

    vols = subprocess.run(docker_cmd(docker_sudo, 'volume', 'ls', '-q'), capture_output=True, text=True, check=False)
    if vols.returncode == 0:
        for vname in sorted(v.strip() for v in vols.stdout.splitlines() if v.strip()):
            vinsp = subprocess.run(
                docker_cmd(
                    docker_sudo,
                    'volume',
                    'inspect',
                    '--format',
                    '{"name":{{json .Name}},"driver":{{json .Driver}},"mountpoint":{{json .Mountpoint}},"labels":{{json .Labels}}}',
                    '--',
                    vname,
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            if vinsp.returncode == 0:
                try:
                    vdoc = json.loads(vinsp.stdout)
                    if not isinstance(vdoc.get('labels'), dict):
                        vdoc['labels'] = {}
                    result['volumes'][vname] = vdoc
                except ValueError:
                    result['volumes'][vname] = {'name': vname, 'labels': {}}
            else:
                result['volumes'][vname] = {'name': vname, 'labels': {}}
    else:
        result['errors'].append('Docker volume inventory incomplete')

    nets = subprocess.run(
        docker_cmd(docker_sudo, 'network', 'ls', '--format', '{{json .Name}}'),
        capture_output=True,
        text=True,
        check=False,
    )
    if nets.returncode == 0:
        names = []
        for line in nets.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                names.append(json.loads(line))
            except ValueError:
                names.append(line.strip('"'))
        result['networks'] = sorted(names)
    else:
        result['errors'].append('Docker network inventory incomplete')

    return result


def collect_network_telemetry() -> tuple[dict, list[str]]:
    network = {}
    errors = []
    commands = {
        'addresses': ['ip', '-json', 'address'],
        'routes': ['ip', '-json', 'route', 'show', 'table', 'all'],
        'rules': ['ip', '-json', 'rule'],
        'wireguard_interfaces': ['wg', 'show', 'interfaces'],
        'ipv4_forwarding': ['sysctl', '-n', 'net.ipv4.ip_forward'],
        'ipv6_forwarding': ['sysctl', '-n', 'net.ipv6.conf.all.forwarding'],
    }
    for name, cmd in commands.items():
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            network[name] = proc.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            errors.append(f'{name} not captured; inspect manually')
    return network, errors


def probe_paths(source_root: Path, rel_paths: list[str]) -> dict:
    probed = {}
    root_real = source_root.resolve()
    for rel in rel_paths:
        try:
            target = resolve_inside(root_real, rel, reject_symlinks=True)
        except ValueError as exc:
            msg = str(exc)
            if 'symlink' in msg.lower():
                probed[rel] = {'exists': True, 'is_symlink': True, 'is_file': False, 'readable': False, 'permission_denied': False}
            else:
                probed[rel] = {'exists': False, 'escape_error': True}
            continue
        if not os.path.lexists(target):
            probed[rel] = {'exists': False, 'is_symlink': False, 'is_file': False, 'readable': False, 'permission_denied': False}
            continue
        if target.is_symlink():
            probed[rel] = {'exists': True, 'is_symlink': True, 'is_file': False, 'readable': False, 'permission_denied': False}
            continue
        try:
            st = target.stat()
            is_file = stat.S_ISREG(st.st_mode)
            is_dir = stat.S_ISDIR(st.st_mode)
            info: dict = {
                'exists': True,
                'is_symlink': False,
                'is_file': is_file,
                'is_dir': is_dir,
                'mode': stat.S_IMODE(st.st_mode),
                'realpath': str(target.resolve()),
                'permission_denied': False,
                'readable': False,
            }
            if is_file:
                try:
                    info['sha256'] = sha256_file(target)
                    info['readable'] = True
                except PermissionError:
                    info['permission_denied'] = True
            probed[rel] = info
        except PermissionError:
            probed[rel] = {'exists': True, 'is_symlink': False, 'permission_denied': True, 'readable': False}
    return probed


def check_abs_paths(paths: list[str]) -> dict:
    out = {}
    for raw in paths:
        p = Path(raw)
        if not os.path.lexists(p):
            out[raw] = {'exists': False, 'is_file': False, 'is_dir': False, 'realpath': str(p)}
            continue
        try:
            st = p.stat()
            info: dict = {
                'exists': True,
                'is_file': stat.S_ISREG(st.st_mode),
                'is_dir': stat.S_ISDIR(st.st_mode),
                'realpath': str(p.resolve()),
                'mode': stat.S_IMODE(st.st_mode),
                'permission_denied': False,
            }
            if info['is_file']:
                try:
                    info['sha256'] = sha256_file(p)
                except PermissionError:
                    info['permission_denied'] = True
            out[raw] = info
        except FileNotFoundError:
            out[raw] = {'exists': False, 'is_symlink': p.is_symlink(), 'is_file': False, 'is_dir': False, 'realpath': raw}
        except PermissionError:
            out[raw] = {'exists': True, 'permission_denied': True, 'realpath': raw}
    return out


def handle_audit(req: dict) -> dict:
    source_root = Path(req['source_root'])
    if not source_root.is_dir():
        return {'ok': False, 'error': f'Source root is not an existing directory on remote host: {source_root}'}
    docker_sudo = bool(req.get('docker_sudo', False))
    docker_info = run_docker_audit(docker_sudo)
    net_info, net_errors = collect_network_telemetry()
    probed = probe_paths(source_root, req.get('probe_paths', []))
    checked = check_abs_paths(req.get('check_paths', []))
    resolved_imgs = inspect_image_ids(docker_sudo, req.get('inspect_images', []))
    return {
        'ok': True,
        'hostname': socket.gethostname(),
        'home_dir': str(Path.home().resolve()),
        'sudo_home_dir': docker_info['sudo_home_dir'],
        'source_root_realpath': str(source_root.resolve()),
        'compose_version': docker_info['compose_version'],
        'containers': docker_info['containers'],
        'images': docker_info['images'],
        'resolved_images': resolved_imgs,
        'volumes': docker_info['volumes'],
        'networks': docker_info['networks'],
        'network': net_info,
        'probed_files': probed,
        'checked_paths': checked,
        'docker_errors': docker_info['errors'],
        'errors': docker_info['errors'] + net_errors,
    }


def handle_inspect_paths(req: dict) -> dict:
    """Lightweight read-only path probe + image ID lookup without duplicating full container/network audit."""
    source_root = Path(req['source_root'])
    if not source_root.is_dir():
        return {'ok': False, 'error': f'Source root is not an existing directory on remote host: {source_root}'}
    docker_sudo = bool(req.get('docker_sudo', False))
    probed = probe_paths(source_root, req.get('probe_paths', []))
    checked = check_abs_paths(req.get('check_paths', []))
    resolved_imgs = inspect_image_ids(docker_sudo, req.get('inspect_images', []))
    return {
        'ok': True,
        'probed_files': probed,
        'checked_paths': checked,
        'resolved_images': resolved_imgs,
    }


def handle_capture_host(req: dict) -> dict:
    system_root = Path(req.get('system_root', '/')).resolve()
    if not system_root.is_dir():
        return {'ok': False, 'error': f'System root does not exist on remote host: {system_root}'}
    patterns = req.get('patterns', [])
    files: dict[str, str] = {}
    unreadable: list[str] = []

    for pattern in patterns:
        pat_path = Path(pattern)
        parent_rel = pat_path.parent
        name_pat = pat_path.name
        parent_abs = system_root / parent_rel
        # Detect unreadable ancestor directories (e.g. mode 0700 /etc/wireguard)
        cur = system_root
        blocked = False
        for part in parent_rel.parts:
            cur = cur / part
            if os.path.lexists(cur):
                if cur.is_dir() and not os.access(cur, os.R_OK | os.X_OK):
                    unreadable.append('/' + str(cur.relative_to(system_root)))
                    blocked = True
                    break
            else:
                blocked = True
                break
        if blocked or not parent_abs.is_dir():
            continue
        try:
            entries = sorted(os.listdir(parent_abs))
        except PermissionError:
            unreadable.append('/' + str(parent_rel))
            continue
        for entry_name in entries:
            if not fnmatch.fnmatch(entry_name, name_pat):
                continue
            candidate = parent_abs / entry_name
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not _is_within(resolved, system_root):
                return {'ok': False, 'error': f'Host config path escapes system root: {pattern}'}
            if not candidate.is_file():
                continue
            rel = str(candidate.relative_to(system_root))
            try:
                raw = candidate.read_bytes()
            except PermissionError:
                unreadable.append('/' + rel)
                continue
            files[rel] = base64.b64encode(raw).decode('ascii')

    fw_outputs: dict[str, str] = {}
    runtime_fw = req.get('runtime_firewall')
    if runtime_fw:
        cmds = (
            {'LIVE_IPTABLES_SAVE': ['iptables-save'], 'LIVE_IP6TABLES_SAVE': ['ip6tables-save']}
            if runtime_fw == 'iptables'
            else {'LIVE_NFT_RULESET': ['nft', 'list', 'ruleset']}
        )
        for key, cmd in cmds.items():
            try:
                proc = subprocess.run(cmd, capture_output=True, check=False)
            except OSError as exc:
                return {'ok': False, 'error': f'Runtime firewall tool {cmd[0]} unavailable: {exc.strerror or exc}'}
            if proc.returncode != 0:
                return {'ok': False, 'error': f'Runtime firewall command {cmd[0]} failed (exit {proc.returncode}); rerun with --sudo if root privileges are required'}
            fw_outputs[key] = base64.b64encode(proc.stdout).decode('ascii')

    return {
        'ok': True,
        'files': files,
        'unreadable': sorted(set(unreadable)),
        'runtime_firewall_outputs': fw_outputs,
    }


def handle_read_files(req: dict) -> dict:
    source_root = Path(req['source_root'])
    if not source_root.is_dir():
        return {'ok': False, 'error': f'Source root is not an existing directory on remote host: {source_root}'}
    results: dict[str, dict] = {}
    for rel in req.get('paths', []):
        try:
            target = resolve_inside(source_root, rel, reject_symlinks=True)
        except ValueError as exc:
            results[rel] = {'status': 'symlink_error' if 'symlink' in str(exc).lower() else 'escape_error'}
            continue
        if not os.path.lexists(target):
            results[rel] = {'status': 'missing'}
            continue
        if not target.is_file():
            results[rel] = {'status': 'not_a_file'}
            continue
        try:
            raw = target.read_bytes()
            st = target.stat()
            results[rel] = {
                'status': 'found',
                'b64': base64.b64encode(raw).decode('ascii'),
                'sha256': sha256_bytes(raw),
                'mode': stat.S_IMODE(st.st_mode),
            }
        except PermissionError:
            results[rel] = {'status': 'permission_denied'}
    return {'ok': True, 'files': results}


def handle_preflight(req: dict) -> dict:
    audit_res = handle_audit(req)
    if not audit_res.get('ok'):
        return audit_res
    docker_sudo = bool(req.get('docker_sudo', False))
    source_root = Path(req['source_root']).resolve()
    rollback_dir = Path(req['rollback_dir'])
    try:
        ensure_disjoint_rollback_dir(rollback_dir, source_root)
    except ValueError as exc:
        return {'ok': False, 'error': str(exc)}

    missing_images = []
    for img in req.get('check_images', []):
        if not img or img.startswith('-'):
            missing_images.append(str(img))
            continue
        proc = subprocess.run(
            docker_cmd(docker_sudo, 'image', 'inspect', '--', img),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            missing_images.append(img)

    missing_volumes = []
    for vol in req.get('check_volumes', []):
        if not vol or vol.startswith('-'):
            missing_volumes.append(str(vol))
            continue
        proc = subprocess.run(
            docker_cmd(docker_sudo, 'volume', 'inspect', '--', vol),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            missing_volumes.append(vol)

    unwritable_dirs = []
    for d in req.get('writable_dirs', []):
        probe = Path(d)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if not probe.is_dir() or not os.access(probe, os.W_OK | os.X_OK):
            unwritable_dirs.append(d)

    rollback_exists = os.path.lexists(rollback_dir)
    rb_probe = rollback_dir
    while not rb_probe.exists() and rb_probe.parent != rb_probe:
        rb_probe = rb_probe.parent
    rollback_writable = rb_probe.is_dir() and os.access(rb_probe, os.W_OK | os.X_OK)

    audit_res.update({
        'missing_images': missing_images,
        'missing_volumes': missing_volumes,
        'unwritable_dirs': unwritable_dirs,
        'rollback_exists': rollback_exists,
        'rollback_writable': rollback_writable,
    })
    return audit_res


def load_raw_env_file(path: Path, secrets: set[str]) -> dict[str, str]:
    st = path.stat()
    if st.st_mode & 0o077:
        raise ValueError('Materialized secret environment file must have mode 0600 or stricter')
    values: dict[str, str] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line:
            continue
        key, sep, val = line.partition('=')
        if not sep or not KEY_RE.fullmatch(key) or key.startswith('COMPOSE_') or key in values:
            raise ValueError('Invalid materialized secret environment file')
        values[key] = val
        if val:
            secrets.add(val)
    return values


def _wait_services_healthy(
    docker_sudo: bool,
    project_name: str,
    services: list[str],
    timeout_sec: float,
) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        audit = run_docker_audit(docker_sudo)
        if audit['errors']:
            return False, f'Docker inspection failed while waiting for health: {"; ".join(audit["errors"])}'
        by_svc = {
            c.get('service'): c
            for c in audit['containers']
            if c.get('project') == project_name
        }
        pending = []
        for svc in services:
            c = by_svc.get(svc)
            if not c or not c.get('running'):
                return False, f'Dependency service {svc} is not running'
            if c.get('restarting'):
                return False, f'Dependency service {svc} is crash-looping (restarting)'
            health = str(c.get('health') or '').lower()
            if health == 'unhealthy':
                return False, f'Dependency service {svc} reported unhealthy'
            if health == 'starting':
                pending.append(svc)
        if not pending:
            return True, ''
        if time.monotonic() >= deadline:
            return False, f'Timed out waiting for healthy status on service(s): {", ".join(pending)}'
        time.sleep(1.0)


def _remove_new_service_containers(
    state: dict,
    docker_sudo: bool,
    secrets: set[str],
) -> tuple[list[str], list[str]]:
    """Stop and remove ONLY newly added service containers (never volumes)."""
    new_services = [
        validate_identifier(s, SERVICE_NAME_RE, 'service name')
        for s in (state.get('new_services') or [])
    ]
    if not new_services:
        return [], []

    removed: list[str] = []
    errors: list[str] = []
    project_name = validate_identifier(state['project_name'], PROJECT_NAME_RE, 'project_name')
    remote_stack_dir = state['remote_stack_dir']
    compose_home = state.get('compose_home')
    applied_compose_files = state.get('applied_compose_files') or []

    # First attempt targeted `docker compose rm -s -f -- <new_services...>` while applied files still exist
    compose_rm_ok = False
    if applied_compose_files and all(
        (Path(cf) if Path(cf).is_absolute() else (Path(remote_stack_dir) / cf)).is_file()
        for cf in applied_compose_files
    ):
        env_updates: dict[str, str] = {}
        primary_env = state.get('primary_env_file')
        if primary_env and Path(primary_env).is_file():
            try:
                env_updates = load_raw_env_file(Path(primary_env), secrets)
            except Exception:
                env_updates = {}
        compose_args = ['-p', project_name]
        for cf in applied_compose_files:
            compose_args.extend(['-f', cf])
        compose_args.extend(['rm', '-s', '-f', '--'] + new_services)
        proc = run_compose(docker_sudo, compose_args, remote_stack_dir, env_updates, compose_home)
        if proc.returncode == 0:
            compose_rm_ok = True
            removed.extend(new_services)
        else:
            detail = redact((proc.stderr or proc.stdout or '').strip(), secrets)
            errors.append(f'compose rm -sf for new services failed (exit {proc.returncode}): {detail}')

    # Fallback: remove remaining containers matching project + service in new_services directly (no -v!)
    if not compose_rm_ok:
        audit = run_docker_audit(docker_sudo)
        fallback_ok = True
        for c in audit.get('containers', []):
            if c.get('project') == project_name and c.get('service') in new_services:
                cid = c.get('id') or c.get('name')
                if not cid:
                    continue
                rm = subprocess.run(
                    docker_cmd(docker_sudo, 'rm', '-f', '--', str(cid)),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if rm.returncode == 0:
                    removed.append(str(c.get('service')))
                else:
                    fallback_ok = False
                    detail = redact((rm.stderr or rm.stdout or '').strip(), secrets)
                    errors.append(f'docker rm -f {c.get("service")} failed: {detail}')
        if fallback_ok:
            errors.clear()

    return sorted(set(removed)), errors


def _format_minimal_override_yaml(pinned_images: dict[str, str]) -> str:
    lines = ['# Temporary rollback image pin override generated by ssh_remote_agent.py', 'services:']
    for svc in sorted(pinned_images):
        val = json.dumps(pinned_images[svc])
        lines.append(f'  {svc}:')
        lines.append(f'    image: {val}')
    return '\n'.join(lines) + '\n'


def _restore_stack_state(
    state_path: Path,
    docker_sudo: bool,
    force_compose_restore: bool,
    secrets: set[str],
) -> dict:
    report: dict = {
        'ok': True,
        'restored_files': [],
        'removed_files': [],
        'removed_new_services': [],
        'compose_restored': False,
        'errors': [],
    }
    if not state_path.is_file():
        report['ok'] = False
        report['errors'].append(f'Rollback state not found: {state_path}')
        return report

    state = json.loads(state_path.read_text(encoding='utf-8'))
    stack_rb_dir = state_path.parent.resolve()
    remote_root = Path(state['remote_root']).resolve()
    ensure_disjoint_rollback_dir(stack_rb_dir, remote_root)
    project_name = validate_identifier(state['project_name'], PROJECT_NAME_RE, 'project_name')
    remote_stack_dir = resolve_inside(remote_root, state['remote_stack_dir'], reject_symlinks=True)

    # 0. Harvest secrets from ALL backed-up files and existing created secret files BEFORE running anything
    for entry in state.get('backed_up_files', []):
        try:
            backup = resolve_inside(stack_rb_dir, entry['backup_rel'], reject_symlinks=True)
            orig_p = Path(entry.get('path', ''))
            if backup.is_file() and not is_compose_definition_path(orig_p):
                harvest_secret_values(backup.read_bytes(), secrets)
        except Exception:
            pass
    for entry in state.get('created_files', []):
        try:
            target = resolve_inside(remote_root, entry['path'], reject_symlinks=True)
            if target.is_file() and ('.secrets' in target.parts or target.name.endswith('.env')):
                harvest_secret_values(target.read_bytes(), secrets)
        except Exception:
            pass

    should_compose = bool(state.get('compose_up_attempted')) or bool(force_compose_restore)

    # 1. Stop & remove newly created service containers BEFORE deleting/restoring Compose files (never volumes!)
    if should_compose and state.get('new_services'):
        removed_svcs, rm_errs = _remove_new_service_containers(state, docker_sudo, secrets)
        report['removed_new_services'] = removed_svcs
        if rm_errs:
            report['ok'] = False
            report['errors'].extend(rm_errs)

    # 2. Remove newly created files
    for entry in state.get('created_files', []):
        raw_path = entry['path']
        try:
            target = resolve_inside(remote_root, raw_path, reject_symlinks=True)
            if os.path.lexists(target):
                target.unlink()
                report['removed_files'].append(str(target))
        except Exception as exc:
            report['ok'] = False
            report['errors'].append(f'Failed removing {raw_path}: {redact(str(exc), secrets)}')

    # Clean up empty created directories (e.g. .secrets)
    for dpath in sorted(state.get('created_dirs', []), key=len, reverse=True):
        try:
            d = resolve_inside(remote_root, dpath, reject_symlinks=True)
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass

    # 3. Restore backed-up files (both overwritten and archived legacy files)
    for entry in state.get('backed_up_files', []):
        raw_path = entry['path']
        orig_mode = int(entry.get('orig_mode', 0o600))
        try:
            target = resolve_inside(remote_root, raw_path, reject_symlinks=True)
            backup = resolve_inside(stack_rb_dir, entry['backup_rel'], reject_symlinks=True)
            raw = backup.read_bytes()
            if not is_compose_definition_path(target):
                harvest_secret_values(raw, secrets)
            atomic_write_bytes(target, raw, orig_mode)
            report['restored_files'].append(str(target))
        except Exception as exc:
            report['ok'] = False
            report['errors'].append(f'Failed restoring {raw_path}: {redact(str(exc), secrets)}')

    # 4. Restore compose state ONLY when compose up was attempted (preserve stopped & one-shot services!)
    orig_running = [
        validate_identifier(s, SERVICE_NAME_RE, 'service name')
        for s in (state.get('originally_running_services') or [])
    ]
    if should_compose and state.get('original_compose_files') and orig_running:
        # Pin original running image IDs if local tag no longer resolves to the exact original image ID
        rb_pins_needed: dict[str, str] = {}
        rb_pin_meta = state.get('rollback_pinned_images') or {}
        refs_to_check = [
            info['configured_image']
            for svc, info in rb_pin_meta.items()
            if svc in orig_running and info.get('configured_image') and not str(info['configured_image']).startswith('sha256:')
        ]
        resolved_orig = inspect_image_ids(docker_sudo, sorted(set(refs_to_check))) if refs_to_check else {}
        for svc in orig_running:
            info = rb_pin_meta.get(svc)
            if not info or not info.get('image_id'):
                continue
            cfg_img = str(info.get('configured_image') or '')
            if not cfg_img or cfg_img.startswith('sha256:') or resolved_orig.get(cfg_img) != info['image_id']:
                rb_pins_needed[svc] = info['image_id']

        rb_override_path: Path | None = None
        try:
            compose_base = ['-p', project_name]
            for cfile in state['original_compose_files']:
                compose_base.extend(['-f', cfile])
            if rb_pins_needed:
                rb_override_name = '.homelab-rollback.override.yml'
                rb_override_path = resolve_inside(remote_root, str(remote_stack_dir / rb_override_name), reject_symlinks=True)
                atomic_write_bytes(rb_override_path, _format_minimal_override_yaml(rb_pins_needed).encode('utf-8'), 0o600)
                compose_base.extend(['-f', rb_override_name])

            waves = state.get('rollback_start_waves') or [{'services': orig_running, 'wait_healthy': []}]
            compose_home = state.get('compose_home')
            all_waves_ok = True
            for wave in waves:
                w_svcs = [
                    validate_identifier(s, SERVICE_NAME_RE, 'service name')
                    for s in (wave.get('services') or [])
                    if s in orig_running
                ]
                if not w_svcs:
                    continue
                proc = run_compose(
                    docker_sudo,
                    compose_base + ['up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--'] + w_svcs,
                    str(remote_stack_dir),
                    env_updates={},
                    compose_home=compose_home,
                )
                if proc.returncode != 0:
                    all_waves_ok = False
                    report['ok'] = False
                    detail = redact((proc.stderr or proc.stdout or '').strip(), secrets)
                    report['errors'].append(f'Rollback compose up failed (exit {proc.returncode}): {detail}')
                    break
                wait_h = [s for s in (wave.get('wait_healthy') or []) if s in w_svcs]
                if wait_h:
                    ok_h, err_h = _wait_services_healthy(docker_sudo, project_name, wait_h, 30.0)
                    if not ok_h:
                        all_waves_ok = False
                        report['ok'] = False
                        report['errors'].append(f'Rollback health wait failed: {redact(err_h, secrets)}')
                        break
            if all_waves_ok:
                report['compose_restored'] = True
        finally:
            if rb_override_path is not None:
                rb_override_path.unlink(missing_ok=True)

    return report


def _verify_post_apply(
    docker_sudo: bool,
    stack: str,
    project_name: str,
    req: dict,
) -> tuple[bool, str, int]:
    expected_running = list(req.get('expected_running_services') or [])
    expected_stopped = list(req.get('expected_stopped_services') or [])
    oneshot_services = list(req.get('oneshot_services') or [])
    oneshot_before = dict(req.get('oneshot_before') or {})
    expected_images = dict(req.get('expected_service_images') or {})
    expected_mounts = dict(req.get('expected_service_mounts') or {})
    timeout_sec = float(req.get('health_wait_timeout', 30.0))

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        post_audit = run_docker_audit(docker_sudo)
        if post_audit['errors']:
            return False, f'Post-apply Docker inspection failed for {stack}: {"; ".join(post_audit["errors"])}', 0
        proj_containers = [c for c in post_audit['containers'] if c.get('project') == project_name]
        by_service = {c.get('service'): c for c in proj_containers}

        starting = []
        for svc in expected_running:
            c = by_service.get(svc)
            if not c or not c.get('running'):
                return False, f'Post-apply verification failed for {stack}: service {svc} is not running', len(proj_containers)
            if c.get('restarting'):
                return False, f'Post-apply verification failed for {stack}: service {svc} is restarting (crash-looping)', len(proj_containers)
            health = str(c.get('health') or '').lower()
            if health == 'unhealthy':
                return False, f'Post-apply verification failed for {stack}: service {svc} is unhealthy', len(proj_containers)
            if health == 'starting':
                starting.append(svc)
        if not starting:
            break
        if time.monotonic() >= deadline:
            return False, f'Post-apply verification timed out waiting for healthy status on {stack}: {", ".join(starting)}', len(proj_containers)
        time.sleep(1.0)

    # Verify image identity and mount/volume identity for every expected running service
    for svc in expected_running:
        c = by_service[svc]
        exp_img = expected_images.get(svc)
        if exp_img and c.get('image') != exp_img:
            return (
                False,
                f'Post-apply image mismatch for {stack}/{svc}: expected {exp_img}, got {c.get("image")}',
                len(proj_containers),
            )
        actual_mounts = c.get('mounts') or []
        by_dst = {
            posixpath.normpath(str(m.get('Destination') or '')): m
            for m in actual_mounts
            if m.get('Destination')
        }
        for em in expected_mounts.get(svc, []):
            dst = posixpath.normpath(str(em['destination']))
            am = by_dst.get(dst)
            if not am:
                return (
                    False,
                    f'Post-apply mount missing for {stack}/{svc}:{dst}',
                    len(proj_containers),
                )
            if em['type'] == 'volume':
                if am.get('Type') != 'volume' or am.get('Name') != em['name']:
                    return (
                        False,
                        f'Post-apply volume mismatch for {stack}/{svc}:{dst}: expected "{em["name"]}", got {am.get("Type")}:{am.get("Name")}',
                        len(proj_containers),
                    )
            elif em['type'] == 'bind':
                asrc = posixpath.normpath(str(am.get('Source') or ''))
                allowed_srcs = {posixpath.normpath(str(em['source']))}
                if em.get('source_realpath'):
                    allowed_srcs.add(posixpath.normpath(str(em['source_realpath'])))
                if am.get('Type') != 'bind' or asrc not in allowed_srcs:
                    return (
                        False,
                        f'Post-apply bind mount mismatch for {stack}/{svc}:{dst}: expected {sorted(allowed_srcs)}, got "{asrc}"',
                        len(proj_containers),
                    )

    # Verify originally stopped services remained stopped
    for svc in expected_stopped:
        c = by_service.get(svc)
        if c and c.get('running'):
            return False, f'Post-apply verification failed for {stack}: previously stopped service {svc} was started', len(proj_containers)

    # Verify one-shot services were NEVER started or recreated
    for svc in oneshot_services:
        c = by_service.get(svc)
        before = oneshot_before.get(svc) or {}
        if c and c.get('running'):
            return False, f'Post-apply verification failed for {stack}: one-shot service {svc} is running', len(proj_containers)
        if not before.get('present') and c is not None:
            return False, f'Post-apply verification failed for {stack}: pruned one-shot service {svc} was unexpectedly created', len(proj_containers)
        if before.get('present') and before.get('id') and c is not None and c.get('id') != before.get('id'):
            return False, f'Post-apply verification failed for {stack}: one-shot service {svc} container ID changed (was recreated)', len(proj_containers)

    return True, '', len(proj_containers)


def handle_apply_stack(req: dict, secrets: set[str]) -> dict:
    secrets.update(req.get('redact_hints', []))
    docker_sudo = bool(req.get('docker_sudo', False))
    remote_root = Path(req['remote_root']).resolve()
    remote_stack_dir = resolve_inside(remote_root, req['remote_stack_dir'], reject_symlinks=True)
    stack = validate_identifier(req['stack'], STACK_NAME_RE, 'stack')
    project_name = validate_identifier(req['project_name'], PROJECT_NAME_RE, 'project_name')
    rollback_dir = ensure_disjoint_rollback_dir(Path(req['rollback_dir']), remote_root)
    stacks_rb_root = rollback_dir / 'stacks'

    # Collect secret payloads for redaction and validate ALL paths (rejecting symlinks!) before touching disk
    staged_entries = []
    for item in req.get('staged_files', []):
        target = resolve_inside(remote_root, item['remote_path'], reject_symlinks=True)
        raw = base64.b64decode(item['b64'])
        if item.get('is_secret'):
            harvest_secret_values(raw, secrets)
            try:
                secrets.add(raw.decode('utf-8'))
            except UnicodeDecodeError:
                pass
        staged_entries.append((target, raw, int(item['mode']), bool(item.get('is_secret'))))

    override_filename = validate_identifier(req['override_filename'], FILENAME_RE, 'override_filename')
    override_path = resolve_inside(remote_root, str(remote_stack_dir / override_filename), reject_symlinks=True)
    use_override = bool(req.get('use_override', True))
    override_raw = base64.b64decode(req['override_b64']) if use_override else b''

    archive_paths = [resolve_inside(remote_root, p, reject_symlinks=True) for p in req.get('archive_legacy_files', [])]

    # Ensure protected rollback directory structure (0700)
    rollback_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(rollback_dir, 0o700)
    stacks_rb_root.mkdir(parents=True, exist_ok=True)
    os.chmod(stacks_rb_root, 0o700)
    stack_rb_dir = resolve_inside(stacks_rb_root, stack, reject_symlinks=True)
    files_rb_dir = stack_rb_dir / 'files'
    files_rb_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(stack_rb_dir, 0o700)
    os.chmod(files_rb_dir, 0o700)

    # Record backup metadata in index.json
    index_path = rollback_dir / 'index.json'
    index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {
        'plan_id': req.get('plan_id'),
        'host': req.get('host'),
        'remote_root': str(remote_root),
        'backup_evidence': req.get('backup_evidence'),
        'applied_stacks': [],
    }
    if stack not in index['applied_stacks']:
        index['applied_stacks'].append(stack)
    atomic_write_bytes(index_path, (json.dumps(index, indent=2) + '\n').encode('utf-8'), 0o600)

    # Phase 1: Backup every affected file BEFORE any modification!
    seen_targets: set[Path] = set()
    backed_up_files = []
    created_files = []
    created_dirs: list[str] = []
    backup_idx = 0

    all_targets = [t for t, _, _, _ in staged_entries] + ([override_path] if use_override else []) + archive_paths
    if not use_override and os.path.lexists(override_path):
        all_targets.append(override_path)

    for target in all_targets:
        if target in seen_targets:
            continue
        seen_targets.add(target)
        # Track directories we are about to create (e.g. .secrets)
        parent = target.parent
        while parent != remote_root and not parent.exists():
            if str(parent) not in created_dirs:
                created_dirs.append(str(parent))
            parent = parent.parent
        if os.path.lexists(target):
            if target.is_symlink():
                raise ValueError(f'Refusing symlink target: {target}')
            st = target.stat()
            orig_bytes = target.read_bytes()
            if not is_compose_definition_path(target):
                harvest_secret_values(orig_bytes, secrets)
            backup_rel = f'files/{backup_idx:04d}.bak'
            backup_idx += 1
            atomic_write_bytes(stack_rb_dir / backup_rel, orig_bytes, 0o600)
            backed_up_files.append({
                'path': str(target),
                'backup_rel': backup_rel,
                'orig_mode': stat.S_IMODE(st.st_mode),
            })
        else:
            created_files.append({'path': str(target)})

    compose_files = list(req['compose_files'])
    applied_compose_files = list(compose_files) + ([override_filename] if use_override else [])
    compose_home = req.get('compose_home') or str(Path.home().resolve())
    originally_running_services = list(req.get('originally_running_services') or req.get('expected_running_services') or [])
    originally_stopped_services = list(req.get('expected_stopped_services') or [])
    oneshot_services = list(req.get('oneshot_services') or [])
    new_services = list(req.get('new_services') or [])
    start_waves = list(req.get('start_waves') or [])
    if not start_waves and (originally_running_services or new_services):
        start_waves = [{'services': sorted(set(originally_running_services + new_services)), 'wait_healthy': []}]

    state = {
        'stack': stack,
        'project_name': project_name,
        'remote_root': str(remote_root),
        'remote_stack_dir': str(remote_stack_dir),
        'compose_home': compose_home,
        'original_compose_files': req.get('original_compose_files', []),
        'applied_compose_files': applied_compose_files,
        'use_override': use_override,
        'override_filename': override_filename,
        'primary_env_file': req.get('primary_env_file'),
        'originally_running_services': originally_running_services,
        'originally_stopped_services': originally_stopped_services,
        'oneshot_services': oneshot_services,
        'new_services': new_services,
        'rollback_pinned_images': req.get('rollback_pinned_images') or {},
        'rollback_start_waves': req.get('rollback_start_waves') or [],
        'backed_up_files': backed_up_files,
        'created_files': created_files,
        'created_dirs': created_dirs,
        'compose_up_attempted': False,
    }
    state_path = stack_rb_dir / 'state.json'
    atomic_write_bytes(state_path, (json.dumps(state, indent=2) + '\n').encode('utf-8'), 0o600)

    # Phase 2: Stage files atomically and archive legacy files
    try:
        for target, raw, mode, is_secret in staged_entries:
            atomic_write_bytes(target, raw, mode, private_parents=is_secret and target.parent.name == '.secrets')
        if use_override:
            atomic_write_bytes(override_path, override_raw, 0o600)
        elif os.path.lexists(override_path):
            override_path.unlink()
        for legacy in archive_paths:
            if os.path.lexists(legacy):
                legacy.unlink()
    except Exception as exc:
        rb = _restore_stack_state(state_path, docker_sudo, False, secrets)
        return {
            'ok': False,
            'phase': 'stage_files',
            'error': redact(f'Failed staging files for {stack}: {exc}', secrets),
            'local_rollback': rb,
        }

    # Phase 3: Load materialized .secrets.env (raw, NO shell eval!)
    env_updates: dict[str, str] = {}
    env_file_path = req.get('primary_env_file')
    if env_file_path:
        try:
            env_updates = load_raw_env_file(resolve_inside(remote_root, env_file_path, reject_symlinks=True), secrets)
        except Exception as exc:
            rb = _restore_stack_state(state_path, docker_sudo, False, secrets)
            return {
                'ok': False,
                'phase': 'load_env',
                'error': redact(f'Failed loading materialized env for {stack}: {exc}', secrets),
                'local_rollback': rb,
            }

    compose_base = ['-p', project_name]
    for cf in applied_compose_files:
        compose_base.extend(['-f', cf])

    # Phase 4: Quiet config validation BEFORE live up -d
    cfg = run_compose(docker_sudo, compose_base + ['config', '--quiet'], str(remote_stack_dir), env_updates, compose_home)
    if cfg.returncode != 0:
        rb = _restore_stack_state(state_path, docker_sudo, False, secrets)
        detail = redact((cfg.stderr or cfg.stdout or '').strip(), secrets)
        return {
            'ok': False,
            'phase': 'compose_config',
            'error': f'docker compose config --quiet failed for {stack} (exit {cfg.returncode}): {detail}',
            'local_rollback': rb,
        }

    # Phase 5: Execute explicit service-targeted waves with --no-deps (preserves stopped & one-shot services!)
    if start_waves:
        state['compose_up_attempted'] = True
        atomic_write_bytes(state_path, (json.dumps(state, indent=2) + '\n').encode('utf-8'), 0o600)

        for wave in start_waves:
            w_svcs = [validate_identifier(s, SERVICE_NAME_RE, 'service name') for s in (wave.get('services') or [])]
            if not w_svcs:
                continue
            up = run_compose(
                docker_sudo,
                compose_base + ['up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--'] + w_svcs,
                str(remote_stack_dir),
                env_updates,
                compose_home,
            )
            if up.returncode != 0:
                detail = redact((up.stderr or up.stdout or '').strip(), secrets)
                return {
                    'ok': False,
                    'phase': 'compose_up',
                    'error': f'docker compose up failed for {stack} (exit {up.returncode}): {detail}',
                }
            wait_h = list(wave.get('wait_healthy') or [])
            if wait_h:
                ok_h, err_h = _wait_services_healthy(
                    docker_sudo,
                    project_name,
                    wait_h,
                    float(req.get('health_wait_timeout', 60.0)),
                )
                if not ok_h:
                    return {
                        'ok': False,
                        'phase': 'compose_up_health',
                        'error': redact(f'Dependency health check failed for {stack}: {err_h}', secrets),
                    }

    # Phase 6: Post-apply verification (running/health/restarting/image/volumes/mounts/stopped/one-shot)
    ok_verify, err_verify, verified_count = _verify_post_apply(docker_sudo, stack, project_name, req)
    if not ok_verify:
        return {
            'ok': False,
            'phase': 'post_verify',
            'error': redact(err_verify, secrets),
        }

    return {
        'ok': True,
        'stack': stack,
        'rollback_state_path': str(state_path),
        'containers_verified': verified_count,
    }


def handle_rollback(req: dict, secrets: set[str]) -> dict:
    docker_sudo = bool(req.get('docker_sudo', False))
    rollback_dir = Path(req['rollback_dir']).resolve(strict=False)
    if not rollback_dir.is_dir():
        return {'ok': False, 'error': f'Rollback directory not found: {rollback_dir}'}
    stacks_rb_root = rollback_dir / 'stacks'
    index_path = rollback_dir / 'index.json'
    stacks = req.get('stacks')
    if not stacks:
        if not index_path.is_file():
            return {'ok': False, 'error': f'Rollback index not found: {index_path}'}
        index = json.loads(index_path.read_text(encoding='utf-8'))
        stacks = list(reversed(index.get('applied_stacks', [])))
    force_compose = bool(req.get('force_compose_restore', False))

    results = {}
    all_ok = True
    for raw_stack in stacks:
        try:
            stack = validate_identifier(raw_stack, STACK_NAME_RE, 'stack')
            stack_dir = resolve_inside(stacks_rb_root, stack, reject_symlinks=True)
        except ValueError as exc:
            all_ok = False
            results[str(raw_stack)] = {'ok': False, 'errors': [str(exc)]}
            continue
        state_path = stack_dir / 'state.json'
        if not state_path.is_file():
            results[stack] = {'ok': True, 'skipped': True, 'reason': 'stack was not modified'}
            continue
        res = _restore_stack_state(state_path, docker_sudo, force_compose, secrets)
        results[stack] = res
        if not res['ok']:
            all_ok = False

    if not all_ok:
        err_parts = []
        for s, r in results.items():
            if not r.get('ok'):
                errs = '; '.join(r.get('errors') or ['rollback failed'])
                err_parts.append(f'{s}: {errs}')
        return {
            'ok': False,
            'error': redact('Rollback failed: ' + ' | '.join(err_parts), secrets),
            'results': results,
        }
    return {'ok': True, 'results': results}


def main() -> int:
    active_secrets: set[str] = set()
    try:
        req = json.load(sys.stdin)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': f'Invalid remote request JSON: {exc}'}))
        return 1
    action = req.get('action')
    if action == 'apply_stack':
        try:
            response = handle_apply_stack(req, active_secrets)
        except Exception as exc:
            msg = redact(f'Remote helper failed: {type(exc).__name__}: {exc}', active_secrets)
            print(json.dumps({'ok': False, 'error': msg}))
            return 1
        print(json.dumps(response))
        return 0
    if action == 'rollback':
        try:
            response = handle_rollback(req, active_secrets)
        except Exception as exc:
            msg = redact(f'Remote helper failed: {type(exc).__name__}: {exc}', active_secrets)
            print(json.dumps({'ok': False, 'error': msg}))
            return 1
        print(json.dumps(response))
        return 0

    handlers = {
        'audit': handle_audit,
        'inspect_paths': handle_inspect_paths,
        'capture_host': handle_capture_host,
        'read_files': handle_read_files,
        'preflight': handle_preflight,
    }
    handler = handlers.get(action)
    if not handler:
        print(json.dumps({'ok': False, 'error': f'Unknown remote action: {action}'}))
        return 1
    try:
        response = handler(req)
    except Exception as exc:
        msg = redact(f'Remote helper failed: {type(exc).__name__}: {exc}', active_secrets)
        print(json.dumps({'ok': False, 'error': msg}))
        return 1
    print(json.dumps(response))
    return 0


if __name__ == '__main__':
    sys.exit(main())
