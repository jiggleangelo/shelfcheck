import json
import os
import runpy
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from shelfcheck.config import load_config
from shelfcheck.models import AgentRun
from shelfcheck.sampler import BudgetExceeded, extract_mentions, run_sample
from shelfcheck.stats import summarize
from shelfcheck.traffic import classify_sessions, parse_access_log

ROOT = Path(__file__).resolve().parent.parent


class DemoEndToEndTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("network call in test")))

    def _entrypoint(self):
        with patch("sys.argv", ["shelfcheck", "demo"]):
            with self.assertRaises(SystemExit) as result:
                runpy.run_module("shelfcheck.__main__", run_name="__main__")
        self.assertEqual(0, result.exception.code)

    def test_full_core_flow_from_module_entrypoint(self):
        config = load_config(ROOT / "fixtures/brand_demo.json")
        brands = (config.primary,) + config.competitors
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SHELFCHECK_OUT": directory, "SHELFCHECK_IP_KEY": ""}
        ):
            self._entrypoint()
            root = Path(directory)
            weeks = list((root / config.slug).iterdir())
            self.assertEqual(1, len(weeks))
            target = weeks[0]
            self.assertRegex(target.name, r"^\d{4}-W\d{2}$")
            self.assertEqual(
                {"samples.json", "readiness.json", "traffic.json", "report.html", "report.txt"},
                {path.name for path in target.iterdir()},
            )
            sample_bytes = (target / "samples.json").read_bytes()
            samples = json.loads(sample_bytes)
            self.assertEqual("fake", samples["sample_type"])
            self.assertEqual(0, samples["ambiguity_count"])
            runs = [AgentRun(**row) for row in samples["runs"]]
            self.assertEqual(500, len(runs))
            self.assertEqual(500, len({(r.surface, r.prompt_id, r.repeat) for r in runs}))
            self.assertEqual({"muse": 250, "chatgpt": 250}, Counter(r.surface for r in runs))
            for surface in config.surfaces:
                selected = [r for r in runs if r.surface == surface]
                self.assertEqual(50, len({r.prompt_id for r in selected}))
                self.assertTrue(all(count == 5 for count in Counter(r.prompt_id for r in selected).values()))
            recommendations = {name for run in runs for name in run.recommended}
            self.assertIn("Northfield Outdoor", recommendations)
            self.assertIn("Summit Thread", recommendations)
            for run in runs:
                self.assertTrue(3 <= len(run.recommended) <= 5)
                self.assertTrue(run.browsed_urls)
                found, ambiguous = extract_mentions(run, brands)
                expected = {brand.name: run.recommended.index(brand.name) + 1
                            for brand in brands if brand.name in run.recommended}
                self.assertEqual(expected, found)
                self.assertEqual(0, ambiguous)
            shares = summarize(runs, brands)
            self.assertEqual(8, len(shares))
            for share in shares:
                selected = [r for r in runs if r.surface == share.surface]
                count = sum(share.brand in r.recommended for r in selected)
                self.assertEqual(count, share.mentions)
                self.assertEqual(count / 250, share.share)
                self.assertTrue(0 <= share.ci_low <= share.share <= share.ci_high <= 1)
            primary = {s.surface: s.share for s in shares if s.brand == config.primary.name}
            self.assertNotEqual(primary["muse"], primary["chatgpt"])

            readiness = json.loads((target / "readiness.json").read_text())
            checks = {item["id"]: item for item in readiness}
            self.assertEqual({"robots", "catalog", "checkout"}, {item["area"] for item in readiness})
            self.assertEqual(16, len(checks))
            self.assertEqual("warn", checks["robots_gptbot"]["status"])
            self.assertEqual("pass", checks["robots_googlebot"]["status"])
            self.assertEqual("fail", checks["gtin_present"]["status"])
            self.assertIn("ridge-light", checks["gtin_present"]["evidence"])
            self.assertEqual("warn", checks["agentic_checkout_enrolled"]["status"])
            for ident in ("price_present", "image_present", "description_depth",
                          "product_type_set", "inventory_signal"):
                self.assertEqual("fail", checks[ident]["status"])

            traffic = json.loads((target / "traffic.json").read_text())
            actual = {(row["tier"], row["agent"]):
                      (row["sessions"], row["product_pages"], row["checkout_hits"])
                      for row in traffic}
            self.assertEqual({
                ("declared", "ChatGPT-User"): (1, 1, 1),
                ("referred", "chatgpt.com referral"): (3, 3, 0),
                ("referred", "meta.ai referral"): (3, 3, 0),
                ("referred", "perplexity.ai referral"): (1, 0, 1),
                ("suspected", "unattributed-agent-like"): (1, 3, 1),
            }, actual)
            html = (target / "report.html").read_text(encoding="utf-8")
            text = (target / "report.txt").read_text(encoding="utf-8")
            for rendered in (html, text):
                for phrase in ("DEMO DATA", "95% interval", "not proof of Muse", "500 runs"):
                    self.assertIn(phrase, rendered)
                for share in shares:
                    self.assertIn(share.brand, rendered)
                    self.assertIn(f"{share.share:.1%}", rendered)
                    self.assertIn(f"{share.ci_low:.1%}–{share.ci_high:.1%}", rendered)
            emails = list((root / "outbox").glob("*.eml"))
            self.assertEqual(1, len(emails))
            message = BytesParser(policy=policy.default).parsebytes(emails[0].read_bytes())
            self.assertEqual(config.recipient_token, str(message["To"]))
            self.assertEqual(f"Shelfcheck: {config.primary.name}", str(message["Subject"]))
            self.assertEqual(text, message.get_body(preferencelist=("plain",)).get_content())
            self.assertEqual(html, message.get_body(preferencelist=("html",)).get_content().rstrip("\n"))
            for path in root.rglob("*"):
                if path.is_file():
                    stored = path.read_text(encoding="utf-8")
                    for raw_ip in ("192.0.2.10", "198.51.100.20", "203.0.113.30",
                                   "203.0.113.40", "192.0.2.50"):
                        self.assertNotIn(raw_ip, stored)
            self._entrypoint()
            self.assertEqual(sample_bytes, (target / "samples.json").read_bytes())
            self.assertEqual(2, len(list((root / "outbox").glob("*.eml"))))

    def test_decoy_aliases_and_case_insensitive_legitimate_matches(self):
        config = load_config(ROOT / "fixtures/brand_demo.json")
        brands = (config.primary,) + config.competitors
        decoys = AgentRun("muse", "p01", 0, ("Northfield Outdoor", "Summit Thread"))
        self.assertEqual(({}, 0), extract_mentions(decoys, brands))
        real = AgentRun("muse", "p01", 0,
                        ("Try NORTHFIELD SOCKS for hiking", "https://summit.example/products/x"))
        self.assertEqual(({config.primary.name: 1, config.competitors[0].name: 2}, 0),
                         extract_mentions(real, brands))

    def test_googlebot_burst_is_not_declared_or_suspected(self):
        sessions = parse_access_log(ROOT / "fixtures/access_demo.log", b"test-key")
        self.assertEqual(0, sessions.skipped_lines)
        google = [session for session in sessions
                  if any("Googlebot" in hit.ua for hit in session)]
        self.assertEqual(1, len(google))
        self.assertEqual(3, len(google[0]))
        self.assertEqual([], classify_sessions(google))
        self.assertEqual(1, sum(bucket.sessions for bucket in classify_sessions(sessions)
                                if bucket.tier == "suspected"))

    def test_budget_guard_precedes_all_client_calls(self):
        config = replace(load_config(ROOT / "fixtures/brand_demo.json"), max_runs_per_week=499)

        class ForbiddenClient:
            def __init__(self, surface):
                self.surface = surface

            def run(self, prompt, *, seed):
                raise AssertionError("client called before budget rejection")

        with self.assertRaises(BudgetExceeded):
            run_sample(config, [ForbiddenClient(surface) for surface in config.surfaces])


if __name__ == "__main__":
    unittest.main()
