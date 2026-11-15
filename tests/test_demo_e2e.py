import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from shelfcheck.cli import main


class DemoTests(unittest.TestCase):
    def test_demo(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHELFCHECK_OUT": directory}):
            self.assertEqual(0, main(["demo"]))
            weeks = list((Path(directory)/"demo").iterdir())
            self.assertEqual(1, len(weeks))
            for name in ("samples.json", "readiness.json", "traffic.json", "report.html", "report.txt"):
                self.assertTrue((weeks[0]/name).is_file())
            self.assertEqual(1, len(list((Path(directory)/"outbox").glob("*.eml"))))
