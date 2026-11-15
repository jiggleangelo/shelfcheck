import unittest
from dataclasses import replace
from pathlib import Path
from shelfcheck.agents import FakeAgentClient
from shelfcheck.config import load_config
from shelfcheck.models import AgentRun, Brand
from shelfcheck.sampler import BudgetExceeded, extract_mentions, run_sample

ROOT = Path(__file__).parent.parent


class Spy:
    surface = "muse"
    calls = 0
    def run(self, prompt, *, seed):
        self.calls += 1
        return AgentRun("muse", "", 0, ())


class SamplerTests(unittest.TestCase):
    def test_deterministic_500(self):
        cfg = load_config(ROOT/"fixtures/brand_demo.json")
        catalog = [cfg.primary.name] + [x.name for x in cfg.competitors]
        clients = [FakeAgentClient(x, catalog, [4, 3, 2, 1]) for x in cfg.surfaces]
        first = run_sample(cfg, clients)
        second = run_sample(cfg, clients)
        self.assertEqual(500, len(first)); self.assertEqual(first, second)

    def test_budget_before_calls(self):
        cfg = replace(load_config(ROOT/"fixtures/brand_demo.json"), surfaces=("muse",), max_runs_per_week=1)
        spy = Spy()
        with self.assertRaises(BudgetExceeded): run_sample(cfg, [spy])
        self.assertEqual(0, spy.calls)

    def test_boundaries_and_ambiguity(self):
        brands = (Brand("Northfield", ("Northfield",), "a"), Brand("Outdoor", ("Northfield Outdoor",), "b"))
        found, ambiguous = extract_mentions(AgentRun("x", "p", 0, ("Northfielder", "Northfield Outdoor")), brands)
        self.assertNotIn("Northfield", found)
        self.assertEqual(found["Outdoor"], 2); self.assertEqual(1, ambiguous)
