import json
import re
from pathlib import Path
from .models import Brand, BrandConfig

_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")
_TOKEN = re.compile(r"^v_[0-9a-f]{16}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(ValueError):
    pass


def _plain(value, label):
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        raise ValidationError(f"{label} must be a non-empty single-line string")
    return value


def _brand(value):
    try:
        aliases = tuple(_plain(alias, "brand alias") for alias in value["aliases"])
        if not aliases:
            raise ValidationError("each brand requires at least one alias")
        return Brand(_plain(value["name"], "brand name"), aliases,
                     _plain(value["domain"], "brand domain"))
    except (KeyError, TypeError) as exc:
        raise ValidationError("invalid brand configuration") from exc


def load_config(path):
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read config {source.name}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON in config {source.name}") from exc

    try:
        competitors = tuple(_brand(item) for item in raw["competitors"])
        if len(competitors) != 3:
            raise ValidationError("exactly three competitors are required")
        slug = raw["slug"]
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ValidationError("slug must match ^[a-z0-9-]{1,40}$")
        recipient = raw.get("recipient_token")
        if recipient is not None and not _TOKEN.fullmatch(str(recipient)):
            raise ValidationError("recipient_token must match ^v_[0-9a-f]{16}$")
        surfaces = tuple(raw.get("surfaces", ("muse", "chatgpt")))
        if not surfaces or any(not isinstance(item, str) or not item for item in surfaces):
            raise ValidationError("at least one valid surface is required")
        cfg = BrandConfig(
            slug=slug,
            primary=_brand(raw["primary"]),
            competitors=competitors,
            category=_plain(raw["category"], "category"),
            price_band=_plain(raw["price_band"], "price_band"),
            surfaces=surfaces,
            repeats=int(raw.get("repeats", 5)),
            max_runs_per_week=int(raw.get("max_runs_per_week", 600)),
            recipient_token=recipient,
        )
    except ValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid config structure") from exc
    if cfg.repeats < 1 or cfg.max_runs_per_week < 1:
        raise ValidationError("positive repeats and max_runs_per_week are required")
    return cfg
