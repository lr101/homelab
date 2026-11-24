"""End-to-end tests for controller-side SSH migration without a real production host."""
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

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'scripts/ssh-migrate.py'


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

proc = subprocess.run(['/bin/sh', '-c', remote_cmd], env=env)
sys.exit(proc.returncode)
'''


FAKE_SUDO_PY = r'''#!/usr/bin/env python3
"""Fake sudo -n shim for testing explicit --sudo invocation."""
import json
import os
from pathlib import Path
import subprocess
import sys

log_path = Path(os.environ['FAKE_SUDO_LOG'])
argv = sys.argv[1:]
with log_path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'argv': argv}) + '\n')

if os.environ.get('FAKE_SUDO_FAIL') == '1':
    print('sudo: a password is required', file=sys.stderr)
    sys.exit(1)

if not argv or argv[0] != '-n':
    print('fake-sudo: expected -n flag', file=sys.stderr)
    sys.exit(1)
rest = argv[1:]
if rest and rest[0] == '--':
    rest = rest[1:]
env = dict(os.environ)
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
"""Stateful fake docker/compose CLI for testing remote adoption, identity preservation, and rollback."""
import json
import os
from pathlib import Path
import sys
import yaml

state_path = Path(os.environ['FAKE_DOCKER_STATE'])
log_path = Path(os.environ['FAKE_DOCKER_LOG'])


def load_state():
    return json.loads(state_path.read_text(encoding='utf-8'))


def save_state(state):
    state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')


def log_call(entry):
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry) + '\n')


argv = sys.argv[1:]
state = load_state()

if argv[:2] == ['ps', '-aq']:
    for cid in state.get('containers', {}):
        print(cid)
    sys.exit(0)

if argv[:1] == ['inspect']:
    fmt = ''
    cid = argv[-1]
    if '--format' in argv:
        fmt = argv[argv.index('--format') + 1]
    container = state.get('containers', {}).get(cid)
    if not container:
        print(f'Error: No such object: {cid}', file=sys.stderr)
        sys.exit(1)
    if '.Config.Env' in fmt:
        print('TEST-FAILURE: docker inspect requested Config.Env!', file=sys.stderr)
        sys.exit(99)
    out = {
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
    }
    print(json.dumps(out))
    sys.exit(0)

if argv[:2] == ['image', 'inspect']:
    ref = argv[-1]
    images = state.get('images', {})
    if ref not in images:
        print(f'Error: No such image: {ref}', file=sys.stderr)
        sys.exit(1)
    if '--format' in argv:
        fmt = argv[argv.index('--format') + 1]
        if 'RepoDigests' in fmt:
            print(json.dumps(images[ref].get('repo_digests', [])))
            sys.exit(0)
    print(json.dumps([images[ref]]))
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
    }))
    sys.exit(0)

if argv[:2] == ['network', 'ls']:
    for nname in state.get('networks', []):
        print(json.dumps(nname))
    sys.exit(0)

if argv[:2] == ['compose', 'version']:
    print('Docker Compose version v2.29.7')
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
    merged_services = {}
    merged_volumes = {}
    for fname in files:
        fpath = (cwd / fname) if not Path(fname).is_absolute() else Path(fname)
        data = yaml.safe_load(fpath.read_text(encoding='utf-8')) or {}
        for sname, scfg in (data.get('services') or {}).items():
            cur = merged_services.setdefault(sname, {})
            cur.update(scfg or {})
        for vname, vcfg in (data.get('volumes') or {}).items():
            cur = merged_volumes.setdefault(vname, {})
            if isinstance(vcfg, dict):
                cur.update(vcfg)

    log_call({
        'argv': argv,
        'cwd': str(cwd),
        'project': project,
        'files': files,
        'subcmd': subcmd,
        'env_keys': sorted(k for k in os.environ if k in ('DB_PASSWORD', 'APP_TOKEN', 'UPLOAD_LOCATION', 'TEST_SECRET', 'CF_DNS_API_TOKEN')),
        'env_snapshot': {k: os.environ[k] for k in ('APP_TOKEN', 'UPLOAD_LOCATION', 'TEST_SECRET') if k in os.environ},
        'merged_volumes': merged_volumes,
        'merged_images': {k: v.get('image') for k, v in merged_services.items()},
    })

    for vkey, vcfg in merged_volumes.items():
        if isinstance(vcfg, dict) and vcfg.get('external'):
            target_vname = vcfg.get('name', vkey)
            if target_vname not in state.get('volumes', {}):
                print(f'external volume "{target_vname}" not found', file=sys.stderr)
                sys.exit(1)

    fail_rules = state.get('fail_rules', {})
    action = subcmd[0] if subcmd else ''
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

    if action == 'up':
        if '--no-build' not in subcmd or '--pull' not in subcmd:
            print('fake-docker: expected --no-build and --pull never during adoption', file=sys.stderr)
            sys.exit(1)
        pull_idx = subcmd.index('--pull')
        if pull_idx + 1 >= len(subcmd) or subcmd[pull_idx + 1] != 'never':
            print('fake-docker: expected --pull never', file=sys.stderr)
            sys.exit(1)
        containers = state.setdefault('containers', {})
        for cid, c in list(containers.items()):
            if c.get('project') == project and c.get('service') in merged_services:
                svc = merged_services[c['service']]
                if 'image' in svc:
                    c['image'] = svc['image']
                c['config_files'] = ','.join(str((cwd / f).resolve()) for f in files)
                c['running'] = not state.get('force_stopped_after_up', {}).get(project, False)
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
                'sha256:111111111111': {'repo_digests': ['example/webapp@sha256:1111']},
                'sha256:222222222222': {'repo_digests': ['mariadb@sha256:2222']},
                'sha256:333333333333': {'repo_digests': ['jellyfin/jellyfin@sha256:3333']},
                'sha256:444444444444': {'repo_digests': ['radius/db@sha256:4444']},
                'sha256:555555555555': {'repo_digests': ['radius/app@sha256:5555']},
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
        self.assertEqual(webapp_up['merged_images']['web'], 'sha256:111111111111')
        self.assertEqual(webapp_up['merged_images']['db'], 'sha256:222222222222')
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

        # 1. One-shot schema migration (hiring_scraper) is blocked from automatic adoption
        hs_plan = self.outside / 'hs-plan.json'
        res_hs = self.run_cli(
            'plan', '--host', 'medion', '--ssh-target', 'medion',
            '--source-root', str(self.remote_root), '--stack', 'hiring_scraper',
            '--plan', str(hs_plan),
        )
        self.assertNotEqual(res_hs.returncode, 0)
        self.assertIn('migrate', res_hs.stderr)
        self.assertEqual((self.remote_root / 'medion/hiring_scraper/hiring_db/PG_VERSION').read_text(), '16\n')

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


if __name__ == '__main__':
    unittest.main()
