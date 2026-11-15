import csv
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from app import Handler, MAX_BODY, Store, _multipart, _profile_config
from shelfcheck.models import AgentRun, BrandShare
from shelfcheck.readiness import check_catalog_rows
from shelfcheck.report import REPORT_NOTICE, render_snapshot
from shelfcheck.stats import summarize
from shelfcheck.traffic import parse_ga4_rows


class ReviewFixTests(unittest.TestCase):
    def test_shopify_extra_image_rows_are_not_variants(self):
        rows = [
            {"Handle":"sock", "Option1 Value":"Default Title", "Variant Price":"20", "Variant Barcode":"123", "Variant Inventory Qty":"4", "Image Src":"one.jpg", "Body (HTML)":"x"*220, "Type":"Socks"},
            {"Handle":"sock", "Option1 Value":"", "Variant Price":"", "Variant Barcode":"", "Variant Inventory Qty":"", "Image Src":"two.jpg", "Body (HTML)":"", "Type":""},
        ]
        checks = {x.id:x for x in check_catalog_rows(rows)}
        self.assertEqual("pass", checks["price_present"].status)
        self.assertEqual("pass", checks["gtin_present"].status)
        self.assertEqual("pass", checks["inventory_signal"].status)

    def test_missing_inventory_column_is_explained_as_warning(self):
        rows = [{"Handle":"sock", "Variant SKU":"S1", "Variant Price":"20", "Variant Barcode":"123", "Image Src":"x", "Body (HTML)":"x"*220, "Type":"Socks"}]
        item = next(x for x in check_catalog_rows(rows) if x.id == "inventory_signal")
        self.assertEqual("warn", item.status)
        self.assertIn("does not include", item.evidence)

    def test_ga4_accepts_display_headers_and_reports_counts(self):
        rows = csv.DictReader(io.StringIO("Session source,Session medium,Page path,Sessions\nchatgpt.com,referral,/products/a,3\nnewsletter,email,/,2\nbad,referral,/,nope\n"))
        parsed = parse_ga4_rows(rows)
        self.assertEqual((3, 3), (parsed[0].sessions, parsed[0].product_pages))
        self.assertEqual((3, 1, 2), (parsed.lines_read, parsed.matched_rows, parsed.skipped_lines))

    def test_ga4_metadata_lines_can_be_skipped(self):
        text = "# GA4 exploration\n# exported today\nSession source,Sessions,Page path\nmeta.ai,2,/cart\n"
        reader = csv.DictReader(line for line in io.StringIO(text) if not line.lstrip().startswith("#"))
        parsed = parse_ga4_rows(reader)
        self.assertEqual(2, parsed[0].sessions)
        self.assertEqual(2, parsed[0].checkout_hits)

    def test_prompt_clusters_use_tagged_prompt_ids(self):
        cfg = _profile_config({"name":"A", "domain":"a.test"})
        runs = [AgentRun("manual", "p01", 0, ("A",)), AgentRun("manual", "p01", 1, ()), AgentRun("manual", "p02", 2, ("A",))]
        share = summarize(runs, (cfg.primary,))[0]
        self.assertEqual(2, share.mentions)
        self.assertEqual(3, share.runs)

    def test_report_computes_comparisons_and_only_flags_material_share_changes(self):
        current = [
            asdict(BrandShare("A", "manual", 9, 10, .9, .65, .98, None, None)),
            asdict(BrandShare("B", "manual", 1, 10, .1, .02, .35, None, None)),
            asdict(BrandShare("C", "manual", 5, 10, .5, .3, .7, None, None)),
        ]
        prior = [
            asdict(BrandShare("A", "manual", 4, 10, .4, .2, .6, None, None)),
            asdict(BrandShare("B", "manual", 1, 10, .1, .02, .35, None, None)),
            asdict(BrandShare("C", "manual", 5, 10, .5, .3, .7, None, None)),
        ]
        snap = {"date":"2026-02-02", "profile":{"name":"A"}, "readiness":[], "traffic":[], "shares":current, "prompt":{"competitors":[{"name":"B"},{"name":"C"}]}}
        old = {"date":"2026-01-01", "readiness":[], "traffic":[], "shares":prior}
        page, text = render_snapshot(snap["profile"], snap, old)
        self.assertIn("A ahead of B", page)
        self.assertIn("not distinguishable", page)
        self.assertIn("A/manual: change flagged", text)
        self.assertNotIn("B/manual: stable", text)
        self.assertNotIn("C/manual: stable", text)

    def test_report_contains_notice_privacy_and_prune_warning(self):
        snap = {"date":"now", "profile":{"name":"A"}, "readiness":[], "traffic":[], "shares":[], "pruned_oldest":True}
        page, text = render_snapshot(snap["profile"], snap)
        self.assertIn(REPORT_NOTICE, page)
        self.assertIn("https://manykiln.com/privacy", page)
        self.assertIn("oldest snapshot was removed", text)

    def test_replacing_share_removes_every_old_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            token = store.create(client_key="a")
            ident, _, _, _ = store.read(token)
            first = store.share / "first"
            second = store.share / "second"
            store._atomic(first, ident.encode())
            store._atomic(second, ident.encode())
            store.remove_share_pointers(ident)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_workspace_rate_limit_is_per_client(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            for _ in range(10):
                store.create(client_key="one")
            with self.assertRaisesRegex(ValueError, "this client"):
                store.create(client_key="one")
            store.create(client_key="two")

    def test_usage_is_cached_after_initial_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            token = store.create(client_key="a")
            with patch.object(Path, "rglob", side_effect=AssertionError("rescanned")):
                store.save_draft(token, {"profile":{"name":"A"}})

    def test_snapshot_prunes_before_the_count_limit_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            token = store.create(client_key="a")
            ident, _, _, _ = store.read(token)
            for i in range(26):
                store.save_snapshot(ident, {"id":f"s{i:02d}", "date":f"2026-01-{i+1:02d}"})
            latest = {"id":"new", "date":"2026-02-01"}
            self.assertTrue(store.save_snapshot(ident, latest))
            files = list((store.ws / ident / "snapshots").glob("*.json"))
            self.assertEqual(26, len(files))
            self.assertFalse((store.ws / ident / "snapshots/s00.json").exists())
            self.assertTrue(json.loads((store.ws / ident / "snapshots/new.json").read_text())["pruned_oldest"])

    def test_snapshot_order_uses_dates_not_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            token = store.create(client_key="a")
            ident, _, _, _ = store.read(token)
            store.save_snapshot(ident, {"id":"sample-z", "date":"2026-01-01"})
            store.save_snapshot(ident, {"id":"20250101", "date":"2026-02-01"})
            snapshots = store.read(token)[3]
            self.assertEqual(["sample-z", "20250101"], [x["id"] for x in snapshots])

    def test_multipart_preserves_trailing_newlines_and_quoted_semicolon_filename(self):
        boundary = "abc"
        body = (b"--abc\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a;b.csv\"\r\nContent-Type: text/csv\r\n\r\nx\r\n\r\n--abc--\r\n")
        parts = _multipart("multipart/form-data; boundary=abc", body)
        self.assertEqual("a;b.csv", parts["file"][0])
        self.assertEqual(b"x\r\n", parts["file"][1])

    def test_exact_one_megabyte_file_fits_with_multipart_overhead(self):
        data = b"x" * MAX_BODY
        body = b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.log\"\r\n\r\n" + data + b"\r\n--b--\r\n"
        self.assertGreater(len(body), MAX_BODY)
        self.assertEqual(MAX_BODY, len(_multipart("multipart/form-data; boundary=b", body)["file"][1]))

    def test_heavy_semaphore_can_be_acquired_without_blocking(self):
        class Busy:
            def acquire(self, blocking):
                self.blocking = blocking
                return False
        busy = Busy()
        self.assertFalse(busy.acquire(False))
        self.assertFalse(busy.blocking)

    def test_referer_origin_comparison_rejects_prefix_attack(self):
        handler = object.__new__(Handler)
        handler.headers = {"Referer":"https://app-shelfcheck.manykiln.com.evil.example/form"}
        handler.server = type("Server", (), {"origin":"https://app-shelfcheck.manykiln.com"})()
        calls = []
        handler._problem = lambda status, message: calls.append(status)
        self.assertFalse(handler._valid_post())
        self.assertEqual([403], calls)


if __name__ == "__main__":
    unittest.main()
