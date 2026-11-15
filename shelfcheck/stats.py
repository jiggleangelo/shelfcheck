import math
import statistics
from .models import BrandShare
from .sampler import compile_aliases, extract_mentions


def wilson(successes, total, z=1.96):
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    den = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / den
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(runs, brands):
    result = []
    aliases = compile_aliases(brands)
    for surface in sorted({run.surface for run in runs}):
        selected = [run for run in runs if run.surface == surface]
        mention_rows = [(run, extract_mentions(run, compiled=aliases)[0]) for run in selected]
        for brand in brands:
            ranks = [found[brand.name] for _, found in mention_rows if brand.name in found]
            wilson_interval = wilson(len(ranks), len(selected))
            grouped = {}
            for run, found in mention_rows:
                grouped.setdefault(run.prompt_id, []).append(1.0 if brand.name in found else 0.0)
            means = [sum(values) / len(values) for values in grouped.values()]
            clustered = None
            if means:
                mean = sum(means) / len(means)
                half = (1.96 * statistics.stdev(means) / math.sqrt(len(means))) if len(means) > 1 else 0.0
                clustered = (max(0.0, mean - half), min(1.0, mean + half))
            low, high = wilson_interval
            if clustered and clustered[1] - clustered[0] > high - low:
                low, high = clustered
            enough = len(ranks) >= 10
            result.append(BrandShare(
                brand.name, surface, len(ranks), len(selected),
                len(ranks) / len(selected) if selected else 0.0,
                low, high,
                statistics.fmean(ranks) if enough else None,
                sum(rank == 1 for rank in ranks) if enough else None))
    return result


def comparison(a, b):
    return f"{a.brand} ahead of {b.brand}" if a.ci_low > b.ci_high else (
        f"{b.brand} ahead of {a.brand}" if b.ci_low > a.ci_high else
        "not distinguishable at this sample size")


def change_flag(current, prior):
    if prior is None:
        return "no prior week"
    changed = abs(current.share - prior.share) >= .05
    excluded = not (current.ci_low <= prior.share <= current.ci_high)
    return "change flagged" if changed and excluded else "stable within noise"
