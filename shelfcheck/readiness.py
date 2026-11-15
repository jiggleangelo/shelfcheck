import csv
import json
import re
from datetime import date
from pathlib import Path
from urllib.robotparser import RobotFileParser

from .config import ValidationError
from .models import ReadinessItem

_DATA = Path(__file__).with_name("data")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _rules():
    return json.loads((_DATA / "readiness_rules.json").read_text(encoding="utf-8"))


def _signatures():
    return json.loads((_DATA / "agent_signatures.json").read_text(encoding="utf-8"))


def _label(value, limit=80):
    return _CONTROL.sub(" ", str(value)).strip()[:limit]


def check_robots(text, today=None):
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    output = []
    today = today or date.today()
    paths = ("/", "/products/x", "/collections/x", "/cart")
    for signature in _signatures()["user_agents"]:
        token = signature["token"]
        blocked = [path for path in paths if not parser.can_fetch(token, path)]
        if blocked and signature["role"] == "training":
            status = "warn"
            evidence = f"{token} training crawler blocked at {', '.join(blocked)}; this may be intentional"
        elif blocked:
            status = "fail"
            evidence = f"{token} cannot fetch {', '.join(blocked)}"
        else:
            status = "pass"
            evidence = f"{token} can fetch root, shopping, and cart paths"
        if (today - date.fromisoformat(signature["verified_on"])).days > 60:
            evidence += "; signature list may be stale"
        fix = (f"Review the {token} rules for {', '.join(blocked)} and allow only intended access."
               if blocked else f"Keep the {token} robots policy under review.")
        output.append(ReadinessItem("robots_" + token.lower(), "robots", status,
                                    3 if status == "fail" else 1, evidence, fix))
    return output


def check_catalog_rows(rows):
    rows = list(rows)
    headers = set().union(*(row.keys() for row in rows)) if rows else set()
    handles = sorted({_label(row.get("Handle", "")) for row in rows if row.get("Handle")})
    by_handle = {}
    products = {}
    for row in rows:
        handle = _label(row.get("Handle", ""))
        if not handle:
            continue
        by_handle.setdefault(handle, []).append(row)
        products.setdefault(handle, row)

    discriminator_columns = [name for name in ("Option1 Value", "Variant SKU") if name in headers]
    variant_value_columns = ("Variant Price", "Variant Barcode", "Variant Inventory Qty")
    variant_rows = []
    for row in rows:
        if not _label(row.get("Handle", "")):
            continue
        if discriminator_columns:
            is_variant = any(str(row.get(name, "") or "").strip() for name in discriminator_columns)
        else:
            is_variant = any(str(row.get(name, "") or "").strip() for name in variant_value_columns)
        if is_variant:
            variant_rows.append(row)

    missing_price = [_label(row.get("Handle", "")) for row in variant_rows
                     if not str(row.get("Variant Price", "") or "").strip()]
    missing_barcode = [_label(row.get("Handle", "")) for row in variant_rows
                       if not str(row.get("Variant Barcode", "") or "").strip()]
    missing_image = [handle for handle in handles
                     if not any(str(row.get("Image Src", "") or "").strip()
                                for row in by_handle.get(handle, ()))]
    shallow = [handle for handle, row in products.items()
               if len(re.sub(r"<[^>]+>", "", str(row.get("Body (HTML)", "") or "")).strip()) < 200]
    missing_type = [handle for handle, row in products.items()
                    if not str(row.get("Type", "") or "").strip()]
    inventory_header_present = "Variant Inventory Qty" in headers
    missing_inventory = ([_label(row.get("Handle", "")) for row in variant_rows
                          if not str(row.get("Variant Inventory Qty", "") or "").strip()]
                         if inventory_header_present else [])

    variant_count = max(1, len(variant_rows))
    definitions = [
        ("price_present", missing_price, bool(missing_price), "fail", "Add a variant price.", None),
        ("gtin_present", missing_barcode, len(missing_barcode) > .2 * variant_count,
         "fail", "Add valid GTIN/barcodes.", None),
        ("image_present", missing_image, bool(missing_image), "fail", "Add at least one product image.", None),
        ("description_depth", shallow, len(shallow) > .3 * max(1, len(products)),
         "fail", "Expand product descriptions beyond 200 characters.", None),
        ("product_type_set", missing_type, bool(missing_type), "fail", "Set a product type.", None),
        ("inventory_signal", missing_inventory, bool(missing_inventory), "fail",
         "Export variant inventory quantities.",
         None if inventory_header_present else
         "The export does not include the Variant Inventory Qty column, so inventory readiness could not be checked."),
    ]
    output = []
    for ident, failures, failed, failed_status, fix, override in definitions:
        examples = sorted(set(filter(None, failures)))[:5]
        if override:
            status = "warn"
            evidence = override
        else:
            status = failed_status if failed else "pass"
            evidence = f"{len(failures)} affected across {len(variant_rows) if ident in {'price_present','gtin_present','inventory_signal'} else len(products)} evaluated rows/products; examples: {', '.join(examples) or 'none'}"
        output.append(ReadinessItem(ident, "catalog", status, 2, evidence, fix))
    return output


def check_catalog(csv_path):
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        return check_catalog_rows(csv.DictReader(handle))


def check_settings_answers(answers):
    output = []
    for rule in _rules()["settings"]:
        value = str(answers.get(rule["id"], "unknown")).lower()
        if value not in {"yes", "no", "unknown"}:
            raise ValidationError(f"setting {rule['id']} must be yes, no, or unknown")
        status = "pass" if value == "yes" else "fail" if value == "no" else "warn"
        output.append(ReadinessItem(rule["id"], "checkout", status, rule["severity"],
                                    f"Answer: {value}. Verify against current Shopify admin.",
                                    rule["fix"]))
    return output


def check_settings(path):
    return check_settings_answers(json.loads(Path(path).read_text(encoding="utf-8")))
