import csv
import hashlib
import hmac
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .models import TrafficBucket
from .redact import anonymise_ip

_LOG = re.compile(r'^(\S{1,64}) \S{1,64} \S{1,64} \[([^]]{1,128})] "(\S{1,16}) (\S{1,4096}) [^"]{1,32}" (\d{3}) \S{1,32} "([^"]{0,4096})" "([^"]{0,4096})"(?: "([^"]{0,4096})")?$')
_MAX_LINE = 16384
_MAX_LINES = 300000


def _signatures():
    return json.loads((Path(__file__).with_name("data") / "agent_signatures.json").read_text(encoding="utf-8"))


@dataclass
class _Hit:
    key: str
    when: datetime
    path: str
    referrer: str
    ua: str
    accept: str


class ParsedSessions(list):
    def __init__(self, values=(), skipped_lines=0, lines_read=0, stopped_early=False):
        super().__init__(values)
        self.skipped_lines = skipped_lines
        self.lines_read = lines_read
        self.stopped_early = stopped_early


class ParsedGA4(list):
    def __init__(self, values=(), lines_read=0, matched_rows=0, skipped_lines=0):
        super().__init__(values)
        self.lines_read = lines_read
        self.matched_rows = matched_rows
        self.skipped_lines = skipped_lines


def parse_access_lines(iterable, key, deadline=None):
    hits = []
    skipped = lines_read = 0
    stopped = False
    for raw_line in iterable:
        if lines_read >= _MAX_LINES or (deadline is not None and time.monotonic() >= deadline):
            stopped = True
            break
        lines_read += 1
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", "replace")
        if len(raw_line) > _MAX_LINE:
            skipped += 1
            continue
        match = _LOG.match(raw_line.rstrip("\r\n"))
        if not match:
            skipped += 1
            continue
        ip, stamp, _, request_path, _, referrer, ua, accept = match.groups()
        try:
            when = datetime.strptime(stamp.split()[0], "%d/%b/%Y:%H:%M:%S")
            network = anonymise_ip(ip, key)
        except (ValueError, IndexError):
            skipped += 1
            continue
        ua_key = hmac.new(key, ua.encode(), hashlib.sha256).hexdigest()[:12]
        hits.append(_Hit(network + ":" + ua_key, when, urlparse(request_path).path,
                         referrer, ua, accept or ""))
    hits.sort(key=lambda hit: (hit.key, hit.when))
    sessions, current = [], []
    for hit in hits:
        if current and (hit.key != current[-1].key or hit.when - current[-1].when > timedelta(minutes=30)):
            sessions.append(current)
            current = []
        current.append(hit)
    if current:
        sessions.append(current)
    return ParsedSessions(sessions, skipped, lines_read, stopped)


def parse_access_log(path, key):
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        return parse_access_lines(handle, key, None)


def _ref_agent(referrer, refs):
    parsed = urlparse(referrer)
    source = parse_qs(parsed.query).get("utm_source", [""])[0].lower()
    host = (parsed.hostname or "").lower()
    for token in refs:
        if token.lower() in (host, source) or host.endswith("." + token.lower()):
            return token
    return None


def classify_sessions(sessions):
    sig = _signatures()
    buckets = defaultdict(lambda: [0, 0, 0])
    for session in sessions:
        matched = [item for item in sig["user_agents"]
                   if any(item["token"].lower() in hit.ua.lower() for hit in session)]
        if any(item.get("traffic_attribution", True) is False for item in matched):
            continue
        declared = next((item["token"] for item in matched), None)
        referred = next((_ref_agent(hit.referrer, sig["referrers"]) for hit in session
                         if _ref_agent(hit.referrer, sig["referrers"])), None)
        tier = agent = None
        if declared:
            tier, agent = "declared", declared
        elif referred:
            tier, agent = "referred", referred + " referral"
        else:
            dynamic = [hit for hit in session if not re.search(r"\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?)$", hit.path, re.I)]
            shopping = [hit for hit in session if hit.path.startswith(("/products/", "/collections/"))]
            burst = len(shopping) >= 3 and shopping[-1].when - shopping[0].when <= timedelta(seconds=60)
            score = (2 * int(burst) + int(len(dynamic) == len(session))
                     + int(not any(hit.path.startswith(("/account", "/cookie")) for hit in session))
                     + int(any(re.search(r"headless|python|curl|bot", hit.ua, re.I) for hit in session))
                     + int(any(hit.accept and "text/html" not in hit.accept for hit in session)))
            if burst and score >= 4:
                tier, agent = "suspected", "unattributed-agent-like"
        if tier:
            values = buckets[(tier, agent)]
            values[0] += 1
            values[1] += sum(hit.path.startswith(("/products/", "/collections/")) for hit in session)
            values[2] += sum(hit.path.startswith(("/cart", "/checkouts")) for hit in session)
    return [TrafficBucket(tier, agent, *values) for (tier, agent), values in sorted(buckets.items())]


def _normalise_header(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def parse_ga4_rows(rows):
    sig = _signatures()
    buckets = defaultdict(lambda: [0, 0, 0])
    lines_read = matched = skipped = 0
    aliases = {
        "session_source": ("session_source", "session_source_platform", "source"),
        "session_medium": ("session_medium", "medium"),
        "page_path": ("page_path", "page_path_and_screen_class", "page_location"),
        "sessions": ("sessions",),
    }
    for raw in rows:
        lines_read += 1
        row = {_normalise_header(key): value for key, value in raw.items() if key is not None}
        def field(name):
            return next((row[key] for key in aliases[name] if key in row), "")
        source = str(field("session_source") or "").lower()
        agent = next((item for item in sig["referrers"] if item.lower() in source), None)
        try:
            count = max(0, int(float(field("sessions") or 0)))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not agent or count <= 0:
            skipped += 1
            continue
        matched += 1
        page = str(field("page_path") or "")
        if page.startswith(("http://", "https://")):
            page = urlparse(page).path
        values = buckets[("referred", agent + " referral")]
        values[0] += count
        values[1] += count if page.startswith(("/products/", "/collections/")) else 0
        values[2] += count if page.startswith(("/cart", "/checkouts")) else 0
    values = [TrafficBucket(tier, agent, *counts)
              for (tier, agent), counts in sorted(buckets.items())]
    return ParsedGA4(values, lines_read, matched, skipped)


def _ga4_reader(handle):
    return csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))


def parse_ga4_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return parse_ga4_rows(_ga4_reader(handle))


def merge_buckets(*groups):
    merged = defaultdict(lambda: [0, 0, 0])
    for group in groups:
        for bucket in group:
            values = merged[(bucket.tier, bucket.agent)]
            values[0] += bucket.sessions
            values[1] += bucket.product_pages
            values[2] += bucket.checkout_hits
    return [TrafficBucket(tier, agent, *values) for (tier, agent), values in sorted(merged.items())]
