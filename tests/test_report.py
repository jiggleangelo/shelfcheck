import tempfile
import unittest
from pathlib import Path
from shelfcheck.config import load_config
from shelfcheck.mailer import OutboxMailer
from shelfcheck.models import BrandShare, TrafficBucket
from shelfcheck.report import render

ROOT = Path(__file__).parent.parent


class ReportTests(unittest.TestCase):
    def test_banner_intervals_caveat_and_mail(self):
        cfg = load_config(ROOT/"fixtures/brand_demo.json")
        shares = [BrandShare(cfg.primary.name, "muse", 20, 50, .4, .3, .5, 2, 5)]
        traffic = [TrafficBucket("suspected", "unattributed-agent-like", 1, 3, 0)]
        page, text = render(cfg, shares, [], traffic, runs=50)
        self.assertIn("DEMO DATA", page); self.assertIn("95% interval", page)
        self.assertIn("not proof of Muse", page)
        with tempfile.TemporaryDirectory() as directory:
            path = OutboxMailer(directory).send("v_1234567890123456", "Report", page, text)
            message = path.read_text(encoding="utf-8")
            self.assertIn("To: v_1234567890123456", message); self.assertNotIn("person@", message)
