from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    name: str
    aliases: tuple[str, ...]
    domain: str


@dataclass(frozen=True)
class BrandConfig:
    slug: str
    primary: Brand
    competitors: tuple[Brand, ...]
    category: str
    price_band: str
    surfaces: tuple[str, ...] = ("muse", "chatgpt")
    repeats: int = 5
    max_runs_per_week: int = 600
    recipient_token: str | None = None


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    text: str


@dataclass(frozen=True)
class AgentRun:
    surface: str
    prompt_id: str
    repeat: int
    recommended: tuple[str, ...]
    browsed_urls: tuple[str, ...] = ()


@dataclass
class BrandShare:
    brand: str
    surface: str
    mentions: int
    runs: int
    share: float
    ci_low: float
    ci_high: float
    mean_rank: float | None
    top1: int | None


@dataclass
class ReadinessItem:
    id: str
    area: str
    status: str
    severity: int
    evidence: str
    fix: str


@dataclass
class TrafficBucket:
    tier: str
    agent: str
    sessions: int
    product_pages: int
    checkout_hits: int
