import hashlib
import hmac
import ipaddress
import logging
import re
import traceback

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?![\w:])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_QUERY = re.compile(r"(?i)(email=)[^&\s]+")
_INSTALLED = False


def scrub(value):
    text = str(value)
    text = _QUERY.sub(r"\1[redacted]", text)
    text = _EMAIL.sub("[email]", text)
    text = _IPV4.sub("[ip]", text)
    return _IPV6.sub("[ip]", text)


def _scrub_value(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, tuple):
        return tuple(_scrub_value(item) for item in value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    return value


class _RedactingLogRecord(logging.LogRecord):
    def __setattr__(self, name, value):
        if name not in {"exc_info"}:
            value = _scrub_value(value)
        super().__setattr__(name, value)

    def getMessage(self):
        return scrub(super().getMessage())


class RedactingFilter(logging.Filter):
    def filter(self, record):
        _normalise(record)
        return True


def _normalise(record):
    if record.exc_info:
        record.exc_text = scrub("".join(traceback.format_exception(*record.exc_info)))
        record.exc_info = None
    try:
        message = scrub(record.getMessage())
    except Exception:
        message = scrub(record.msg)
    record.msg = message
    record.args = ()
    for name, value in tuple(vars(record).items()):
        if name != "exc_info":
            setattr(record, name, _scrub_value(value))
    return record


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if not isinstance(record, _RedactingLogRecord):
            try:
                record.__class__ = _RedactingLogRecord
            except TypeError:
                replacement = _RedactingLogRecord(
                    record.name, record.levelno, record.pathname, record.lineno,
                    record.msg, record.args, record.exc_info,
                    record.funcName, record.stack_info)
                replacement.__dict__.update(record.__dict__)
                record = replacement
        return _normalise(record)

    logging.setLogRecordFactory(factory)
    _INSTALLED = True


def anonymise_ip(ip, key):
    address = ipaddress.ip_address(ip)
    if address.version == 6:
        prefix = ":".join(address.exploded.split(":")[:3]) + "::/48"
    else:
        prefix = ".".join(str(address).split(".")[:3]) + ".0/24"
    return hmac.new(key, prefix.encode(), hashlib.sha256).hexdigest()[:12]
