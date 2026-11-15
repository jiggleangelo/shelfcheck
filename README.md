# shelfcheck

Prototype: Is your Shopify store ready for AI shopping agents? shelfcheck

**Use it free, no account:** https://app-shelfcheck.manykiln.com/  
**Try the demo (made-up data):** https://try-shelfcheck.manykiln.com/  
**Product page:** https://shelfcheck.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 27 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# Shelfcheck

Shelfcheck generates an offline weekly agent-visibility report using Python 3.12 and the standard library. No installation or third-party dependencies are required. Run commands from the repository root.

## Run the complete demo

```sh
python -m shelfcheck demo
```

The demo samples 50 prompts on two fake surfaces with five repeats: 500 deterministic runs. It checks the bundled robots, product catalog and settings fixtures, analyzes the synthetic access-log and GA4 fixtures, and renders HTML and plain-text reports with confidence intervals.

Artifacts are written to `out/demo/<YYYY-Www>/`: `samples.json`, `readiness.json`, `traffic.json`, `report.html` and `report.txt`. A token-addressed multipart email is written to `out/outbox/`. Set `SHELFCHECK_OUT` to choose another output directory.

## Run stages independently

```sh
python -m shelfcheck sample --config fixtures/brand_demo.json --week 2026-W43
python -m shelfcheck readiness --config fixtures/brand_demo.json --week 2026-W43 --robots fixtures/robots_demo.txt --products fixtures/products_export_demo.csv --settings fixtures/settings_answers_demo.json
SHELFCHECK_IP_KEY=local-test-key python -m shelfcheck traffic --config fixtures/brand_demo.json --week 2026-W43 --log fixtures/access_demo.log --ga4 fixtures/ga4_demo.csv
python -m shelfcheck report --config fixtures/brand_demo.json --week 2026-W43
```

Use the same configuration, output directory and week for all stages. The report checks the preceding ISO week's samples when available.

## Run tests

```sh
python -m unittest discover -v
python -m unittest tests.test_e2e_demo -v
```

Tests use temporary output directories and block network connections. The end-to-end test invokes the runnable module entry point, checks all persisted stages, verifies report and email contents, checks deterministic sampling and asserts that decoys and Googlebot do not inflate the results. Existing tests remain unchanged.

## Fixtures and interpretation

All fixture brands, products and traffic are fictional. Access-log IPs use documentation ranges. Product fixtures intentionally include readiness failures; traffic fixtures include declared, referred and suspected visits, an ordinary visitor and a Googlebot shopping-page burst. GA4 contributes referred traffic only. These two traffic sources are independent synthetic populations; overlapping real exports must not be summed without deduplication.

The demo tracks distinct full brand names and domain aliases. `Northfield Outdoor` and `Summit Thread` are untracked names, not aliases of the tracked brands. Googlebot remains in the robots checklist but is excluded from agent-traffic attribution, including suspected-traffic fallback.

The `sample` command uses `FakeAgentClient`, writes `sample_type: fake`, and never contacts Muse or ChatGPT. Live sampling adapters are intentionally out of scope. Shares are simulated, and suspected traffic is heuristic, not proof of Muse. Before live use, compliant sampling access remains an unresolved dependency.

## Privacy and delivery

Real traffic processing fails closed unless `SHELFCHECK_IP_KEY` is set. Uploaded IPs are truncated and keyed-hashed during ingestion. Use a stable secret key for real uploads; the demo can generate an ephemeral key.

Waitlist enrollment requires a stable `SHELFCHECK_VAULT_KEY`. Pass an address through standard input with `python -m shelfcheck waitlist --email-stdin`; the command returns an opaque recipient token. Changing the vault key changes tokens. Vault records are encrypted and authenticated, and the application exposes only `put()` and `contains()`.

`OutboxMailer` writes local RFC emails addressed to opaque tokens; it does not send email. No address resolver or live delivery adapter ships in this MVP. Production delivery would require a separate privileged adapter inside the vault boundary that resolves tokens without exposing addresses to analytics or reporting.

## Usable app

Run the persistent, self-serve application with:

```sh
DATA_DIR=./app-data \
PUBLIC_BASE_URL=https://app-shelfcheck.manykiln.com \
APP_HOST=127.0.0.1 PORT=8080 \
python3 app.py
```

`DATA_DIR` is the app's only writable location. The platform may additionally set `APP_HTTPS_TERMINATED=true` and `APP_DATA_CAP_MB` (default 200). `PUBLIC_BASE_URL` controls accepted Host, Origin and Referer values; when testing locally, continue connecting to the local socket while sending the public Host header.

The app creates private, token-addressed workspaces with saved dated snapshots, printable HTML and plain-text reports, JSON export, immediate deletion and optional read-only share links. It accepts pasted or uploaded robots rules, Shopify product-export CSV files, agentic-settings answers, combined access logs (plain or gzip), GA4 exploration CSV files and optional manually collected prompt answers.

The app makes no AI or other outbound calls. Its prompt list is deterministic: users ask the prompts in their own browser and paste brand recommendations back into Shelfcheck. Matching, readiness checks, traffic classification, confidence intervals and snapshot comparisons are deterministic local rules. Uploaded catalog CSV bodies and raw traffic logs are not retained. Access-log IP grouping uses a fresh random key for each upload and stores only aggregate buckets.

Sample workspaces contain made-up data and are visibly marked `SAMPLE DATA`. The legacy CLI, fake clients, outbox mailer, vault and waitlist remain available for the offline demo and its tests, but `app.py` does not import or use them.
