"""End-to-end tests for controller-side SSH migration without a real production host."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'scripts/ssh-migrate.py'
AGENT_SCRIPT = REPO_ROOT / 'scripts/ssh_remote_agent.py'

def _load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

ssh_migrate = _load_mod('ssh_migrate_mod', SCRIPT)
ssh_remote_agent = _load_mod('ssh_remote_agent_mod', AGENT_SCRIPT)

def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding='utf-8')
    path.chmod(0o755)


def _find_real_binary(name, glob_pattern):
    candidates = []
    base = Path('/opt/mise/installs')
    if base.is_dir():
        for candidate in reversed(sorted(base.glob(glob_pattern))):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                candidates.append(str(candidate))
    found = shutil.which(name)
    if found:
        candidates.append(found)
    for cand in candidates:
        probe = subprocess.run([cand, '--version'], capture_output=True, text=True, check=False)
        if probe.returncode == 0:
            return cand
    return None


REAL_SOPS = _find_real_binary('sops', 'github-getsops-sops/*/sops*')
REAL_AGE_KEYGEN = _find_real_binary('age-keygen', 'github-filo-sottile-age/*/age-keygen')
REAL_COMPOSE = _find_real_binary('docker-compose', 'docker-compose/*/docker-compose')


FAKE_SSH_PY = r'''#!/usr/bin/env python3
"""Fake ssh binary that validates strict options and runs the remote command locally."""
import json
import os
from pathlib import Path
import subprocess
import sys

log_path = Path(os.environ['FAKE_SSH_LOG'])
argv = sys.argv[1:]
with log_path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'argv': argv}) + '\n')

opts = []
i = 0
target = None
remote_cmd = None
while i < len(argv):
    arg = argv[i]
    if arg == '--':
        if i + 2 >= len(argv):
            print('fake-ssh: missing target or command after --', file=sys.stderr)
            sys.exit(255)
        target = argv[i + 1]
        remote_cmd = ' '.join(argv[i + 2:])
        break
    elif arg == '-o':
        opts.append(argv[i + 1])
        i += 2
    elif arg in ('-F', '-i', '-p'):
        i += 2
    else:
        print(f'fake-ssh: unexpected option before --: {arg}', file=sys.stderr)
        sys.exit(255)

if target is None or remote_cmd is None:
    print('fake-ssh: missing -- separator', file=sys.stderr)
    sys.exit(255)
if target.startswith('-'):
    print('fake-ssh: refusing option-like target', file=sys.stderr)
    sys.exit(255)
required_opts = {'BatchMode=yes', 'StrictHostKeyChecking=yes', 'PasswordAuthentication=no'}
if not required_opts.issubset(set(opts)):
    print(f'fake-ssh: missing required ssh options: {required_opts - set(opts)}', file=sys.stderr)
    sys.exit(255)

env = dict(os.environ)
env['HOME'] = os.environ['FAKE_REMOTE_HOME']
env['PATH'] = os.environ['FAKE_REMOTE_BIN'] + os.pathsep + os.environ.get('PATH', '')
# Ensure remote process does NOT inherit controller SOPS keys!
for key in list(env):
    if key.startswith('SOPS_'):
        env.pop(key, None)

drop_uid = os.environ.get('FAKE_SSH_DROP_UID')
run_kwargs = {'env': env}
if drop_uid and os.geteuid() == 0:
    run_kwargs['user'] = int(drop_uid)
    run_kwargs['group'] = int(os.environ.get('FAKE_SSH_DROP_GID', drop_uid))
proc = subprocess.run(['/bin/sh', '-c', remote_cmd], **run_kwargs)
sys.exit(proc.returncode)
'''


FAKE_SUDO_PY = r'''#!/usr/bin/env python3
"""Fake sudo -n shim simulating real Linux Defaults env_reset and HOME=/root."""
import json
import os
from pathlib import Path
import subprocess
import sys

log_path = Path(os.environ['FAKE_SUDO_LOG'])
argv = sys.argv[1:]
with log_path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'argv': argv, 'caller_env_keys': sorted(os.environ.keys())}) + '\n')

if os.environ.get('FAKE_SUDO_FAIL') == '1':
    print('sudo: a password is required', file=sys.stderr)
    sys.exit(1)

if not argv or argv[0] != '-n':
    print('fake-sudo: expected -n flag', file=sys.stderr)
    sys.exit(1)
rest = argv[1:]
if rest and rest[0] == '--':
    rest = rest[1:]
if rest and rest[0] == '-E':
    print('fake-sudo: refusing broad sudo -E', file=sys.stderr)
    sys.exit(1)

# Simulate Defaults env_reset: strip all non-allowlisted vars & reset HOME to root's home!
ALLOWLIST = {
    'PATH', 'LANG', 'LC_ALL', 'TERM',
    'FAKE_DOCKER_STATE', 'FAKE_DOCKER_LOG', 'FAKE_SUDO_LOG',
    'FAKE_SUDO_FAIL', 'FAKE_SUDO_HOME', 'FAKE_UNREADABLE_UNLOCK',
    'FAKE_SSH_LOG', 'FAKE_SOPS_CAPTURE_DIR', 'FAKE_REMOTE_BIN', 'FAKE_REMOTE_HOME',
    'DOCKER_HOST',
}
env = {k: v for k, v in os.environ.items() if k in ALLOWLIST}
env['HOME'] = os.environ.get('FAKE_SUDO_HOME', '/root')
env['FAKE_SUDO_ACTIVE'] = '1'
if os.environ.get('FAKE_UNREADABLE_UNLOCK'):
    for item in os.environ['FAKE_UNREADABLE_UNLOCK'].split(os.pathsep):
        if item:
            p = Path(item)
            if p.exists():
                p.chmod(0o700 if p.is_dir() else 0o600)
proc = subprocess.run(rest, env=env)
sys.exit(proc.returncode)
'''


FAKE_DOCKER_PY = r'''#!/usr/bin/env python3
"""Stateful fake docker/compose CLI simulating Compose v2.40+, --no-deps, rm -sf, labels, health, and interpolation."""
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import yaml

state_path = Path(os.environ['FAKE_DOCKER_STATE'])
log_path = Path(os.environ['FAKE_DOCKER_LOG'])
REQ_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(:\?|\?)([^}]*)\}')
VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?(?::\?[^}]*|\?[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)')


def load_state():
    return json.loads(state_path.read_text(encoding='utf-8'))


def save_state(state):
    state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')


def log_call(entry):
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry) + '\n')


def interp(text, env_map):
    def repl(m):
        name = m.group(1) or m.group(4)
        op = m.group(2)
        default = m.group(3)
        if name in env_map:
            val = env_map[name]
            if op == ':-' and val == '':
                return default or ''
            return val
        if op in (':-', '-'):
            return default or ''
        return ''
    return VAR_RE.sub(repl, text)


def load_dotenv_fallback(cwd):
    env_map = {}
    dotenv = cwd / '.env'
    if dotenv.is_file():
        for line in dotenv.read_text(encoding='utf-8', errors='ignore').splitlines():
            s = line.strip()
            if not s or s.startswith('#') or '=' not in s:
                continue
            k, _, v = s.partition('=')
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            env_map[k.strip()] = v
    env_map.update(os.environ)
    return env_map


def resolve_image_id(state, ref):
    images = state.get('images', {})
    if ref in images:
        return ref
    tags = state.get('image_tags', {})
    if ref in tags:
        return tags[ref]
    for img_id, meta in images.items():
        if ref in (meta.get('tags') or []):
            return img_id
    return None


def positional_args(args):
    if '--' in args:
        return args[args.index('--') + 1:]
    return [a for a in args if not a.startswith('-')]


argv = sys.argv[1:]
state = load_state()

if argv[:2] == ['ps', '-aq']:
    if state.get('ps_fail'):
        print(str(state['ps_fail']), file=sys.stderr)
        sys.exit(1)
    for cid in state.get('containers', {}):
        print(cid)
    sys.exit(0)

if argv[:1] == ['inspect']:
    fmt = ''
    if '--format' in argv:
        fmt = argv[argv.index('--format') + 1]
    cid = argv[-1]
    container = state.get('containers', {}).get(cid)
    if not container:
        print(f'Error: No such object: {cid}', file=sys.stderr)
        sys.exit(1)
    if '.Config.Env' in fmt:
        print('TEST-FAILURE: docker inspect requested Config.Env!', file=sys.stderr)
        sys.exit(99)
    out = {
        'id': container.get('id', cid),
        'name': container['name'],
        'image': container['image'],
        'configured_image': container['configured_image'],
        'project': container['project'],
        'service': container['service'],
        'working_dir': container['working_dir'],
        'config_files': container['config_files'],
        'mounts': container['mounts'],
        'port_bindings': container.get('port_bindings', {}),
        'networks': container.get('networks', {}),
        'running': container.get('running', True),
        'restarting': container.get('restarting', False),
        'exit_code': container.get('exit_code', 0),
        'health': container.get('health', ''),
    }
    print(json.dumps(out))
    sys.exit(0)

if argv[:2] == ['image', 'inspect']:
    ref = argv[-1]
    img_id = resolve_image_id(state, ref)
    if not img_id or img_id not in state.get('images', {}):
        print(f'Error: No such image: {ref}', file=sys.stderr)
        sys.exit(1)
    img_doc = state['images'][img_id]
    if '--format' in argv:
        fmt = argv[argv.index('--format') + 1]
        if '{{.Id}}' in fmt:
            print(img_id)
            sys.exit(0)
        if 'RepoDigests' in fmt:
            print(json.dumps(img_doc.get('repo_digests', [])))
            sys.exit(0)
    print(json.dumps([{**img_doc, 'Id': img_id}]))
    sys.exit(0)

if argv[:2] == ['volume', 'ls']:
    for vname in state.get('volumes', {}):
        print(vname)
    sys.exit(0)

if argv[:2] == ['volume', 'inspect']:
    vname = argv[-1]
    vol = state.get('volumes', {}).get(vname)
    if not vol:
        print(f'Error: No such volume: {vname}', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({
        'name': vname,
        'driver': vol.get('driver', 'local'),
        'mountpoint': vol.get('mountpoint', f'/var/lib/docker/volumes/{vname}/_data'),
        'labels': vol.get('labels', {}),
    }))
    sys.exit(0)

if argv[:2] == ['network', 'ls']:
    for nname in state.get('networks', []):
        print(json.dumps(nname))
    sys.exit(0)

if argv[:1] == ['rm']:
    if '-v' in argv or '--volumes' in argv:
        print('TEST-FAILURE: docker rm passed -v (must never delete volumes)!', file=sys.stderr)
        sys.exit(99)
    targets = positional_args(argv[1:])
    containers = state.setdefault('containers', {})
    for tid in targets:
        if tid in containers:
            del containers[tid]
        else:
            for k, v in list(containers.items()):
                if v.get('name') == tid or v.get('id') == tid:
                    del containers[k]
    save_state(state)
    log_call({'argv': argv, 'action': 'docker_rm', 'targets': targets})
    sys.exit(0)

if argv[:2] == ['compose', 'version']:
    print(state.get('compose_version', 'Docker Compose version v2.40.3'))
    sys.exit(0)

if argv[:1] == ['compose']:
    project = None
    files = []
    idx = 1
    while idx < len(argv):
        if argv[idx] == '-p':
            project = argv[idx + 1]
            idx += 2
        elif argv[idx] == '-f':
            files.append(argv[idx + 1])
            idx += 2
        else:
            break
    subcmd = argv[idx:]
    cwd = Path.cwd()
    env_map = load_dotenv_fallback(cwd)
    merged_services = {}
    merged_volumes = {}
    raw_texts = []
    for fname in files:
        fpath = (cwd / fname) if not Path(fname).is_absolute() else Path(fname)
        raw_txt = fpath.read_text(encoding='utf-8')
        raw_texts.append(raw_txt)
        data = yaml.safe_load(raw_txt) or {}
        for sname, scfg in (data.get('services') or {}).items():
            cur = merged_services.setdefault(sname, {})
            for k, v in (scfg or {}).items():
                if k == 'volumes' and isinstance(v, list) and isinstance(cur.get(k), list):
                    cur[k] = list(cur[k]) + list(v)
                else:
                    cur[k] = v
        for vname, vcfg in (data.get('volumes') or {}).items():
            cur = merged_volumes.setdefault(vname, {})
            if isinstance(vcfg, dict):
                cur.update(vcfg)

    action = subcmd[0] if subcmd else ''
    target_services = []
    if action in ('up', 'rm'):
        if '--' in subcmd:
            target_services = subcmd[subcmd.index('--') + 1:]
        else:
            i = 1
            while i < len(subcmd):
                tok = subcmd[i]
                if tok == '--pull':
                    i += 2
                    continue
                if tok.startswith('-'):
                    i += 1
                    continue
                target_services.append(tok)
                i += 1

    log_call({
        'argv': argv,
        'cwd': str(cwd),
        'home': os.environ.get('HOME'),
        'sudo_active': os.environ.get('FAKE_SUDO_ACTIVE'),
        'project': project,
        'files': files,
        'subcmd': subcmd,
        'target_services': target_services,
        'no_deps': '--no-deps' in subcmd,
        'env_keys': sorted(k for k in os.environ if k in ('DB_PASSWORD', 'APP_TOKEN', 'UPLOAD_LOCATION', 'TEST_SECRET', 'CF_DNS_API_TOKEN', 'OPT_SECRET', 'HIRING_DB_IMAGE', 'HIRING_APP_IMAGE')),
        'env_snapshot': {k: os.environ[k] for k in ('APP_TOKEN', 'UPLOAD_LOCATION', 'TEST_SECRET', 'OPT_SECRET', 'HIRING_DB_IMAGE', 'HIRING_APP_IMAGE') if k in os.environ},
        'merged_volumes': merged_volumes,
        'merged_images': {k: interp(str(v.get('image', '')), env_map) if v.get('image') else None for k, v in merged_services.items()},
    })

    if action in ('config', 'up'):
        for raw_txt in raw_texts:
            for m in REQ_VAR_RE.finditer(raw_txt):
                vname, op, msg = m.group(1), m.group(2), m.group(3)
                val = env_map.get(vname)
                if val is None or (op == ':?' and val == ''):
                    print(f'required variable {vname} is missing a value: {msg}', file=sys.stderr)
                    sys.exit(1)

    for vkey, vcfg in merged_volumes.items():
        if isinstance(vcfg, dict) and vcfg.get('external'):
            target_vname = vcfg.get('name', vkey)
            if target_vname not in state.get('volumes', {}):
                print(f'external volume "{target_vname}" not found', file=sys.stderr)
                sys.exit(1)

    fail_rules = state.get('fail_rules', {})
    rule_key = f'{project}:{action}'
    if rule_key in fail_rules:
        msg = fail_rules[rule_key]
        if isinstance(msg, dict):
            text = msg.get('message', 'simulated compose failure')
            if msg.get('once'):
                del fail_rules[rule_key]
                save_state(state)
            print(text, file=sys.stderr)
        else:
            print(str(msg), file=sys.stderr)
        sys.exit(1)

    if action == 'config':
        if '--quiet' not in subcmd and '-q' not in subcmd:
            print('fake-docker: refusing non-quiet config', file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if action == 'rm':
        if '-v' in subcmd or '--volumes' in subcmd:
            print('TEST-FAILURE: compose rm passed -v (must never delete volumes)!', file=sys.stderr)
            sys.exit(99)
        containers = state.setdefault('containers', {})
        rm_set = set(target_services) if target_services else set(merged_services.keys())
        for cid, c in list(containers.items()):
            if c.get('project') == project and c.get('service') in rm_set:
                del containers[cid]
        save_state(state)
        sys.exit(0)

    if action == 'up':
        if '--no-build' not in subcmd or '--pull' not in subcmd:
            print('fake-docker: expected --no-build and --pull never during adoption', file=sys.stderr)
            sys.exit(1)
        pull_idx = subcmd.index('--pull')
        if pull_idx + 1 >= len(subcmd) or subcmd[pull_idx + 1] != 'never':
            print('fake-docker: expected --pull never', file=sys.stderr)
            sys.exit(1)

        if target_services and '--no-deps' in subcmd:
            active_targets = list(target_services)
        else:
            active_targets = list(merged_services.keys())

        containers = state.setdefault('containers', {})
        by_svc = {c.get('service'): cid for cid, c in containers.items() if c.get('project') == project}
        home_dir = os.environ.get('HOME', '/root')
        cfg_files_str = ','.join(str((cwd / f).resolve()) for f in files)

        def split_vol_spec(spec):
            parts, buf, depth, idx = [], [], 0, 0
            while idx < len(spec):
                ch = spec[idx]
                if ch == '$' and idx + 1 < len(spec):
                    if spec[idx + 1] == '$':
                        buf.append('$$')
                        idx += 2
                        continue
                    if spec[idx + 1] == '{':
                        depth += 1
                        buf.append('${')
                        idx += 2
                        continue
                if ch == '}' and depth > 0:
                    depth -= 1
                    buf.append(ch)
                    idx += 1
                    continue
                if ch == ':' and depth == 0:
                    parts.append(''.join(buf))
                    buf = []
                    idx += 1
                    continue
                buf.append(ch)
                idx += 1
            parts.append(''.join(buf))
            return parts

        def build_mounts(scfg):
            out = []
            for raw_v in scfg.get('volumes') or []:
                if isinstance(raw_v, str):
                    parts = split_vol_spec(raw_v)
                    if len(parts) < 2:
                        continue
                    rsrc, rdst = interp(parts[0], env_map), interp(parts[1], env_map)
                    if rsrc in merged_volumes and not rsrc.startswith(('/', './', '../', '~')):
                        vcfg = merged_volumes.get(rsrc) or {}
                        vname = vcfg.get('name', f'{project}_{rsrc}')
                        out.append({'Type': 'volume', 'Name': vname, 'Source': f'/var/lib/docker/volumes/{vname}/_data', 'Destination': rdst})
                    else:
                        if rsrc.startswith('~/'):
                            src = posixpath.normpath(posixpath.join(home_dir, rsrc[2:]))
                        elif rsrc.startswith('/'):
                            src = posixpath.normpath(rsrc)
                        else:
                            src = posixpath.normpath(posixpath.join(str(cwd.resolve()), rsrc))
                        out.append({'Type': 'bind', 'Source': src, 'Destination': rdst})
            return out

        for sname in active_targets:
            if sname not in merged_services:
                print(f'no such service: {sname}', file=sys.stderr)
                sys.exit(1)
            svc = merged_services[sname]
            cfg_img = interp(str(svc.get('image') or ''), env_map)
            if not cfg_img and 'build' in svc:
                # Compose v2 default image name for build-only service is <project>-<service>
                cfg_img = f'{project}-{sname}'
                resolved_id = resolve_image_id(state, cfg_img)
                if not resolved_id:
                    print(f'pull access denied for {cfg_img}, repository does not exist', file=sys.stderr)
                    sys.exit(1)
            else:
                resolved_id = resolve_image_id(state, cfg_img) or cfg_img
            new_mounts = build_mounts(svc)
            if sname in by_svc:
                c = containers[by_svc[sname]]
                if cfg_img:
                    c['configured_image'] = cfg_img
                    c['image'] = resolved_id
                if new_mounts:
                    c['mounts'] = new_mounts
                c['config_files'] = cfg_files_str
                c['running'] = not state.get('force_stopped_after_up', {}).get(project, False)
                c['restarting'] = False
                if c.get('health') == 'unhealthy':
                    c['health'] = ''
            else:
                new_cid = f'cid-{project}-{sname}'
                containers[new_cid] = {
                    'id': new_cid,
                    'name': f'/{project}-{sname}-1',
                    'image': resolved_id,
                    'configured_image': cfg_img,
                    'project': project,
                    'service': sname,
                    'working_dir': str(cwd.resolve()),
                    'config_files': cfg_files_str,
                    'mounts': new_mounts,
                    'running': not state.get('force_stopped_after_up', {}).get(project, False),
                    'restarting': False,
                    'exit_code': 0,
                    'health': '',
                }

        mutations = state.get('post_up_mutations', {}).get(project, {})
        for sname, patch in mutations.items():
            for cid, c in containers.items():
                if c.get('project') == project and c.get('service') == sname:
                    c.update(patch)
        if project in state.get('post_up_mutations', {}) and state.get('post_up_mutations_once'):
            del state['post_up_mutations'][project]

        save_state(state)
        sys.exit(0)

print(f'fake-docker: unsupported command {argv}', file=sys.stderr)
sys.exit(1)
'''


FAKE_SOPS_PY = r'''#!/usr/bin/env python3
"""Fake sops CLI supporting both encrypt (stdin JSON) and decrypt (file path)."""
import json
import os
from pathlib import Path
import sys

argv = sys.argv[1:]
if argv[:1] == ['encrypt']:
    if os.environ.get('FAKE_SOPS_FAIL_ENCRYPT') == '1':
        secret_leak = os.environ.get('FAKE_SECRET_LEAK', '')
        print(f'sops encrypt failed: {secret_leak}', file=sys.stderr)
        sys.exit(1)
    values = json.load(sys.stdin)
    capture_dir = os.environ.get('FAKE_SOPS_CAPTURE_DIR')
    if capture_dir:
        fname = 'default.json'
        if '--filename-override' in argv:
            fname = argv[argv.index('--filename-override') + 1].replace('/', '__')
        Path(capture_dir, fname).write_text(json.dumps(values), encoding='utf-8')
    out = {
        key: (f'ENC[FAKE:{json.dumps(val)}]' if val != '' else '')
        for key, val in values.items()
    }
    out['sops'] = {'mac': 'ENC[FAKE:mac]', 'version': '3.13.3'}
    print(json.dumps(out))
    sys.exit(0)

if argv[:1] == ['decrypt']:
    target = Path(argv[-1])
    doc = json.loads(target.read_text(encoding='utf-8'))
    if os.environ.get('FAKE_SOPS_FAIL_DECRYPT') == '1':
        print(f'sops decrypt error: {os.environ.get("FAKE_SECRET_LEAK", "denied")}', file=sys.stderr)
        sys.exit(1)
    plain = {}
    for key, val in doc.items():
        if key == 'sops':
            continue
        if val == '':
            plain[key] = ''
        elif isinstance(val, str) and val.startswith('ENC[FAKE:') and val.endswith(']'):
            plain[key] = json.loads(val[len('ENC[FAKE:'):-1])
        else:
            plain[key] = val
    print(json.dumps(plain))
    sys.exit(0)

print(f'fake-sops: unsupported args {argv}', file=sys.stderr)
sys.exit(1)
'''


class SshMigrationTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.remote_home = self.base / 'remote_home'
        self.remote_root = self.remote_home / 'homelab'
        self.system_root = self.base / 'remote_system'
        self.controller_bin = self.base / 'controller_bin'
        self.remote_bin = self.base / 'remote_bin'
        self.capture_dir = self.base / 'sops_capture'
        self.outside = self.base / 'outside'

        for d in (
            self.repo / 'shared/secrets',
            self.remote_root,
            self.system_root / 'etc/wireguard',
            self.system_root / 'etc/docker',
            self.controller_bin,
            self.remote_bin,
            self.capture_dir,
            self.outside,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.ssh_log = self.base / 'ssh_log.jsonl'
        self.sudo_log = self.base / 'sudo_log.jsonl'
        self.docker_log = self.base / 'docker_log.jsonl'
        self.docker_state_path = self.base / 'docker_state.json'

        self._write_exec(self.controller_bin / 'ssh', FAKE_SSH_PY)
        self._write_exec(self.controller_bin / 'sops', FAKE_SOPS_PY)
        self._write_exec(self.remote_bin / 'docker', FAKE_DOCKER_PY.replace('#!/usr/bin/env python3', f'#!{sys.executable}', 1))
        self._write_exec(self.remote_bin / 'sudo', FAKE_SUDO_PY)
        for tool, out in (
            ('ip', '[]\n'),
            ('wg', 'wg0\n'),
            ('sysctl', '1\n'),
            ('iptables-save', '-A FORWARD -i wg0 -j ACCEPT\n'),
            ('ip6tables-save', '-A FORWARD -i wg0 -j ACCEPT\n'),
        ):
            self._write_exec(self.remote_bin / tool, f'#!/bin/sh\nprintf "%s" "{out}"\n')

        self._setup_sample_homelab()

    def _write_exec(self, path, content):
        path.write_text(content, encoding='utf-8')
        path.chmod(0o700)

    def _setup_sample_homelab(self):
        # 1. Secret-managed stack with named volume + secret file + dependency (medion/webapp + webapp-extra)
        webapp_repo = self.repo / 'medion/webapp'
        webapp_repo.mkdir(parents=True)
        (webapp_repo / 'config').mkdir()
        (webapp_repo / 'config/app.conf').write_text('mode = sops-v2\n', encoding='utf-8')
        (webapp_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'web': {
                    'image': 'example/webapp:1.2',
                    'env_file': [
                        {'path': '.env', 'required': False},
                        {'path': '.secrets.env', 'format': 'raw'},
                        {'path': '.secrets.extra.env', 'format': 'raw'},
                    ],
                    'volumes': [
                        './config/app.conf:/etc/app/app.conf:ro',
                        '${UPLOAD_LOCATION}:/uploads',
                    ],
                    'secrets': ['db_password'],
                },
                'db': {
                    'image': 'mariadb:11.8',
                    'environment': {'MYSQL_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['database:/var/lib/mysql'],
                    'secrets': ['db_password'],
                },
            },
            'volumes': {'database': None},
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')

        # 2. Credentialless stack (medion/jellyfin)
        jf_repo = self.repo / 'medion/jellyfin'
        jf_repo.mkdir(parents=True)
        (jf_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'jellyfin': {
                    'image': 'jellyfin/jellyfin:10.9',
                    'volumes': ['./config:/config'],
                },
            },
        }, sort_keys=False), encoding='utf-8')

        # 3. Stack with one-shot schema migration service (medion/hiring_scraper)
        hs_repo = self.repo / 'medion/hiring_scraper'
        (hs_repo / 'docker').mkdir(parents=True)
        (hs_repo / 'docker/chromium-seccomp.json').write_text('{"defaultAction":"SCMP_ACT_ERRNO"}\n', encoding='utf-8')
        (hs_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': '${HIRING_DB_IMAGE}',
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'volumes': ['./hiring_db:/var/lib/postgresql/data'],
                },
                'migrate': {
                    'image': '${HIRING_APP_IMAGE}',
                    'command': ['--migrate'],
                    'restart': 'no',
                    'depends_on': {'db': {'condition': 'service_healthy'}},
                },
                'app': {
                    'image': '${HIRING_APP_IMAGE}',
                    'depends_on': {'migrate': {'condition': 'service_completed_successfully'}},
                    'volumes': ['./.secrets/app-bootstrap.env:/run/app-bootstrap.env:ro'],
                    'security_opt': ['seccomp=./docker/chromium-seccomp.json'],
                },
            },
        }, sort_keys=False), encoding='utf-8')

        # 4. Undeployed stack in manifest (medion/undeployed)
        und_repo = self.repo / 'medion/undeployed'
        und_repo.mkdir(parents=True)
        (und_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {'svc': {'image': 'busybox:1.36'}},
        }, sort_keys=False), encoding='utf-8')

        self.manifest = {
            'medion': {
                'webapp': {
                    'directory': 'medion/webapp',
                    'env_file': 'medion/webapp/.secrets.env',
                    'required': ['DB_PASSWORD', 'APP_TOKEN', 'UPLOAD_LOCATION'],
                    'compose': ['docker-compose.yml'],
                    'files': {'DB_PASSWORD': 'medion/webapp/.secrets/db_password'},
                    'file_only': ['DB_PASSWORD'],
                    'dependencies': ['webapp-extra'],
                    'encrypted_file': 'secrets/medion/webapp.enc.json',
                    'migration': {
                        'env_files': ['medion/webapp/.env'],
                        'file_values': {},
                    },
                },
                'webapp-extra': {
                    'directory': 'medion/webapp',
                    'env_file': 'medion/webapp/.secrets.extra.env',
                    'required': ['EXTRA_KEY'],
                    'encrypted_file': 'secrets/medion/webapp-extra.enc.json',
                    'migration': {
                        'env_files': ['medion/webapp/extra.env'],
                        'file_values': {},
                    },
                },
                'hiring_scraper': {
                    'directory': 'medion/hiring_scraper',
                    'env_file': 'medion/hiring_scraper/.secrets.env',
                    'required': ['HIRING_DB_IMAGE', 'HIRING_APP_IMAGE'],
                    'compose': ['docker-compose.yml'],
                    'files': {'APP_BOOTSTRAP_ENV': 'medion/hiring_scraper/.secrets/app-bootstrap.env'},
                    'file_only': ['APP_BOOTSTRAP_ENV'],
                    'encrypted_file': 'secrets/medion/hiring_scraper.enc.json',
                    'migration': {
                        'env_files': ['medion/hiring_scraper/.env'],
                        'file_values': {'APP_BOOTSTRAP_ENV': 'medion/hiring_scraper/.env'},
                    },
                },
                'undeployed': {
                    'directory': 'medion/undeployed',
                    'env_file': 'medion/undeployed/.secrets.env',
                    'required': ['UNUSED_TOKEN'],
                    'compose': ['docker-compose.yml'],
                    'encrypted_file': 'secrets/medion/undeployed.enc.json',
                    'migration': {
                        'env_files': ['medion/undeployed/.env'],
                        'file_values': {},
                    },
                },
            },
        }
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )

        # Remote existing legacy state
        self.uploads_dir = self.base / 'srv_uploads'
        self.uploads_dir.mkdir()
        webapp_remote = self.remote_root / 'medion/webapp'
        (webapp_remote / 'config').mkdir(parents=True)
        (webapp_remote / 'config/app.conf').write_text('mode = legacy-v1\n', encoding='utf-8')
        (webapp_remote / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'web': {'image': 'example/webapp:1.2'},
                'db': {'image': 'mariadb:11.8', 'volumes': ['database:/var/lib/mysql']},
            },
            'volumes': {'database': None},
        }, sort_keys=False), encoding='utf-8')
        (webapp_remote / '.env').write_text(
            f"DB_PASSWORD='db$pass#literal'\n"
            f"APP_TOKEN='tok$en!value'\n"
            f"UPLOAD_LOCATION={self.uploads_dir}\n",
            encoding='utf-8',
        )
        (webapp_remote / '.env').chmod(0o640)
        (webapp_remote / 'extra.env').write_text("EXTRA_KEY='extra$literal'\n", encoding='utf-8')

        jf_remote = self.remote_root / 'medion/jellyfin'
        (jf_remote / 'config').mkdir(parents=True)
        (jf_remote / 'config/jellyfin.db').write_text('EXISTING-JF-DATA', encoding='utf-8')
        (jf_remote / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {'jellyfin': {'image': 'jellyfin/jellyfin:10.9', 'volumes': ['./config:/config']}},
        }, sort_keys=False), encoding='utf-8')

        hs_remote = self.remote_root / 'medion/hiring_scraper'
        (hs_remote / 'hiring_db').mkdir(parents=True)
        (hs_remote / 'hiring_db/PG_VERSION').write_text('16\n', encoding='utf-8')
        (hs_remote / '.env').write_bytes(b'HIRING_DB_IMAGE=radius/db:1\r\nHIRING_APP_IMAGE=radius/app:1\r\n')
        (hs_remote / 'docker-compose.yml').write_text(
            (hs_repo / 'docker-compose.yml').read_text(encoding='utf-8'), encoding='utf-8'
        )

        # Remote host files (WireGuard + Docker daemon)
        (self.system_root / 'etc/wireguard/wg0.conf').write_text(
            '[Interface]\nPrivateKey = wg-fixture-private-key\nAddress = 10.77.0.1/24\n',
            encoding='utf-8',
        )
        (self.system_root / 'etc/docker/daemon.json').write_text('{"live-restore": true}\n', encoding='utf-8')

        # Initial Docker state on remote: legacy project name differs from directory name!
        self.docker_state = {
            'containers': {
                'cid-web': {
                    'name': '/legacy_web',
                    'image': 'sha256:111111111111',
                    'configured_image': 'example/webapp:1.2',
                    'project': 'legacy-webapp-proj',
                    'service': 'web',
                    'working_dir': str(webapp_remote.resolve()),
                    'config_files': str((webapp_remote / 'docker-compose.yml').resolve()),
                    'mounts': [
                        {'Type': 'bind', 'Source': str((webapp_remote / 'config/app.conf').resolve()), 'Destination': '/etc/app/app.conf'},
                        {'Type': 'bind', 'Source': str(self.uploads_dir.resolve()), 'Destination': '/uploads'},
                    ],
                    'running': True,
                    'secret_env_must_never_leak': 'DOCKER_ENV_SUPER_SECRET_12345',
                },
                'cid-db': {
                    'name': '/legacy_db',
                    'image': 'sha256:222222222222',
                    'configured_image': 'mariadb:11.8',
                    'project': 'legacy-webapp-proj',
                    'service': 'db',
                    'working_dir': str(webapp_remote.resolve()),
                    'config_files': str((webapp_remote / 'docker-compose.yml').resolve()),
                    'mounts': [
                        {'Type': 'volume', 'Name': 'legacy-webapp-proj_database', 'Source': '/var/lib/docker/volumes/legacy-webapp-proj_database/_data', 'Destination': '/var/lib/mysql'},
                    ],
                    'running': True,
                },
                'cid-jf': {
                    'name': '/jellyfin',
                    'image': 'sha256:333333333333',
                    'configured_image': 'jellyfin/jellyfin:10.9',
                    'project': 'jellyfin-prod',
                    'service': 'jellyfin',
                    'working_dir': str(jf_remote.resolve()),
                    'config_files': str((jf_remote / 'docker-compose.yml').resolve()),
                    'mounts': [
                        {'Type': 'bind', 'Source': str((jf_remote / 'config').resolve()), 'Destination': '/config'},
                    ],
                    'running': True,
                },
                'cid-hs-db': {
                    'name': '/hiring_db',
                    'image': 'sha256:444444444444',
                    'configured_image': 'radius/db:1',
                    'project': 'hiring_scraper',
                    'service': 'db',
                    'working_dir': str(hs_remote.resolve()),
                    'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
                    'mounts': [
                        {'Type': 'bind', 'Source': str((hs_remote / 'hiring_db').resolve()), 'Destination': '/var/lib/postgresql/data'},
                    ],
                    'running': True,
                },
                'cid-hs-app': {
                    'name': '/hiring_app',
                    'image': 'sha256:555555555555',
                    'configured_image': 'radius/app:1',
                    'project': 'hiring_scraper',
                    'service': 'app',
                    'working_dir': str(hs_remote.resolve()),
                    'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
                    'mounts': [
                        {'Type': 'bind', 'Source': str((hs_remote / '.env').resolve()), 'Destination': '/run/app-bootstrap.env'},
                    ],
                    'running': True,
                },
            },
            'images': {
                'sha256:111111111111': {'repo_digests': ['example/webapp@sha256:1111'], 'tags': ['example/webapp:1.2']},
                'sha256:222222222222': {'repo_digests': ['mariadb@sha256:2222'], 'tags': ['mariadb:11.8']},
                'sha256:333333333333': {'repo_digests': ['jellyfin/jellyfin@sha256:3333'], 'tags': ['jellyfin/jellyfin:10.9']},
                'sha256:444444444444': {'repo_digests': ['radius/db@sha256:4444'], 'tags': ['radius/db:1']},
                'sha256:555555555555': {'repo_digests': ['radius/app@sha256:5555'], 'tags': ['radius/app:1']},
            },
            'volumes': {
                'legacy-webapp-proj_database': {'driver': 'local'},
            },
            'networks': ['frontend', 'backend'],
            'fail_rules': {},
        }
        self._save_docker_state()

    def _save_docker_state(self):
        self.docker_state_path.write_text(json.dumps(self.docker_state, indent=2), encoding='utf-8')

    def run_cli(self, *args, extra_env=None):
        env = {
            **os.environ,
            'PATH': str(self.controller_bin) + os.pathsep + os.environ.get('PATH', ''),
            'FAKE_SSH_LOG': str(self.ssh_log),
            'FAKE_SUDO_LOG': str(self.sudo_log),
            'FAKE_DOCKER_LOG': str(self.docker_log),
            'FAKE_DOCKER_STATE': str(self.docker_state_path),
            'FAKE_REMOTE_HOME': str(self.remote_home),
            'FAKE_REMOTE_BIN': str(self.remote_bin),
            'FAKE_SOPS_CAPTURE_DIR': str(self.capture_dir),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--root', str(self.repo), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def read_jsonl(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


class SshMigrationTests(SshMigrationTestBase):
    def test_strict_ssh_argument_handling_and_option_injection_rejection(self):
        audit_out = self.outside / 'audit.json'
        bad = self.run_cli(
            'audit', '--host', 'medion', '--ssh-target', '-oProxyCommand=id',
            '--source-root', str(self.remote_root), '--output', str(audit_out),
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertFalse(self.ssh_log.exists(), 'ssh must never be invoked for option-like target')

        good = self.run_cli(
            'audit', '--host', 'medion', '--ssh-target', 'medion.example',
            '--source-root', str(self.remote_root), '--output', str(audit_out),
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        calls = self.read_jsonl(self.ssh_log)
        self.assertEqual(len(calls), 1)
        argv = calls[0]['argv']
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('PasswordAuthentication=no', argv)
        self.assertIn('--', argv)
        self.assertEqual(argv[argv.index('--') + 1], 'medion.example')
        self.assertEqual(audit_out.stat().st_mode & 0o777, 0o600)
        report_text = audit_out.read_text(encoding='utf-8')
        self.assertNotIn('DOCKER_ENV_SUPER_SECRET_12345', report_text)
        self.assertNotIn('db$pass#literal', report_text)

    def test_capture_host_dry_run_and_root_only_file_handling(self):
        wg = self.system_root / 'etc/wireguard/wg0.conf'
        dry = self.run_cli(
            'capture-host', '--host', 'medion', '--ssh-target', 'medion',
            '--system-root', str(self.system_root),
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('DRY RUN', dry.stdout)
        self.assertFalse((self.repo / 'secrets/medion/host-config.enc.json').exists())
        self.assertNotIn('wg-fixture-private-key', dry.stdout + dry.stderr)

        # Unreadable root-only file must fail with actionable error, NOT silent omission
        wg.chmod(0o000)
        self.addCleanup(lambda: wg.exists() and wg.chmod(0o600))
        denied = self.run_cli(
            'capture-host', '--host', 'medion', '--ssh-target', 'medion',
            '--system-root', str(self.system_root),
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('wg0.conf', denied.stderr)
        self.assertIn('--sudo', denied.stderr)

        # Explicit opt-out (--skip-unreadable) succeeds with explicit warning
        skipped = self.run_cli(
            'capture-host', '--host', 'medion', '--ssh-target', 'medion',
            '--system-root', str(self.system_root), '--skip-unreadable',
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.assertIn('wg0.conf', skipped.stderr + skipped.stdout)

        # Explicit --sudo invokes sudo -n and captures root-only file + runtime firewall
        applied = self.run_cli(
            'capture-host', '--host', 'medion', '--ssh-target', 'medion',
            '--system-root', str(self.system_root), '--sudo',
            '--runtime-firewall', 'iptables', '--apply',
            extra_env={'FAKE_UNREADABLE_UNLOCK': str(wg)},
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        sudo_calls = self.read_jsonl(self.sudo_log)
        self.assertTrue(sudo_calls, 'expected sudo -n to be invoked with --sudo')
        self.assertEqual(sudo_calls[0]['argv'][0], '-n')
        enc_path = self.repo / 'secrets/medion/host-config.enc.json'
        self.assertTrue(enc_path.is_file())
        self.assertEqual(enc_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('wg-fixture-private-key', applied.stdout + applied.stderr)
        self.assertNotIn('-A FORWARD', applied.stdout + applied.stderr)
        captured = json.loads((self.capture_dir / 'secrets__medion__host-config.enc.json').read_text())
        self.assertIn('LIVE_IPTABLES_SAVE', captured)
        self.assertIn('wg-fixture-private-key', captured['FILE_ETC_WIREGUARD_WG0_CONF'])

    def test_import_secrets_dependencies_missing_stacks_and_redaction(self):
        dry = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('DRY RUN', dry.stdout)
        self.assertFalse((self.repo / 'secrets').exists())
        self.assertNotIn('db$pass#literal', dry.stdout + dry.stderr)
        self.assertNotIn('extra$literal', dry.stdout + dry.stderr)

        # Redaction on SOPS encryption failure
        fail_sops = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            extra_env={'FAKE_SOPS_FAIL_ENCRYPT': '1', 'FAKE_SECRET_LEAK': 'db$pass#literal'},
        )
        self.assertNotEqual(fail_sops.returncode, 0)
        self.assertNotIn('db$pass#literal', fail_sops.stdout + fail_sops.stderr)

        # --all-stacks fails when undeployed stack is missing unless --skip-not-deployed is passed
        all_strict = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--all-stacks',
        )
        self.assertNotEqual(all_strict.returncode, 0)
        self.assertIn('undeployed', all_strict.stderr)

        all_skip = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--all-stacks', '--skip-not-deployed', '--apply',
        )
        self.assertEqual(all_skip.returncode, 0, all_skip.stderr)
        self.assertIn('undeployed', all_skip.stdout)
        self.assertTrue((self.repo / 'secrets/medion/webapp.enc.json').is_file())
        self.assertTrue((self.repo / 'secrets/medion/webapp-extra.enc.json').is_file())
        self.assertTrue((self.repo / 'secrets/medion/hiring_scraper.enc.json').is_file())
        self.assertFalse((self.repo / 'secrets/medion/undeployed.enc.json').exists())
        hs_cap = json.loads((self.capture_dir / 'secrets__medion__hiring_scraper.enc.json').read_text())
        self.assertEqual(
            hs_cap['APP_BOOTSTRAP_ENV'].encode(),
            b'HIRING_DB_IMAGE=radius/db:1\r\nHIRING_APP_IMAGE=radius/app:1\r\n',
        )
        self.assertTrue((self.remote_root / 'medion/webapp/.env').exists())

    def test_end_to_end_plan_apply_identity_preservation_and_explicit_rollback(self):
        imp = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
        )
        self.assertEqual(imp.returncode, 0, imp.stderr)

        plan_path = self.outside / 'medion-plan.json'
        plan = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root),
            '--stack', 'webapp', '--stack', 'jellyfin',
            '--plan', str(plan_path),
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)
        plan_text = plan_path.read_text(encoding='utf-8')
        self.assertNotIn('db$pass#literal', plan_text)
        self.assertNotIn('tok$en!value', plan_text)
        self.assertNotIn('extra$literal', plan_text)
        self.assertEqual(self.read_jsonl(self.docker_log), [])

        plan_data = json.loads(plan_text)
        self.assertIn('plan_digest', plan_data)
        self.assertEqual(plan_data['stacks']['webapp']['project_name'], 'legacy-webapp-proj')
        self.assertEqual(plan_data['stacks']['webapp']['pinned_volumes'], {'database': 'legacy-webapp-proj_database'})
        self.assertEqual(plan_data['stacks']['jellyfin']['project_name'], 'jellyfin-prod')
        self.assertEqual(plan_data['stacks']['jellyfin']['kind'], 'credentialless')

        # Apply refuses without backup confirmation & evidence
        no_gate = self.run_cli('apply', '--plan', str(plan_path))
        self.assertNotEqual(no_gate.returncode, 0)
        self.assertIn('backup', no_gate.stderr.lower())
        self.assertEqual(self.read_jsonl(self.docker_log), [])

        applied = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested',
            '--backup-evidence', 'restic-snap-abc123 verified restore 2026-11-24',
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertNotIn('db$pass#literal', applied.stdout + applied.stderr)
        self.assertNotIn('tok$en!value', applied.stdout + applied.stderr)

        for call in self.read_jsonl(self.ssh_log):
            joined = ' '.join(call['argv'])
            self.assertNotIn('db$pass#literal', joined)
            self.assertNotIn('tok$en!value', joined)
            self.assertNotIn('extra$literal', joined)

        webapp_remote = self.remote_root / 'medion/webapp'
        self.assertFalse((webapp_remote / '.env').exists(), 'legacy .env should be archived to rollback dir')
        self.assertFalse((webapp_remote / 'extra.env').exists(), 'legacy extra.env should be archived to rollback dir')
        self.assertEqual((webapp_remote / 'config/app.conf').read_text(), 'mode = sops-v2\n')
        sec_env = webapp_remote / '.secrets.env'
        sec_extra = webapp_remote / '.secrets.extra.env'
        sec_db = webapp_remote / '.secrets/db_password'
        self.assertEqual(sec_env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sec_extra.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sec_db.stat().st_mode & 0o777, 0o600)
        self.assertEqual((webapp_remote / '.secrets').stat().st_mode & 0o777, 0o700)
        self.assertEqual(sec_db.read_text(), 'db$pass#literal')
        self.assertIn('APP_TOKEN=tok$en!value\n', sec_env.read_text())
        self.assertNotIn('DB_PASSWORD=', sec_env.read_text())
        self.assertEqual(sec_extra.read_text(), 'EXTRA_KEY=extra$literal\n')

        # Persistent data directory untouched
        self.assertEqual((self.remote_root / 'medion/jellyfin/config/jellyfin.db').read_text(), 'EXISTING-JF-DATA')

        # Docker Compose preserved project, pinned external named volume, pinned image IDs, and env
        docker_calls = self.read_jsonl(self.docker_log)
        up_calls = [c for c in docker_calls if c['subcmd'][:1] == ['up']]
        self.assertEqual(len(up_calls), 2)
        webapp_up = next(c for c in up_calls if c['project'] == 'legacy-webapp-proj')
        self.assertEqual(webapp_up['merged_volumes']['database'], {'external': True, 'name': 'legacy-webapp-proj_database'})
        # Local tags resolve to exact running image IDs -> keep tag refs intact (preserves Watchtower & re-plan)
        self.assertEqual(webapp_up['merged_images']['web'], 'example/webapp:1.2')
        self.assertEqual(webapp_up['merged_images']['db'], 'mariadb:11.8')
        self.assertTrue(webapp_up['no_deps'])
        self.assertEqual(sorted(webapp_up['target_services']), ['db', 'web'])
        self.assertEqual(webapp_up['env_snapshot']['APP_TOKEN'], 'tok$en!value')
        self.assertIn('--no-build', webapp_up['subcmd'])
        self.assertIn('--pull', webapp_up['subcmd'])

        rollback_dir = Path(plan_data['rollback_dir'])
        self.assertEqual(rollback_dir.stat().st_mode & 0o777, 0o700)

        # Explicit rollback restores previous files, legacy .env + original mode, removes .secrets, restores compose
        rb = self.run_cli('rollback', '--plan', str(plan_path))
        self.assertEqual(rb.returncode, 0, rb.stderr)
        self.assertTrue((webapp_remote / '.env').is_file())
        self.assertEqual((webapp_remote / '.env').stat().st_mode & 0o777, 0o640)
        self.assertTrue((webapp_remote / 'extra.env').is_file())
        self.assertEqual((webapp_remote / 'config/app.conf').read_text(), 'mode = legacy-v1\n')
        self.assertFalse(sec_env.exists())
        self.assertFalse(sec_db.exists())
        self.assertFalse((webapp_remote / '.homelab-adopt.override.yml').exists())
        self.assertEqual((self.remote_root / 'medion/jellyfin/config/jellyfin.db').read_text(), 'EXISTING-JF-DATA')

    def test_plan_blocks_oneshot_migration_missing_volume_mount_drift_and_image_drift(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--all-stacks', '--skip-not-deployed', '--apply',
            ).returncode,
            0,
        )

        # 1. Active or failed one-shot schema migration (hiring_scraper) is blocked from automatic adoption
        hs_remote = self.remote_root / 'medion/hiring_scraper'
        self.docker_state['containers']['cid-hs-mig'] = {
            'id': 'cid-hs-mig',
            'name': '/hiring_migrate',
            'image': 'sha256:555555555555',
            'configured_image': 'radius/app:1',
            'project': 'hiring_scraper',
            'service': 'migrate',
            'working_dir': str(hs_remote.resolve()),
            'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
            'mounts': [],
            'running': True,
            'exit_code': 0,
        }
        self._save_docker_state()
        hs_plan = self.outside / 'hs-plan.json'
        res_hs = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'hiring_scraper',
            '--plan', str(hs_plan),
        )
        self.assertNotEqual(res_hs.returncode, 0)
        self.assertIn('migrate', res_hs.stderr)
        self.assertEqual((hs_remote / 'hiring_db/PG_VERSION').read_text(), '16\n')
        del self.docker_state['containers']['cid-hs-mig']
        self._save_docker_state()

        # 2. Missing existing Docker named volume fails safely
        saved_vols = dict(self.docker_state['volumes'])
        self.docker_state['volumes'] = {}
        self._save_docker_state()
        res_vol = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'vol-plan.json'),
        )
        self.assertNotEqual(res_vol.returncode, 0)
        self.assertIn('volume', res_vol.stderr.lower())
        self.docker_state['volumes'] = saved_vols
        self._save_docker_state()

        # 3. Bind mount drift fails safely
        orig_mounts = copy.deepcopy(self.docker_state['containers']['cid-web']['mounts'])
        self.docker_state['containers']['cid-web']['mounts'][1]['Source'] = '/different/uploads'
        self._save_docker_state()
        res_mount = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'mount-plan.json'),
        )
        self.assertNotEqual(res_mount.returncode, 0)
        self.assertIn('mount', res_mount.stderr.lower())
        self.docker_state['containers']['cid-web']['mounts'] = orig_mounts
        self._save_docker_state()

        # 4. Unreviewed image/schema tag change fails safely unless --pin-running-images is passed
        self.docker_state['containers']['cid-db']['configured_image'] = 'mariadb:11.4'
        self.docker_state['images']['sha256:222222222222']['tags'] = ['mariadb:11.4']
        self._save_docker_state()
        res_img = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'img-plan.json'),
        )
        self.assertNotEqual(res_img.returncode, 0)
        self.assertIn('image', res_img.stderr.lower())
        res_pin = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--pin-running-images',
            '--plan', str(self.outside / 'img-pin-plan.json'),
        )
        self.assertEqual(res_pin.returncode, 0, res_pin.stderr)

    def test_edited_or_stale_plan_and_source_drift_are_rejected(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'drift-plan.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--plan', str(plan_path),
            ).returncode,
            0,
        )
        original_plan = plan_path.read_text(encoding='utf-8')

        # 1. Edited plan digest mismatch
        tampered = json.loads(original_plan)
        tampered['stacks']['webapp']['project_name'] = 'tampered-project'
        plan_path.write_text(json.dumps(tampered), encoding='utf-8')
        res_tamper = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'snap-1',
        )
        self.assertNotEqual(res_tamper.returncode, 0)
        self.assertIn('digest', res_tamper.stderr.lower())
        plan_path.write_text(original_plan, encoding='utf-8')

        # 2. Repo config file drifted after plan
        app_conf = self.repo / 'medion/webapp/config/app.conf'
        app_conf.write_text('mode = edited-after-plan\n', encoding='utf-8')
        res_repo_drift = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'snap-1',
        )
        self.assertNotEqual(res_repo_drift.returncode, 0)
        self.assertIn('drift', res_repo_drift.stderr.lower())
        app_conf.write_text('mode = sops-v2\n', encoding='utf-8')

        # 3. Remote file drifted after plan
        remote_conf = self.remote_root / 'medion/webapp/config/app.conf'
        remote_conf.write_text('mode = hotfix-on-server\n', encoding='utf-8')
        res_remote_drift = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'snap-1',
        )
        self.assertNotEqual(res_remote_drift.returncode, 0)
        self.assertIn('drift', res_remote_drift.stderr.lower())
        self.assertEqual(self.read_jsonl(self.docker_log), [])

    def test_partial_failure_triggers_automatic_rollback_and_redacts_errors(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'partial-fail-plan.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root),
                '--stack', 'webapp', '--stack', 'jellyfin',
                '--plan', str(plan_path),
            ).returncode,
            0,
        )
        self.docker_state['fail_rules'] = {
            'jellyfin-prod:up': {'message': 'boom leaked tok$en!value', 'once': True},
        }
        self._save_docker_state()

        res = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'snap-verified',
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('rollback', (res.stdout + res.stderr).lower())
        self.assertNotIn('tok$en!value', res.stdout + res.stderr)
        self.assertNotIn('db$pass#literal', res.stdout + res.stderr)

        webapp_remote = self.remote_root / 'medion/webapp'
        self.assertTrue((webapp_remote / '.env').is_file(), 'webapp legacy .env must be restored')
        self.assertEqual((webapp_remote / 'config/app.conf').read_text(), 'mode = legacy-v1\n')
        self.assertFalse((webapp_remote / '.secrets.env').exists())
        self.assertFalse((webapp_remote / '.secrets/db_password').exists())
        self.assertFalse((webapp_remote / '.homelab-adopt.override.yml').exists())
        self.assertFalse((self.remote_root / 'medion/jellyfin/.homelab-adopt.override.yml').exists())

    def test_prepare_combined_workflow(self):
        audit_out = self.outside / 'prep-audit.json'
        dry = self.run_cli(
            'prepare', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root),
            '--system-root', str(self.system_root),
            '--audit-output', str(audit_out),
            '--stack', 'webapp',
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('DRY RUN', dry.stdout)
        self.assertFalse((self.repo / 'secrets').exists())

        apply = self.run_cli(
            'prepare', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root),
            '--system-root', str(self.system_root),
            '--audit-output', str(audit_out),
            '--stack', 'webapp', '--apply',
        )
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertTrue(audit_out.is_file())
        self.assertTrue((self.repo / 'secrets/medion/host-config.enc.json').is_file())
        self.assertTrue((self.repo / 'secrets/medion/webapp.enc.json').is_file())
        self.assertTrue((self.repo / 'secrets/medion/webapp-extra.enc.json').is_file())



    def test_unreadable_root_only_directory_is_not_silently_omitted(self):
        wg_dir = self.system_root / 'etc/wireguard'
        wg_dir.chmod(0o000)
        self.addCleanup(lambda: wg_dir.exists() and wg_dir.chmod(0o700))
        res = self.run_cli(
            'capture-host', '--host', 'medion', '--ssh-target', 'medion',
            '--system-root', str(self.system_root),
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('/etc/wireguard', res.stderr)
        self.assertIn('--sudo', res.stderr)

    def test_shared_backup_tilde_expansion_inplace_secret_and_build_service(self):
        # Add shared-backup (+ backup-credentials in-place .autorestic.env) and a build: service
        (self.repo / 'shared/backup').mkdir(parents=True)
        (self.repo / 'medion/.autorestic.yml').write_text('version: 2\n', encoding='utf-8')
        (self.repo / 'shared/backup/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'autorestic': {
                    'build': '.',
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'volumes': [
                        '~/.ssh:/root/.ssh:ro',
                        '${LOCAL_BACKUP_PATH}:/backup',
                    ],
                },
            },
        }, sort_keys=False), encoding='utf-8')
        (self.repo / 'shared/backup/compose.medion.yml').write_text(yaml.safe_dump({
            'services': {
                'autorestic': {
                    'volumes': ['${IMMICH_UPLOAD_LOCATION}:/external/immich-upload:ro'],
                },
            },
        }, sort_keys=False), encoding='utf-8')

        self.manifest['medion']['shared-backup'] = {
            'directory': 'shared/backup',
            'env_file': 'shared/backup/.secrets.env',
            'required': ['LOCAL_BACKUP_PATH', 'IMMICH_UPLOAD_LOCATION'],
            'compose': ['docker-compose.yml', 'compose.medion.yml'],
            'dependencies': ['backup-credentials'],
            'encrypted_file': 'secrets/medion/shared-backup.enc.json',
            'migration': {'env_files': ['shared/backup/.env'], 'file_values': {}},
        }
        self.manifest['medion']['backup-credentials'] = {
            'directory': 'medion',
            'required': ['AUTORESTIC_ENV'],
            'files': {'AUTORESTIC_ENV': 'medion/.autorestic.env'},
            'encrypted_file': 'secrets/medion/backup-credentials.enc.json',
            'migration': {'env_files': [], 'file_values': {'AUTORESTIC_ENV': 'medion/.autorestic.env'}},
        }
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )

        ssh_dir = self.remote_home / '.ssh'
        ssh_dir.mkdir(exist_ok=True)
        backup_dir = self.base / 'srv_backups'
        backup_dir.mkdir()
        sb_remote = self.remote_root / 'shared/backup'
        sb_remote.mkdir(parents=True)
        (sb_remote / 'docker-compose.yml').write_text(
            (self.repo / 'shared/backup/docker-compose.yml').read_text(), encoding='utf-8'
        )
        (sb_remote / 'compose.medion.yml').write_text(
            (self.repo / 'shared/backup/compose.medion.yml').read_text(), encoding='utf-8'
        )
        (sb_remote / '.env').write_text(
            f'LOCAL_BACKUP_PATH={backup_dir}\nIMMICH_UPLOAD_LOCATION={self.uploads_dir}\n',
            encoding='utf-8',
        )
        autorestic_env = self.remote_root / 'medion/.autorestic.env'
        autorestic_env.write_text('AUTORESTIC_BACKEND_KEY=keep-exact-literal\n', encoding='utf-8')

        self.docker_state['containers']['cid-backup'] = {
            'name': '/autorestic',
            'image': 'sha256:666666666666',
            'configured_image': 'backup-autorestic:local-built',
            'project': 'backup',
            'service': 'autorestic',
            'working_dir': str(sb_remote.resolve()),
            'config_files': f'{(sb_remote / "docker-compose.yml").resolve()},{(sb_remote / "compose.medion.yml").resolve()}',
            'mounts': [
                {'Type': 'bind', 'Source': str(ssh_dir.resolve()), 'Destination': '/root/.ssh'},
                {'Type': 'bind', 'Source': str(backup_dir.resolve()), 'Destination': '/backup'},
                {'Type': 'bind', 'Source': str(self.uploads_dir.resolve()), 'Destination': '/external/immich-upload'},
            ],
            'running': True,
        }
        self.docker_state['images']['sha256:666666666666'] = {'repo_digests': []}
        self._save_docker_state()

        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'shared-backup', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'shared-backup-plan.json'
        plan_res = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'shared-backup',
            '--plan', str(plan_path),
        )
        self.assertEqual(plan_res.returncode, 0, plan_res.stderr)
        apply_res = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'verified-backup-snap',
        )
        self.assertEqual(apply_res.returncode, 0, apply_res.stderr)

        # In-place .autorestic.env must STILL exist with exact content and mode 0600!
        self.assertTrue(autorestic_env.is_file())
        self.assertEqual(autorestic_env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(autorestic_env.read_text(), 'AUTORESTIC_BACKEND_KEY=keep-exact-literal\n')
        # build: service must have pinned the audited image ID and passed --no-build --pull never!
        up_call = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'backup' and c['subcmd'][:1] == ['up']][0]
        self.assertEqual(up_call['merged_images']['autorestic'], 'sha256:666666666666')
        self.assertIn('--no-build', up_call['subcmd'])

    def test_ambiguous_project_secret_drift_and_post_verify_rollback(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        # 1. Ambiguous project across services in same directory fails plan
        self.docker_state['containers']['cid-db']['project'] = 'other-project'
        self._save_docker_state()
        res_amb = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'amb-plan.json'),
        )
        self.assertNotEqual(res_amb.returncode, 0)
        self.assertIn('ambiguous', res_amb.stderr.lower())
        self.docker_state['containers']['cid-db']['project'] = 'legacy-webapp-proj'
        self._save_docker_state()

        # 2. Valid plan -> encrypted secret file drift rejected before apply
        plan_path = self.outside / 'sec-drift-plan.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--plan', str(plan_path),
            ).returncode,
            0,
        )
        enc_file = self.repo / 'secrets/medion/webapp.enc.json'
        orig_enc = enc_file.read_text(encoding='utf-8')
        enc_doc = json.loads(orig_enc)
        enc_doc['APP_TOKEN'] = 'ENC[FAKE:"mutated-token"]'
        enc_file.write_text(json.dumps(enc_doc), encoding='utf-8')
        res_sdrift = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'snap-2',
        )
        self.assertNotEqual(res_sdrift.returncode, 0)
        self.assertIn('drift', res_sdrift.stderr.lower())
        enc_file.write_text(orig_enc, encoding='utf-8')

        # 3. Post-apply verification failure (container fails to stay running) triggers rollback + compose restore
        self.docker_state['force_stopped_after_up'] = {'legacy-webapp-proj': True}
        self._save_docker_state()
        res_post = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'snap-2',
        )
        self.assertNotEqual(res_post.returncode, 0)
        self.assertIn('rollback', (res_post.stdout + res_post.stderr).lower())
        webapp_remote = self.remote_root / 'medion/webapp'
        self.assertTrue((webapp_remote / '.env').is_file())
        self.assertFalse((webapp_remote / '.secrets.env').exists())
        up_calls = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'legacy-webapp-proj' and c['subcmd'][:1] == ['up']]
        self.assertGreaterEqual(len(up_calls), 2, 'expected both initial up and rollback restore up')

    def test_real_repository_stack_discovery_and_staging_never_touches_data(self):
        spec = importlib.util.spec_from_file_location('ssh_migrate_mod', SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        manifest, _ = mod.load_manifest(REPO_ROOT)

        medion = mod.discover_host_stacks(REPO_ROOT, manifest, 'medion')
        for expected in ('actual-budget', 'jellyfin', 'stick-it-homepage'):
            self.assertIn(expected, medion)
            self.assertEqual(medion[expected]['kind'], 'credentialless')
        thinkpad = mod.discover_host_stacks(REPO_ROOT, manifest, 'thinkpad')
        self.assertIn('glance', thinkpad)
        self.assertEqual(thinkpad['glance']['kind'], 'credentialless')

        # Verify hiring_scraper one-shot migrate service is caught on the real repo!
        hs_services, _, _ = mod.load_merged_compose(REPO_ROOT, medion['hiring_scraper'])
        self.assertEqual(mod.detect_oneshot_services(hs_services), ['migrate'])

        # Verify staged file lists across EVERY host & stack never include secret or DB files
        for host in manifest:
            catalog = mod.discover_host_stacks(REPO_ROOT, manifest, host)
            for name, entry in catalog.items():
                if entry['kind'] == 'dependency':
                    continue
                deps = [manifest[host][d] for d in entry.get('dependencies', [])]
                secret_rels = set(entry.get('files', {}).values())
                if entry.get('env_file'):
                    secret_rels.add(entry['env_file'])
                staged = mod.collect_staged_repo_files(REPO_ROOT, entry, deps, secret_rels)
                self.assertTrue(staged, f'{host}/{name} should stage at least its compose file')
                for rel in staged:
                    self.assertNotIn('.secrets', rel)
                    self.assertFalse(rel.endswith('/.env'))
                    self.assertNotIn('hiring_db', rel)


    def test_docker_sudo_env_reset_preserves_secrets_and_home_without_argv_leak(self):
        # Add required ${APP_TOKEN:?}, optional ${OPT_SECRET}, and ~/.ssh bind mount matching SSH user home
        ssh_dir = self.remote_home / '.ssh'
        ssh_dir.mkdir(exist_ok=True)
        (self.repo / 'medion/webapp/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': 'mariadb:11.8',
                    'environment': {'MARIADB_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['database:/var/lib/mysql'],
                    'secrets': ['db_password'],
                },
                'web': {
                    'image': 'example/webapp:1.2',
                    'depends_on': ['db'],
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'environment': {
                        'REQ_TOKEN': '${APP_TOKEN:?APP_TOKEN is required}',
                        'OPT_TOKEN': '${OPT_SECRET:-}',
                    },
                    'volumes': [
                        './config/app.conf:/etc/app/app.conf:ro',
                        '${UPLOAD_LOCATION}:/uploads',
                        '~/.ssh:/root/.ssh:ro',
                    ],
                },
            },
            'volumes': {'database': None},
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')
        self.manifest['medion']['webapp']['optional'] = ['OPT_SECRET']
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )
        (self.remote_root / 'medion/webapp/.env').write_text(
            "DB_PASSWORD='db$pass#literal'\n"
            "APP_TOKEN='tok$en!value'\n"
            "OPT_SECRET='opt-secret-value'\n"
            f'UPLOAD_LOCATION={self.uploads_dir}\n',
            encoding='utf-8',
        )
        self.docker_state['containers']['cid-web']['mounts'].append(
            {'Type': 'bind', 'Source': str(ssh_dir.resolve()), 'Destination': '/root/.ssh'}
        )
        self._save_docker_state()

        # First verify sudo -n failure fails clearly during plan
        fail_plan = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--docker-sudo', '--plan', str(self.outside / 'sudo-fail-plan.json'),
            extra_env={'FAKE_SUDO_FAIL': '1'},
        )
        self.assertNotEqual(fail_plan.returncode, 0)
        self.assertIn('sudo', fail_plan.stderr.lower())

        # Import secrets and run full plan + apply under --docker-sudo (FAKE_SUDO_PY wipes env + sets HOME=/root!)
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--docker-sudo', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'sudo-plan.json'
        plan_res = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--docker-sudo', '--plan', str(plan_path),
        )
        self.assertEqual(plan_res.returncode, 0, plan_res.stderr)
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        self.assertEqual(plan['stacks']['webapp']['compose_home'], str(self.remote_home))

        apply_res = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'sudo-snap',
        )
        self.assertEqual(apply_res.returncode, 0, apply_res.stderr)

        # Verify docker compose received secrets + SSH user's HOME despite sudo env_reset
        up_calls = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'legacy-webapp-proj' and c['subcmd'][:1] == ['up']]
        self.assertTrue(up_calls)
        for c in up_calls:
            self.assertEqual(c['sudo_active'], '1')
            self.assertEqual(c['home'], str(self.remote_home))
            self.assertEqual(c['env_snapshot'].get('APP_TOKEN'), 'tok$en!value')
            self.assertEqual(c['env_snapshot'].get('OPT_SECRET'), 'opt-secret-value')
        # Zero secrets in sudo or docker argv
        sudo_entries = self.read_jsonl(self.sudo_log)
        self.assertTrue(sudo_entries)
        for entry in sudo_entries:
            joined = ' '.join(entry['argv'])
            self.assertNotIn('tok$en!value', joined)
            self.assertNotIn('opt-secret-value', joined)
            self.assertNotIn('db$pass#literal', joined)
            self.assertNotIn('-E', entry['argv'])
        for entry in self.read_jsonl(self.docker_log):
            joined = ' '.join(entry['argv'])
            self.assertNotIn('tok$en!value', joined)
            self.assertNotIn('opt-secret-value', joined)

        # Also test --docker-sudo when existing ~/.ssh mount matches sudo_home_dir (root home)
        sudo_root_home = self.base / 'fake_root_home'
        (sudo_root_home / '.ssh').mkdir(parents=True)
        self.docker_state['containers']['cid-web']['mounts'][-1]['Source'] = str((sudo_root_home / '.ssh').resolve())
        self._save_docker_state()
        root_plan_path = self.outside / 'sudo-root-plan.json'
        root_plan_res = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--docker-sudo', '--plan', str(root_plan_path),
            extra_env={'FAKE_SUDO_HOME': str(sudo_root_home)},
        )
        self.assertEqual(root_plan_res.returncode, 0, root_plan_res.stderr)
        root_plan = json.loads(root_plan_path.read_text(encoding='utf-8'))
        self.assertEqual(root_plan['stacks']['webapp']['compose_home'], str(sudo_root_home))

    def test_mutable_rw_configs_and_untracked_files_not_staged(self):
        # 1. Verify on real homelab repository that RW mutable config dirs are NEVER staged
        real_manifest, _ = ssh_migrate.load_manifest(REPO_ROOT)
        def stage_for(host, stack):
            stacks = ssh_migrate.discover_host_stacks(REPO_ROOT, real_manifest, host)
            entry = stacks[stack]
            deps = [real_manifest[host][d] for d in entry.get('dependencies', [])]
            sec_rels = set(entry.get('files', {}).values())
            if entry.get('env_file'):
                sec_rels.add(entry['env_file'])
            return ssh_migrate.collect_staged_repo_files(REPO_ROOT, entry, deps, sec_rels)

        for host, stack, forbidden_prefix in [
            ('thinkpad', 'ha', 'thinkpad/ha/config/'),
            ('dell', 't3-code', 'dell/t3-code/skills/'),
            ('medion', 'immich-app', 'medion/immich-app/config/'),
            ('thinkpad', 'glance', 'thinkpad/glance/config/'),
            ('thinkpad', 'glance', 'thinkpad/glance/assets/'),
        ]:
            staged = stage_for(host, stack)
            self.assertFalse(
                any(p.startswith(forbidden_prefix) for p in staged),
                f'{host}/{stack} unexpectedly staged mutable RW path under {forbidden_prefix}: {staged}',
            )
        # Verify ionos/traefik/dynamic/oidc-middleware.yml IS staged (:ro) and hiring_scraper seccomp IS staged
        self.assertIn('ionos/traefik/dynamic/oidc-middleware.yml', stage_for('ionos', 'traefik'))
        self.assertIn('medion/hiring_scraper/docker/chromium-seccomp.json', stage_for('medion', 'hiring_scraper'))

        # 2. In a real git repo, verify untracked files inside :ro dirs and tracked RW files are NOT staged
        git_repo = self.base / 'git_stage_repo'
        (git_repo / 'medion/app/ro-cfg').mkdir(parents=True)
        (git_repo / 'medion/app/rw-cfg').mkdir(parents=True)
        (git_repo / 'medion/app/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'web': {
                    'image': 'nginx:alpine',
                    'volumes': [
                        './ro-cfg:/etc/ro-cfg:ro',
                        './rw-cfg:/etc/rw-cfg',
                    ],
                }
            }
        }, sort_keys=False), encoding='utf-8')
        (git_repo / 'medion/app/ro-cfg/tracked.yml').write_text('tracked: true\n', encoding='utf-8')
        (git_repo / 'medion/app/ro-cfg/untracked-scratch.yml').write_text('scratch: true\n', encoding='utf-8')
        (git_repo / 'medion/app/rw-cfg/default.yml').write_text('rw: true\n', encoding='utf-8')
        subprocess.run(['git', 'init', '-q'], cwd=str(git_repo), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(git_repo), check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=str(git_repo), check=True)
        subprocess.run(
            ['git', 'add', 'medion/app/docker-compose.yml', 'medion/app/ro-cfg/tracked.yml', 'medion/app/rw-cfg/default.yml'],
            cwd=str(git_repo), check=True,
        )
        subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=str(git_repo), check=True)

        spec = {
            'name': 'app',
            'host': 'medion',
            'directory': 'medion/app',
            'compose': ['docker-compose.yml'],
        }
        staged = ssh_migrate.collect_staged_repo_files(git_repo, spec, [], set())
        self.assertEqual(staged, ['medion/app/docker-compose.yml', 'medion/app/ro-cfg/tracked.yml'])

    def test_stopped_services_and_new_services_wave_adoption_and_rollback(self):
        # 2 running services (db healthy, app depends_on db:service_healthy), 1 stopped service (worker), 1 new service (sidecar)
        (self.repo / 'medion/webapp/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': 'mariadb:11.8',
                    'environment': {'MARIADB_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['database:/var/lib/mysql'],
                    'secrets': ['db_password'],
                },
                'web': {
                    'image': 'example/webapp:1.2',
                    'depends_on': {'db': {'condition': 'service_healthy'}},
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'volumes': ['./config/app.conf:/etc/app/app.conf:ro', '${UPLOAD_LOCATION}:/uploads'],
                },
                'worker': {
                    'image': 'example/webapp:1.2',
                    'depends_on': ['db'],
                },
                'sidecar': {
                    'image': 'tecnativa/docker-socket-proxy:0.3',
                },
            },
            'volumes': {'database': None},
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')
        self.docker_state['containers']['cid-db']['health'] = 'healthy'
        self.docker_state['containers']['cid-worker'] = {
            'id': 'cid-worker',
            'name': '/legacy-webapp-proj-worker-1',
            'image': 'sha256:111111111111',
            'configured_image': 'example/webapp:1.2',
            'project': 'legacy-webapp-proj',
            'service': 'worker',
            'working_dir': str((self.remote_root / 'medion/webapp').resolve()),
            'config_files': str((self.remote_root / 'medion/webapp/docker-compose.yml').resolve()),
            'mounts': [],
            'running': False,
            'exit_code': 0,
        }
        self.docker_state['images']['sha256:999999999999'] = {'repo_digests': [], 'tags': ['tecnativa/docker-socket-proxy:0.3']}
        self.docker_state.setdefault('image_tags', {})['tecnativa/docker-socket-proxy:0.3'] = 'sha256:999999999999'
        self._save_docker_state()

        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        # Without --allow-new-services, sidecar is rejected
        blocked = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'no-new.json'),
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('sidecar', blocked.stderr)

        # With --allow-new-services, plan generates 2 waves: wave 1 (db, sidecar; wait_healthy=[db]), wave 2 (app); worker excluded!
        plan_path = self.outside / 'waves-plan.json'
        plan_res = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--allow-new-services', '--plan', str(plan_path),
        )
        self.assertEqual(plan_res.returncode, 0, plan_res.stderr)
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        st = plan['stacks']['webapp']
        self.assertEqual(st['expected_stopped_services'], ['worker'])
        self.assertEqual(st['new_services'], ['sidecar'])
        self.assertEqual(len(st['start_waves']), 2)
        self.assertIn('db', st['start_waves'][0]['services'])
        self.assertEqual(st['start_waves'][0]['wait_healthy'], ['db'])
        self.assertEqual(st['start_waves'][1]['services'], ['web'])

        apply_res = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'waves-snap',
        )
        self.assertEqual(apply_res.returncode, 0, apply_res.stderr)

        # Verify waves ran with --no-deps and explicit service targets, worker stayed stopped, sidecar is running
        up_calls = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'legacy-webapp-proj' and c['subcmd'][:1] == ['up']]
        self.assertEqual(len(up_calls), 2)
        self.assertTrue(all(c['no_deps'] for c in up_calls))
        self.assertNotIn('worker', up_calls[0]['target_services'] + up_calls[1]['target_services'])
        state_after = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        self.assertFalse(state_after['containers']['cid-worker']['running'])
        self.assertTrue(any(c['service'] == 'sidecar' and c['running'] for c in state_after['containers'].values()))

        # Simulate local tag moving before rollback -> rollback must pin original image ID via temp override,
        # remove ONLY sidecar via compose rm -s -f (no -v!), and keep worker stopped!
        state_after.setdefault('image_tags', {})['example/webapp:1.2'] = 'sha256:eeeeeeeeeeee'
        state_after['images']['sha256:eeeeeeeeeeee'] = {'repo_digests': [], 'tags': ['example/webapp:1.2']}
        self.docker_state_path.write_text(json.dumps(state_after, indent=2), encoding='utf-8')

        rb_res = self.run_cli('rollback', '--plan', str(plan_path))
        self.assertEqual(rb_res.returncode, 0, rb_res.stderr)
        state_rb = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        self.assertFalse(any(c['service'] == 'sidecar' for c in state_rb['containers'].values()))
        self.assertFalse(state_rb['containers']['cid-worker']['running'])
        self.assertEqual(state_rb['containers']['cid-web']['image'], 'sha256:111111111111')
        rm_calls = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'legacy-webapp-proj' and c['subcmd'][:1] == ['rm']]
        self.assertTrue(rm_calls)
        self.assertEqual(rm_calls[-1]['target_services'], ['sidecar'])
        self.assertNotIn('-v', rm_calls[-1]['subcmd'])
        # Temporary rollback override file must be cleaned up
        self.assertFalse((self.remote_root / 'medion/webapp/.homelab-rollback.override.yml').exists())

        # Also verify pre-Compose failure (config --quiet fails -> compose_up_attempted=False) restores files WITHOUT running compose up!
        self.docker_state['containers']['cid-worker']['running'] = False
        self._save_docker_state()
        plan2 = self.outside / 'pre-compose-fail-plan.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--allow-new-services', '--plan', str(plan2),
            ).returncode,
            0,
        )
        self.docker_log.write_text('', encoding='utf-8')
        cur_state = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        cur_state['fail_rules']['legacy-webapp-proj:config'] = 'simulated config validation failure'
        self.docker_state_path.write_text(json.dumps(cur_state, indent=2), encoding='utf-8')
        fail_apply = self.run_cli(
            'apply', '--plan', str(plan2),
            '--confirm-backup-tested', '--backup-evidence', 'pre-compose-snap',
        )
        self.assertNotEqual(fail_apply.returncode, 0)
        up_after_config_fail = [c for c in self.read_jsonl(self.docker_log) if c['subcmd'][:1] == ['up']]
        self.assertEqual(up_after_config_fail, [])
        self.assertTrue((self.remote_root / 'medion/webapp/.env').is_file())

    def test_hiring_scraper_oneshot_safe_adoption_and_failure_blocks(self):
        # Set up synthetic hiring_scraper with db (healthy), migrate (restart: "no"), app (depends_on migrate:service_completed_successfully), worker
        (self.repo / 'medion/hiring_scraper').mkdir(parents=True, exist_ok=True)
        (self.repo / 'medion/hiring_scraper/docker/chromium-seccomp.json').write_text('{}\n', encoding='utf-8')
        (self.repo / 'medion/hiring_scraper/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': '${HIRING_DB_IMAGE:-postgres:16-alpine}',
                    'restart': 'unless-stopped',
                    'environment': {'POSTGRES_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['./hiring_db:/var/lib/postgresql/data'],
                    'secrets': ['db_password'],
                },
                'migrate': {
                    'image': '${HIRING_APP_IMAGE:-ghcr.io/example/hiring:1.0}',
                    'restart': 'no',
                    'depends_on': {'db': {'condition': 'service_healthy'}},
                    'command': ['alembic', 'upgrade', 'head'],
                },
                'app': {
                    'image': '${HIRING_APP_IMAGE:-ghcr.io/example/hiring:1.0}',
                    'restart': 'unless-stopped',
                    'depends_on': {'migrate': {'condition': 'service_completed_successfully'}},
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                },
                'worker': {
                    'image': '${HIRING_APP_IMAGE:-ghcr.io/example/hiring:1.0}',
                    'restart': 'unless-stopped',
                    'depends_on': {'migrate': {'condition': 'service_completed_successfully'}},
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                },
            },
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')
        self.manifest['medion']['hiring_scraper'] = {
            'directory': 'medion/hiring_scraper',
            'compose': ['docker-compose.yml'],
            'env_file': 'medion/hiring_scraper/.secrets.env',
            'required': ['DB_PASSWORD', 'APP_TOKEN'],
            'files': {'DB_PASSWORD': 'medion/hiring_scraper/.secrets/db_password'},
            'encrypted_file': 'secrets/medion/hiring_scraper.enc.json',
            'migration': {'env_files': ['medion/hiring_scraper/.env'], 'file_values': {}},
        }
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )
        hs_remote = self.remote_root / 'medion/hiring_scraper'
        (hs_remote / 'hiring_db').mkdir(parents=True, exist_ok=True)
        (hs_remote / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {'image': 'postgres:16-alpine', 'volumes': ['./hiring_db:/var/lib/postgresql/data']},
                'migrate': {'image': 'ghcr.io/example/hiring:1.0', 'restart': 'no'},
                'app': {'image': 'ghcr.io/example/hiring:1.0'},
                'worker': {'image': 'ghcr.io/example/hiring:1.0'},
            },
        }, sort_keys=False), encoding='utf-8')
        (hs_remote / '.env').write_text('DB_PASSWORD=hiring-pw\nAPP_TOKEN=hiring-tok\n', encoding='utf-8')

        self.docker_state['images']['sha256:aaaa11111111'] = {'repo_digests': [], 'tags': ['ghcr.io/example/hiring:1.0']}
        self.docker_state.setdefault('image_tags', {})['ghcr.io/example/hiring:1.0'] = 'sha256:aaaa11111111'
        self.docker_state.setdefault('image_tags', {})['postgres:16-alpine'] = 'sha256:111111111111'
        base_containers = {
            'cid-hs-db': {
                'id': 'cid-hs-db', 'name': '/hiring_scraper-db-1',
                'image': 'sha256:111111111111', 'configured_image': 'postgres:16-alpine',
                'project': 'hiring_scraper', 'service': 'db',
                'working_dir': str(hs_remote.resolve()),
                'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
                'mounts': [{'Type': 'bind', 'Source': str((hs_remote / 'hiring_db').resolve()), 'Destination': '/var/lib/postgresql/data'}],
                'running': True, 'health': 'healthy', 'exit_code': 0,
            },
            'cid-hs-app': {
                'id': 'cid-hs-app', 'name': '/hiring_scraper-app-1',
                'image': 'sha256:aaaa11111111', 'configured_image': 'ghcr.io/example/hiring:1.0',
                'project': 'hiring_scraper', 'service': 'app',
                'working_dir': str(hs_remote.resolve()),
                'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
                'mounts': [], 'running': True, 'exit_code': 0,
            },
            'cid-hs-worker': {
                'id': 'cid-hs-worker', 'name': '/hiring_scraper-worker-1',
                'image': 'sha256:aaaa11111111', 'configured_image': 'ghcr.io/example/hiring:1.0',
                'project': 'hiring_scraper', 'service': 'worker',
                'working_dir': str(hs_remote.resolve()),
                'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
                'mounts': [], 'running': True, 'exit_code': 0,
            },
            'cid-hs-migrate': {
                'id': 'cid-hs-migrate', 'name': '/hiring_scraper-migrate-1',
                'image': 'sha256:aaaa11111111', 'configured_image': 'ghcr.io/example/hiring:1.0',
                'project': 'hiring_scraper', 'service': 'migrate',
                'working_dir': str(hs_remote.resolve()),
                'config_files': str((hs_remote / 'docker-compose.yml').resolve()),
                'mounts': [], 'running': False, 'exit_code': 0,
            },
        }
        for _k in [k for k, v in self.docker_state['containers'].items() if v.get('project') == 'hiring_scraper']:
            del self.docker_state['containers'][_k]
        self.docker_state['containers'].update(base_containers)
        self._save_docker_state()

        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'hiring_scraper', '--apply',
            ).returncode,
            0,
        )

        # Case A: migrate running -> BLOCKED
        self.docker_state['containers']['cid-hs-migrate']['running'] = True
        self._save_docker_state()
        res_run = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'hiring_scraper',
            '--plan', str(self.outside / 'hs-run.json'),
        )
        self.assertNotEqual(res_run.returncode, 0)
        self.assertIn('currently running', res_run.stderr)

        # Case B: migrate exited non-zero -> BLOCKED
        self.docker_state['containers']['cid-hs-migrate']['running'] = False
        self.docker_state['containers']['cid-hs-migrate']['exit_code'] = 1
        self._save_docker_state()
        res_fail = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'hiring_scraper',
            '--plan', str(self.outside / 'hs-fail.json'),
        )
        self.assertNotEqual(res_fail.returncode, 0)
        self.assertIn('non-zero status (1)', res_fail.stderr)

        # Case C: migrate exited 0 -> ALLOWED, waves propagate db(wait_healthy) -> app/worker, migrate NEVER run!
        self.docker_state['containers']['cid-hs-migrate']['exit_code'] = 0
        self._save_docker_state()
        hs_plan = self.outside / 'hs-ok.json'
        res_ok = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'hiring_scraper',
            '--plan', str(hs_plan),
        )
        self.assertEqual(res_ok.returncode, 0, res_ok.stderr)
        pdoc = json.loads(hs_plan.read_text(encoding='utf-8'))
        st = pdoc['stacks']['hiring_scraper']
        self.assertEqual(st['oneshot_services'], ['migrate'])
        self.assertEqual(st['start_waves'][0]['services'], ['db'])
        self.assertEqual(st['start_waves'][0]['wait_healthy'], ['db'])
        self.assertEqual(sorted(st['start_waves'][1]['services']), ['app', 'worker'])

        self.docker_log.write_text('', encoding='utf-8')
        apply_res = self.run_cli(
            'apply', '--plan', str(hs_plan),
            '--confirm-backup-tested', '--backup-evidence', 'hs-snap',
        )
        self.assertEqual(apply_res.returncode, 0, apply_res.stderr)
        up_calls = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'hiring_scraper' and c['subcmd'][:1] == ['up']]
        self.assertEqual(len(up_calls), 2)
        self.assertTrue(all(c['no_deps'] for c in up_calls))
        all_targeted = [s for c in up_calls for s in c['target_services']]
        self.assertNotIn('migrate', all_targeted)
        self.assertEqual(up_calls[0]['target_services'], ['db'])
        self.assertEqual(sorted(up_calls[1]['target_services']), ['app', 'worker'])

        # Rollback also never runs migrate
        rb_res = self.run_cli('rollback', '--plan', str(hs_plan))
        self.assertEqual(rb_res.returncode, 0, rb_res.stderr)
        all_up_after_rb = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'hiring_scraper' and c['subcmd'][:1] == ['up']]
        self.assertNotIn('migrate', [s for c in all_up_after_rb for s in c['target_services']])

        # Case D: migrate container was pruned (absent) -> still succeeds WITHOUT --allow-new-services and never creates migrate!
        cur = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        cur['containers'].pop('cid-hs-migrate', None)
        self.docker_state_path.write_text(json.dumps(cur, indent=2), encoding='utf-8')
        hs_pruned = self.outside / 'hs-pruned.json'
        res_pruned = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'hiring_scraper',
            '--plan', str(hs_pruned),
        )
        self.assertEqual(res_pruned.returncode, 0, res_pruned.stderr)
        self.assertEqual(
            self.run_cli(
                'apply', '--plan', str(hs_pruned),
                '--confirm-backup-tested', '--backup-evidence', 'hs-pruned-snap',
            ).returncode,
            0,
        )
        cur_after = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        self.assertFalse(any(c['project'] == 'hiring_scraper' and c['service'] == 'migrate' for c in cur_after['containers'].values()))

    def test_traefik_docker_sock_transition_and_anonymous_volumes(self):
        (self.repo / 'medion/traefik/dynamic').mkdir(parents=True)
        (self.repo / 'medion/traefik/dynamic/oidc.yml').write_text('http: {}\n', encoding='utf-8')
        (self.repo / 'medion/traefik/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'socket-proxy': {
                    'image': 'tecnativa/docker-socket-proxy:0.3',
                    'volumes': ['/var/run/docker.sock:/var/run/docker.sock:ro'],
                },
                'traefik': {
                    'image': 'traefik:v3.2',
                    'depends_on': ['socket-proxy'],
                    'volumes': [
                        './dynamic:/etc/traefik/dynamic:ro',
                        'acme_data:/letsencrypt',
                    ],
                },
            },
            'volumes': {'acme_data': None},
        }, sort_keys=False), encoding='utf-8')
        tr_remote = self.remote_root / 'medion/traefik'
        (tr_remote / 'dynamic').mkdir(parents=True)
        (tr_remote / 'docker-compose.yml').write_text('services:\n  traefik:\n    image: traefik:v3.2\n', encoding='utf-8')
        sock_path = self.base / 'fake_docker.sock'
        sock_path.write_text('', encoding='utf-8')

        anon_hex = 'a' * 64
        self.docker_state['volumes']['traefik_acme_data'] = {'driver': 'local', 'mountpoint': '/v/acme', 'labels': {'com.docker.compose.volume': 'acme_data'}}
        self.docker_state['volumes']['anon_labeled_vol'] = {'driver': 'local', 'mountpoint': '/v/anon1', 'labels': {'com.docker.volume.anonymous': ''}}
        self.docker_state['volumes'][anon_hex] = {'driver': 'local', 'mountpoint': '/v/anon2', 'labels': {}}
        self.docker_state['images']['sha256:bbbb11111111'] = {'repo_digests': [], 'tags': ['traefik:v3.2']}
        self.docker_state.setdefault('image_tags', {})['traefik:v3.2'] = 'sha256:bbbb11111111'
        self.docker_state['images']['sha256:cccc11111111'] = {'repo_digests': [], 'tags': ['tecnativa/docker-socket-proxy:0.3']}
        self.docker_state.setdefault('image_tags', {})['tecnativa/docker-socket-proxy:0.3'] = 'sha256:cccc11111111'
        self.docker_state['containers']['cid-traefik'] = {
            'id': 'cid-traefik',
            'name': '/traefik',
            'image': 'sha256:bbbb11111111',
            'configured_image': 'traefik:v3.2',
            'project': 'traefik',
            'service': 'traefik',
            'working_dir': str(tr_remote.resolve()),
            'config_files': str((tr_remote / 'docker-compose.yml').resolve()),
            'mounts': [
                {'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock'},
                {'Type': 'bind', 'Source': str((tr_remote / 'dynamic').resolve()), 'Destination': '/etc/traefik/dynamic'},
                {'Type': 'volume', 'Name': 'traefik_acme_data', 'Source': '/v/acme', 'Destination': '/letsencrypt'},
                {'Type': 'volume', 'Name': 'anon_labeled_vol', 'Source': '/v/anon1', 'Destination': '/tmp/anon1'},
                {'Type': 'volume', 'Name': anon_hex, 'Source': '/v/anon2', 'Destination': '/tmp/anon2'},
            ],
            'running': True,
        }
        self._save_docker_state()

        # Note: /var/run/docker.sock might not exist in container test env; mock check_abs_paths if absent
        # Wait: /var/run/docker.sock is checked by check_abs_paths; test using a real existing path if /var/run/docker.sock doesn't exist
        if not Path('/var/run/docker.sock').exists():
            # Verify docker.sock transition logic via unit call + temp socket path for full CLI check
            self.assertTrue(ssh_migrate.is_anonymous_volume('anon_labeled_vol', self.docker_state['volumes']['anon_labeled_vol']))
            self.assertTrue(ssh_migrate.is_anonymous_volume(anon_hex, self.docker_state['volumes'][anon_hex]))
            self.assertFalse(ssh_migrate.is_anonymous_volume('traefik_acme_data', self.docker_state['volumes']['traefik_acme_data']))
            pass
        # Even better: test directly via in-process main() with patched abs_paths so it runs identically whether /var/run/docker.sock exists or not!
        orig_call = ssh_migrate.SshClient.call
        def fake_sock_run(self_ssh, payload, *args, **kwargs):
            res = orig_call(self_ssh, payload, *args, **kwargs)
            if isinstance(res, dict) and 'checked_paths' in res and '/var/run/docker.sock' in res['checked_paths']:
                res['checked_paths']['/var/run/docker.sock'] = {'exists': True, 'realpath': '/var/run/docker.sock'}
            return res

        tr_plan = self.outside / 'traefik-plan.json'
        old_env = dict(os.environ)
        os.environ.update({
            'PATH': f'{self.controller_bin}:{os.environ.get("PATH", "")}',
            'FAKE_SSH_LOG': str(self.ssh_log),
            'FAKE_REMOTE_BIN': str(self.remote_bin),
            'FAKE_REMOTE_HOME': str(self.remote_home),
            'FAKE_DOCKER_STATE': str(self.docker_state_path),
            'FAKE_DOCKER_LOG': str(self.docker_log),
            'FAKE_SUDO_LOG': str(self.sudo_log),
        })
        def run_inproc(cli_args):
            with unittest.mock.patch.object(ssh_migrate.SshClient, 'call', autospec=True, side_effect=fake_sock_run):
                with unittest.mock.patch.object(sys, 'argv', ['ssh-migrate.py'] + cli_args):
                    return ssh_migrate.main()

        try:
            rc = run_inproc([
                '--root', str(self.repo), 'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'traefik',
                '--allow-new-services', '--plan', str(tr_plan),
            ])
            self.assertEqual(rc, 0)
            # Now add a real (non-anonymous) named volume mismatch -> MUST FAIL!
            self.docker_state['volumes']['real_unmapped_vol'] = {'driver': 'local', 'mountpoint': '/v/real', 'labels': {'com.docker.compose.volume': 'real'}}
            self.docker_state['containers']['cid-traefik']['mounts'].append(
                {'Type': 'volume', 'Name': 'real_unmapped_vol', 'Source': '/v/real', 'Destination': '/real-data'}
            )
            self._save_docker_state()
            rc_bad_vol = run_inproc([
                '--root', str(self.repo), 'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'traefik',
                '--allow-new-services', '--plan', str(self.outside / 'tr-bad-vol.json'),
            ])
            self.assertNotEqual(rc_bad_vol, 0)
            # Remove real_unmapped_vol and remove socket-proxy -> dropping docker.sock WITHOUT socket-proxy MUST FAIL!
            self.docker_state['containers']['cid-traefik']['mounts'].pop()
            self._save_docker_state()
            (self.repo / 'medion/traefik/docker-compose.yml').write_text(yaml.safe_dump({
                'services': {
                    'traefik': {
                        'image': 'traefik:v3.2',
                        'volumes': ['./dynamic:/etc/traefik/dynamic:ro', 'acme_data:/letsencrypt'],
                    },
                },
                'volumes': {'acme_data': None},
            }, sort_keys=False), encoding='utf-8')
            rc_no_proxy = run_inproc([
                '--root', str(self.repo), 'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'traefik',
                '--plan', str(self.outside / 'tr-no-proxy.json'),
            ])
            self.assertNotEqual(rc_no_proxy, 0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_path_containment_nonexistent_traversal_and_symlinks(self):
        root = self.base / 'contain_root'
        (root / 'medion/webapp').mkdir(parents=True)
        outside_target = self.base / 'escaped.txt'
        outside_target.write_text('do-not-touch\n', encoding='utf-8')

        # 1. Non-existent intermediate + '..' traversal MUST be rejected
        with self.assertRaises(ValueError):
            ssh_remote_agent.resolve_inside(root, 'nonexistent/../../escaped.txt')
        with self.assertRaises(ValueError):
            ssh_remote_agent.resolve_inside(root, '../escaped.txt')
        # Valid non-existent nested path succeeds
        ok_p = ssh_remote_agent.resolve_inside(root, 'medion/webapp/.secrets/db_password', reject_symlinks=True)
        self.assertEqual(ok_p, root.resolve() / 'medion/webapp/.secrets/db_password')

        # 2. Symlink and dangling symlink strictly inside root rejected when reject_symlinks=True
        sym = root / 'medion/webapp/link.yml'
        sym.symlink_to(outside_target)
        with self.assertRaises(ValueError):
            ssh_remote_agent.resolve_inside(root, 'medion/webapp/link.yml', reject_symlinks=True)
        dangling = root / 'medion/webapp/dangling.yml'
        dangling.symlink_to(self.base / 'does-not-exist.yml')
        with self.assertRaises(ValueError):
            ssh_remote_agent.resolve_inside(root, 'medion/webapp/dangling.yml', reject_symlinks=True)
        dir_sym = root / 'medion/symlink_dir'
        dir_sym.symlink_to(self.base)
        with self.assertRaises(ValueError):
            ssh_remote_agent.resolve_inside(root, 'medion/symlink_dir/escaped.txt', reject_symlinks=True)

        # 3. Disjoint rollback_dir enforcement
        with self.assertRaises(ValueError):
            ssh_remote_agent.ensure_disjoint_rollback_dir(root / 'rb', root)
        with self.assertRaises(ValueError):
            ssh_remote_agent.ensure_disjoint_rollback_dir(root, root)

        # 4. handle_apply_stack rejects bad stack name / symlink target / rollback_dir inside root BEFORE creating rollback_dir
        rb_bad_stack = self.outside / 'rb-bad-stack'
        with self.assertRaises(ValueError):
            ssh_remote_agent.handle_apply_stack({
                'remote_root': str(root),
                'rollback_dir': str(rb_bad_stack),
                'stack': '../evil',
                'remote_stack_dir': 'medion/webapp',
                'project_name': 'webapp',
                'compose_files': ['docker-compose.yml'],
            }, set())
        self.assertFalse(rb_bad_stack.exists())

        rb_symlink = self.outside / 'rb-symlink'
        with self.assertRaises(ValueError) as ctx_sym:
            ssh_remote_agent.handle_apply_stack({
                'remote_root': str(root),
                'rollback_dir': str(rb_symlink),
                'stack': 'webapp',
                'remote_stack_dir': 'medion/webapp',
                'project_name': 'webapp',
                'compose_files': ['docker-compose.yml'],
                'use_override': False,
                'override_filename': '.homelab-adopt.override.yml',
                'staged_files': [{'remote_path': 'medion/webapp/link.yml', 'b64': base64.b64encode(b'x').decode(), 'mode': 0o644}],
            }, set())
        self.assertIn('symlink', str(ctx_sym.exception).lower())
        self.assertFalse(rb_symlink.exists())
        self.assertEqual(outside_target.read_text(), 'do-not-touch\n')

        # 5. handle_rollback rejects missing rollback_dir and rejects tampered backup_rel traversal
        res_missing = ssh_remote_agent.handle_rollback({
            'rollback_dir': str(self.outside / 'no-such-rb'),
            'stacks': ['webapp'],
        }, set())
        self.assertFalse(res_missing['ok'])

        tampered_rb = self.outside / 'tampered-rb'
        (tampered_rb / 'stacks/webapp').mkdir(parents=True)
        (tampered_rb / 'index.json').write_text(json.dumps({
            'remote_root': str(root),
            'stacks': ['webapp'],
        }), encoding='utf-8')
        (tampered_rb / 'stacks/webapp/state.json').write_text(json.dumps({
            'stack': 'webapp',
            'remote_root': str(root),
            'remote_stack_dir': 'medion/webapp',
            'directory': 'medion/webapp',
            'project_name': 'webapp',
            'compose_up_attempted': False,
            'backed_up_files': [{'path': 'medion/webapp/docker-compose.yml', 'backup_rel': '../../escaped.txt', 'mode': 0o644}],
            'created_files': [],
        }), encoding='utf-8')
        res_tamp = ssh_remote_agent.handle_rollback({
            'rollback_dir': str(tampered_rb),
            'stacks': ['webapp'],
        }, set())
        self.assertFalse(res_tamp['ok'])
        self.assertIn('escape', res_tamp['error'].lower())

    def test_explicit_rollback_surfaces_actionable_error_and_redacts_restored_env(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'rb-err-plan.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--plan', str(plan_path),
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                'apply', '--plan', str(plan_path),
                '--confirm-backup-tested', '--backup-evidence', 'rb-err-snap',
            ).returncode,
            0,
        )
        # Fail compose up during explicit rollback with a message containing restored legacy .env secret!
        cur = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        cur['fail_rules']['legacy-webapp-proj:up'] = 'compose failed: bad token tok$en!value and db$pass#literal'
        self.docker_state_path.write_text(json.dumps(cur, indent=2), encoding='utf-8')

        rb = self.run_cli('rollback', '--plan', str(plan_path))
        self.assertNotEqual(rb.returncode, 0)
        self.assertNotIn('unknown remote error', rb.stderr.lower())
        self.assertIn('compose failed', rb.stderr.lower())
        self.assertNotIn('tok$en!value', rb.stdout + rb.stderr)
        self.assertNotIn('db$pass#literal', rb.stdout + rb.stderr)
        self.assertIn('[REDACTED]', rb.stderr)

    def test_post_apply_verification_negative_cases_trigger_rollback(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        cases = [
            ('restarting', {'web': {'restarting': True}}, 'restarting'),
            ('unhealthy', {'db': {'health': 'unhealthy'}}, 'unhealthy'),
            ('wrong_image', {'web': {'image': 'sha256:deadbeefdead'}}, 'image mismatch'),
            ('wrong_volume', {'db': {'mounts': [{'Type': 'volume', 'Name': 'wrong_empty_vol', 'Source': '/v/x', 'Destination': '/var/lib/mysql'}]}}, 'volume mismatch'),
        ]
        for label, mutation, expected_err in cases:
            with self.subTest(case=label):
                plan_path = self.outside / f'neg-{label}.json'
                self.assertEqual(
                    self.run_cli(
                        'plan', '--host', 'medion', '--ssh-target', 'medion',
                        '--source-root', str(self.remote_root), '--stack', 'webapp',
                        '--plan', str(plan_path),
                    ).returncode,
                    0,
                )
                cur = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
                cur['post_up_mutations'] = {'legacy-webapp-proj': mutation}
                cur['post_up_mutations_once'] = True
                self.docker_state_path.write_text(json.dumps(cur, indent=2), encoding='utf-8')

                res = self.run_cli(
                    'apply', '--plan', str(plan_path),
                    '--confirm-backup-tested', '--backup-evidence', f'snap-{label}',
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(expected_err, res.stderr.lower())
                self.assertIn('automatic rollback', res.stderr.lower())
                # Legacy .env restored cleanly
                self.assertTrue((self.remote_root / 'medion/webapp/.env').is_file())
                self.assertFalse((self.remote_root / 'medion/webapp/.secrets.env').exists())

    def test_skip_not_deployed_audit_before_decrypt_and_backup_credentials_dep(self):
        # 1. Undeployed stack with NO .enc.json file -> plan --all-stacks --skip-not-deployed must succeed (audit before decrypt!)
        (self.repo / 'medion/undeployed-app').mkdir(parents=True)
        (self.repo / 'medion/undeployed-app/docker-compose.yml').write_text(
            'services:\n  u:\n    image: nginx:alpine\n', encoding='utf-8'
        )
        self.manifest['medion']['undeployed-app'] = {
            'directory': 'medion/undeployed-app',
            'env_file': 'medion/undeployed-app/.secrets.env',
            'required': ['MISSING_SECRET'],
            'encrypted_file': 'secrets/medion/undeployed-app.enc.json',
            'migration': {'env_files': ['medion/undeployed-app/.env'], 'file_values': {}},
        }
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        # Remove setUp's hiring_scraper containers & legacy .env so only webapp + jellyfin are deployed
        for _k in [k for k, v in list(self.docker_state['containers'].items()) if v.get('project') == 'hiring_scraper']:
            del self.docker_state['containers'][_k]
        self._save_docker_state()
        (self.remote_root / 'medion/hiring_scraper/.env').unlink(missing_ok=True)
        self.assertFalse((self.repo / 'secrets/medion/undeployed-app.enc.json').exists())
        plan_all = self.outside / 'plan-all-skip.json'
        res = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--all-stacks', '--skip-not-deployed',
            '--plan', str(plan_all),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        pdoc = json.loads(plan_all.read_text(encoding='utf-8'))
        self.assertEqual(sorted(pdoc['stacks'].keys()), ['jellyfin', 'webapp'])

        # 2. Deployed shared-backup with missing backup-credentials source -> import-secrets --all-stacks --skip-not-deployed MUST NOT skip backup-credentials!
        (self.repo / 'shared/backup').mkdir(parents=True)
        (self.repo / 'shared/backup/docker-compose.yml').write_text(
            'services:\n  autorestic:\n    image: cupcakearmy/autorestic:1.8\n', encoding='utf-8'
        )
        self.manifest['medion']['shared-backup'] = {
            'directory': 'shared/backup',
            'env_file': 'shared/backup/.secrets.env',
            'required': ['LOCAL_BACKUP_PATH'],
            'compose': ['docker-compose.yml'],
            'dependencies': ['backup-credentials'],
            'encrypted_file': 'secrets/medion/shared-backup.enc.json',
            'migration': {'env_files': ['shared/backup/.env'], 'file_values': {}},
        }
        self.manifest['medion']['backup-credentials'] = {
            'directory': 'medion',
            'required': ['AUTORESTIC_ENV'],
            'files': {'AUTORESTIC_ENV': 'medion/.autorestic.env'},
            'encrypted_file': 'secrets/medion/backup-credentials.enc.json',
            'migration': {'env_files': [], 'file_values': {'AUTORESTIC_ENV': 'medion/.autorestic.env'}},
        }
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )
        sb_dir = self.remote_root / 'shared/backup'
        sb_dir.mkdir(parents=True)
        (sb_dir / '.env').write_text('LOCAL_BACKUP_PATH=/backups\n', encoding='utf-8')
        self.docker_state['containers']['cid-sb'] = {
            'id': 'cid-sb', 'name': '/autorestic',
            'image': 'sha256:666666666666', 'configured_image': 'cupcakearmy/autorestic:1.8',
            'project': 'backup', 'service': 'autorestic',
            'working_dir': str(sb_dir.resolve()),
            'config_files': str((sb_dir / 'docker-compose.yml').resolve()),
            'mounts': [], 'running': True,
        }
        self._save_docker_state()

        imp_res = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--all-stacks', '--skip-not-deployed', '--skip-existing',
        )
        self.assertNotEqual(imp_res.returncode, 0)
        self.assertIn('backup-credentials', imp_res.stderr)

    def test_conditional_image_pinning_watchtower_latest_normalization_and_replan(self):
        # 1. Image ref normalization unit checks
        self.assertEqual(ssh_migrate.normalize_image_ref('nginx'), ssh_migrate.normalize_image_ref('docker.io/library/nginx:latest'))
        self.assertEqual(ssh_migrate.normalize_image_ref('postgres:16-alpine'), ssh_migrate.normalize_image_ref('docker.io/library/postgres:16-alpine'))
        self.assertEqual(ssh_migrate.normalize_image_ref('localhost:5000/myapp'), 'localhost:5000/myapp:latest')
        self.assertEqual(ssh_migrate.normalize_image_ref('ghcr.io/org/app'), 'ghcr.io/org/app:latest')

        # 2. Stack with matching local image tag and no volume remap -> NO .homelab-adopt.override.yml generated!
        plan1 = self.outside / 'jf-plan1.json'
        res1 = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'jellyfin',
            '--plan', str(plan1),
        )
        self.assertEqual(res1.returncode, 0, res1.stderr)
        p1 = json.loads(plan1.read_text(encoding='utf-8'))
        self.assertFalse(p1['stacks']['jellyfin']['use_override'])
        self.assertEqual(p1['stacks']['jellyfin']['override_yaml'], '')

        self.assertEqual(
            self.run_cli(
                'apply', '--plan', str(plan1),
                '--confirm-backup-tested', '--backup-evidence', 'jf-snap',
            ).returncode,
            0,
        )
        self.assertFalse((self.remote_root / 'medion/jellyfin/.homelab-adopt.override.yml').exists())
        up_jf = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'jellyfin-prod' and c['subcmd'][:1] == ['up']][-1]
        self.assertNotIn('.homelab-adopt.override.yml', up_jf['files'])
        # Watchtower tag preserved (not sha256)
        cur = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        self.assertEqual(cur['containers']['cid-jf']['configured_image'], 'jellyfin/jellyfin:10.9')

        # Idempotent re-plan after apply succeeds!
        plan2 = self.outside / 'jf-plan2.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'jellyfin',
                '--plan', str(plan2),
            ).returncode,
            0,
        )

        # 3a. Same repo tag but local tag moved to newer image ID -> auto-pins running ID + warns about Watchtower!
        cur.setdefault('image_tags', {})['jellyfin/jellyfin:10.9'] = 'sha256:ffffffffffff'
        cur['images']['sha256:ffffffffffff'] = {'repo_digests': [], 'tags': ['jellyfin/jellyfin:10.9']}
        self.docker_state_path.write_text(json.dumps(cur, indent=2), encoding='utf-8')
        plan_moved = self.outside / 'jf-moved.json'
        res_moved = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'jellyfin',
            '--plan', str(plan_moved),
        )
        self.assertEqual(res_moved.returncode, 0, res_moved.stderr)
        self.assertIn('watchtower', res_moved.stderr.lower())
        p_moved = json.loads(plan_moved.read_text(encoding='utf-8'))
        self.assertTrue(p_moved['stacks']['jellyfin']['use_override'])
        self.assertIn('sha256:333333333333', p_moved['stacks']['jellyfin']['override_yaml'])
        self.assertTrue(any('watchtower' in w.lower() for w in p_moved['stacks']['jellyfin']['warnings']))

        # 3b. Repo Compose tag changed (10.10 vs running 10.9) -> fails without --pin-running-images, pins + warns with it!
        (self.repo / 'medion/jellyfin/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {'jellyfin': {'image': 'jellyfin/jellyfin:10.10', 'volumes': ['./config:/config']}},
        }, sort_keys=False), encoding='utf-8')
        res_drift = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'jellyfin',
            '--plan', str(self.outside / 'jf-drift.json'),
        )
        self.assertNotEqual(res_drift.returncode, 0)
        self.assertIn('unreviewed image/schema change', res_drift.stderr.lower())

        plan_pin = self.outside / 'jf-pin.json'
        res_pin = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'jellyfin',
            '--pin-running-images', '--plan', str(plan_pin),
        )
        self.assertEqual(res_pin.returncode, 0, res_pin.stderr)
        p_pin = json.loads(plan_pin.read_text(encoding='utf-8'))
        self.assertTrue(p_pin['stacks']['jellyfin']['use_override'])
        self.assertIn('sha256:333333333333', p_pin['stacks']['jellyfin']['override_yaml'])
        self.assertTrue(any('watchtower' in w.lower() for w in p_pin['stacks']['jellyfin']['warnings']))

    def test_preflight_compose_version_writable_parents_hostname_override_and_extra_config(self):
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )

        # 1. Compose < 2.30 rejected in plan
        self.docker_state['compose_version'] = 'Docker Compose version v2.29.7'
        self._save_docker_state()
        res_old_c = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'old-compose.json'),
        )
        self.assertNotEqual(res_old_c.returncode, 0)
        self.assertIn('2.30', res_old_c.stderr)
        self.docker_state['compose_version'] = 'Docker Compose version v2.40.3'
        self._save_docker_state()

        # 2. Docker ps failure rejected in import-secrets and plan
        self.docker_state['ps_fail'] = 'permission denied connecting to docker.sock'
        self._save_docker_state()
        res_ps_imp = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--all-stacks', '--skip-not-deployed',
        )
        self.assertNotEqual(res_ps_imp.returncode, 0)
        self.assertIn('docker inventory incomplete', res_ps_imp.stderr.lower())
        del self.docker_state['ps_fail']
        self._save_docker_state()

        # 3. Extra local docker-compose.override.yml in running config_files rejected in plan
        orig_cfg = self.docker_state['containers']['cid-web']['config_files']
        self.docker_state['containers']['cid-web']['config_files'] = (
            orig_cfg + ',' + str((self.remote_root / 'medion/webapp/docker-compose.override.yml').resolve())
        )
        self._save_docker_state()
        res_extra = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(self.outside / 'extra-cfg.json'),
        )
        self.assertNotEqual(res_extra.returncode, 0)
        self.assertIn('docker-compose.override.yml', res_extra.stderr)
        self.docker_state['containers']['cid-web']['config_files'] = orig_cfg
        self._save_docker_state()

        # 4. Create valid 2-stack plan (jellyfin + webapp), then test:
        plan_path = self.outside / 'preflight-checks.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'jellyfin', '--stack', 'webapp',
                '--plan', str(plan_path),
            ).returncode,
            0,
        )

        # 4a. Compose downgraded before apply -> rejected in preflight
        self.docker_state['compose_version'] = 'Docker Compose version v2.28.0'
        self._save_docker_state()
        res_apply_old = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'pf-snap',
        )
        self.assertNotEqual(res_apply_old.returncode, 0)
        self.assertIn('2.30', res_apply_old.stderr)
        self.docker_state['compose_version'] = 'Docker Compose version v2.40.3'
        self._save_docker_state()

        # 4b. Hostname mismatch between plan and apply -> rejected in preflight
        tampered_host_plan = self.outside / 'wrong-host-plan.json'
        pdoc = json.loads(plan_path.read_text(encoding='utf-8'))
        pdoc['executing_hostname'] = 'completely-different-host'
        pdoc['plan_digest'] = ssh_migrate.compute_plan_digest(pdoc)
        tampered_host_plan.write_text(json.dumps(pdoc, indent=2), encoding='utf-8')
        res_wrong_host = self.run_cli(
            'apply', '--plan', str(tampered_host_plan),
            '--confirm-backup-tested', '--backup-evidence', 'pf-snap',
        )
        self.assertNotEqual(res_wrong_host.returncode, 0)
        self.assertIn('hostname', res_wrong_host.stderr.lower())

        # 4c. Newly created .homelab-adopt.override.yml on remote host between plan and apply -> drift detected!
        ov = self.remote_root / 'medion/webapp/.homelab-adopt.override.yml'
        ov.write_text('services: {}\n', encoding='utf-8')
        res_ov_drift = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'pf-snap',
        )
        self.assertNotEqual(res_ov_drift.returncode, 0)
        self.assertIn('drift', res_ov_drift.stderr.lower())
        ov.unlink()

        # 4d. Unwritable parent dir on stack 2 (webapp/.secrets) caught in preflight BEFORE stack 1 (jellyfin) is mutated!
        secrets_dir = self.remote_root / 'medion/webapp/.secrets'
        secrets_dir.mkdir(exist_ok=True)
        secrets_dir.chmod(0o500)
        self.addCleanup(lambda: secrets_dir.exists() and secrets_dir.chmod(0o700))
        self.docker_log.write_text('', encoding='utf-8')
        res_unw = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'pf-snap',
        )
        self.assertNotEqual(res_unw.returncode, 0)
        self.assertIn('not writable', res_unw.stderr.lower())
        # Zero compose up calls ran (stack 1 jellyfin untouched!)
        self.assertEqual([c for c in self.read_jsonl(self.docker_log) if c['subcmd'][:1] == ['up']], [])
        secrets_dir.chmod(0o700)

    def test_brace_depth_volume_splitting_and_real_repo_required_interpolation(self):
        """Finding 1 (BLOCKER): brace-depth-aware ':' splitting for ${VAR:?...} volume specs across real repo stacks."""
        env_map = {
            'UPLOAD_LOCATION': '/srv/immich/upload',
            'DB_DATA_LOCATION': '/srv/immich/postgres',
            'DEVICE_FOLDER_PATH': '/srv/homelab/medion',
            'LOCAL_BACKUP_PATH': '/srv/backups',
            'SHARED_FOLDER_PATH': '/srv/homelab/shared',
            'IMMICH_UPLOAD_LOCATION': '/srv/immich/upload',
            'NESTED_FALLBACK': '/srv/nested/src',
            'TARGET_DIR': '/container/target',
        }

        # Direct unit checks: colons inside ${VAR:?...}, nested braces, and escaped $$
        synthetic_svc = {
            'volumes': [
                '${UPLOAD_LOCATION:?Set UPLOAD_LOCATION: must be absolute}:/usr/src/app/upload:ro',
                '${MISSING_OUTER:-${NESTED_FALLBACK:?nested err: detail}}:/nested:ro',
                './hooks:${TARGET_DIR:?Set TARGET_DIR}:ro',
                '$${LITERAL_NOT_INTERP}:/lit',
            ],
        }
        parsed = ssh_migrate.parse_compose_mounts(synthetic_svc, set(), '/srv/stack', '/home/op', env_map)
        by_dst = {m['destination']: m for m in parsed}
        self.assertEqual(by_dst['/usr/src/app/upload']['source'], '/srv/immich/upload')
        self.assertEqual(by_dst['/nested']['source'], '/srv/nested/src')
        self.assertEqual(by_dst['/container/target']['source'], '/srv/stack/hooks')
        self.assertEqual(by_dst['/lit']['raw_source'], '$${LITERAL_NOT_INTERP}')

        # Verify collect_staged_repo_files preserves :ro detection when ':' appears inside ${VAR:?...}
        ro_repo = self.repo / 'medion/rotest'
        (ro_repo / 'hooks').mkdir(parents=True)
        (ro_repo / 'hooks/run.sh').write_text('#!/bin/sh\necho ok\n', encoding='utf-8')
        (ro_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'svc': {
                    'image': 'alpine:3',
                    'volumes': [
                        './hooks:${TARGET_DIR:?Set TARGET_DIR: path}:ro',
                        '${IMMICH_UPLOAD_LOCATION:?Set path}:/ext:ro',
                    ],
                },
            },
        }, sort_keys=False), encoding='utf-8')
        staged = ssh_migrate.collect_staged_repo_files(
            self.repo,
            {'directory': 'medion/rotest', 'compose': ['docker-compose.yml']},
            [],
            set(),
        )
        self.assertIn('medion/rotest/hooks/run.sh', staged)

        # Test REAL repo medion/immich-app and shared/backup (+ compose.medion.yml) across all hosts
        real_manifest, _ = ssh_migrate.load_manifest(REPO_ROOT)

        immich_entry = real_manifest['medion']['immich-app']
        immich_svcs, immich_vols, _ = ssh_migrate.load_merged_compose(REPO_ROOT, immich_entry)
        immich_server_mounts = ssh_migrate.parse_compose_mounts(
            immich_svcs['immich-server'], set(immich_vols.keys()), '/opt/homelab/medion/immich-app', '/home/op', env_map
        )
        immich_db_mounts = ssh_migrate.parse_compose_mounts(
            immich_svcs['database'], set(immich_vols.keys()), '/opt/homelab/medion/immich-app', '/home/op', env_map
        )
        srv_by_dst = {m['destination']: m for m in immich_server_mounts}
        db_by_dst = {m['destination']: m for m in immich_db_mounts}
        self.assertEqual(srv_by_dst['/usr/src/app/upload']['source'], '/srv/immich/upload')
        self.assertEqual(db_by_dst['/var/lib/postgresql/data']['source'], '/srv/immich/postgres')

        for host_name in ('medion', 'thinkpad', 'ionos', 'dell'):
            if 'shared-backup' not in real_manifest.get(host_name, {}):
                continue
            sb_entry = real_manifest[host_name]['shared-backup']
            sb_svcs, sb_vols, _ = ssh_migrate.load_merged_compose(REPO_ROOT, sb_entry)
            sb_mounts = ssh_migrate.parse_compose_mounts(
                sb_svcs['autorestic'], set(sb_vols.keys()), '/opt/homelab/shared/backup', '/home/op', env_map
            )
            sb_by_dst = {m['destination']: m for m in sb_mounts}
            self.assertEqual(sb_by_dst['/data']['source'], '/srv/homelab/medion')
            self.assertEqual(sb_by_dst['/backup']['source'], '/srv/backups')
            self.assertEqual(sb_by_dst['/shared']['source'], '/srv/homelab/shared')
            self.assertEqual(sb_by_dst['/root/.ssh']['source'], '/home/op/.ssh')
            if host_name == 'medion':
                self.assertEqual(sb_by_dst['/external/immich-upload']['source'], '/srv/immich/upload')
            else:
                self.assertNotIn('/external/immich-upload', sb_by_dst)
            sb_staged = ssh_migrate.collect_staged_repo_files(REPO_ROOT, sb_entry, [], set())
            self.assertTrue(any(p.startswith('shared/backup/hooks/') for p in sb_staged), sb_staged)
            self.assertFalse(any(p.startswith('shared/backup/logs') for p in sb_staged), sb_staged)

        # End-to-end plan + apply with required :? volume interpolations (real shared/backup + medion override syntax)
        (self.repo / 'shared/backup/hooks').mkdir(parents=True)
        (self.repo / 'shared/backup/hooks/backup.sh').write_text('#!/bin/sh\necho backup\n', encoding='utf-8')
        (self.repo / 'shared/backup/docker-compose.yml').write_text(
            (REPO_ROOT / 'shared/backup/docker-compose.yml').read_text(encoding='utf-8'),
            encoding='utf-8',
        )
        (self.repo / 'shared/backup/compose.medion.yml').write_text(
            (REPO_ROOT / 'shared/backup/compose.medion.yml').read_text(encoding='utf-8'),
            encoding='utf-8',
        )
        (self.repo / 'medion/.autorestic.yml').write_text('version: 2\n', encoding='utf-8')
        self.manifest['medion']['shared-backup'] = {
            'directory': 'shared/backup',
            'env_file': 'shared/backup/.secrets.env',
            'required': ['DEVICE_FOLDER_PATH', 'LOCAL_BACKUP_PATH', 'SHARED_FOLDER_PATH', 'IMMICH_UPLOAD_LOCATION'],
            'compose': ['docker-compose.yml', 'compose.medion.yml'],
            'dependencies': ['backup-credentials'],
            'encrypted_file': 'secrets/medion/shared-backup.enc.json',
            'migration': {'env_files': ['shared/backup/.env'], 'file_values': {}},
        }
        self.manifest['medion']['backup-credentials'] = {
            'directory': 'medion',
            'required': ['AUTORESTIC_ENV'],
            'files': {'AUTORESTIC_ENV': 'medion/.autorestic.env'},
            'encrypted_file': 'secrets/medion/backup-credentials.enc.json',
            'migration': {'env_files': [], 'file_values': {'AUTORESTIC_ENV': 'medion/.autorestic.env'}},
        }
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )
        dev_dir = self.base / 'dev_data'
        bak_dir = self.base / 'bak_data'
        shr_dir = self.base / 'shr_data'
        for d in (dev_dir, bak_dir, shr_dir, self.remote_home / '.ssh'):
            d.mkdir(parents=True, exist_ok=True)
        sb_remote = self.remote_root / 'shared/backup'
        (sb_remote / 'logs').mkdir(parents=True)
        (sb_remote / 'hooks').mkdir(parents=True)
        (sb_remote / 'rclone.conf').write_text('[remote]\ntype = sftp\n', encoding='utf-8')
        (sb_remote / 'docker-compose.yml').write_text(
            (REPO_ROOT / 'shared/backup/docker-compose.yml').read_text(encoding='utf-8'), encoding='utf-8'
        )
        (sb_remote / 'compose.medion.yml').write_text(
            (REPO_ROOT / 'shared/backup/compose.medion.yml').read_text(encoding='utf-8'), encoding='utf-8'
        )
        (sb_remote / '.env').write_text(
            f'DEVICE_FOLDER_PATH={dev_dir}\nLOCAL_BACKUP_PATH={bak_dir}\n'
            f'SHARED_FOLDER_PATH={shr_dir}\nIMMICH_UPLOAD_LOCATION={self.uploads_dir}\n',
            encoding='utf-8',
        )
        (self.remote_root / 'medion/.autorestic.env').write_text('RESTIC_PASSWORD=secret\n', encoding='utf-8')
        fake_sock = self.base / 'docker.sock'
        fake_sock.write_text('', encoding='utf-8')
        # Point /var/run/docker.sock check to existing path via test-local compose copy only if needed;
        # use real compose text except replace /var/run/docker.sock if absent on this test runner
        if not Path('/var/run/docker.sock').exists():
            patched_sb = (REPO_ROOT / 'shared/backup/docker-compose.yml').read_text(encoding='utf-8').replace(
                '/var/run/docker.sock:/var/run/docker.sock', f'{fake_sock}:/var/run/docker.sock'
            )
            (self.repo / 'shared/backup/docker-compose.yml').write_text(patched_sb, encoding='utf-8')
            (sb_remote / 'docker-compose.yml').write_text(patched_sb, encoding='utf-8')
            sock_src = str(fake_sock.resolve())
        else:
            sock_src = '/var/run/docker.sock'

        self.docker_state['containers']['cid-sb'] = {
            'name': '/autorestic',
            'image': 'sha256:777777777777',
            'configured_image': 'ghcr.io/lr101/autorestic:latest',
            'project': 'backup',
            'service': 'autorestic',
            'working_dir': str(sb_remote.resolve()),
            'config_files': f'{(sb_remote / "docker-compose.yml").resolve()},{(sb_remote / "compose.medion.yml").resolve()}',
            'mounts': [
                {'Type': 'bind', 'Source': str(dev_dir.resolve()), 'Destination': '/data'},
                {'Type': 'bind', 'Source': str(bak_dir.resolve()), 'Destination': '/backup'},
                {'Type': 'bind', 'Source': str(shr_dir.resolve()), 'Destination': '/shared'},
                {'Type': 'bind', 'Source': str((sb_remote / 'logs').resolve()), 'Destination': '/var/log/autorestic'},
                {'Type': 'bind', 'Source': str((sb_remote / 'hooks').resolve()), 'Destination': '/hooks'},
                {'Type': 'bind', 'Source': sock_src, 'Destination': '/var/run/docker.sock'},
                {'Type': 'bind', 'Source': str((sb_remote / 'rclone.conf').resolve()), 'Destination': '/root/.rclone.conf'},
                {'Type': 'bind', 'Source': str((self.remote_home / '.ssh').resolve()), 'Destination': '/root/.ssh'},
                {'Type': 'bind', 'Source': str(self.uploads_dir.resolve()), 'Destination': '/external/immich-upload'},
            ],
            'running': True,
        }
        self.docker_state['images']['sha256:777777777777'] = {
            'repo_digests': ['ghcr.io/lr101/autorestic@sha256:7777'],
            'tags': ['ghcr.io/lr101/autorestic:latest'],
        }
        self._save_docker_state()

        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'shared-backup', '--apply',
            ).returncode,
            0,
        )
        sb_plan = self.outside / 'sb-required-interp-plan.json'
        plan_res = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'shared-backup',
            '--plan', str(sb_plan),
        )
        self.assertEqual(plan_res.returncode, 0, plan_res.stderr)
        apply_res = self.run_cli(
            'apply', '--plan', str(sb_plan),
            '--confirm-backup-tested', '--backup-evidence', 'sb-interp-snap',
        )
        self.assertEqual(apply_res.returncode, 0, apply_res.stderr)

    def test_rollback_override_stripping_and_compose_v1_build_only_rollback_pin(self):
        """Finding 2 (HIGH): Compose v1 underscore build-only image pins on rollback, and temp rollback override is stripped on subsequent plan."""
        b_repo = self.repo / 'medion/v1build'
        b_repo.mkdir(parents=True)
        compose_doc = {
            'services': {
                'web': {
                    'build': '.',
                    'restart': 'unless-stopped',
                },
            },
        }
        (b_repo / 'docker-compose.yml').write_text(yaml.safe_dump(compose_doc, sort_keys=False), encoding='utf-8')
        b_remote = self.remote_root / 'medion/v1build'
        b_remote.mkdir(parents=True)
        (b_remote / 'docker-compose.yml').write_text(yaml.safe_dump(compose_doc, sort_keys=False), encoding='utf-8')

        # Compose v1 built image tag uses underscore (v1proj_web) and resolves to sha256:v1build,
        # while Compose v2 default image name (v1proj-web) does NOT exist!
        self.docker_state['containers']['cid-v1'] = {
            'name': '/v1proj_web_1',
            'image': 'sha256:v1build000001',
            'configured_image': 'v1proj_web',
            'project': 'v1proj',
            'service': 'web',
            'working_dir': str(b_remote.resolve()),
            'config_files': str((b_remote / 'docker-compose.yml').resolve()),
            'mounts': [],
            'running': True,
        }
        self.docker_state['images']['sha256:v1build000001'] = {
            'repo_digests': [],
            'tags': ['v1proj_web', 'v1proj_web:latest'],
        }
        self._save_docker_state()

        plan1_path = self.outside / 'v1build-plan1.json'
        p1 = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'v1build',
            '--plan', str(plan1_path),
        )
        self.assertEqual(p1.returncode, 0, p1.stderr)
        a1 = self.run_cli(
            'apply', '--plan', str(plan1_path),
            '--confirm-backup-tested', '--backup-evidence', 'v1build-snap',
        )
        self.assertEqual(a1.returncode, 0, a1.stderr)

        # Rollback MUST pin sha256:v1build000001 via .homelab-rollback.override.yml so Compose v2 does not fail looking for v1proj-web
        rb1 = self.run_cli('rollback', '--plan', str(plan1_path))
        self.assertEqual(rb1.returncode, 0, rb1.stderr)
        self.assertFalse((b_remote / '.homelab-rollback.override.yml').exists())
        state_after_rb = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        c_after_rb = state_after_rb['containers']['cid-v1']
        self.assertEqual(c_after_rb['image'], 'sha256:v1build000001')
        # Container config_files label now has the deleted .homelab-rollback.override.yml stamped by Compose!
        self.assertIn('.homelab-rollback.override.yml', c_after_rb['config_files'])

        # Subsequent plan MUST accept the stamped .homelab-rollback.override.yml, strip it, and NEVER propagate it!
        plan2_path = self.outside / 'v1build-plan2.json'
        p2 = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'v1build',
            '--plan', str(plan2_path),
        )
        self.assertEqual(p2.returncode, 0, p2.stderr)
        plan2 = json.loads(plan2_path.read_text(encoding='utf-8'))
        orig_cfs = plan2['stacks']['v1build']['original_compose_files']
        self.assertFalse(any('.homelab-rollback.override.yml' in cf for cf in orig_cfs), orig_cfs)

        # Subsequent apply + rollback MUST succeed without referencing the deleted rollback override
        a2 = self.run_cli(
            'apply', '--plan', str(plan2_path),
            '--confirm-backup-tested', '--backup-evidence', 'v1build-snap-2',
        )
        self.assertEqual(a2.returncode, 0, a2.stderr)
        rb2 = self.run_cli('rollback', '--plan', str(plan2_path))
        self.assertEqual(rb2.returncode, 0, rb2.stderr)

        # Real extra local overrides MUST still be rejected
        state_extra = json.loads(self.docker_state_path.read_text(encoding='utf-8'))
        state_extra['containers']['cid-v1']['config_files'] = (
            f'{(b_remote / "docker-compose.yml").resolve()},{(b_remote / "docker-compose.local.yml").resolve()}'
        )
        self.docker_state_path.write_text(json.dumps(state_extra, indent=2), encoding='utf-8')
        p3 = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'v1build',
            '--plan', str(self.outside / 'v1build-plan3.json'),
        )
        self.assertNotEqual(p3.returncode, 0)
        self.assertIn('extra local Compose file', p3.stderr)

    def test_unreadable_0700_dirs_lstat_permission_denied_and_exact_audited_bind_source(self):
        """Finding 3 (HIGH): explicit lstat distinguishes absence vs PermissionError; unreadable 0700 dirs do not crash probe_paths/read_files; exact audited bind source allowed safely."""
        # Create a directory that triggers REAL kernel PermissionError (EACCES) on traversal.
        # If running as root, make it 0700 owned by root and drop uid to nobody (65534);
        # if non-root, chmod 0000 causes identical kernel EACCES on lstat/is_symlink.
        locked_root = self.base / 'real_locked_root'
        locked_ssh = locked_root / '.ssh'
        locked_ssh.mkdir(parents=True)
        (locked_ssh / 'id_ed25519').write_text('dummy-key\n', encoding='utf-8')

        locked_in_src = self.remote_root / 'medion/webapp/locked_root_dir'
        locked_in_src.mkdir(parents=True)
        (locked_in_src / 'secret.env').write_text('K=V\n', encoding='utf-8')
        readable_in_src = self.remote_root / 'medion/webapp/readable.txt'
        readable_in_src.write_text('ok\n', encoding='utf-8')

        is_root = os.geteuid() == 0
        if is_root:
            locked_root.chmod(0o700)
            locked_in_src.chmod(0o700)
            # Ensure parent dirs are traversable by nobody
            for anc in (self.base, self.remote_root, self.remote_root / 'medion', self.remote_root / 'medion/webapp'):
                anc.chmod(0o755)
            drop_env = {'FAKE_SSH_DROP_UID': '65534', 'FAKE_SSH_DROP_GID': '65534'}
        else:
            locked_root.chmod(0o000)
            locked_in_src.chmod(0o000)
            self.addCleanup(lambda: locked_root.exists() and locked_root.chmod(0o700))
            self.addCleanup(lambda: locked_in_src.exists() and locked_in_src.chmod(0o700))
            drop_env = {}

        def run_agent_direct(req_payload):
            kwargs = {'input': json.dumps(req_payload), 'capture_output': True, 'text': True, 'check': False}
            if is_root:
                kwargs['user'] = 65534
                kwargs['group'] = 65534
            proc = subprocess.run([sys.executable, str(AGENT_SCRIPT)], **kwargs)
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
            return json.loads(proc.stdout)

        # Sanity check: prove kernel genuinely raises PermissionError on lstat(locked_ssh)
        sanity_kwargs = {'capture_output': True, 'text': True, 'check': False}
        if is_root:
            sanity_kwargs['user'] = 65534
            sanity_kwargs['group'] = 65534
        sanity = subprocess.run(
            [sys.executable, '-c', f'import os; os.lstat({str(locked_ssh)!r})'],
            **sanity_kwargs,
        )
        self.assertNotEqual(sanity.returncode, 0)
        self.assertIn('PermissionError', sanity.stderr)

        # 1. check_abs_paths & probe_paths must NOT swallow PermissionError or crash on is_symlink()
        absent_path = str(self.base / 'definitely_absent_path')
        inspect_resp = run_agent_direct({
            'action': 'inspect_paths',
            'source_root': str(self.remote_root),
            'probe_paths': ['medion/webapp/locked_root_dir/secret.env', 'medion/webapp/readable.txt', 'medion/webapp/missing.txt'],
            'check_paths': [str(locked_ssh), absent_path],
            'inspect_images': [],
            'docker_sudo': False,
        })
        self.assertTrue(inspect_resp['ok'], inspect_resp)
        chk_locked = inspect_resp['checked_paths'][str(locked_ssh)]
        self.assertTrue(chk_locked.get('permission_denied'), chk_locked)
        self.assertIsNot(chk_locked.get('exists'), False)
        chk_absent = inspect_resp['checked_paths'][absent_path]
        self.assertFalse(chk_absent.get('exists'))
        self.assertFalse(chk_absent.get('permission_denied', False))

        probed_locked = inspect_resp['probed_files']['medion/webapp/locked_root_dir/secret.env']
        self.assertTrue(probed_locked.get('permission_denied'), probed_locked)
        probed_ok = inspect_resp['probed_files']['medion/webapp/readable.txt']
        self.assertTrue(probed_ok.get('exists'))
        self.assertTrue(probed_ok.get('readable'))
        probed_missing = inspect_resp['probed_files']['medion/webapp/missing.txt']
        self.assertFalse(probed_missing.get('exists'))
        self.assertFalse(probed_missing.get('permission_denied', False))

        # 2. handle_read_files must NOT crash on unreadable 0700 dir; returns per-entry permission_denied
        read_resp = run_agent_direct({
            'action': 'read_files',
            'source_root': str(self.remote_root),
            'paths': ['medion/webapp/locked_root_dir/secret.env', 'medion/webapp/readable.txt', 'medion/webapp/missing.txt'],
        })
        self.assertTrue(read_resp['ok'], read_resp)
        self.assertEqual(read_resp['files']['medion/webapp/locked_root_dir/secret.env']['status'], 'permission_denied')
        self.assertEqual(read_resp['files']['medion/webapp/readable.txt']['status'], 'found')
        self.assertEqual(read_resp['files']['medion/webapp/missing.txt']['status'], 'missing')

        # 3. End-to-end plan with --docker-sudo and unreadable root home:
        #    a) exact audited bind source (~/.ssh -> locked_root/.ssh) MUST succeed
        (self.repo / 'medion/webapp/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': 'mariadb:11.8',
                    'environment': {'MARIADB_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['database:/var/lib/mysql'],
                    'secrets': ['db_password'],
                },
                'web': {
                    'image': 'example/webapp:1.2',
                    'depends_on': ['db'],
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'volumes': [
                        './config/app.conf:/etc/app/app.conf:ro',
                        '${UPLOAD_LOCATION}:/uploads',
                        '~/.ssh:/root/.ssh:ro',
                    ],
                },
            },
            'volumes': {'database': None},
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')
        self.docker_state['containers']['cid-web']['mounts'].append(
            {'Type': 'bind', 'Source': str(locked_ssh), 'Destination': '/root/.ssh'}
        )
        self._save_docker_state()

        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan_ok = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--docker-sudo', '--plan', str(self.outside / 'locked-root-ok-plan.json'),
            extra_env={**drop_env, 'FAKE_SUDO_HOME': str(locked_root)},
        )
        self.assertEqual(plan_ok.returncode, 0, plan_ok.stderr)

        #    b) non-audited bind source inside the same unreadable root home MUST fail closed with Permission denied
        (self.repo / 'medion/webapp/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': 'mariadb:11.8',
                    'environment': {'MARIADB_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['database:/var/lib/mysql'],
                    'secrets': ['db_password'],
                },
                'web': {
                    'image': 'example/webapp:1.2',
                    'depends_on': ['db'],
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'volumes': [
                        './config/app.conf:/etc/app/app.conf:ro',
                        '${UPLOAD_LOCATION}:/uploads',
                        '~/.ssh:/root/.ssh:ro',
                        '~/.other-unverified:/root/.other:ro',
                    ],
                },
            },
            'volumes': {'database': None},
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')
        plan_denied = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--docker-sudo', '--plan', str(self.outside / 'locked-root-denied-plan.json'),
            extra_env={**drop_env, 'FAKE_SUDO_HOME': str(locked_root)},
        )
        self.assertNotEqual(plan_denied.returncode, 0)
        self.assertIn('permission denied', plan_denied.stderr.lower())

    def test_remove_new_service_containers_retains_fallback_audit_errors(self):
        """Finding 4 (HIGH): _remove_new_service_containers must NOT clear errors when fallback run_docker_audit fails with empty containers."""
        state = {
            'project_name': 'testproj',
            'remote_stack_dir': str(self.remote_root / 'medion/webapp'),
            'applied_compose_files': ['docker-compose.yml'],
            'new_services': ['socket-proxy'],
            'compose_home': str(self.remote_home),
        }
        old_env = dict(os.environ)
        os.environ['PATH'] = f'{self.remote_bin}:{os.environ.get("PATH", "")}'
        os.environ['FAKE_DOCKER_STATE'] = str(self.docker_state_path)
        os.environ['FAKE_DOCKER_LOG'] = str(self.docker_log)
        try:
            # Make both compose rm AND fallback docker ps -aq fail!
            self.docker_state['fail_rules']['testproj:rm'] = 'compose rm failed'
            self.docker_state['ps_fail'] = 'Cannot connect to Docker daemon'
            self._save_docker_state()

            removed, errs = ssh_remote_agent._remove_new_service_containers(state, docker_sudo=False, secrets=set())
            self.assertEqual(removed, [])
            self.assertTrue(errs, 'Expected inventory failure errors to be retained, got empty list!')
            joined = ' '.join(errs).lower()
            self.assertIn('inventory', joined)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            self.docker_state.pop('ps_fail', None)
            self.docker_state['fail_rules'].pop('testproj:rm', None)
            self._save_docker_state()

    def test_repeat_adopt_rollback_loads_restored_preexisting_primary_env_file_incl_sudo(self):
        """Finding 5 (HIGH): rollback of repeat adoption loads restored preexisting .secrets.env (required + optional vars) incl --docker-sudo."""
        (self.repo / 'medion/webapp/docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'db': {
                    'image': 'mariadb:11.8',
                    'environment': {'MARIADB_PASSWORD_FILE': '/run/secrets/db_password'},
                    'volumes': ['database:/var/lib/mysql'],
                    'secrets': ['db_password'],
                },
                'web': {
                    'image': 'example/webapp:1.2',
                    'depends_on': ['db'],
                    'env_file': [{'path': '.secrets.env', 'format': 'raw'}],
                    'environment': {
                        'REQ_TOKEN': '${APP_TOKEN:?APP_TOKEN is required}',
                        'OPT_TOKEN': '${OPT_SECRET:-}',
                    },
                    'volumes': [
                        './config/app.conf:/etc/app/app.conf:ro',
                        '${UPLOAD_LOCATION}:/uploads',
                    ],
                },
            },
            'volumes': {'database': None},
            'secrets': {'db_password': {'file': './.secrets/db_password'}},
        }, sort_keys=False), encoding='utf-8')
        self.manifest['medion']['webapp']['optional'] = ['OPT_SECRET']
        (self.repo / 'shared/secrets/deployments.json').write_text(
            json.dumps(self.manifest, indent=2), encoding='utf-8'
        )
        (self.remote_root / 'medion/webapp/.env').write_text(
            "DB_PASSWORD='db$pass#literal'\n"
            "APP_TOKEN='orig-required-tok'\n"
            "OPT_SECRET='orig-optional-val'\n"
            f'UPLOAD_LOCATION={self.uploads_dir}\n',
            encoding='utf-8',
        )

        # First adoption: archives legacy .env, creates .secrets.env (mode 0600)
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan1 = self.outside / 'repeat-plan1.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--docker-sudo', '--plan', str(plan1),
            ).returncode,
            0,
        )
        a1 = self.run_cli(
            'apply', '--plan', str(plan1),
            '--confirm-backup-tested', '--backup-evidence', 'repeat-snap-1',
        )
        self.assertEqual(a1.returncode, 0, a1.stderr)
        webapp_remote = self.remote_root / 'medion/webapp'
        self.assertFalse((webapp_remote / '.env').exists())
        self.assertTrue((webapp_remote / '.secrets.env').is_file())

        # Second (repeat) adoption: update staged config and secrets, plan + apply under --docker-sudo, then rollback!
        (self.repo / 'medion/webapp/config/app.conf').write_text('mode=v2\n', encoding='utf-8')
        enc_path = self.repo / 'secrets/medion/webapp.enc.json'
        enc_doc = json.loads(enc_path.read_text(encoding='utf-8'))
        # Fake sops uses ENC[FAKE:<json>]
        enc_doc['APP_TOKEN'] = 'ENC[FAKE:' + json.dumps('new-required-tok') + ']'
        enc_doc['OPT_SECRET'] = 'ENC[FAKE:' + json.dumps('new-optional-val') + ']'
        enc_path.write_text(json.dumps(enc_doc, indent=2), encoding='utf-8')

        plan2 = self.outside / 'repeat-plan2.json'
        p2 = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--docker-sudo', '--plan', str(plan2),
        )
        self.assertEqual(p2.returncode, 0, p2.stderr)
        a2 = self.run_cli(
            'apply', '--plan', str(plan2),
            '--confirm-backup-tested', '--backup-evidence', 'repeat-snap-2',
        )
        self.assertEqual(a2.returncode, 0, a2.stderr)

        # Clear docker & sudo logs before explicit rollback so we inspect ONLY rollback calls
        self.docker_log.write_text('', encoding='utf-8')
        self.sudo_log.write_text('', encoding='utf-8')

        rb2 = self.run_cli('rollback', '--plan', str(plan2))
        self.assertEqual(rb2.returncode, 0, rb2.stderr)
        # Restored preexisting .secrets.env must have original values and be passed to rollback compose up under sudo!
        self.assertIn('APP_TOKEN=orig-required-tok\n', (webapp_remote / '.secrets.env').read_text(encoding='utf-8'))
        rb_up_calls = [c for c in self.read_jsonl(self.docker_log) if c['project'] == 'legacy-webapp-proj' and c['subcmd'][:1] == ['up']]
        self.assertTrue(rb_up_calls)
        for c in rb_up_calls:
            self.assertEqual(c['sudo_active'], '1')
            self.assertEqual(c['env_snapshot'].get('APP_TOKEN'), 'orig-required-tok')
            self.assertEqual(c['env_snapshot'].get('OPT_SECRET'), 'orig-optional-val')
        # Zero secrets leaked in sudo argv
        for entry in self.read_jsonl(self.sudo_log):
            joined = ' '.join(entry['argv'])
            self.assertNotIn('orig-required-tok', joined)
            self.assertNotIn('orig-optional-val', joined)

    def test_preflight_rejects_unpinned_tag_drift_before_any_mutation(self):
        """Finding 6 (MEDIUM): unpinned tag moved between plan and apply is rejected in preflight BEFORE any file mutation or compose up."""
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'tag-drift-plan.json'
        p = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(plan_path),
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        # example/webapp:1.2 is unpinned (local tag matched running sha256:111111111111)
        self.assertEqual(plan['stacks']['webapp']['pinned_images'], {})
        self.assertEqual(
            plan['stacks']['webapp'].get('expected_image_refs', {}).get('example/webapp:1.2'),
            'sha256:111111111111',
        )

        # Move tag example/webapp:1.2 to a new image BEFORE apply while old image + container remain untouched!
        self.docker_state['images']['sha256:111111111111']['tags'] = []
        self.docker_state['images']['sha256:999999999999'] = {
            'repo_digests': ['example/webapp@sha256:9999'],
            'tags': ['example/webapp:1.2'],
        }
        self._save_docker_state()

        webapp_remote = self.remote_root / 'medion/webapp'
        compose_before = (webapp_remote / 'docker-compose.yml').read_text(encoding='utf-8')
        env_before = (webapp_remote / '.env').read_text(encoding='utf-8')
        self.docker_log.write_text('', encoding='utf-8')

        a = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'tag-drift-snap',
        )
        self.assertNotEqual(a.returncode, 0)
        self.assertIn('drift', a.stderr.lower())
        # Verify ZERO mutations occurred on remote host!
        self.assertEqual((webapp_remote / 'docker-compose.yml').read_text(encoding='utf-8'), compose_before)
        self.assertEqual((webapp_remote / '.env').read_text(encoding='utf-8'), env_before)
        self.assertFalse((webapp_remote / '.secrets.env').exists())
        self.assertFalse(Path(plan['rollback_dir']).exists())
        self.assertEqual([c for c in self.read_jsonl(self.docker_log) if c['subcmd'][:1] == ['up']], [])

    def test_run_docker_sock_alias_in_socket_relocation(self):
        """Finding 7A (LOW): /run/docker.sock alias accepted in reviewed Traefik -> socket-proxy socket relocation."""
        tr_repo = self.repo / 'medion/traefik'
        tr_repo.mkdir(parents=True)
        (tr_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'socket-proxy': {
                    'image': 'lscr.io/linuxserver/socket-proxy:latest',
                    'volumes': ['/var/run/docker.sock:/var/run/docker.sock:ro'],
                },
                'traefik': {
                    'image': 'traefik:v3.3',
                    'depends_on': ['socket-proxy'],
                    'volumes': [],
                },
            },
        }, sort_keys=False), encoding='utf-8')
        tr_remote = self.remote_root / 'medion/traefik'
        tr_remote.mkdir(parents=True)
        (tr_remote / 'docker-compose.yml').write_text(yaml.safe_dump({
            'services': {
                'traefik': {
                    'image': 'traefik:v3.3',
                    'volumes': ['/run/docker.sock:/run/docker.sock:ro'],
                },
            },
        }, sort_keys=False), encoding='utf-8')

        self.docker_state['containers']['cid-tr-sock'] = {
            'name': '/traefik',
            'image': 'sha256:aaaa11111111',
            'configured_image': 'traefik:v3.3',
            'project': 'traefik',
            'service': 'traefik',
            'working_dir': str(tr_remote.resolve()),
            'config_files': str((tr_remote / 'docker-compose.yml').resolve()),
            'mounts': [
                {'Type': 'bind', 'Source': '/run/docker.sock', 'Destination': '/run/docker.sock'},
            ],
            'running': True,
        }
        self.docker_state['images']['sha256:aaaa11111111'] = {'repo_digests': [], 'tags': ['traefik:v3.3']}
        self.docker_state['images']['sha256:bbbb22222222'] = {'repo_digests': [], 'tags': ['lscr.io/linuxserver/socket-proxy:latest']}
        self._save_docker_state()

        orig_call = ssh_migrate.SshClient.call
        def fake_sock_run(self_ssh, payload, *args, **kwargs):
            res = orig_call(self_ssh, payload, *args, **kwargs)
            if isinstance(res, dict) and 'checked_paths' in res:
                for sp in ('/var/run/docker.sock', '/run/docker.sock'):
                    if sp in res['checked_paths']:
                        res['checked_paths'][sp] = {'exists': True, 'realpath': '/run/docker.sock', 'permission_denied': False}
            return res

        old_env = dict(os.environ)
        os.environ.update({
            'PATH': f'{self.controller_bin}:{os.environ.get("PATH", "")}',
            'FAKE_SSH_LOG': str(self.ssh_log),
            'FAKE_REMOTE_BIN': str(self.remote_bin),
            'FAKE_REMOTE_HOME': str(self.remote_home),
            'FAKE_DOCKER_STATE': str(self.docker_state_path),
            'FAKE_DOCKER_LOG': str(self.docker_log),
            'FAKE_SUDO_LOG': str(self.sudo_log),
        })
        def run_inproc(cli_args):
            with unittest.mock.patch.object(ssh_migrate.SshClient, 'call', autospec=True, side_effect=fake_sock_run):
                with unittest.mock.patch.object(sys, 'argv', ['ssh-migrate.py'] + cli_args):
                    return ssh_migrate.main()
        try:
            rc = run_inproc([
                '--root', str(self.repo), 'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'traefik',
                '--allow-new-services', '--plan', str(self.outside / 'tr-run-sock-plan.json'),
            ])
            self.assertEqual(rc, 0)

            # Negative check: if socket-proxy does NOT mount docker.sock, /run/docker.sock MUST still be rejected
            (tr_repo / 'docker-compose.yml').write_text(yaml.safe_dump({
                'services': {
                    'socket-proxy': {'image': 'lscr.io/linuxserver/socket-proxy:latest', 'volumes': []},
                    'traefik': {'image': 'traefik:v3.3', 'depends_on': ['socket-proxy'], 'volumes': []},
                },
            }, sort_keys=False), encoding='utf-8')
            rc_neg = run_inproc([
                '--root', str(self.repo), 'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'traefik',
                '--allow-new-services', '--plan', str(self.outside / 'tr-run-sock-neg-plan.json'),
            ])
            self.assertNotEqual(rc_neg, 0)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_harvest_secret_values_json_including_trailing_comma(self):
        """Finding 7B (LOW): JSON secret harvesting extracts JSON string values including trailing commas and escaped chars."""
        # 1. Standard JSON (like firebase-token.json / secret.json) where lines end with commas
        json_payload = (
            '{\n'
            '  "private_key": "super-secret-json-key-1",\n'
            '  "escaped_val": "esc\\"quote\\\\slash",\n'
            '  "nested": {"api_token": "nested-json-token-2", "list": ["arr-secret-3"]}\n'
            '}\n'
        ).encode('utf-8')
        secrets_set = set()
        ssh_remote_agent.harvest_secret_values(json_payload, secrets_set)
        sample_err = 'leak: super-secret-json-key-1 esc"quote\\slash nested-json-token-2 arr-secret-3'
        redacted = ssh_remote_agent.redact(sample_err, secrets_set)
        self.assertNotIn('super-secret-json-key-1', redacted)
        self.assertNotIn('esc"quote\\slash', redacted)
        self.assertNotIn('nested-json-token-2', redacted)
        self.assertNotIn('arr-secret-3', redacted)

        # 2. JSON with trailing comma before closing brace (non-strict JSON)
        trailing_comma_payload = (
            '{\n'
            '  "token": "trailing-comma-secret-9",\n'
            '}\n'
        ).encode('utf-8')
        secrets_tc = set()
        ssh_remote_agent.harvest_secret_values(trailing_comma_payload, secrets_tc)
        redacted_tc = ssh_remote_agent.redact('err: trailing-comma-secret-9', secrets_tc)
        self.assertNotIn('trailing-comma-secret-9', redacted_tc)

    def test_preflight_rejects_newly_unreadable_path_or_directory_at_absent_file(self):
        """Finding 7C (LOW): preflight must reject directory or newly unreadable path at previously absent file instead of treating hash as None."""
        self.assertEqual(
            self.run_cli(
                'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            ).returncode,
            0,
        )
        plan_path = self.outside / 'pf-dir-perm-plan.json'
        p = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp',
            '--plan', str(plan_path),
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        webapp_remote = self.remote_root / 'medion/webapp'
        self.assertIsNone(plan['stacks']['webapp']['remote_file_hashes']['medion/webapp/.secrets.env'])

        # Case A: a directory appears where previously absent .secrets.env is expected
        sec_env_dir = webapp_remote / '.secrets.env'
        sec_env_dir.mkdir()
        self.addCleanup(lambda: sec_env_dir.is_dir() and sec_env_dir.rmdir())
        res_dir = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'pf-dir-snap',
        )
        self.assertNotEqual(res_dir.returncode, 0)
        self.assertIn('not a regular file', res_dir.stderr.lower())
        self.assertFalse(Path(plan['rollback_dir']).exists())
        sec_env_dir.rmdir()

        # Case B: previously absent .secrets.env becomes a newly unreadable file inside a WRITABLE parent dir
        sec_env_file = webapp_remote / '.secrets.env'
        sec_env_file.write_text('UNREADABLE=1\n', encoding='utf-8')
        if os.geteuid() == 0:
            sec_env_file.chmod(0o600)
            for anc in (self.base, self.remote_root, self.remote_root / 'medion', webapp_remote):
                anc.chmod(0o777)
            drop_env = {'FAKE_SSH_DROP_UID': '65534', 'FAKE_SSH_DROP_GID': '65534'}
        else:
            sec_env_file.chmod(0o000)
            self.addCleanup(lambda: sec_env_file.exists() and (sec_env_file.chmod(0o600) or sec_env_file.unlink()))
            drop_env = {}

        res_perm = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'pf-perm-snap',
            extra_env=drop_env,
        )
        self.assertNotEqual(res_perm.returncode, 0)
        self.assertIn('permission denied', res_perm.stderr.lower())
        self.assertFalse(Path(plan['rollback_dir']).exists())

@unittest.skipUnless(REAL_SOPS and REAL_AGE_KEYGEN, 'real sops and age-keygen binaries not available')
class RealSopsAgeRoundTripTests(SshMigrationTestBase):
    def test_real_sops_age_encrypt_plan_and_apply_round_trip(self):
        key_file = self.outside / 'ephemeral.agekey'
        gen = subprocess.run([REAL_AGE_KEYGEN, '-o', str(key_file)], capture_output=True, text=True, check=True)
        recipient = None
        for line in (gen.stdout + gen.stderr + key_file.read_text()).splitlines():
            if 'age1' in line:
                for token in line.split():
                    if token.startswith('age1'):
                        recipient = token.strip()
                        break
            if recipient:
                break
        self.assertIsNotNone(recipient, 'failed to extract age public recipient')

        (self.repo / '.sops.yaml').write_text(
            'creation_rules:\n'
            '  - path_regex: ^secrets/medion/.*\\.enc\\.json$\n'
            f'    age: {recipient}\n',
            encoding='utf-8',
        )

        real_sops_shim = self.controller_bin / 'sops'
        real_sops_shim.unlink()
        real_sops_shim.symlink_to(REAL_SOPS)

        sops_env = {'SOPS_AGE_KEY_FILE': str(key_file)}

        cap = self.run_cli(
            'capture-host', '--host', 'medion', '--ssh-target', 'medion',
            '--system-root', str(self.system_root), '--apply',
            extra_env=sops_env,
        )
        self.assertEqual(cap.returncode, 0, cap.stderr)
        imp = self.run_cli(
            'import-secrets', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'webapp', '--apply',
            extra_env=sops_env,
        )
        self.assertEqual(imp.returncode, 0, imp.stderr)

        for rel in (
            'secrets/medion/host-config.enc.json',
            'secrets/medion/webapp.enc.json',
            'secrets/medion/webapp-extra.enc.json',
        ):
            doc = json.loads((self.repo / rel).read_text(encoding='utf-8'))
            self.assertIsInstance(doc.get('sops'), dict)
            self.assertTrue(doc['sops'].get('mac'))
            for k, v in doc.items():
                if k != 'sops':
                    self.assertTrue(v.startswith('ENC[AES256_GCM,'), f'{rel}:{k} not real SOPS ciphertext')
            self.assertNotIn('db$pass#literal', (self.repo / rel).read_text())

        plan_path = self.outside / 'real-sops-plan.json'
        self.assertEqual(
            self.run_cli(
                'plan', '--host', 'medion', '--ssh-target', 'medion',
                '--source-root', str(self.remote_root), '--stack', 'webapp',
                '--plan', str(plan_path),
                extra_env=sops_env,
            ).returncode,
            0,
        )
        res = self.run_cli(
            'apply', '--plan', str(plan_path),
            '--confirm-backup-tested', '--backup-evidence', 'real-sops-test-snap',
            extra_env=sops_env,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        webapp_remote = self.remote_root / 'medion/webapp'
        self.assertEqual((webapp_remote / '.secrets/db_password').read_text(), 'db$pass#literal')
        self.assertIn('APP_TOKEN=tok$en!value\n', (webapp_remote / '.secrets.env').read_text())
        self.assertEqual((webapp_remote / '.secrets.extra.env').read_text(), 'EXTRA_KEY=extra$literal\n')
        self.assertNotIn('db$pass#literal', res.stdout + res.stderr)





@unittest.skipUnless(REAL_COMPOSE, 'real docker-compose binary not found in environment')
class RealComposeCliTests(unittest.TestCase):
    """Exercise the REAL Docker Compose v2 CLI against an isolated local HTTP mock Docker Engine."""

    def _start_mock_docker_engine(self):
        import http.server
        import socketserver
        import threading
        from urllib.parse import parse_qs, urlparse

        state = {
            'requests': [],
            'containers': {},
            'networks': {},
            'lock': threading.Lock(),
        }

        class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _send(self, code, obj=None):
                self.send_response(code)
                self.send_header('Api-Version', '1.47')
                if obj is None:
                    self.end_headers()
                    return
                body = json.dumps(obj).encode('utf-8')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                self._send(200)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                state['requests'].append(('GET', self.path, None))
                if path.endswith('/_ping'):
                    self.send_response(200)
                    self.send_header('Api-Version', '1.47')
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'OK')
                    return
                if path.endswith('/version'):
                    self._send(200, {'Version': '27.5.0', 'ApiVersion': '1.47', 'MinAPIVersion': '1.24', 'Os': 'linux', 'Arch': 'amd64'})
                    return
                if path.endswith('/info'):
                    self._send(200, {'ID': 'mock-engine', 'Containers': len(state['containers']), 'OSType': 'linux', 'Architecture': 'x86_64'})
                    return
                if path.endswith('/containers/json'):
                    with state['lock']:
                        items = [
                            {
                                'Id': cid,
                                'Names': ['/' + c['Name'].lstrip('/')],
                                'Image': c['Config']['Image'],
                                'ImageID': 'sha256:1111111111111111111111111111111111111111111111111111111111111111',
                                'Labels': c['Config'].get('Labels', {}),
                                'State': c['State']['Status'],
                                'Status': 'Up 1 minute' if c['State']['Running'] else 'Exited (0) 1 minute ago',
                                'NetworkSettings': {'Networks': c['NetworkSettings']['Networks']},
                            }
                            for cid, c in state['containers'].items()
                        ]
                    self._send(200, items)
                    return
                if '/containers/' in path and path.endswith('/json'):
                    cid = path.split('/containers/', 1)[1].rsplit('/json', 1)[0]
                    with state['lock']:
                        c = state['containers'].get(cid)
                    self._send(200, c) if c else self._send(404, {'message': 'No such container'})
                    return
                if path.endswith('/networks'):
                    with state['lock']:
                        self._send(200, list(state['networks'].values()))
                    return
                if '/networks/' in path:
                    nid = path.split('/networks/', 1)[1]
                    with state['lock']:
                        net = state['networks'].get(nid) or next((n for n in state['networks'].values() if n['Name'] == nid), None)
                    self._send(200, net) if net else self._send(404, {'message': 'network not found'})
                    return
                if path.endswith('/volumes'):
                    self._send(200, {'Volumes': []})
                    return
                if '/images/' in path and path.endswith('/json'):
                    img = path.split('/images/', 1)[1].rsplit('/json', 1)[0]
                    self._send(200, {
                        'Id': 'sha256:1111111111111111111111111111111111111111111111111111111111111111',
                        'RepoTags': [img],
                        'Config': {'ExposedPorts': {}, 'Env': []},
                    })
                    return
                self._send(200, {})

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length) if length else b''
                body = json.loads(raw.decode('utf-8')) if raw else {}
                state['requests'].append(('POST', self.path, body))
                if path.endswith('/networks/create'):
                    nid = f'net-{len(state["networks"]) + 1}'
                    with state['lock']:
                        state['networks'][nid] = {'Id': nid, 'Name': body.get('Name', nid), 'Driver': 'bridge', 'Labels': body.get('Labels', {})}
                    self._send(201, {'Id': nid, 'Warning': ''})
                    return
                if '/networks/' in path and path.endswith('/connect'):
                    self._send(200)
                    return
                if path.endswith('/containers/create'):
                    with state['lock']:
                        cid = f'cid-{len(state["containers"]) + 1:012d}'
                        name = (qs.get('name') or [cid])[0]
                        state['containers'][cid] = {
                            'Id': cid,
                            'Name': '/' + name.lstrip('/'),
                            'State': {'Status': 'created', 'Running': False, 'Restarting': False, 'ExitCode': 0},
                            'Config': {
                                'Image': body.get('Image', ''),
                                'Env': body.get('Env', []),
                                'Labels': body.get('Labels', {}),
                            },
                            'HostConfig': body.get('HostConfig', {}),
                            'NetworkSettings': {'Networks': {}},
                        }
                    self._send(201, {'Id': cid, 'Warnings': []})
                    return
                if '/containers/' in path and path.endswith('/start'):
                    cid = path.split('/containers/', 1)[1].rsplit('/start', 1)[0]
                    with state['lock']:
                        if cid in state['containers']:
                            state['containers'][cid]['State'] = {'Status': 'running', 'Running': True, 'Restarting': False, 'ExitCode': 0}
                    self._send(204)
                    return
                if '/containers/' in path and path.endswith('/stop'):
                    cid = path.split('/containers/', 1)[1].rsplit('/stop', 1)[0]
                    with state['lock']:
                        if cid in state['containers']:
                            state['containers'][cid]['State'] = {'Status': 'exited', 'Running': False, 'Restarting': False, 'ExitCode': 0}
                    self._send(204)
                    return
                self._send(200, {})

            def do_DELETE(self):
                parsed = urlparse(self.path)
                path = parsed.path
                state['requests'].append(('DELETE', self.path, None))
                if '/containers/' in path:
                    cid = path.split('/containers/', 1)[1]
                    with state['lock']:
                        state['containers'].pop(cid, None)
                    self._send(204)
                    return
                self._send(204)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return port, state

    def test_real_compose_oneshot_no_deps_rm_without_volumes_and_real_repo_config(self):
        port, state = self._start_mock_docker_engine()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            # 1. Validate real repo medion/hiring_scraper compose with real Compose v2.40.3 config --quiet
            hs_env = dict(os.environ, POSTGRES_PASSWORD='pw', HIRING_APP_IMAGE='ghcr.io/example/hiring:1.0', HIRING_DB_IMAGE='postgres:16-alpine')
            # Create dummy .secrets.env in a copy of hiring_scraper dir
            hs_dir = p / 'hiring_scraper'
            shutil.copytree(REPO_ROOT / 'medion/hiring_scraper', hs_dir)
            (hs_dir / '.secrets.env').write_text('POSTGRES_PASSWORD=pw\n', encoding='utf-8')
            cfg_res = subprocess.run(
                [REAL_COMPOSE, '-f', 'docker-compose.yml', 'config', '--quiet'],
                cwd=str(hs_dir), env=hs_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(cfg_res.returncode, 0, cfg_res.stderr)

            # 2. Run real Compose up -d --no-build --pull never --no-deps -- db app against mock Docker Engine
            stack_dir = p / 'stack'
            stack_dir.mkdir()
            (stack_dir / 'docker-compose.yml').write_text(yaml.safe_dump({
                'services': {
                    'db': {'image': 'postgres:16-alpine', 'restart': 'unless-stopped'},
                    'migrate': {
                        'image': 'ghcr.io/example/hiring:1.0',
                        'restart': 'no',
                        'depends_on': {'db': {'condition': 'service_healthy'}},
                        'command': ['alembic', 'upgrade', 'head'],
                    },
                    'app': {
                        'image': 'ghcr.io/example/hiring:1.0',
                        'restart': 'unless-stopped',
                        'depends_on': {'migrate': {'condition': 'service_completed_successfully'}},
                        'environment': {'REQ': '${REQ:?missing}'},
                    },
                    'stopped-worker': {'image': 'ghcr.io/example/hiring:1.0'},
                }
            }, sort_keys=False), encoding='utf-8')

            env = dict(os.environ, DOCKER_HOST=f'tcp://127.0.0.1:{port}', REQ='real-compose-secret')
            up_res = subprocess.run(
                [REAL_COMPOSE, '-p', 'hiring', 'up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--', 'db', 'app'],
                cwd=str(stack_dir), env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(up_res.returncode, 0, up_res.stderr)
            created_services = [
                b.get('Labels', {}).get('com.docker.compose.service')
                for m, path, b in state['requests']
                if m == 'POST' and '/containers/create' in path
            ]
            self.assertEqual(sorted(created_services), ['app', 'db'])
            self.assertNotIn('migrate', created_services)
            self.assertNotIn('stopped-worker', created_services)

            # 3. Run real Compose rm -s -f -- app -> deletes ONLY app, NEVER passes v=1 (no volume deletion!)
            rm_res = subprocess.run(
                [REAL_COMPOSE, '-p', 'hiring', 'rm', '-s', '-f', '--', 'app'],
                cwd=str(stack_dir), env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(rm_res.returncode, 0, rm_res.stderr)
            delete_paths = [path for m, path, _ in state['requests'] if m == 'DELETE']
            self.assertEqual(len(delete_paths), 1)
            self.assertNotIn('v=1', delete_paths[0])
            remaining = [c['Config']['Labels']['com.docker.compose.service'] for c in state['containers'].values()]
            self.assertEqual(remaining, ['db'])

    def test_real_compose_through_run_compose_with_sudo_env_reset_and_tilde(self):
        port, state = self._start_mock_docker_engine()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            sudo_log = base / 'sudo.jsonl'
            ssh_home = base / 'ssh_user_home'
            (ssh_home / '.ssh').mkdir(parents=True)
            stack_dir = base / 'stack'
            stack_dir.mkdir()

            # Write FAKE_SUDO_PY (simulating Defaults env_reset + HOME=/root) and a docker shim forwarding `docker compose` to REAL_COMPOSE
            write_executable(bin_dir / 'sudo', FAKE_SUDO_PY)
            write_executable(
                bin_dir / 'docker',
                f'#!/bin/sh\n'
                f'if [ "$1" = "compose" ]; then\n'
                f'  shift\n'
                f'  export DOCKER_HOST="tcp://127.0.0.1:{port}"\n'
                f'  exec "{REAL_COMPOSE}" "$@"\n'
                f'fi\n'
                f'exit 1\n',
            )
            (stack_dir / 'docker-compose.yml').write_text(yaml.safe_dump({
                'services': {
                    'app': {
                        'image': 'ghcr.io/example/app:1.0',
                        'environment': {'REQ_TOKEN': '${REQ_TOKEN:?must be set}'},
                        'volumes': ['~/.ssh:/root/.ssh:ro'],
                    }
                }
            }, sort_keys=False), encoding='utf-8')

            old_env = dict(os.environ)
            os.environ['PATH'] = f'{bin_dir}:{os.environ.get("PATH", "")}'
            os.environ['FAKE_SUDO_LOG'] = str(sudo_log)
            try:
                proc = ssh_remote_agent.run_compose(
                    docker_sudo=True,
                    compose_args=['-p', 'sudotest', '-f', 'docker-compose.yml', 'up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--', 'app'],
                    cwd=str(stack_dir),
                    env_updates={'REQ_TOKEN': 'super-secret-via-stdin'},
                    compose_home=str(ssh_home),
                )
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            creates = [b for m, path, b in state['requests'] if m == 'POST' and '/containers/create' in path]
            self.assertEqual(len(creates), 1)
            self.assertIn('REQ_TOKEN=super-secret-via-stdin', creates[0]['Env'])
            # Verify real Compose expanded ~/.ssh using ssh_home (NOT /root!)
            binds = creates[0].get('HostConfig', {}).get('Binds') or []
            mounts = creates[0].get('HostConfig', {}).get('Mounts') or []
            expected_ssh = str((ssh_home / '.ssh').resolve())
            self.assertTrue(
                any(expected_ssh in b for b in binds) or any(m.get('Source') == expected_ssh for m in mounts),
                f'Expected {expected_ssh} in binds={binds} mounts={mounts}',
            )
            # Zero secret values in sudo argv!
            sudo_text = sudo_log.read_text(encoding='utf-8')
            self.assertNotIn('super-secret-via-stdin', sudo_text)


    def test_real_compose_parity_for_immich_and_shared_backup_required_volume_interpolations(self):
        """Verify parse_compose_mounts matches real Compose v2.40.3 config --format json for medion/immich-app and shared/backup."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            env_map = {
                'UPLOAD_LOCATION': str(base / 'immich_upload'),
                'DB_DATA_LOCATION': str(base / 'immich_pg'),
                'DB_USERNAME': 'postgres',
                'DB_DATABASE_NAME': 'immich',
                'DEVICE_FOLDER_PATH': str(base / 'medion_dir'),
                'LOCAL_BACKUP_PATH': str(base / 'local_bak'),
                'SHARED_FOLDER_PATH': str(base / 'shared_dir'),
                'IMMICH_UPLOAD_LOCATION': str(base / 'immich_upload'),
            }
            fake_home = str(base / 'op_home')

            # 1. medion/immich-app
            im_dir = base / 'immich-app'
            shutil.copytree(REPO_ROOT / 'medion/immich-app', im_dir)
            (im_dir / '.secrets.env').write_text('', encoding='utf-8')
            (im_dir / '.secrets.kiosk.env').write_text('', encoding='utf-8')
            (im_dir / '.secrets').mkdir(exist_ok=True)
            (im_dir / '.secrets/postgres_password').write_text('pw', encoding='utf-8')
            im_env = dict(os.environ, **env_map, HOME=fake_home)
            cfg_im = subprocess.run(
                [REAL_COMPOSE, '-f', 'docker-compose.yml', 'config', '--format', 'json'],
                cwd=str(im_dir), env=im_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(cfg_im.returncode, 0, cfg_im.stderr)
            im_rendered = json.loads(cfg_im.stdout)

            real_manifest, _ = ssh_migrate.load_manifest(REPO_ROOT)
            im_svcs, im_vols, _ = ssh_migrate.load_merged_compose(REPO_ROOT, real_manifest['medion']['immich-app'])
            for svc_name in ('immich-server', 'database'):
                parsed = ssh_migrate.parse_compose_mounts(
                    im_svcs[svc_name], set(im_vols.keys()), str(im_dir.resolve()), fake_home, env_map
                )
                parsed_binds = {m['destination']: m['source'] for m in parsed if m['type'] == 'bind'}
                real_binds = {
                    v['target']: v['source']
                    for v in im_rendered['services'][svc_name].get('volumes', [])
                    if v.get('type') == 'bind'
                }
                self.assertEqual(parsed_binds, real_binds, f'Mismatch for medion/immich-app/{svc_name}')

            # 2. shared/backup + compose.medion.yml
            sb_dir = base / 'backup'
            shutil.copytree(REPO_ROOT / 'shared/backup', sb_dir)
            (sb_dir / '.secrets.env').write_text('', encoding='utf-8')
            cfg_sb = subprocess.run(
                [REAL_COMPOSE, '-f', 'docker-compose.yml', '-f', 'compose.medion.yml', 'config', '--format', 'json'],
                cwd=str(sb_dir), env=im_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(cfg_sb.returncode, 0, cfg_sb.stderr)
            sb_rendered = json.loads(cfg_sb.stdout)

            sb_svcs, sb_vols, _ = ssh_migrate.load_merged_compose(REPO_ROOT, real_manifest['medion']['shared-backup'])
            sb_parsed = ssh_migrate.parse_compose_mounts(
                sb_svcs['autorestic'], set(sb_vols.keys()), str(sb_dir.resolve()), fake_home, env_map
            )
            sb_parsed_binds = {m['destination']: m['source'] for m in sb_parsed if m['type'] == 'bind'}
            sb_real_binds = {
                v['target']: v['source']
                for v in sb_rendered['services']['autorestic'].get('volumes', [])
                if v.get('type') == 'bind'
            }
            self.assertEqual(sb_parsed_binds, sb_real_binds)

if __name__ == '__main__':
    unittest.main()
