import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from shelfcheck.cli import main


class WaitlistTests(unittest.TestCase):
    def _run(self, directory, value):
        environment = {
            "SHELFCHECK_VAULT": directory,
            "SHELFCHECK_VAULT_KEY": "test vault master key",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, environment, clear=False), \
                patch("sys.stdin", io.StringIO(value)), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["waitlist", "--email-stdin"])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_rejects_empty_and_malformed_email_without_storing(self):
        invalid = ("\n", "garbage\n", "person@\n", " person@example.test\n",
                   "a..b@example.test\n", "a" * 65 + "@example.test\n",
                   "a@" + "b" * 250 + ".test\n")
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                status, stdout, stderr = self._run(directory, value)
                self.assertEqual(2, status)
                self.assertEqual("", stdout)
                self.assertTrue(stderr)
                self.assertFalse((Path(directory) / "email").exists())

    def test_accepts_valid_email_and_returns_recipient_token(self):
        with tempfile.TemporaryDirectory() as directory:
            status, stdout, stderr = self._run(directory, "person@example.test\n")
            self.assertEqual(0, status)
            self.assertRegex(stdout.strip(), r"^v_[0-9a-f]{16}$")
            self.assertEqual("", stderr)
            self.assertTrue((Path(directory) / "email" / stdout.strip()).is_file())
