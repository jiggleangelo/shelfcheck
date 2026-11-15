"""Hosted, in-memory Shelfcheck user-test demo.

Run with: python3 demo.py
"""

from collections import Counter, OrderedDict
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import html
import os
import secrets
import threading

from shelfcheck.agents import FakeAgentClient
from shelfcheck.config import load_config
from shelfcheck.readiness import check_catalog, check_robots, check_settings
from shelfcheck.report import render
from shelfcheck.sampler import compile_aliases, extract_mentions, run_sample
from shelfcheck.stats import summarize
from shelfcheck.traffic import classify_sessions, merge_buckets, parse_access_log, parse_ga4_csv

ROOT = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 32
MAX_CONCURRENT_REPORTS = 2
BANNER = "Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is charged."


class DemoInputError(ValueError):
    pass


def _escape(value):
    return html.escape(str(value), quote=True)


def _page(title, content):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)} — Shelfcheck demo</title>
<style>
:root{{--ink:#17202a;--navy:#17324d;--blue:#2878bd;--paper:#fff;--ground:#eef2f5;--line:#d5dee6;--warn:#fff0b8}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--ground);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:1050px;margin:auto;background:var(--paper);min-height:100vh;padding:28px}}h1,h2{{color:var(--navy)}}
.banner{{background:var(--warn);border:2px solid #8a6000;padding:12px 16px;font-weight:750;margin-bottom:24px}}
.card{{border:1px solid var(--line);border-radius:8px;padding:18px;margin:18px 0}}label{{display:block;font-weight:650;margin-top:12px}}
input{{display:block;width:100%;max-width:620px;padding:10px;margin-top:4px;border:1px solid #9cabb8;border-radius:5px;font:inherit}}
button,.button{{display:inline-block;margin-top:18px;padding:11px 17px;border:0;border-radius:5px;background:var(--blue);color:white;font-weight:700;text-decoration:none;cursor:pointer}}
table{{width:100%;border-collapse:collapse;margin:12px 0 22px}}th,td{{border:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}}th{{background:#edf3f8}}
.metric-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.metric{{background:#f5f8fa;border-radius:6px;padding:14px}}.metric strong{{display:block;font-size:1.5rem}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f8fa;padding:14px;border-radius:6px}}.muted{{color:#526371}}@media(max-width:700px){{main{{padding:16px}}table{{font-size:13px}}}}
</style>
</head>
<body><main><div class="banner">{_escape(BANNER)}</div>{content}</main></body>
</html>"""


def _home():
    return _page("Try it", """
<h1>Shelfcheck user-test demo</h1>
<p>See how a fictional merchant appears in simulated shopping-agent recommendations, inspect suspected agent traffic, and review Shopify readiness. The complete fake weekly report takes only a few seconds.</p>
<form method="post" action="/run" class="card">
<h2>1. Choose the fictional scenario</h2>
<label for="brand">Fictional brand</label>
<input id="brand" name="brand" value="Northfield Socks" maxlength="60" required>
<label for="category">Product category</label>
<input id="category" name="category" value="outdoor apparel" maxlength="80" required>
<label for="price_band">Price band</label>
<input id="price_band" name="price_band" value="mid-range" maxlength="60" required>
<button type="submit">2. Generate my fake weekly report</button>
</form>
<p class="muted">The demo performs 500 deterministic runs against fake Muse and ChatGPT clients. It uses bundled synthetic traffic and catalog fixtures, makes no network calls, sends no email, and forgets all reports when the process restarts.</p>
""")


def _field(form, name, default, limit):
    values = form.get(name, [default])
    if len(values) != 1:
        raise DemoInputError(f"Provide one value for {name}.")
    value = values[0].strip()
    if not value or len(value) > limit or any(ord(character) < 32 for character in value):
        raise DemoInputError(f"{name.replace('_', ' ').title()} must be 1–{limit} printable characters.")
    return value


def _build_report(form):
    base = load_config(ROOT / "fixtures" / "brand_demo.json")
    brand_name = _field(form, "brand", base.primary.name, 60)
    category = _field(form, "category", base.category, 80)
    price_band = _field(form, "price_band", base.price_band, 60)
    primary = replace(base.primary, name=brand_name, aliases=(brand_name,))
    config = replace(base, primary=primary, category=category, price_band=price_band)

    names = [config.primary.name] + [item.name for item in config.competitors] + ["Trail Loom", "Summit Thread"]
    muse_weights = [max(1, len(names) + 2 - index) for index in range(len(names))]
    clients = [
        FakeAgentClient(surface, names, muse_weights if surface == "muse" else list(reversed(muse_weights)))
        for surface in config.surfaces
    ]
    runs = run_sample(config, clients)
    brands = (config.primary,) + config.competitors
    aliases = compile_aliases(brands)
    ambiguity_count = sum(extract_mentions(run, compiled=aliases)[1] for run in runs)
    shares = summarize(runs, brands)

    readiness = check_robots((ROOT / "fixtures" / "robots_demo.txt").read_text(encoding="utf-8"))
    readiness += check_catalog(ROOT / "fixtures" / "products_export_demo.csv")
    readiness += check_settings(ROOT / "fixtures" / "settings_answers_demo.json")

    sessions = parse_access_log(ROOT / "fixtures" / "access_demo.log", b"shelfcheck-hosted-demo-only-key")
    traffic = merge_buckets(
        classify_sessions(sessions),
        parse_ga4_csv(ROOT / "fixtures" / "ga4_demo.csv"),
    )
    report_html, report_text = render(
        config,
        shares,
        readiness,
        traffic,
        demo=True,
        ambiguity_count=ambiguity_count,
        runs=len(runs),
    )
    return {
        "config": config,
        "runs": runs,
        "shares": shares,
        "readiness": readiness,
        "traffic": traffic,
        "report_html": report_html,
        "report_text": report_text,
    }


def _result_page(result):
    config = result["config"]
    shares = result["shares"]
    readiness = result["readiness"]
    traffic = result["traffic"]
    statuses = Counter(item.status for item in readiness)

    share_rows = "".join(
        "<tr>"
        f"<td>{_escape(item.brand)}</td><td>{_escape(item.surface)}</td>"
        f"<td>{item.share:.1%}</td><td>{item.ci_low:.1%}–{item.ci_high:.1%}</td>"
        f"<td>{_escape(f'{item.mean_rank:.2f}' if item.mean_rank is not None else 'insufficient mentions')}</td>"
        "</tr>"
        for item in shares
    )
    traffic_rows = "".join(
        "<tr>"
        f"<td>{_escape(item.tier)}</td><td>{_escape(item.agent)}</td>"
        f"<td>{item.sessions}</td><td>{item.product_pages}</td><td>{item.checkout_hits}</td>"
        "</tr>"
        for item in traffic
    ) or '<tr><td colspan="5">No attributable synthetic traffic.</td></tr>'

    return _page("Weekly report", f"""
<p><a class="button" href="/">Try another fictional scenario</a></p>
<h1>{_escape(config.primary.name)} weekly shelf check</h1>
<p>Category: <strong>{_escape(config.category)}</strong> · Price band: <strong>{_escape(config.price_band)}</strong></p>
<div class="metric-grid">
<div class="metric"><strong>{len(result['runs'])}</strong>fake agent runs</div>
<div class="metric"><strong>{statuses.get('fail', 0)}</strong>readiness failures</div>
<div class="metric"><strong>{statuses.get('warn', 0)}</strong>readiness warnings</div>
<div class="metric"><strong>{sum(item.sessions for item in traffic)}</strong>attributed fake sessions</div>
</div>
<h2>Shopping share of voice</h2>
<table><thead><tr><th>Brand</th><th>Surface</th><th>Share</th><th>95% interval</th><th>Mean rank</th></tr></thead><tbody>{share_rows}</tbody></table>
<h2>Agent traffic by confidence tier</h2>
<p>Suspected traffic is heuristic and is not proof of Muse.</p>
<table><thead><tr><th>Tier</th><th>Signal</th><th>Sessions</th><th>Product pages</th><th>Checkout hits</th></tr></thead><tbody>{traffic_rows}</tbody></table>
<h2>Shopify readiness</h2>
<p>{statuses.get('pass', 0)} passed, {statuses.get('warn', 0)} warnings, and {statuses.get('fail', 0)} failures across robots, catalog, and checkout checks.</p>
<h2>Fake email preview</h2>
<p class="muted">This is the plain-text weekly report the fake mailer would receive. Nothing was sent or written to disk.</p>
<pre>{_escape(result['report_text'])}</pre>
""")


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler_class):
        super().__init__(address, handler_class)
        self.reports = OrderedDict()
        self.reports_lock = threading.Lock()
        self.connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.report_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REPORTS)

    def handle_error(self, request, client_address):
        """Suppress tracebacks so aborted requests cannot leak visitor addresses."""

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def try_start_report(self):
        return self.report_slots.acquire(blocking=False)

    def finish_report(self):
        self.report_slots.release()

    def save_report(self, report):
        token = secrets.token_urlsafe(18)
        with self.reports_lock:
            self.reports[token] = report
            while len(self.reports) > 100:
                self.reports.popitem(last=False)
        return token

    def get_report(self, token):
        with self.reports_lock:
            return self.reports.get(token)


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "ShelfcheckDemo"
    sys_version = ""
    timeout = 10

    def log_message(self, format, *args):
        """Disable the request log, including paths and submitted values."""

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()

    def _send_html(self, status, page):
        body = page.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _problem(self, status, title, message):
        self._send_html(status, _page(title, f'<h1>{_escape(title)}</h1><p>{_escape(message)}</p><p><a class="button" href="/">Return to the demo</a></p>'))

    def do_GET(self):
        target = urlparse(self.path)
        if target.path == "/healthz":
            body = b"ok"
            self._headers(200, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if target.path == "/":
            self._send_html(200, _home())
            return
        if target.path == "/report":
            try:
                values = parse_qs(target.query, keep_blank_values=True, max_num_fields=4)
            except ValueError:
                self._problem(400, "Invalid request", "The report link is invalid.")
                return
            token_values = values.get("id", [])
            report = self.server.get_report(token_values[0]) if len(token_values) == 1 else None
            if report is None:
                self._problem(404, "Report not found", "This in-memory report does not exist or the demo was restarted.")
            else:
                self._send_html(200, _result_page(report))
            return
        self._problem(404, "Page not found", "That demo page does not exist.")

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            self._problem(404, "Page not found", "That demo page does not exist.")
            return
        if self.headers.get("Transfer-Encoding"):
            self._problem(400, "Invalid request", "Chunked request bodies are not accepted.")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._problem(411, "Length required", "A valid Content-Length header is required.")
            return
        if length < 0:
            self._problem(400, "Invalid request", "The request length is invalid.")
            return
        if length > MAX_BODY:
            self.close_connection = True
            self._problem(413, "Request too large", "Request bodies are limited to 64 KB.")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self._problem(415, "Unsupported request", "Submit the provided form to run the demo.")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._problem(400, "Invalid request", "The request body was incomplete.")
            return
        try:
            decoded = body.decode("utf-8")
            form = parse_qs(decoded, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
        except (UnicodeDecodeError, ValueError) as exc:
            self._problem(400, "Check the form", str(exc))
            return

        if not self.server.try_start_report():
            self._problem(503, "Demo busy", "Too many fake reports are already running. Please try again shortly.")
            return
        try:
            try:
                report = _build_report(form)
            except (ValueError, DemoInputError) as exc:
                self._problem(400, "Check the form", str(exc))
                return
            except Exception:
                self._problem(500, "Demo unavailable", "The fake report could not be generated. Please try again.")
                return
        finally:
            self.server.finish_report()

        token = self.server.save_report(report)
        location = "/report?id=" + token
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def create_server(host="0.0.0.0", port=8080):
    return DemoServer((host, port), DemoHandler)


def main():
    raw_port = os.environ.get("PORT", "8080")
    try:
        port = int(raw_port)
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer between 0 and 65535")
    server = create_server("0.0.0.0", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
