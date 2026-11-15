import tempfile
import unittest
from datetime import date
from pathlib import Path
from shelfcheck.readiness import check_catalog, check_robots, check_settings

ROOT = Path(__file__).parent.parent


class ReadinessTests(unittest.TestCase):
    def test_robots(self):
        items = check_robots("User-agent: ChatGPT-User\nDisallow: /products/\n", date(2026, 10, 24))
        item = next(x for x in items if x.id == "robots_chatgpt-user")
        self.assertEqual("fail", item.status)
        training = check_robots("User-agent: GPTBot\nDisallow: /\n", date(2026, 10, 24))
        self.assertEqual("warn", next(x for x in training if x.id == "robots_gptbot").status)

    def test_catalog_and_settings(self):
        items = check_catalog(ROOT/"fixtures/products_export_demo.csv")
        barcode = next(x for x in items if x.id == "gtin_present")
        self.assertEqual("fail", barcode.status); self.assertIn("ridge-light", barcode.evidence)
        settings = check_settings(ROOT/"fixtures/settings_answers_demo.json")
        self.assertEqual("warn", next(x for x in settings if x.id == "agentic_checkout_enrolled").status)
