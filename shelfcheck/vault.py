import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_HEADER = b"SCV1"
_NONCE_BYTES = 16
_TAG_BYTES = hashlib.sha256().digest_size


class Vault:
    def __init__(self, root, key):
        if not isinstance(key, bytes) or not key:
            raise ValueError("vault key must be non-empty bytes")
        self._root = Path(root)
        self._token_key = hmac.new(key, b"shelfcheck:vault:token:v1", hashlib.sha256).digest()
        self._encryption_key = hmac.new(key, b"shelfcheck:vault:encryption:v1", hashlib.sha256).digest()
        self._mac_key = hmac.new(key, b"shelfcheck:vault:mac:v1", hashlib.sha256).digest()

    def _validate(self, kind, value):
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise ValueError("invalid vault value kind")
        if not isinstance(value, str) or not value:
            raise ValueError("vault value must be a non-empty string")

    def _token(self, kind, value):
        payload = kind.encode("utf-8") + b"\0" + value.encode("utf-8")
        digest = hmac.new(self._token_key, payload, hashlib.sha256).hexdigest()
        return "v_" + digest[:16]

    def _path(self, kind, token):
        return self._root / kind / token

    def _stream(self, nonce, length):
        blocks = []
        counter = 0
        remaining = length
        while remaining:
            block = hmac.new(
                self._encryption_key,
                nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
            take = min(remaining, len(block))
            blocks.append(block[:take])
            remaining -= take
            counter += 1
        return b"".join(blocks)

    def _seal(self, kind, value):
        plaintext = value.encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        stream = self._stream(nonce, len(plaintext))
        ciphertext = bytes(left ^ right for left, right in zip(plaintext, stream))
        authenticated = _HEADER + kind.encode("utf-8") + b"\0" + nonce + ciphertext
        tag = hmac.new(self._mac_key, authenticated, hashlib.sha256).digest()
        return _HEADER + nonce + tag + ciphertext

    def contains(self, kind, value):
        self._validate(kind, value)
        return self._path(kind, self._token(kind, value)).is_file()

    def put(self, kind, value):
        self._validate(kind, value)
        token = self._token(kind, value)
        directory = self._root / kind
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        path = self._path(kind, token)
        record = self._seal(kind, value)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return token
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(record)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return token
