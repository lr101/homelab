"""The inventory distinguishes configured protection from verified recovery."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('inventory', Path(__file__).resolve().parents[1] / 'scripts/generate_readme.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def test_backup_is_described_as_configured(self):
        self.assertEqual(inventory.get_backup_status('photos', {'photos': ['nas']}), 'configured')
        self.assertEqual(inventory.get_backup_status('photos', {}), '-')

    def test_missing_backup_destination_is_not_coverage(self):
        self.assertEqual(inventory.get_backup_status('photos', {'photos': []}), '-')

    def test_proxy_auth_counts_even_when_annotation_is_false(self):
        self.assertEqual(inventory.get_sso_status({'labels': [
            'traefik.http.routers.metrics.middlewares=oidc-auth@file'],
            'annotations': ['de.lr-projects.sso=false']}), 'proxy')

    def test_native_sso_is_distinguished_from_proxy_auth(self):
        self.assertEqual(inventory.get_sso_status({'annotations': ['de.lr-projects.sso=true']}), 'app (declared)')

    def test_watchtower_opt_in_is_not_reported_as_applied_update(self):
        self.assertEqual(inventory.get_watchtower_info({'labels': [
            'com.centurylinklabs.watchtower.enable=true']}), 'monitored')

    def test_device_inventory_reports_explicit_proxy_auth(self):
        _, services = inventory.process_device('dell')
        dashboard = next(service for service in services if service['name'] == 'Reverse Proxy')
        self.assertEqual(dashboard['sso'], 'proxy')
