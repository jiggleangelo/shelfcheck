"""Shelfcheck usable self-serve web application. Run with: python3 app.py"""
import csv
import gzip
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import zlib
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from shelfcheck.models import AgentRun, Brand, BrandConfig, TrafficBucket
from shelfcheck.readiness import check_catalog_rows, check_robots, check_settings_answers
from shelfcheck.report import render_snapshot
from shelfcheck.sampler import build_prompts
from shelfcheck.stats import summarize
from shelfcheck.traffic import classify_sessions, merge_buckets, parse_access_lines, parse_ga4_rows

MAX_BODY = 1024 * 1024
MAX_REQUEST_BODY = MAX_BODY + 64 * 1024
MAX_WORKSPACE = 5 * 1024 * 1024
MAX_CONNECTIONS = 32
MAX_HEAVY = 2
NOTICE = ("Operated by AI agents. Early version: keep your own copy of anything important. "
          "Do not enter customer names, emails, addresses or order data (upload only product exports and traffic logs). "
          "Nothing is charged. Empty workspaces expire after 24 hours; other workspaces expire after 90 days without viewing or editing.")
SETTINGS = ("agentic_checkout_enrolled", "default_enrolment_known", "policies_published", "meta_catalog_connected")
_PROMPT_HEADER = re.compile(r"^(p(?:0[1-9]|[1-4][0-9]|50))(?:\s+(manual|muse|chatgpt|meta ai))?:$", re.I)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _json_bytes(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode()


def _items(values):
    return [asdict(x) for x in values]


def _profile_config(profile, competitors=()):
    name = profile.get("name") or "My store"
    domain = profile.get("domain") or "store.invalid"
    primary = Brand(name, (name, domain), domain)
    brands = []
    for item in competitors[:3]:
        competitor = item.get("name", "").strip()
        if competitor:
            aliases = tuple(dict.fromkeys([competitor] + [x.strip() for x in item.get("aliases", "").split(",") if x.strip()]))
            brands.append(Brand(competitor, aliases, item.get("domain") or "invalid"))
    return BrandConfig("app", primary, tuple(brands), profile.get("category") or "products",
                       profile.get("price_band") or "unspecified", ("manual", "muse", "chatgpt"), 1, 200)


def _snapshot_sort(value):
    return value.get("date", ""), value.get("id", "")


class StoreError(ValueError):
    pass


class Store:
    def __init__(self, root, cap_mb=200):
        self.root = Path(root)
        self.cap = int(cap_mb) * 1024 * 1024
        self.ws, self.share, self.tomb = self.root / "ws", self.root / "share", self.root / "tomb"
        for path in (self.ws, self.share, self.tomb):
            path.mkdir(parents=True, exist_ok=True)
        self.guard = threading.RLock()
        self.locks = defaultdict(threading.RLock)
        self.creations = deque()
        self.client_creations = defaultdict(deque)
        self._bytes = sum(x.stat().st_size for x in self.root.rglob("*") if x.is_file())
        self._workspace_bytes = {path.name: sum(x.stat().st_size for x in path.rglob("*") if x.is_file())
                                 for path in self.ws.iterdir() if path.is_dir()}

    def _account(self, path, delta):
        self._bytes += delta
        try:
            relative = path.relative_to(self.ws)
            ident = relative.parts[0]
            self._workspace_bytes[ident] = self._workspace_bytes.get(ident, 0) + delta
        except ValueError:
            pass

    def _atomic(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.stat().st_size if path.exists() else 0
        temp = path.with_name("." + path.name + "." + secrets.token_hex(6))
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        self._account(path, len(data) - old)

    def _delete_file(self, path):
        try:
            size = path.stat().st_size
            path.unlink()
            self._account(path, -size)
        except FileNotFoundError:
            pass

    def create(self, sample=False, client_key="local"):
        with self.guard:
            now = time.time()
            for queue, limit in ((self.creations, 600), (self.client_creations[client_key], 10)):
                while queue and now - queue[0] >= 3600:
                    queue.popleft()
                if len(queue) >= limit:
                    raise StoreError("Too many new workspaces from this client; please try again later." if limit == 10 else "Too many new workspaces; please try again later.")
            if len(self._workspace_bytes) >= 3000:
                raise StoreError("The workspace limit has been reached.")
            if self._bytes >= self.cap:
                raise StoreError("The app data limit has been reached; no new writes are accepted.")
            token, ident = secrets.token_urlsafe(24), None
            token = token
            ident = _hash(token)
            path = self.ws / ident
            path.mkdir()
            (path / "snapshots").mkdir()
            self._workspace_bytes[ident] = 0
            meta = {"created": _now(), "last_used": _now(), "sample": sample, "share": None}
            self._atomic(path / "meta.json", _json_bytes(meta))
            self._atomic(path / "draft.json", _json_bytes({"profile": {}, "robots_text": "", "readiness": {}, "traffic_parts": [], "prompt": {}, "written": False}))
            self.creations.append(now)
            self.client_creations[client_key].append(now)
            return token

    def path(self, token):
        ident = _hash(token)
        path = self.ws / ident
        if not path.is_dir() or (self.tomb / ident).exists():
            raise FileNotFoundError
        return ident, path

    def _touch(self, ident, path, meta):
        try:
            last = datetime.fromisoformat(meta["last_used"]).timestamp()
        except (KeyError, ValueError):
            last = 0
        if time.time() - last >= 3600:
            meta["last_used"] = _now()
            self.write(ident, "meta.json", meta, True)

    def touch_ident(self, ident):
        path = self.ws / ident
        with self.locks[ident]:
            meta = json.loads((path / "meta.json").read_text())
            self._touch(ident, path, meta)

    def read(self, token, touch=True):
        ident, path = self.path(token)
        with self.locks[ident]:
            meta = json.loads((path / "meta.json").read_text())
            draft = json.loads((path / "draft.json").read_text())
            snapshots = sorted((json.loads(x.read_text()) for x in (path / "snapshots").glob("*.json")), key=_snapshot_sort)
            if touch:
                self._touch(ident, path, meta)
            return ident, meta, draft, snapshots

    def write(self, token_or_ident, name, value, hashed=False):
        ident = token_or_ident if hashed else _hash(token_or_ident)
        path = self.ws / ident
        with self.locks[ident], self.guard:
            if not path.is_dir() or (self.tomb / ident).exists():
                raise FileNotFoundError
            data, target = _json_bytes(value), path / name
            old = target.stat().st_size if target.exists() else 0
            if self._workspace_bytes.get(ident, 0) - old + len(data) > MAX_WORKSPACE:
                raise StoreError("This workspace would exceed its 5 MB storage limit.")
            if self._bytes - old + len(data) > self.cap:
                raise StoreError("The app data limit has been reached; no new writes are accepted.")
            self._atomic(target, data)

    def save_snapshot(self, ident, snapshot):
        path = self.ws / ident
        with self.locks[ident], self.guard:
            existing = []
            for file in (path / "snapshots").glob("*.json"):
                try:
                    value = json.loads(file.read_text())
                    existing.append((_snapshot_sort(value), file, file.stat().st_size))
                except (OSError, ValueError):
                    continue
            existing.sort()
            target = path / "snapshots" / (snapshot["id"] + ".json")
            old = target.stat().st_size if target.exists() else 0
            remove = []
            projected_count = len(existing) + (0 if target.exists() else 1)
            data = _json_bytes(snapshot)
            projected_workspace = self._workspace_bytes.get(ident, 0) - old + len(data)
            projected_total = self._bytes - old + len(data)
            candidates = [item for item in existing if item[1] != target]
            while candidates and (projected_count > 26 or projected_workspace > MAX_WORKSPACE or projected_total > self.cap):
                _, file, size = candidates.pop(0)
                remove.append(file)
                projected_count -= 1
                projected_workspace -= size
                projected_total -= size
            if projected_workspace > MAX_WORKSPACE:
                raise StoreError("This snapshot cannot fit within the 5 MB workspace limit.")
            if projected_total > self.cap:
                raise StoreError("The app data limit has been reached; no new writes are accepted.")
            if remove:
                snapshot["pruned_oldest"] = True
                data = _json_bytes(snapshot)
            for file in remove:
                self._delete_file(file)
            self._atomic(target, data)
            return bool(remove)

    def save_draft(self, token, draft):
        ident, path = self.path(token)
        draft["written"] = True
        self.write(ident, "draft.json", draft, True)
        meta = json.loads((path / "meta.json").read_text())
        meta["last_used"] = _now()
        self.write(ident, "meta.json", meta, True)

    def remove_share_pointers(self, ident):
        for pointer in self.share.glob("*"):
            try:
                if pointer.read_text() == ident:
                    self._delete_file(pointer)
            except OSError:
                pass

    def delete(self, token):
        ident = _hash(token)
        with self.locks[ident], self.guard:
            self._atomic(self.tomb / ident, str(time.time()).encode())
            path = self.ws / ident
            size = self._workspace_bytes.pop(ident, 0)
            shutil.rmtree(path, ignore_errors=True)
            self._bytes -= size
            self.remove_share_pointers(ident)

    def sweep(self):
        now = time.time()
        for path in list(self.ws.iterdir()):
            try:
                meta = json.loads((path / "meta.json").read_text())
                draft = json.loads((path / "draft.json").read_text())
                last = datetime.fromisoformat(meta["last_used"]).timestamp()
                created = datetime.fromisoformat(meta["created"]).timestamp()
                if now - last > 90 * 86400 or (not draft.get("written") and now - created > 86400):
                    ident = path.name
                    with self.guard:
                        self._atomic(self.tomb / ident, str(now).encode())
                        size = self._workspace_bytes.pop(ident, 0)
                        shutil.rmtree(path, ignore_errors=True)
                        self._bytes -= size
                        self.remove_share_pointers(ident)
            except (OSError, ValueError, KeyError):
                continue
        for path in self.tomb.glob("*"):
            try:
                if now - path.stat().st_mtime > 86400:
                    self._delete_file(path)
            except OSError:
                pass


CSS = "body{margin:0;background:#eef2f5;color:#17202a;font:16px/1.5 system-ui,sans-serif}main{max-width:1050px;margin:auto;background:#fff;min-height:100vh;padding:28px}h1,h2{color:#17324d}.notice,.sample{padding:12px;border:2px solid #8a6000;background:#fff0b8;margin-bottom:18px}.card{border:1px solid #ccd6df;border-radius:8px;padding:18px;margin:16px 0}.chip{float:right;border-radius:20px;background:#e5edf4;padding:3px 10px}label{display:block;font-weight:650;margin-top:10px}input,textarea,select{max-width:720px;width:100%;padding:8px}textarea{min-height:120px}button,.button{display:inline-block;padding:10px 15px;margin:8px 4px 0 0;border:0;border-radius:5px;background:#2878bd;color:white;text-decoration:none;font-weight:700}.danger{background:#a21b16}table{width:100%;border-collapse:collapse}td,th{border:1px solid #ccd6df;padding:7px;text-align:left}.fail{color:#a21b16}.warn{color:#8a5700}.pass{color:#16713a}.muted{color:#526371}@media(max-width:700px){main{padding:14px}}"


def _page(title, content, sample=False):
    banner = '<div class="sample">SAMPLE DATA</div>' if sample else ""
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} — Shelfcheck</title><style>{CSS}</style></head><body><main>{banner}<div class="notice">{html.escape(NOTICE)} <a href="https://manykiln.com/privacy">Privacy</a></div>{content}</main></body></html>'


def _status(value):
    return "saved" if value else "empty"


def _workspace(token, meta, draft, snapshots, share_token=None):
    e = lambda x: html.escape(str(x), quote=True)
    p = draft.get("profile", {})
    prompts = build_prompts(_profile_config(p))
    setting_html = "".join(f'<fieldset><legend>{e(name.replace("_"," ").title())}</legend>' + " ".join(f'<label style="display:inline"><input style="width:auto" type="radio" name="{name}" value="{v}" {"checked" if draft.get("settings",{}).get(name)==v else ""}>{v}</label>' for v in ("yes","no","unknown")) + "</fieldset>" for name in SETTINGS)
    snap_html = "".join(f'<li><a href="/w/{quote(token)}/r/{e(x["id"])}">{e(x["date"])}</a></li>' for x in snapshots) or "<li>No snapshots yet.</li>"
    parts = draft.get("traffic_parts", [])
    partial = any(x.get("stopped_early") for x in parts)
    overlap = {x.get("kind") for x in parts} == {"log", "ga4"}
    part_html = "".join(f'<li>{e(x.get("kind"))}: {x.get("lines_read",0)} rows/lines read, {x.get("matched",0)} matched, {x.get("skipped",0)} skipped{"; partial" if x.get("stopped_early") else ""}</li>' for x in parts) or "<li>No traffic parts uploaded.</li>"
    share = f'<div class="notice"><strong>Copy this share link now.</strong> It is shown only after creation: <a href="/s/{e(share_token)}">/s/{e(share_token)}</a></div>' if share_token else ""
    competitors = draft.get("prompt", {}).get("competitors", []) + [{}, {}, {}]
    comp_html = "".join(f'<label>Competitor {i+1} name<input name="competitor{i+1}_name" value="{e(competitors[i].get("name",""))}"></label><label>Aliases, comma separated<input name="competitor{i+1}_aliases" value="{e(competitors[i].get("aliases",""))}"></label>' for i in range(3))
    return _page("Workspace", f'''<h1>Shelfcheck workspace</h1><p><strong>Bookmark warning:</strong> this link is the only key; it cannot be recovered.</p><p>This tool does not query ChatGPT, Meta AI or any AI model.</p>
<form class="card" method="post" action="/w/{quote(token)}/profile"><span class="chip">{_status(p)}</span><h2>Store profile</h2><label>Store name<input name="name" maxlength="80" required value="{e(p.get('name',''))}"></label><label>Domain<input name="domain" maxlength="200" required value="{e(p.get('domain',''))}"></label><label>Category<input name="category" maxlength="100" required value="{e(p.get('category',''))}"></label><label>Price band<input name="price_band" maxlength="80" required value="{e(p.get('price_band',''))}"></label><button>Save profile</button></form>
<form class="card" method="post" enctype="multipart/form-data" action="/w/{quote(token)}/robots"><span class="chip">{_status(draft.get('robots_text'))}</span><h2>Robots</h2><label>Paste robots.txt<textarea name="robots">{e(draft.get('robots_text',''))}</textarea></label><label>Or upload .txt<input type="file" name="file" accept=".txt,text/plain"></label><button>Save robots</button></form>
<form class="card" method="post" enctype="multipart/form-data" action="/w/{quote(token)}/catalog"><span class="chip">{_status(draft.get('readiness',{}).get('catalog'))}</span><h2>Catalog CSV</h2><input type="file" name="file" accept=".csv,text/csv" required><button>Analyze catalog</button></form>
<form class="card" method="post" action="/w/{quote(token)}/settings"><span class="chip">{_status(draft.get('settings'))}</span><h2>Agentic settings</h2>{setting_html}<button>Save settings</button></form>
<form class="card" method="post" enctype="multipart/form-data" action="/w/{quote(token)}/traffic"><span class="chip">{_status(parts)}</span><h2>Traffic</h2><label>Format<select name="kind"><option value="log">Combined access log (.log or .gz)</option><option value="ga4">GA4 exploration CSV</option></select></label><p>GA4 CSV columns: Session source, Session medium, Page path, and Sessions. Underscore-style equivalents are also accepted; leading # metadata lines are ignored.</p><input type="file" name="file" required><button>Analyze upload</button><ul>{part_html}</ul>{'<p class="warn">Partially processed. Continue by uploading the remaining part.</p>' if partial else ''}{'<p class="warn">Log and GA4 exports may overlap; do not sum overlapping exports without deduplication.</p>' if overlap else ''}</form><form method="post" action="/w/{quote(token)}/traffic/clear"><button>Clear traffic parts</button></form>
<form class="card" method="post" action="/w/{quote(token)}/prompts"><span class="chip">{_status(draft.get('prompt',{}).get('answers'))}</span><h2>Prompt log (optional)</h2><p>Ask prompts yourself. Each answer block must start with a prompt number and optional surface, for example <code>p01 chatgpt:</code> or <code>p02 manual:</code>, followed by one recommended brand per line. Separate blocks with a blank line. This makes the prompt-clustered interval reflect the prompt actually asked.</p><details><summary>50 generated prompts</summary><ol>{''.join(f'<li><strong>{e(x.prompt_id)}</strong> {e(x.text)}</li>' for x in prompts)}</ol></details>{comp_html}<label>Answers<textarea name="answers">{e(draft.get('prompt',{}).get('answers',''))}</textarea></label><button>Save prompt log</button></form>
<form method="post" action="/w/{quote(token)}/snapshot"><button>Save snapshot</button></form><h2>Snapshots</h2><ul>{snap_html}</ul>{share}<form method="post" action="/w/{quote(token)}/share"><button>Create or replace share link</button></form><form method="post" action="/w/{quote(token)}/share/revoke"><button>Revoke every share link</button></form><p><a class="button" href="/w/{quote(token)}/export">Export everything</a></p><form method="post" action="/w/{quote(token)}/delete"><button class="danger">Delete this workspace now</button></form>''', meta.get("sample", False))


def _multipart(content_type, body):
    if "boundary=" not in content_type.lower():
        raise ValueError("multipart boundary missing")
    message = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("iso-8859-1") + b"\r\n\r\n" + body)
    if not message.is_multipart():
        raise ValueError("invalid multipart request")
    result = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            result[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return result


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, store, public_base):
        super().__init__(address, handler)
        self.store = store
        self.public_base = public_base.rstrip("/")
        parsed = urlparse(self.public_base)
        self.origin = parsed.scheme + "://" + parsed.netloc
        self.expected_host = parsed.netloc
        self.connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.heavy = threading.BoundedSemaphore(MAX_HEAVY)
        self.posts = defaultdict(deque)
        self.client_key = secrets.token_bytes(32)

    def process_request(self, request, client_address):
        if not self.connections.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "Shelfcheck"
    sys_version = ""
    timeout = 10

    def log_message(self, fmt, *args):
        pass

    def _headers(self, status, kind, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status, value, kind="text/html; charset=utf-8", extra=None):
        body = value.encode() if isinstance(value, str) else value
        self._headers(status, kind, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _problem(self, status, message):
        self._send(status, _page("Problem", f"<h1>Problem</h1><p>{html.escape(message)}</p>"))

    def _redirect(self, location):
        self._send(303, b"", extra={"Location": location})

    def _valid_host(self):
        if self.headers.get("Host") != self.server.expected_host:
            self._problem(400, "Invalid Host header.")
            return False
        return True

    def _valid_post(self):
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        referer_origin = None
        if referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                referer_origin = parsed.scheme + "://" + parsed.netloc
        if origin == self.server.origin or (origin is None and referer_origin == self.server.origin):
            return True
        self._problem(403, "This form must be submitted from the Shelfcheck origin.")
        return False

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise StoreError("Chunked request bodies are not accepted.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise StoreError("A valid Content-Length is required.") from exc
        multipart = self.headers.get("Content-Type", "").lower().startswith("multipart/form-data")
        limit = MAX_REQUEST_BODY if multipart else MAX_BODY
        if length < 0 or length > limit:
            raise OverflowError
        deadline, chunks, remaining = time.monotonic() + 10, [], length
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0:
                raise StoreError("The request body took too long to upload.")
            self.connection.settimeout(left)
            try:
                chunk = self.rfile.read1(min(65536, remaining)) if hasattr(self.rfile, "read1") else self.rfile.read(min(65536, remaining))
            except (socket.timeout, TimeoutError) as exc:
                raise StoreError("The request body took too long to upload.") from exc
            if not chunk:
                raise StoreError("The request body was incomplete.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _form(self, body):
        return {key: values[-1] for key, values in parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=100).items()}

    def _file(self, multipart, name="file"):
        data = multipart.get(name, (None, b""))[1] if multipart else b""
        if len(data) > MAX_BODY:
            raise StoreError("Each uploaded file is limited to 1 MB.")
        return data

    def _client_key(self):
        raw = str(self.client_address[0]).encode()
        return hmac.new(self.server.client_key, raw, hashlib.sha256).hexdigest()[:16]

    def do_GET(self):
        if not self._valid_host():
            return
        target, path = urlparse(self.path), urlparse(self.path).path
        if path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return
        if path == "/":
            self._send(200, _page("Home", '<h1>Is your Shopify store ready for AI shopping agents?</h1><p>Check readiness, measure agent-like traffic and compare saved snapshots.</p><p>This tool does not query ChatGPT, Meta AI or any AI model.</p><form method="post" action="/start"><button>Start a workspace</button></form><form method="post" action="/sample"><button>Try with sample data</button></form>'))
            return
        parts = [unquote(x) for x in path.strip("/").split("/")]
        try:
            if len(parts) >= 2 and parts[0] == "w":
                token = parts[1]
                ident, meta, draft, snapshots = self.server.store.read(token)
                if len(parts) == 2:
                    share = parse_qs(target.query).get("share", [None])[0]
                    if share and _hash(share) != meta.get("share"):
                        share = None
                    self._send(200, _workspace(token, meta, draft, snapshots, share))
                    return
                if len(parts) == 3 and parts[2] == "export":
                    self._send(200, _json_bytes({"profile": draft.get("profile", {}), "draft": draft, "snapshots": snapshots}), "application/json", {"Content-Disposition": 'attachment; filename="shelfcheck-export.json"'})
                    return
                if len(parts) == 4 and parts[2] == "r":
                    suffix = Path(parts[3]).suffix
                    report_id = parts[3][:-len(suffix)] if suffix in (".html", ".txt") else parts[3]
                    index = next(i for i, x in enumerate(snapshots) if x["id"] == report_id)
                    page, text = render_snapshot(snapshots[index]["profile"], snapshots[index], snapshots[index-1] if index else None)
                    if suffix == ".txt":
                        self._send(200, text, "text/plain; charset=utf-8", {"Content-Disposition": 'attachment; filename="shelfcheck-report.txt"'})
                    elif suffix == ".html":
                        self._send(200, page, extra={"Content-Disposition": 'attachment; filename="shelfcheck-report.html"'})
                    else:
                        links = f'<p><a href="{html.escape(parts[3])}.html">Download HTML</a> · <a href="{html.escape(parts[3])}.txt">Download text</a></p>'
                        self._send(200, page.replace("</main>", links + "</main>"))
                    return
            if len(parts) >= 2 and parts[0] == "s":
                pointer = self.server.store.share / _hash(parts[1])
                ident = pointer.read_text()
                ws = self.server.store.ws / ident
                if not ws.is_dir() or (self.server.store.tomb / ident).exists():
                    raise FileNotFoundError
                self.server.store.touch_ident(ident)
                snapshots = sorted((json.loads(x.read_text()) for x in (ws / "snapshots").glob("*.json")), key=_snapshot_sort)
                if len(parts) == 2:
                    links = "".join(f'<li><a href="/s/{quote(parts[1])}/r/{x["id"]}">{html.escape(x["date"])}</a></li>' for x in snapshots)
                    self._send(200, _page("Shared reports", f"<h1>Shared Shelfcheck reports</h1><ul>{links}</ul>"))
                    return
                if len(parts) == 4 and parts[2] == "r":
                    index = next(i for i, x in enumerate(snapshots) if x["id"] == parts[3])
                    page, _ = render_snapshot(snapshots[index]["profile"], snapshots[index], snapshots[index-1] if index else None)
                    self._send(200, page)
                    return
        except (FileNotFoundError, StopIteration, ValueError, KeyError, json.JSONDecodeError):
            pass
        self._problem(404, "Page not found.")

    def do_POST(self):
        if not self._valid_host() or not self._valid_post():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except OverflowError:
            self.close_connection = True
            self._problem(413, "Files are limited to 1 MB; multipart framing may add a small amount of request overhead.")
            return
        except StoreError as exc:
            self._problem(400, str(exc))
            return
        try:
            if path in ("/start", "/sample"):
                token = self.server.store.create(path == "/sample", self._client_key())
                if path == "/sample":
                    self._make_sample(token)
                self._redirect("/w/" + quote(token))
                return
            parts = [unquote(x) for x in path.strip("/").split("/")]
            if len(parts) < 3 or parts[0] != "w":
                self._problem(404, "Page not found.")
                return
            token, action = parts[1], "/".join(parts[2:])
            ident, meta, draft, snapshots = self.server.store.read(token)
            now, budget = time.time(), self.server.posts[ident]
            while budget and now - budget[0] >= 60:
                budget.popleft()
            if len(budget) >= 30:
                self._problem(429, "Please slow down and try again in a minute.")
                return
            budget.append(now)
            ctype = self.headers.get("Content-Type", "")
            multipart = _multipart(ctype, body) if ctype.lower().startswith("multipart/form-data") else None
            form = self._form(body) if multipart is None else {k: v[1].decode("utf-8", "replace") for k, v in multipart.items() if not v[0]}
            if action == "delete":
                self.server.store.delete(token)
                self._redirect("/")
                return
            if action == "profile":
                draft["profile"] = {k: form.get(k, "").strip()[:limit] for k, limit in (("name",80),("domain",200),("category",100),("price_band",80))}
            elif action == "robots":
                uploaded = self._file(multipart)
                text = (uploaded.decode("utf-8", "replace") if uploaded else form.get("robots", ""))[:65536]
                draft["robots_text"] = text
                draft.setdefault("readiness", {})["robots"] = _items(check_robots(text))
            elif action == "catalog":
                data = self._file(multipart)
                if not self.server.heavy.acquire(False):
                    self._problem(503, "The analyzer is busy; please try again shortly.")
                    return
                try:
                    rows = []
                    for i, row in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace"), newline=""))):
                        if i >= 20000:
                            break
                        rows.append(row)
                    draft.setdefault("readiness", {})["catalog"] = _items(check_catalog_rows(rows))
                finally:
                    self.server.heavy.release()
            elif action == "settings":
                answers = {key: form.get(key, "unknown") for key in SETTINGS}
                draft["settings"] = answers
                draft.setdefault("readiness", {})["settings"] = _items(check_settings_answers(answers))
            elif action == "traffic/clear":
                draft["traffic_parts"] = []
            elif action == "traffic":
                data, kind = self._file(multipart), form.get("kind", "log")
                if not self.server.heavy.acquire(False):
                    self._problem(503, "The analyzer is busy; please try again shortly.")
                    return
                try:
                    if kind == "ga4":
                        source = io.StringIO(data.decode("utf-8-sig", "replace"), newline="")
                        rows = csv.DictReader(line for line in source if not line.lstrip().startswith("#"))
                        buckets = parse_ga4_rows(rows)
                        part = {"kind":"ga4", "lines_read":buckets.lines_read, "matched":buckets.matched_rows,
                                "skipped":buckets.skipped_lines, "stopped_early":False, "buckets":_items(buckets)}
                    else:
                        if data.startswith(b"\x1f\x8b"):
                            stream, output = gzip.GzipFile(fileobj=io.BytesIO(data)), io.BytesIO()
                            while output.tell() <= 20 * 1024 * 1024:
                                chunk = stream.read(65536)
                                if not chunk:
                                    break
                                output.write(chunk)
                            if output.tell() > 20 * 1024 * 1024:
                                raise StoreError("The decompressed log exceeds 20 MB.")
                            data = output.getvalue()
                        parsed = parse_access_lines(io.BytesIO(data), secrets.token_bytes(32), time.monotonic() + getattr(self.server, "parse_budget", 8))
                        buckets = classify_sessions(parsed)
                        part = {"kind":"log", "lines_read":parsed.lines_read,
                                "matched":max(0, parsed.lines_read-parsed.skipped_lines), "skipped":parsed.skipped_lines,
                                "stopped_early":parsed.stopped_early, "buckets":_items(buckets)}
                    draft.setdefault("traffic_parts", []).append(part)
                finally:
                    self.server.heavy.release()
            elif action == "prompts":
                competitors = [{"name":form.get(f"competitor{i}_name","")[:80], "aliases":form.get(f"competitor{i}_aliases","")[:300]} for i in range(1,4)]
                draft["prompt"] = {"answers":form.get("answers","")[:100000], "competitors":competitors}
            elif action == "snapshot":
                snapshot = self._snapshot(draft, meta)
                self.server.store.save_snapshot(ident, snapshot)
                self._redirect(f"/w/{quote(token)}/r/{snapshot['id']}")
                return
            elif action == "share":
                self.server.store.remove_share_pointers(ident)
                share = secrets.token_urlsafe(24)
                share_hash = _hash(share)
                self.server.store._atomic(self.server.store.share / share_hash, ident.encode())
                meta["share"] = share_hash
                meta["last_used"] = _now()
                self.server.store.write(ident, "meta.json", meta, True)
                self._redirect(f"/w/{quote(token)}?share={quote(share)}")
                return
            elif action == "share/revoke":
                self.server.store.remove_share_pointers(ident)
                meta["share"] = None
                meta["last_used"] = _now()
                self.server.store.write(ident, "meta.json", meta, True)
                self._redirect(f"/w/{quote(token)}")
                return
            else:
                self._problem(404, "Page not found.")
                return
            self.server.store.save_draft(token, draft)
            self._redirect(f"/w/{quote(token)}")
        except FileNotFoundError:
            self._problem(404, "Workspace not found.")
        except (StoreError, ValueError, KeyError, csv.Error, gzip.BadGzipFile, EOFError, zlib.error) as exc:
            self._problem(400 if not isinstance(exc, StoreError) or "limit" not in str(exc) else 507, str(exc) or "The uploaded file is invalid or truncated.")
        except Exception:
            self._problem(500, "Shelfcheck could not process that request. No partial upload was saved; please try again.")

    def _snapshot(self, draft, meta):
        ready = [item for values in draft.get("readiness", {}).values() for item in values]
        groups = [[TrafficBucket(**x) for x in part.get("buckets", [])] for part in draft.get("traffic_parts", [])]
        traffic = _items(merge_buckets(*groups)) if groups else []
        shares, prompt = [], draft.get("prompt", {})
        blocks = [x for x in prompt.get("answers", "").replace("\r\n", "\n").split("\n\n") if x.strip()][:200]
        runs = []
        for i, block in enumerate(blocks):
            lines = [x.strip() for x in block.splitlines()[:12] if x.strip()]
            match = _PROMPT_HEADER.fullmatch(lines[0]) if lines else None
            if not match:
                continue
            surface = (match.group(2) or "manual").lower().replace("meta ai", "muse")
            runs.append(AgentRun(surface, match.group(1).lower(), i, tuple(lines[1:])))
        if runs:
            config = _profile_config(draft.get("profile", {}), prompt.get("competitors", []))
            shares = _items(summarize(runs, (config.primary,) + config.competitors))
        return {"id":datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + secrets.token_hex(3),
                "date":_now(), "profile":draft.get("profile", {}), "readiness":ready, "traffic":traffic,
                "traffic_parts":draft.get("traffic_parts", []), "prompt":prompt, "shares":shares,
                "sample":meta.get("sample", False)}

    def _make_sample(self, token):
        _, meta, draft, _ = self.server.store.read(token)
        draft["profile"] = {"name":"Northfield Socks","domain":"northfield.example","category":"outdoor apparel","price_band":"mid-range"}
        draft["robots_text"] = "User-agent: ChatGPT-User\nDisallow: /products/\n"
        draft["readiness"] = {"robots":_items(check_robots(draft["robots_text"])), "settings":_items(check_settings_answers({x:"unknown" for x in SETTINGS}))}
        draft["settings"] = {x:"unknown" for x in SETTINGS}
        draft["traffic_parts"] = [{"kind":"log","lines_read":12,"matched":12,"skipped":0,"stopped_early":False,"buckets":[asdict(TrafficBucket("suspected","unattributed-agent-like",1,3,0))]}]
        self.server.store.save_draft(token, draft)
        first = self._snapshot(draft, meta)
        first.update({"date":"2026-10-19T00:00:00+00:00", "id":"sample-1"})
        self.server.store.save_snapshot(_hash(token), first)
        draft["robots_text"] = "User-agent: *\nAllow: /\n"
        draft["readiness"]["robots"] = _items(check_robots(draft["robots_text"]))
        self.server.store.save_draft(token, draft)
        second = self._snapshot(draft, meta)
        second["id"] = "sample-2"
        self.server.store.save_snapshot(_hash(token), second)


def create_server(host=None, port=None, data_dir=None, public_base_url=None):
    host = host if host is not None else os.environ.get("APP_HOST", "127.0.0.1")
    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    root = data_dir if data_dir is not None else os.environ.get("DATA_DIR", "data")
    public = public_base_url if public_base_url is not None else os.environ.get("PUBLIC_BASE_URL", "https://app-shelfcheck.manykiln.com")
    store = Store(root, os.environ.get("APP_DATA_CAP_MB", "200"))
    store.sweep()
    server = AppServer((host, port), Handler, store, public)
    server.parse_budget = 8
    return server


def _sweeper(server):
    while not server._BaseServer__shutdown_request:
        time.sleep(86400)
        server.store.sweep()


def main():
    server = create_server()
    threading.Thread(target=_sweeper, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
