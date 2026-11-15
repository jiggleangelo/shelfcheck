import html as html_lib
import re
from pathlib import Path

from .models import BrandShare, ReadinessItem, TrafficBucket
from .stats import change_flag, comparison

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
REPORT_NOTICE = "Operated by AI agents. Early version: keep your own copy of anything important. Do not enter customer names, emails, addresses or order data. Nothing is charged."
PRIVACY_URL = "https://manykiln.com/privacy"


def _clean(value, limit=240):
    return _CONTROL.sub(" ", str(value)).strip()[:limit]


def _e(value, limit=240):
    return html_lib.escape(_clean(value, limit), quote=True)


def render(config, shares, readiness, traffic, prior=None, *, demo=True,
           ambiguity_count=0, runs=0, repeats=None):
    css = (Path(__file__).parent.parent / "web" / "report.css").read_text(encoding="utf-8")
    fixes = sorted((x for x in readiness if x.status != "pass"), key=lambda x: x.severity, reverse=True)[:3]
    prior_map = {(x.brand, x.surface): x for x in (prior or [])}
    fix_html = "".join(f"<li><b>{_e(x.area)}: {_e(x.id)}</b> — {_e(x.fix)} ({_e(x.evidence)})</li>" for x in fixes) or "<li>No urgent fixes found.</li>"
    traffic_html = "".join(f"<tr><td>{_e(x.tier,40)}</td><td>{_e(x.agent,80)}</td><td>{x.sessions}</td><td>{x.product_pages}</td><td>{x.checkout_hits}</td></tr>" for x in traffic) or '<tr><td colspan="5">No attributable traffic found.</td></tr>'
    share_rows = []
    for x in shares:
        rank = f"{x.mean_rank:.2f}" if x.mean_rank is not None else "insufficient mentions"
        share_rows.append(f"<tr><td>{_e(x.brand,80)}</td><td>{_e(x.surface,40)}</td><td>{x.share:.1%}</td><td>{x.ci_low:.1%}–{x.ci_high:.1%}</td><td>{_e(rank)}</td><td>{_e(change_flag(x, prior_map.get((x.brand,x.surface))))}</td></tr>")
    comparisons = []
    for surface in config.surfaces:
        rows = {x.brand: x for x in shares if x.surface == surface}
        if config.primary.name in rows:
            comparisons.extend(f"{surface}: {comparison(rows[config.primary.name], rows[c.name])}" for c in config.competitors if c.name in rows)
    ready_html = "".join(f'<tr><td>{_e(x.area)}</td><td>{_e(x.id)}</td><td class="{_e(x.status,16)}">{_e(x.status,16)}</td><td>{_e(x.evidence)}</td><td>{_e(x.fix)}</td></tr>' for x in readiness)
    banner = '<div class="banner">DEMO DATA – fake agent client</div>' if demo else ""
    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>Shelfcheck — {_e(config.primary.name,80)}</title><style>{css}</style></head><body><main>{banner}<h1>{_e(config.primary.name,80)} weekly shelf check</h1><h2>Top 3 fixes</h2><ol>{fix_html}</ol><h2>Agent traffic by confidence tier</h2><p class="warning">Suspected traffic is heuristic, may include other bots; not proof of Muse.</p><table><tr><th>Tier</th><th>Agent/signal</th><th>Sessions</th><th>Product pages</th><th>Checkout hits</th></tr>{traffic_html}</table><p>GA4 data can populate referred traffic only.</p><h2>Shopping share of voice</h2><table><tr><th>Brand</th><th>Surface</th><th>Share</th><th>95% interval</th><th>Mean rank</th><th>Change</th></tr>{''.join(share_rows)}</table><ul>{''.join(f'<li>{_e(x)}</li>' for x in comparisons)}</ul><h2>Readiness checklist</h2><table><tr><th>Area</th><th>Check</th><th>Status</th><th>Evidence</th><th>Fix</th></tr>{ready_html}</table><details><summary>Methodology and caveats</summary><p>{runs} runs; {repeats or config.repeats} repeats per prompt; {ambiguity_count} ambiguous recommendation matches. The wider of the Wilson and prompt-clustered intervals is shown. Sampling surface type: {'fake' if demo else 'live'}. Signature evidence notes stale data. Repeated runs are not fully independent.</p></details></main></body></html>'''
    lines = [f"SHELFCHECK — {_clean(config.primary.name,80)}", "DEMO DATA – fake agent client" if demo else "LIVE SAMPLE", "", "TOP 3 FIXES"]
    lines += [f"- {_clean(x.area)}/{_clean(x.id)}: {_clean(x.fix)} ({_clean(x.evidence)})" for x in fixes]
    lines += ["", "AGENT TRAFFIC", "Suspected traffic is heuristic, may include other bots; not proof of Muse."]
    lines += [f"- {_clean(x.tier,40)} / {_clean(x.agent,80)}: {x.sessions} sessions, {x.product_pages} product pages, {x.checkout_hits} checkout hits" for x in traffic]
    lines += ["", "SHOPPING SHARE OF VOICE"]
    lines += [f"- {_clean(x.brand,80)} / {_clean(x.surface,40)}: {x.share:.1%} (95% interval {x.ci_low:.1%}–{x.ci_high:.1%}); {_clean(change_flag(x,prior_map.get((x.brand,x.surface))))}" for x in shares]
    lines += [_clean(x) for x in comparisons] + ["", "READINESS"]
    lines += [f"- {_clean(x.area)}/{_clean(x.id)}: {_clean(x.status)}; {_clean(x.evidence)}; Fix: {_clean(x.fix)}" for x in readiness]
    lines += ["", f"Methodology: {runs} runs, {repeats or config.repeats} repeats, {ambiguity_count} ambiguous matches; wider Wilson/prompt-clustered 95% interval shown."]
    return page, "\n".join(lines) + "\n"


def render_snapshot(profile, snapshot, previous=None):
    readiness = [ReadinessItem(**x) for x in snapshot.get("readiness", [])]
    traffic = [TrafficBucket(**x) for x in snapshot.get("traffic", [])]
    shares = [BrandShare(**x) for x in snapshot.get("shares", [])]
    prior = [BrandShare(**x) for x in (previous or {}).get("shares", [])]
    fixes = sorted((x for x in readiness if x.status != "pass"), key=lambda x: (-x.severity, x.id))[:3]
    old_ready = {x["id"]: x["status"] for x in (previous or {}).get("readiness", [])}
    ready_changes = [f"{x.id}: {old_ready[x.id]} → {x.status}" for x in readiness if x.id in old_ready and old_ready[x.id] != x.status]
    old_traffic = {(x["tier"], x["agent"]): x for x in (previous or {}).get("traffic", [])}
    traffic_changes = []
    for x in traffic:
        old = old_traffic.get((x.tier, x.agent), {})
        delta = x.sessions - int(old.get("sessions", 0))
        if delta:
            traffic_changes.append(f"{x.tier}/{x.agent}: {delta:+d} sessions")
    prior_map = {(x.brand, x.surface): x for x in prior}
    share_changes = []
    if previous:
        for x in shares:
            flag = change_flag(x, prior_map.get((x.brand, x.surface)))
            if flag == "change flagged":
                share_changes.append(f"{x.brand}/{x.surface}: {flag} ({x.share:.1%})")
    comparisons = []
    surfaces = sorted({x.surface for x in shares})
    primary_name = profile.get("name", "Store")
    competitors = [x for x in snapshot.get("prompt", {}).get("competitors", []) if x.get("name")]
    for surface in surfaces:
        rows = {x.brand: x for x in shares if x.surface == surface}
        primary = rows.get(primary_name)
        if primary:
            for competitor in competitors:
                other = rows.get(competitor["name"])
                if other:
                    comparisons.append(f"{surface}: {comparison(primary, other)}")
    date = snapshot.get("date", "unknown date")
    previous_date = (previous or {}).get("date")
    fixes_html = "".join(f"<li><strong>{_e(x.id)}</strong>: {_e(x.fix)} — {_e(x.evidence)}</li>" for x in fixes) or "<li>No urgent fixes found.</li>"
    traffic_html = "".join(f"<tr><td>{_e(x.tier)}</td><td>{_e(x.agent)}</td><td>{x.sessions}</td><td>{x.product_pages}</td><td>{x.checkout_hits}</td></tr>" for x in traffic) or '<tr><td colspan="5">No attributable traffic found.</td></tr>'
    ready_html = "".join(f'<tr><td>{_e(x.area)}</td><td>{_e(x.id)}</td><td class="{_e(x.status)}">{_e(x.status)}</td><td>{_e(x.evidence)}</td><td>{_e(x.fix)}</td></tr>' for x in readiness)
    share_html = "".join(f"<tr><td>{_e(x.brand)}</td><td>{_e(x.surface)}</td><td>{x.share:.1%}</td><td>{x.ci_low:.1%}–{x.ci_high:.1%}</td><td>{_e(f'{x.mean_rank:.2f}' if x.mean_rank is not None else 'insufficient mentions')}</td></tr>" for x in shares)
    changes = ready_changes + traffic_changes + share_changes
    change_html = "".join(f"<li>{_e(x)}</li>" for x in changes) or "<li>No material changes.</li>"
    comparison_html = "".join(f"<li>{_e(x)}</li>" for x in comparisons) or "<li>No competitor comparison is available.</li>"
    share_section = f'<h2>Share of voice</h2><table><tr><th>Brand</th><th>Surface</th><th>Share</th><th>95% Wilson/prompt-clustered interval</th><th>Mean rank</th></tr>{share_html}</table><h3>Comparisons</h3><ul>{comparison_html}</ul>' if shares else ""
    css = (Path(__file__).parent.parent / "web" / "report.css").read_text(encoding="utf-8")
    banner = '<div class="banner">SAMPLE DATA</div>' if snapshot.get("sample") else ""
    prune = f'<div class="banner">The oldest snapshot was removed to keep the 26-snapshot or 5 MB workspace limit.</div>' if snapshot.get("pruned_oldest") else ""
    notice = f'<div class="banner">{_e(REPORT_NOTICE,500)} <a href="{PRIVACY_URL}">Privacy</a></div>'
    page = f'''<!doctype html><html><head><meta charset="utf-8"><title>Shelfcheck — {_e(primary_name)}</title><style>{css}</style></head><body><main>{notice}{banner}{prune}<h1>{_e(primary_name)} shelf check</h1><p>Snapshot {_e(date)}</p><h2>Top 3 fixes</h2><ol>{fixes_html}</ol><h2>Agent traffic by confidence tier</h2><p class="warning">Suspected traffic is heuristic, may include other bots; not proof of Muse.</p><table><tr><th>Tier</th><th>Agent/signal</th><th>Sessions</th><th>Product pages</th><th>Checkout hits</th></tr>{traffic_html}</table><h2>Readiness checklist</h2><table><tr><th>Area</th><th>Check</th><th>Status</th><th>Evidence</th><th>Where to verify/fix</th></tr>{ready_html}</table>{share_section}<h2>Changes since {_e(previous_date or 'the first snapshot')}</h2><ul>{change_html}</ul></main></body></html>'''
    lines = [REPORT_NOTICE, f"Privacy: {PRIVACY_URL}", f"SHELFCHECK — {_clean(primary_name)}", f"Snapshot {date}", "", "TOP 3 FIXES"]
    if snapshot.get("pruned_oldest"):
        lines += ["The oldest snapshot was removed to keep the workspace limits."]
    lines += [f"- {x.id}: {x.fix} ({x.evidence})" for x in fixes] or ["- No urgent fixes found."]
    lines += ["", "AGENT TRAFFIC", "Suspected traffic is heuristic, may include other bots; not proof of Muse."]
    lines += [f"- {x.tier}/{x.agent}: {x.sessions} sessions, {x.product_pages} product pages, {x.checkout_hits} checkout hits" for x in traffic]
    lines += ["", "READINESS"] + [f"- {x.area}/{x.id}: {x.status}; {x.evidence}; {x.fix}" for x in readiness]
    if shares:
        lines += ["", "SHARE OF VOICE"] + [f"- {x.brand}/{x.surface}: {x.share:.1%} (95% Wilson/prompt-clustered interval {x.ci_low:.1%}–{x.ci_high:.1%})" for x in shares]
        lines += comparisons or ["No competitor comparison is available."]
    lines += ["", f"CHANGES SINCE {previous_date or 'THE FIRST SNAPSHOT'}"] + ([f"- {x}" for x in changes] or ["- No material changes."])
    return page, "\n".join(lines) + "\n"
