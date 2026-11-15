import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from .models import Prompt


class BudgetExceeded(RuntimeError):
    pass


def build_prompts(config):
    path = Path(__file__).with_name("data") / "prompts_template.json"
    templates = json.loads(path.read_text(encoding="utf-8"))
    return [Prompt(f"p{i:02d}", text.format(category=config.category,
                                            price_band=config.price_band))
            for i, text in enumerate(templates, 1)]


def run_sample(config, clients):
    prompts = build_prompts(config)
    by_surface = {client.surface: client for client in clients}
    planned = len(prompts) * len(config.surfaces) * config.repeats
    if planned > config.max_runs_per_week:
        raise BudgetExceeded(f"planned {planned} runs exceeds budget {config.max_runs_per_week}")
    missing = set(config.surfaces) - set(by_surface)
    if missing:
        raise ValueError(f"missing clients: {', '.join(sorted(missing))}")
    output = []
    for prompt in prompts:
        for surface in config.surfaces:
            for repeat in range(config.repeats):
                raw = f"{surface}|{prompt.prompt_id}|{repeat}".encode()
                seed = int(hashlib.sha256(raw).hexdigest()[:8], 16)
                run = by_surface[surface].run(prompt.text, seed=seed)
                output.append(replace(run, surface=surface,
                                      prompt_id=prompt.prompt_id, repeat=repeat))
    return output


def compile_aliases(brands):
    aliases = [(alias, brand.name,
                re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", re.I))
               for brand in brands for alias in brand.aliases]
    aliases.sort(key=lambda item: len(item[0]), reverse=True)
    return tuple(aliases)


def extract_mentions(run, brands=None, compiled=None):
    aliases = compiled if compiled is not None else compile_aliases(brands or ())
    found = {}
    ambiguous = 0
    for rank, recommendation in enumerate(run.recommended, 1):
        matches = [(alias, brand) for alias, brand, pattern in aliases
                   if pattern.search(recommendation)]
        if len({brand for _, brand in matches}) > 1:
            ambiguous += 1
        if matches:
            found.setdefault(matches[0][1], rank)
    return found, ambiguous
