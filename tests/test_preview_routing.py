"""The merged preview route must match real preview names, not a literal star."""
from pathlib import Path
import re
import unittest

import yaml


class PreviewRoutingTests(unittest.TestCase):
    def test_preview_hostname_matches_router_rule(self):
        root=Path(__file__).resolve().parents[1]
        service=yaml.safe_load((root/'dell/t3-code/docker-compose.yml').read_text())['services']['t3-code']
        label=next(item for item in service['labels'] if 'dev-worktrees.rule=' in item)
        pattern=label.split('`')[1]
        self.assertIsNotNone(re.fullmatch(pattern,'feature.dev.dell.lr-projects.de'))
        self.assertIsNone(re.fullmatch(pattern,'feature.dev.dell.lr-projects.de.attacker.example'))
