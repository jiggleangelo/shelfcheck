import re
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

_TOKEN = re.compile(r"^v_[0-9a-f]{16}$")


class Mailer(Protocol):
    def send(self, recipient_token: str, subject: str, html: str, text: str): ...


class OutboxMailer:
    """Development stub which writes token-addressed messages; it does not deliver mail."""

    def __init__(self, root):
        self.root = Path(root)

    def send(self, recipient_token, subject, html, text):
        if not _TOKEN.fullmatch(str(recipient_token)):
            raise ValueError("recipient must be an opaque vault token")
        message = EmailMessage()
        message["To"] = recipient_token
        message["From"] = "shelfcheck@localhost"
        message["Subject"] = " ".join(str(subject).splitlines())
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        root = self.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = (root / f"{stamp}-{recipient_token}.eml").resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("unsafe outbox path") from exc
        path.write_bytes(message.as_bytes())
        return path
