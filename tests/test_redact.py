import logging
import unittest
from shelfcheck.redact import RedactingFilter


class RedactTests(unittest.TestCase):
    def test_scrubs(self):
        record = logging.LogRecord("x", logging.INFO, "", 0,
                                   "mail a@b.test from 192.0.2.5?email=c@d.test", (), None)
        RedactingFilter().filter(record)
        text = record.getMessage()
        self.assertNotIn("a@b.test", text); self.assertNotIn("192.0.2.5", text)
        self.assertIn("[email]", text); self.assertIn("[ip]", text)
