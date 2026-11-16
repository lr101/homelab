import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("scan", Path(__file__).parents[1] / "scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class ScanTests(unittest.TestCase):
    def test_duplicate_matches_and_fixable_counts(self):
        match = {"vulnerability": {"id": "CVE-1", "severity": "High", "fix": {"state": "fixed"}},
                 "artifact": {"id": "package-1"}}
        other = {"vulnerability": {"id": "CVE-2", "severity": "unexpected"}, "artifact": {"id": "package-2"}}
        counts = scan.summarize({"matches": [match, match, other]})
        self.assertEqual((counts["total"], counts["high"], counts["unknown"], counts["fixable"]), (2, 1, 1, 1))
        with self.assertRaises(ValueError):
            scan.summarize({})

    def test_metric_escaping_and_no_false_zero_before_first_success(self):
        state = {"host": {"tags": {"target": "host a,b=c\n"}, "success": 0, "last_attempt": 100}}
        result = scan.metrics(state, "server")
        self.assertIn("target=host\\ a\\,b\\=c_", result)
        self.assertIn("last_success=0i", result)
        self.assertNotIn("total=", result)
        self.assertEqual(len(result.splitlines()), 1)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "reports").mkdir()
        (self.root / "metrics").mkdir()
        self.args = SimpleNamespace(state_dir=self.root, server="test-server", image=scan.IMAGE, timeout=30)
        self.target = ("container:web", "docker:sha256:abc", {
            "kind": "container", "target": "web", "project": "test", "service": "web"})

    def state(self):
        return json.loads((self.root / "state.json").read_text())

    def run_with(self, targets, result):
        with patch.object(scan, "discover", return_value=targets), patch.object(scan, "run_scan", side_effect=result) as runner:
            code = scan.scan(self.args)
        return code, runner

    def test_shared_images_scanned_once_and_removed_targets_pruned(self):
        second = ("container:worker", self.target[1], {**self.target[2], "target": "worker"})
        code, runner = self.run_with([self.target, second], lambda *args: {"total": 3})
        self.assertEqual(code, 0)
        self.assertEqual(runner.call_count, 2)  # Host + one shared image.
        self.assertEqual(self.state()["container:worker"]["counts"]["total"], 3)
        self.run_with([], lambda *args: {"total": 0})
        self.assertNotIn("container:web", self.state())

    def test_failed_scans_keep_last_successful_counts(self):
        self.run_with([self.target], lambda *args: {"total": 7})
        before = self.state()["container:web"]["last_success"]
        code, _ = self.run_with([self.target], subprocess.CalledProcessError(1, "grype"))
        self.assertEqual(code, 1)
        state = self.state()["container:web"]
        self.assertEqual(state["counts"]["total"], 7)
        self.assertEqual(state["last_success"], before)
        self.assertEqual(state["success"], 0)

    def test_recreated_container_does_not_inherit_old_image_counts(self):
        self.run_with([self.target], lambda *args: {"total": 7})
        changed = (self.target[0], "docker:sha256:new", self.target[2])
        self.run_with([changed], subprocess.CalledProcessError(1, "grype"))
        self.assertNotIn("counts", self.state()["container:web"])

    def test_discovery_failure_retains_but_invalidates_previous_targets(self):
        self.run_with([self.target], lambda *args: {"total": 7})
        discovery_time = self.state()["discovery"]["last_success"]
        with patch.object(scan, "discover", side_effect=OSError("Docker unavailable")), patch.object(scan, "run_scan", return_value={"total": 0}):
            self.assertEqual(scan.scan(self.args), 1)
        self.assertEqual(self.state()["container:web"]["success"], 0)
        self.assertEqual(self.state()["discovery"]["success"], 0)
        self.assertEqual(self.state()["discovery"]["last_success"], discovery_time)
        self.assertEqual(self.state()["container:web"]["counts"]["total"], 7)

    def test_atomic_metrics_permissions(self):
        self.run_with([], lambda *args: {"total": 0})
        self.assertEqual((self.root / "metrics" / "metrics.lp").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.root / "state.json").stat().st_mode & 0o777, 0o600)

    def test_timeout_removes_scan_container(self):
        def run(command, **kwargs):
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, 30)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(scan.subprocess, "run", side_effect=run) as runner:
            with self.assertRaises(subprocess.TimeoutExpired):
                scan.run_scan("docker:sha256:abc", self.args, self.root / "report.json")
        self.assertEqual(runner.call_args.args[0][:3], ["docker", "rm", "-f"])

    def test_host_report_validation_and_read_only_mount(self):
        for distro in ("fedora", "debian", "unknown"):
            with self.subTest(distro=distro):
                def run(command, **kwargs):
                    kwargs["stdout"].write(json.dumps({"matches": [], "distro": {"name": distro}}))
                    self.assertIn("type=bind,src=/,dst=/host,readonly,bind-propagation=rprivate", command)
                    self.assertIn("./proc/**", command)
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(scan.subprocess, "run", side_effect=run):
                    if distro == "unknown":
                        with self.assertRaises(ValueError):
                            scan.run_scan("dir:/host", self.args, self.root / "report.json")
                    else:
                        self.assertEqual(scan.run_scan("dir:/host", self.args, self.root / "report.json")["total"], 0)


if __name__ == "__main__":
    unittest.main()
