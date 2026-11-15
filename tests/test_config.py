import re
import unittest
from pathlib import Path

from shelfcheck.agents import FakeAgentClient
from shelfcheck.config import load_config
from shelfcheck.sampler import run_sample


ROOT = Path(__file__).parent.parent


class ConfigTests(unittest.TestCase):
    def test_demo_recipient_token_uses_vault_token_format(self):
        config = load_config(ROOT / "fixtures" / "brand_demo.json")
        self.assertRegex(config.recipient_token, r"^v_[0-9a-f]{16}$")

    def test_demo_fixture_has_required_fields_and_runs_500_within_budget(self):
        config = load_config(ROOT / "fixtures" / "brand_demo.json")
        self.assertEqual("demo", config.slug)
        self.assertEqual("outdoor apparel", config.category)
        self.assertEqual("mid-range", config.price_band)
        self.assertEqual(("muse", "chatgpt"), config.surfaces)
        self.assertEqual(5, config.repeats)
        self.assertEqual(500, config.max_runs_per_week)

        catalog = [config.primary.name] + [brand.name for brand in config.competitors]
        clients = [
            FakeAgentClient(surface, catalog, [4, 3, 2, 1])
            for surface in config.surfaces
        ]
        runs = run_sample(config, clients)
        self.assertEqual(500, len(runs))
        self.assertLessEqual(len(runs), config.max_runs_per_week)
