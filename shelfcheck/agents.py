import random
from typing import Protocol
from .models import AgentRun


class AgentClient(Protocol):
    surface: str
    def run(self, prompt: str, *, seed: int) -> AgentRun: ...


class LiveClientNotConfigured(RuntimeError):
    def __init__(self):
        super().__init__("live clients are not configured; see README live-use limitations")


class FakeAgentClient:
    def __init__(self, surface, catalog, weights, decoy="Northfield Outdoor"):
        self.surface = surface
        self.catalog = tuple(catalog)
        self.weights = tuple(weights)
        self.decoy = decoy

    def run(self, prompt, *, seed):
        rng = random.Random(seed)
        names = list(self.catalog)
        weights = list(self.weights)
        chosen = []
        for _ in range(min(rng.randint(3, 5), len(names))):
            name = rng.choices(names, weights=weights, k=1)[0]
            index = names.index(name)
            chosen.append(name)
            names.pop(index)
            weights.pop(index)
        if chosen and rng.random() < .10:
            chosen[rng.randrange(len(chosen))] = self.decoy
        urls = tuple(f"https://example.invalid/products/{i}" for i in range(len(chosen)))
        return AgentRun(self.surface, "", 0, tuple(chosen), urls)
