import hashlib
import hmac
import inspect
import os
import tempfile
import unittest
from pathlib import Path

from shelfcheck.vault import Vault


class VaultTests(unittest.TestCase):
    def test_write_only_stable_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(Path(directory), b"key")
            one = vault.put("email", "person@example.test")
            two = vault.put("email", "person@example.test")
            self.assertEqual(one, two)
            self.assertTrue(vault.contains("email", "person@example.test"))
            path = Path(directory) / "email" / one
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
            self.assertNotIn(b"person@example.test", path.read_bytes())
            public = [name for name, _ in inspect.getmembers(Vault, inspect.isfunction)
                      if not name.startswith("_")]
            self.assertEqual(["contains", "put"], public)
            self.assertNotIn("read_text", inspect.getsource(Vault))

    def test_uses_distinct_derived_keys_for_each_purpose(self):
        master = b"key"
        vault = Vault(Path("unused"), master)
        expected = {
            hmac.new(master, b"shelfcheck:vault:token:v1", hashlib.sha256).digest(),
            hmac.new(master, b"shelfcheck:vault:encryption:v1", hashlib.sha256).digest(),
            hmac.new(master, b"shelfcheck:vault:mac:v1", hashlib.sha256).digest(),
        }
        actual = {vault._token_key, vault._encryption_key, vault._mac_key}
        self.assertEqual(expected, actual)
        self.assertEqual(3, len(actual))
        self.assertNotIn(master, actual)
