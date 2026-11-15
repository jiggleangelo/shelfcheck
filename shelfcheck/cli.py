import argparse
import json
import logging
import os
import re
import secrets
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from .agents import FakeAgentClient
from .config import ValidationError, load_config
from .mailer import OutboxMailer
from .models import AgentRun, ReadinessItem, TrafficBucket
from .readiness import check_catalog, check_robots, check_settings
from .redact import install, scrub
from .report import render
from .sampler import BudgetExceeded, compile_aliases, extract_mentions, run_sample
from .stats import summarize
from .traffic import classify_sessions, merge_buckets, parse_access_log, parse_ga4_csv
from .vault import Vault

_ROOT = Path(__file__).parent.parent
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_MAX_EMAIL_LENGTH = 254
_LOG = logging.getLogger(__name__)


def _week(value=None):
    result = value or date.today().strftime("%G-W%V")
    match = _WEEK.fullmatch(result)
    if not match:
        raise ValidationError("week must match YYYY-Www")
    try:
        date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise ValidationError("week is not a valid ISO week") from exc
    return result


def _validate_email(value):
    if not value:
        raise ValidationError("email must not be empty")
    if len(value) > _MAX_EMAIL_LENGTH:
        raise ValidationError("email must contain at most 254 characters")
    if value != value.strip() or "\r" in value or "\n" in value:
        raise ValidationError("email is invalid")
    if value.count("@") != 1:
        raise ValidationError("email is invalid")
    local, _ = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValidationError("email is invalid")
    if not _EMAIL.fullmatch(value):
        raise ValidationError("email is invalid")
    return value


def _read_waitlist_email(stream):
    raw = stream.readline(_MAX_EMAIL_LENGTH + 2)
    if len(raw) > _MAX_EMAIL_LENGTH and not raw.endswith(("\n", "\r")):
        raise ValidationError("email must contain at most 254 characters")
    return _validate_email(raw.rstrip("\r\n"))


def _safe_child(root, *parts):
    root = Path(root).resolve()
    path = root.joinpath(*parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("output path escapes SHELFCHECK_OUT") from exc
    return path


def _out(config, week=None):
    root = Path(os.environ.get("SHELFCHECK_OUT", "out"))
    path = _safe_child(root, config.slug, _week(week))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _clients(config):
    names = [config.primary.name] + [item.name for item in config.competitors] + ["Trail Loom", "Summit Thread"]
    muse = [max(1, len(names) + 2 - index) for index in range(len(names))]
    chatgpt = list(reversed(muse))
    return [FakeAgentClient(surface, names, muse if surface == "muse" else chatgpt)
            for surface in config.surfaces]


def sample(config, target):
    runs = run_sample(config, _clients(config))
    brands = (config.primary,) + config.competitors
    aliases = compile_aliases(brands)
    ambiguity = sum(extract_mentions(run, compiled=aliases)[1] for run in runs)
    _write(target / "samples.json", {"sample_type": "fake", "ambiguity_count": ambiguity,
                                      "runs": [asdict(run) for run in runs]})
    return runs, ambiguity


def readiness(config, target, robots, products, settings):
    items = check_robots(Path(robots).read_text(encoding="utf-8"))
    items += check_catalog(products) + check_settings(settings)
    _write(target / "readiness.json", [asdict(item) for item in items])
    return items


def traffic(config, target, log, ga4=None, *, demo=False):
    raw_key = os.environ.get("SHELFCHECK_IP_KEY")
    if not raw_key and not demo:
        raise ValidationError("SHELFCHECK_IP_KEY is required for traffic analysis")
    key = raw_key.encode() if raw_key else secrets.token_bytes(32)
    sessions = parse_access_log(log, key)
    if sessions.skipped_lines:
        _LOG.warning("skipped %d malformed or oversized access-log lines", sessions.skipped_lines)
    groups = [classify_sessions(sessions)]
    if ga4:
        groups.append(parse_ga4_csv(ga4))
    buckets = merge_buckets(*groups)
    _write(target / "traffic.json", [asdict(item) for item in buckets])
    return buckets


def _runs(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data, [AgentRun(item["surface"], item["prompt_id"], item["repeat"],
                           tuple(item["recommended"]), tuple(item.get("browsed_urls", ())))
                  for item in data["runs"]]


def _prior_shares(config, target, week):
    current = _week(week or target.name)
    match = _WEEK.fullmatch(current)
    monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    prior_week = (monday - timedelta(weeks=1)).strftime("%G-W%V")
    prior_path = _safe_child(target.parent.parent, config.slug, prior_week) / "samples.json"
    try:
        _, runs = _runs(prior_path)
    except FileNotFoundError:
        return None
    return summarize(runs, (config.primary,) + config.competitors)


def make_report(config, target, week=None):
    sample_data, runs = _runs(target / "samples.json")
    ready = [ReadinessItem(**item) for item in json.loads((target / "readiness.json").read_text(encoding="utf-8"))]
    buckets = [TrafficBucket(**item) for item in json.loads((target / "traffic.json").read_text(encoding="utf-8"))]
    shares = summarize(runs, (config.primary,) + config.competitors)
    prior = _prior_shares(config, target, week)
    html, text = render(config, shares, ready, buckets, prior=prior,
                        demo=sample_data.get("sample_type") == "fake",
                        ambiguity_count=sample_data.get("ambiguity_count", 0), runs=len(runs))
    (target / "report.html").write_text(html, encoding="utf-8")
    (target / "report.txt").write_text(text, encoding="utf-8")
    token = config.recipient_token or "v_0000000000000000"
    OutboxMailer(target.parent.parent / "outbox").send(
        token, f"Shelfcheck: {config.primary.name}", html, text)


def parser():
    root = argparse.ArgumentParser(prog="shelfcheck")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    sample_parser = sub.add_parser("sample", help="generate deterministic demo-only fake samples")
    sample_parser.add_argument("--config", required=True)
    sample_parser.add_argument("--week")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--config", required=True)
    report_parser.add_argument("--week")
    item = sub.add_parser("readiness")
    item.add_argument("--config", required=True)
    item.add_argument("--robots", required=True)
    item.add_argument("--products", required=True)
    item.add_argument("--settings", required=True)
    item.add_argument("--week")
    item = sub.add_parser("traffic")
    item.add_argument("--config", required=True)
    item.add_argument("--log", required=True)
    item.add_argument("--ga4")
    item.add_argument("--week")
    item = sub.add_parser("waitlist")
    item.add_argument("--email-stdin", action="store_true")
    return root


def _file_error(exc):
    filename = getattr(exc, "filename", None)
    suffix = f": {Path(filename).name}" if filename else ""
    return f"file operation failed ({type(exc).__name__}{suffix})"


def main(argv=None):
    install()
    try:
        args = parser().parse_args(argv)
        if args.command == "waitlist":
            if not args.email_stdin:
                raise ValidationError("waitlist requires --email-stdin")
            key = os.environ.get("SHELFCHECK_VAULT_KEY")
            if not key:
                raise ValidationError("SHELFCHECK_VAULT_KEY is required")
            email = _read_waitlist_email(sys.stdin)
            vault = Vault(Path(os.environ.get("SHELFCHECK_VAULT", "vault")), key.encode())
            print(vault.put("email", email))
            return 0
        if args.command == "demo":
            cfg = load_config(_ROOT / "fixtures" / "brand_demo.json")
            target = _out(cfg)
            sample(cfg, target)
            readiness(cfg, target, _ROOT / "fixtures/robots_demo.txt", _ROOT / "fixtures/products_export_demo.csv", _ROOT / "fixtures/settings_answers_demo.json")
            traffic(cfg, target, _ROOT / "fixtures/access_demo.log", _ROOT / "fixtures/ga4_demo.csv", demo=True)
            make_report(cfg, target)
            return 0
        cfg = load_config(args.config)
        target = _out(cfg, args.week)
        if args.command == "sample":
            sample(cfg, target)
        elif args.command == "readiness":
            readiness(cfg, target, args.robots, args.products, args.settings)
        elif args.command == "traffic":
            traffic(cfg, target, args.log, args.ga4)
        elif args.command == "report":
            make_report(cfg, target, args.week)
        return 0
    except BudgetExceeded as exc:
        print(scrub(exc), file=sys.stderr)
        return 3
    except ValidationError as exc:
        print(scrub(exc), file=sys.stderr)
        return 2
    except json.JSONDecodeError:
        _LOG.debug("invalid JSON input", exc_info=True)
        print("invalid JSON input", file=sys.stderr)
        return 2
    except OSError as exc:
        _LOG.debug("file operation failed", exc_info=True)
        print(scrub(_file_error(exc)), file=sys.stderr)
        return 2
