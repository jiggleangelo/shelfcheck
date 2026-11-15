import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from shelfcheck.traffic import classify_sessions, parse_access_log

ROOT = Path(__file__).parent.parent


class TrafficTests(unittest.TestCase):
    def test_tiers_and_privacy(self):
        buckets = classify_sessions(parse_access_log(ROOT/"fixtures/access_demo.log", b"secret"))
        tiers = {x.tier for x in buckets}
        self.assertEqual({"declared", "referred", "suspected"}, tiers)
        rendered = json.dumps([asdict(x) for x in buckets])
        for raw in ("192.0.2.10", "198.51.100.20", "203.0.113.30", "203.0.113.40"):
            self.assertNotIn(raw, rendered)
        self.assertEqual(1, sum(x.sessions for x in buckets if x.tier == "suspected"))
