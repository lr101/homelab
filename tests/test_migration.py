"""Migration must never modify source state or print plaintext credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/migrate-homelab.py'


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.source = Path(self.temp.name) / 'legacy'
        (self.root / 'shared/secrets').mkdir(parents=True)
        (self.source / 'medion/app').mkdir(parents=True)
        self.env = self.source / 'medion/app/.env'
        self.env.write_text("TEST_VALUE='literal$test # value'\nSETTING=existing\n")
        (self.source / 'medion/app/token.json').write_text('{"example":true}')
        self.manifest = {'medion': {'app': {'directory':'medion/app',
            'encrypted_file':'secrets/medion/app.enc.json', 'env_file':'medion/app/.secrets.env',
            'required':['TEST_VALUE'], 'files':{'TOKEN_JSON':'medion/app/.secrets/token.json'},
            'file_only':['TOKEN_JSON'], 'migration':{'env_files':['medion/app/.env'],
                'file_values':{'TOKEN_JSON':'medion/app/token.json'}}}}}
        self.write_manifest()
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'sops'
        fake.write_text('#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\nvalues=json.load(sys.stdin)\nPath(os.environ["TEST_CAPTURE"]).write_text(json.dumps(values))\nprint(json.dumps({**{key:("ENC[fixture]" if value else "") for key,value in values.items()}, "sops":{"mac":"ENC[fixture]"}}))\n')
        fake.chmod(0o700)

    def write_manifest(self):
        (self.root / 'shared/secrets/deployments.json').write_text(json.dumps(self.manifest))

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root), *args],
            capture_output=True, text=True, env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],
            'TEST_CAPTURE':str(Path(self.temp.name)/'capture.json')})

    def command(self, *extra):
        return self.run_script('import-secrets','--host','medion','--stack','app','--source-root',str(self.source),*extra)

    def test_default_import_is_a_plan_and_never_runs_encryption(self):
        result=self.command()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('DRY RUN',result.stdout)
        self.assertFalse((Path(self.temp.name)/'capture.json').exists())
        self.assertNotIn('literal$test',result.stdout+result.stderr)
        self.assertFalse((self.root/'secrets').exists())

    def test_apply_encrypts_literal_values_and_original_file_content_without_editing_sources(self):
        before=self.env.read_bytes()
        result=self.command('--apply')
        self.assertEqual(result.returncode,0,result.stderr)
        captured=json.loads((Path(self.temp.name)/'capture.json').read_text())
        self.assertEqual(captured,{'TEST_VALUE':'literal$test # value','SETTING':'existing','TOKEN_JSON':'{"example":true}'})
        self.assertEqual(self.env.read_bytes(),before)
        self.assertNotIn('literal$test',result.stdout+result.stderr)
        output=self.root/'secrets/medion/app.enc.json'
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(output.read_text())['TEST_VALUE'],'ENC[fixture]')

    def test_existing_encrypted_file_is_never_overwritten(self):
        target=self.root/'secrets/medion/app.enc.json';target.parent.mkdir(parents=True);target.write_text('existing ciphertext')
        result=self.command('--apply')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(target.read_text(),'existing ciphertext')

    def test_unresolved_interpolation_is_rejected_instead_of_guessing(self):
        self.env.write_text('TEST_VALUE=${UNKNOWN}\n')
        result=self.command('--apply')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('interpolation',result.stderr)
        self.assertFalse((self.root/'secrets').exists())

    def test_duplicate_values_are_rejected(self):
        self.env.write_text('TEST_VALUE=one\nTEST_VALUE=two\n')
        result=self.command('--apply')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Duplicate',result.stderr)

    def test_missing_source_is_not_silently_skipped(self):
        (self.source/'medion/app/token.json').unlink()
        result=self.command('--apply')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'secrets').exists())

    def test_capture_host_encrypts_wireguard_without_touching_it(self):
        system=Path(self.temp.name)/'system';wg=system/'etc/wireguard/wg0.conf';wg.parent.mkdir(parents=True)
        wg.write_text('[Interface]\nPrivateKey = fixture-key\nAddress = 10.0.0.1/24\n')
        result=self.run_script('capture-host','--host','medion','--system-root',str(system),'--apply')
        self.assertEqual(result.returncode,0,result.stderr)
        values=json.loads((Path(self.temp.name)/'capture.json').read_text())
        self.assertIn(wg.read_text(),values.values())
        self.assertNotIn('fixture-key',result.stdout+result.stderr)
        self.assertEqual(wg.read_text(),'[Interface]\nPrivateKey = fixture-key\nAddress = 10.0.0.1/24\n')

    def test_audit_projects_mounts_and_excludes_container_environment(self):
        docker=self.bin/'docker'
        docker.write_text('#!/usr/bin/env python3\nimport json,sys\nif sys.argv[1:3]==["ps","-aq"]:print("container-id")\nelse:print(json.dumps({"name":"/app","image":"image-id","project":"keep-this-project","service":"app","mounts":[{"Source":"/actual/data","Destination":"/data","Type":"bind"}],"running":True}))\n')
        docker.chmod(0o700)
        report=Path(self.temp.name)/'audit.json'
        result=self.run_script('audit','--host','medion','--source-root',str(self.source),'--output',str(report))
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(report.read_text())
        self.assertEqual(data['containers'][0]['project'],'keep-this-project')
        self.assertEqual(data['containers'][0]['mounts'][0]['Source'],'/actual/data')
        self.assertNotIn('literal$test',report.read_text())
        self.assertEqual(report.stat().st_mode&0o777,0o600)

    def test_optional_empty_environment_values_are_preserved(self):
        self.env.write_text("TEST_VALUE='literal'\nSETTING=\n")
        result=self.command('--apply')
        self.assertEqual(result.returncode,0,result.stderr)
        target=json.loads((self.root/'secrets/medion/app.enc.json').read_text())
        self.assertEqual(target['SETTING'],'')

    def test_runtime_firewall_capture_is_encrypted_not_printed(self):
        system=Path(self.temp.name)/'system';wg=system/'etc/wireguard/wg0.conf';wg.parent.mkdir(parents=True);wg.write_text('fixture')
        for name in ('iptables-save','ip6tables-save'):
            tool=self.bin/name;tool.write_text('#!/bin/sh\nprintf "%s\\n" "-A FORWARD -i wg0 -j ACCEPT"\n');tool.chmod(0o700)
        result=self.run_script('capture-host','--host','medion','--system-root',str(system),'--runtime-firewall','iptables','--apply')
        self.assertEqual(result.returncode,0,result.stderr)
        captured=json.loads((Path(self.temp.name)/'capture.json').read_text())
        self.assertEqual(captured['LIVE_IPTABLES_SAVE'],'-A FORWARD -i wg0 -j ACCEPT\n')
        self.assertNotIn('-A FORWARD',result.stdout+result.stderr)

    def test_audit_refuses_a_nonexistent_local_source(self):
        report=Path(self.temp.name)/'audit.json'
        result=self.run_script('audit','--host','medion','--source-root',str(self.source/'missing'),'--output',str(report))
        self.assertNotEqual(result.returncode,0)
        self.assertIn('existing local directory',result.stderr)
        self.assertFalse(report.exists())

    def test_file_inputs_preserve_original_crlf_bytes(self):
        payload=b'{\r\n"example":true\r\n}\r\n'
        (self.source/'medion/app/token.json').write_bytes(payload)
        result=self.command('--apply')
        self.assertEqual(result.returncode,0,result.stderr)
        values=json.loads((Path(self.temp.name)/'capture.json').read_text())
        self.assertEqual(values['TOKEN_JSON'].encode(),payload)
