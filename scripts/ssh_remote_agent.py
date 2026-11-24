#!/usr/bin/env python3
"""Stdlib-only remote helper executed over SSH by scripts/ssh-migrate.py.

Receives a single JSON request on stdin and emits a single JSON response on stdout.
Never requires Git, SOPS, private age keys, or third-party Python packages on the remote host.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile

KEY_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

DOCKER_FORMAT = (
    '{"name":{{json .Name}},"image":{{json .Image}},"configured_image":{{json .Config.Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '"config_files":{{json (index .Config.Labels "com.docker.compose.project.config_files")}},'
    '"mounts":{{json .Mounts}},"port_bindings":{{json .HostConfig.PortBindings}},'
    '"networks":{{json .NetworkSettings.Networks}},"running":{{json .State.Running}}}'
)

CONTAINER_FIELDS = (
    'name', 'image', 'configured_image', 'project', 'service',
    'working_dir', 'config_files', 'mounts', 'port_bindings', 'networks', 'running',
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


def resolve_inside(root: Path, rel_or_abs: str) -> Path:
    root_real = root.resolve()
    raw = Path(rel_or_abs)
    candidate = raw if raw.is_absolute() else (root_real / raw)
    # Walk from candidate up to first existing ancestor to catch escaping symlinks
    probe = candidate
    tail: list[str] = []
    while not probe.exists() and not probe.is_symlink():
        if probe.parent == probe:
            break
        tail.append(probe.name)
        probe = probe.parent
    resolved_base = probe.resolve()
    resolved = resolved_base.joinpath(*reversed(tail)) if tail else resolved_base
    if not resolved.is_relative_to(root_real):
        raise ValueError(f'Path escapes allowed root: {rel_or_abs}')
    return resolved


def docker_cmd(docker_sudo: bool, *args: str) -> list[str]:
    prefix = ['sudo', '-n', '--', 'docker'] if docker_sudo else ['docker']
    return prefix + list(args)


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


def run_docker_audit(docker_sudo: bool) -> dict:
    result: dict = {
        'compose_version': None,
        'containers': [],
        'images': {},
        'volumes': {},
        'networks': [],
        'errors': [],
    }
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
            docker_cmd(docker_sudo, 'inspect', '--format', DOCKER_FORMAT, cid),
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
        result['containers'].append(safe)
        if safe.get('image'):
            image_ids.add(safe['image'])

    for img_id in sorted(image_ids):
        img_insp = subprocess.run(
            docker_cmd(docker_sudo, 'image', 'inspect', '--format', '{{json .RepoDigests}}', img_id),
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
                    '{"name":{{json .Name}},"driver":{{json .Driver}},"mountpoint":{{json .Mountpoint}}}',
                    vname,
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            if vinsp.returncode == 0:
                try:
                    result['volumes'][vname] = json.loads(vinsp.stdout)
                except ValueError:
                    result['volumes'][vname] = {'name': vname}
            else:
                result['volumes'][vname] = {'name': vname}
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
    for rel in rel_paths:
        try:
            target = resolve_inside(source_root, rel)
        except ValueError:
            probed[rel] = {'exists': False, 'escape_error': True}
            continue
        if not target.exists() and not target.is_symlink():
            probed[rel] = {'exists': False, 'is_file': False, 'readable': False, 'permission_denied': False}
            continue
        try:
            st = target.stat()
            is_file = stat.S_ISREG(st.st_mode)
            is_dir = stat.S_ISDIR(st.st_mode)
            info: dict = {
                'exists': True,
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
            probed[rel] = {'exists': True, 'permission_denied': True, 'readable': False}
    return probed


def check_abs_paths(paths: list[str]) -> dict:
    out = {}
    for raw in paths:
        p = Path(raw)
        if not p.exists() and not p.is_symlink():
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
    return {
        'ok': True,
        'hostname': socket.gethostname(),
        'home_dir': str(Path.home().resolve()),
        'source_root_realpath': str(source_root.resolve()),
        'compose_version': docker_info['compose_version'],
        'containers': docker_info['containers'],
        'images': docker_info['images'],
        'volumes': docker_info['volumes'],
        'networks': docker_info['networks'],
        'network': net_info,
        'probed_files': probed,
        'checked_paths': checked,
        'errors': docker_info['errors'] + net_errors,
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
            if not resolved.is_relative_to(system_root):
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
            target = resolve_inside(source_root, rel)
        except ValueError:
            results[rel] = {'status': 'escape_error'}
            continue
        if not target.exists() and not target.is_symlink():
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
    missing_images = []
    for img in req.get('check_images', []):
        proc = subprocess.run(
            docker_cmd(docker_sudo, 'image', 'inspect', img),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            missing_images.append(img)

    missing_volumes = []
    for vol in req.get('check_volumes', []):
        proc = subprocess.run(
            docker_cmd(docker_sudo, 'volume', 'inspect', vol),
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
        if not os.access(probe, os.W_OK):
            unwritable_dirs.append(d)

    rollback_dir = Path(req['rollback_dir'])
    rollback_exists = rollback_dir.exists()
    rb_probe = rollback_dir
    while not rb_probe.exists() and rb_probe.parent != rb_probe:
        rb_probe = rb_probe.parent
    rollback_writable = os.access(rb_probe, os.W_OK)

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


def _restore_stack_state(state_path: Path, docker_sudo: bool, force_compose_restore: bool, secrets: set[str]) -> dict:
    report: dict = {
        'ok': True,
        'restored_files': [],
        'removed_files': [],
        'compose_restored': False,
        'errors': [],
    }
    if not state_path.is_file():
        report['ok'] = False
        report['errors'].append(f'Rollback state not found: {state_path}')
        return report

    state = json.loads(state_path.read_text(encoding='utf-8'))
    stack_rb_dir = state_path.parent
    remote_root = Path(state['remote_root']).resolve()

    # 1. Remove newly created files
    for entry in state.get('created_files', []):
        target = Path(entry['path'])
        try:
            resolve_inside(remote_root, str(target))
            if target.exists() or target.is_symlink():
                target.unlink()
                report['removed_files'].append(str(target))
        except Exception as exc:
            report['ok'] = False
            report['errors'].append(f'Failed removing {target}: {redact(str(exc), secrets)}')

    # Clean up empty .secrets directories that did not exist before apply
    for dpath in sorted(state.get('created_dirs', []), key=len, reverse=True):
        d = Path(dpath)
        try:
            resolve_inside(remote_root, str(d))
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass

    # 2. Restore backed-up files (both overwritten and archived legacy files)
    for entry in state.get('backed_up_files', []):
        target = Path(entry['path'])
        backup = stack_rb_dir / entry['backup_rel']
        orig_mode = int(entry.get('orig_mode', 0o600))
        try:
            resolve_inside(remote_root, str(target))
            raw = backup.read_bytes()
            atomic_write_bytes(target, raw, orig_mode)
            report['restored_files'].append(str(target))
        except Exception as exc:
            report['ok'] = False
            report['errors'].append(f'Failed restoring {target}: {redact(str(exc), secrets)}')

    # 3. Restore compose state if compose up was attempted or explicit rollback was requested
    should_compose = bool(state.get('compose_up_attempted')) or force_compose_restore
    if should_compose and state.get('original_compose_files'):
        cmd = docker_cmd(docker_sudo, 'compose', '-p', state['project_name'])
        for cfile in state['original_compose_files']:
            cmd.extend(['-f', cfile])
        cmd.extend(['up', '-d', '--no-build', '--pull', 'never'])
        proc = subprocess.run(
            cmd,
            cwd=state['remote_stack_dir'],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            report['compose_restored'] = True
        else:
            report['ok'] = False
            detail = redact((proc.stderr or proc.stdout or '').strip(), secrets)
            report['errors'].append(f'Rollback compose up failed (exit {proc.returncode}): {detail}')

    return report


def handle_apply_stack(req: dict) -> dict:
    secrets: set[str] = set(req.get('redact_hints', []))
    docker_sudo = bool(req.get('docker_sudo', False))
    remote_root = Path(req['remote_root']).resolve()
    remote_stack_dir = resolve_inside(remote_root, req['remote_stack_dir'])
    stack = req['stack']
    rollback_dir = Path(req['rollback_dir'])
    stack_rb_dir = rollback_dir / 'stacks' / stack
    files_rb_dir = stack_rb_dir / 'files'

    # Ensure protected rollback directory structure (0700)
    rollback_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(rollback_dir, 0o700)
    (rollback_dir / 'stacks').mkdir(parents=True, exist_ok=True)
    os.chmod(rollback_dir / 'stacks', 0o700)
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

    # Collect secret payloads for redaction and validate all paths before touching disk
    staged_entries = []
    for item in req.get('staged_files', []):
        target = resolve_inside(remote_root, item['remote_path'])
        raw = base64.b64decode(item['b64'])
        if item.get('is_secret'):
            try:
                text_val = raw.decode('utf-8')
                secrets.add(text_val)
                for line in text_val.splitlines():
                    if '=' in line:
                        _, _, v = line.partition('=')
                        if v:
                            secrets.add(v)
            except UnicodeDecodeError:
                pass
        staged_entries.append((target, raw, int(item['mode']), bool(item.get('is_secret'))))

    override_filename = req['override_filename']
    override_path = resolve_inside(remote_root, str(remote_stack_dir / override_filename))
    override_raw = base64.b64decode(req['override_b64'])

    archive_paths = [resolve_inside(remote_root, p) for p in req.get('archive_legacy_files', [])]

    # Phase 1: Backup every affected file BEFORE any modification!
    seen_targets: set[Path] = set()
    backed_up_files = []
    created_files = []
    created_dirs: list[str] = []
    backup_idx = 0

    all_targets = [t for t, _, _, _ in staged_entries] + [override_path] + archive_paths
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
        if target.exists():
            st = target.stat()
            orig_bytes = target.read_bytes()
            backup_rel = f'files/{backup_idx:04d}'
            backup_idx += 1
            atomic_write_bytes(stack_rb_dir / backup_rel, orig_bytes, 0o600)
            backed_up_files.append({
                'path': str(target),
                'backup_rel': backup_rel,
                'orig_mode': stat.S_IMODE(st.st_mode),
            })
        else:
            created_files.append({'path': str(target)})

    state = {
        'stack': stack,
        'project_name': req['project_name'],
        'remote_root': str(remote_root),
        'remote_stack_dir': str(remote_stack_dir),
        'original_compose_files': req.get('original_compose_files', []),
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
        atomic_write_bytes(override_path, override_raw, 0o600)
        for legacy in archive_paths:
            if legacy.exists() or legacy.is_symlink():
                legacy.unlink()
    except Exception as exc:
        rb = _restore_stack_state(state_path, docker_sudo, False, secrets)
        return {
            'ok': False,
            'phase': 'stage_files',
            'error': redact(f'Failed staging files for {stack}: {exc}', secrets),
            'local_rollback': rb,
        }

    # Phase 3: Build Compose process environment from materialized .secrets.env (raw, NO shell eval!)
    env = dict(os.environ)
    env_file_path = req.get('primary_env_file')
    if env_file_path:
        try:
            env.update(load_raw_env_file(resolve_inside(remote_root, env_file_path), secrets))
        except Exception as exc:
            rb = _restore_stack_state(state_path, docker_sudo, False, secrets)
            return {
                'ok': False,
                'phase': 'load_env',
                'error': redact(f'Failed loading materialized env for {stack}: {exc}', secrets),
                'local_rollback': rb,
            }

    compose_base = docker_cmd(docker_sudo, 'compose', '-p', req['project_name'])
    for cf in req['compose_files']:
        compose_base.extend(['-f', cf])
    compose_base.extend(['-f', override_filename])

    # Phase 4: Quiet config validation BEFORE live up -d
    cfg = subprocess.run(
        compose_base + ['config', '--quiet'],
        cwd=str(remote_stack_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if cfg.returncode != 0:
        rb = _restore_stack_state(state_path, docker_sudo, False, secrets)
        detail = redact((cfg.stderr or cfg.stdout or '').strip(), secrets)
        return {
            'ok': False,
            'phase': 'compose_config',
            'error': f'docker compose config --quiet failed for {stack} (exit {cfg.returncode}): {detail}',
            'local_rollback': rb,
        }

    # Phase 5: Mark compose_up_attempted and execute in-place up -d --no-build --pull never
    state['compose_up_attempted'] = True
    atomic_write_bytes(state_path, (json.dumps(state, indent=2) + '\n').encode('utf-8'), 0o600)

    up = subprocess.run(
        compose_base + ['up', '-d', '--no-build', '--pull', 'never'],
        cwd=str(remote_stack_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if up.returncode != 0:
        detail = redact((up.stderr or up.stdout or '').strip(), secrets)
        return {
            'ok': False,
            'phase': 'compose_up',
            'error': f'docker compose up failed for {stack} (exit {up.returncode}): {detail}',
        }

    # Phase 6: Post-apply verification (container running state + volume identity)
    post_audit = run_docker_audit(docker_sudo)
    proj_containers = [c for c in post_audit['containers'] if c.get('project') == req['project_name']]
    by_service = {c.get('service'): c for c in proj_containers}
    for svc in req.get('expected_running_services', []):
        c = by_service.get(svc)
        if not c or not c.get('running'):
            return {
                'ok': False,
                'phase': 'post_verify',
                'error': f'Post-apply verification failed for {stack}: service {svc} is not running',
            }

    return {
        'ok': True,
        'stack': stack,
        'rollback_state_path': str(state_path),
        'containers_verified': len(proj_containers),
    }


def handle_rollback(req: dict) -> dict:
    docker_sudo = bool(req.get('docker_sudo', False))
    rollback_dir = Path(req['rollback_dir'])
    secrets: set[str] = set()
    index_path = rollback_dir / 'index.json'
    stacks = req.get('stacks')
    if not stacks:
        if not index_path.is_file():
            return {'ok': False, 'error': f'Rollback index not found: {index_path}'}
        index = json.loads(index_path.read_text(encoding='utf-8'))
        stacks = list(reversed(index.get('applied_stacks', [])))
    force_compose = bool(req.get('force_compose_restore', True))

    results = {}
    all_ok = True
    for stack in stacks:
        state_path = rollback_dir / 'stacks' / stack / 'state.json'
        if not state_path.is_file():
            results[stack] = {'ok': True, 'skipped': True, 'reason': 'stack was not modified'}
            continue
        res = _restore_stack_state(state_path, docker_sudo, force_compose, secrets)
        results[stack] = res
        if not res['ok']:
            all_ok = False
    return {'ok': all_ok, 'results': results}


def main() -> int:
    try:
        req = json.load(sys.stdin)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': f'Invalid remote request JSON: {exc}'}))
        return 1
    action = req.get('action')
    handlers = {
        'audit': handle_audit,
        'capture_host': handle_capture_host,
        'read_files': handle_read_files,
        'preflight': handle_preflight,
        'apply_stack': handle_apply_stack,
        'rollback': handle_rollback,
    }
    handler = handlers.get(action)
    if not handler:
        print(json.dumps({'ok': False, 'error': f'Unknown remote action: {action}'}))
        return 1
    try:
        response = handler(req)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': f'Remote helper failed: {type(exc).__name__}: {exc}'}))
        return 1
    print(json.dumps(response))
    return 0


if __name__ == '__main__':
    sys.exit(main())
