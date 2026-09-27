"""Test suite for the component resource director.

Run: python3 -m unittest discover -s tests -v   (from the project root)

The allocator is a pure function, so the safety properties that matter on a
4-miner solo node are directly assertable here without touching the live
system. The few tests that do touch systemd are marked and skip cleanly when
no user bus is available.
"""

from __future__ import annotations

import time
import unittest

from crd.allocator import (
    MINER_WEIGHT_CEILING,
    MINER_WEIGHT_FLOOR,
    TIER_PRIORITY,
    plan_allocation,
)
from crd.config import Component, Policy, SafetyPolicy, load_policy
from crd.sampler import Sample, parse_cpu_stat

MIB = 1024 * 1024
GIB = 1024**3

HOST = {
    "ncpu": 12,
    "mem_total": 7550 * MIB,
    "mem_available": 4200 * MIB,
    "io_controlled": False,
}

# Safety envelope for a real 4-miner solo node on this box.
# 4 miners @ 1GiB = 4000Mi, plus agent + background ~= 4700Mi resident.
# budget = 7550Mi * 0.80 - 1024Mi = 5016Mi, which holds it with ~300Mi spare.
# A 0.70/1536Mi envelope does NOT hold four 1GiB miners -- see
# test_four_miners_need_a_wider_envelope_than_the_default.
RIG_SAFETY = SafetyPolicy(reserve_bytes=1024 * MIB,
                          max_managed_mem_high_fraction=0.80)


def sample(unit, cpu=0.0, mem=200 * MIB, peak=None, active=True,
           cpu_w=100, mem_high=-1, io_w=-1):
    return Sample(
        unit=unit, cgroup=f"/sys/fs/cgroup/{unit}", timestamp=time.monotonic(),
        cpu_frac=cpu, cpu_weight_current=cpu_w, mem_current=mem,
        mem_high_current=mem_high, mem_max_current=-1,
        mem_peak=peak if peak is not None else mem,
        io_weight_current=io_w, allowed_cpus=None, pids=3, active=active,
        oom_kills=0,
    )


def miner(name="miner-1"):
    # cpu_weight_min is deliberately low so the test can tell the tier floor
    # (MINER_WEIGHT_FLOOR) apart from the component's own minimum.
    return Component(unit=name, label=name, tier="mining", kind="miner",
                     cpu_weight_min=200, cpu_weight_max=8000,
                     mem_high_floor=512 * MIB, mem_high_ceiling=1200 * MIB,
                     min_interval_s=0)


def agent(name="gateway"):
    return Component(unit=name, label=name, tier="critical",
                     cpu_weight_min=300, cpu_weight_max=4000,
                     mem_high_floor=256 * MIB, mem_high_ceiling=768 * MIB,
                     min_interval_s=0)


def bg(name="indexer"):
    return Component(unit=name, label=name, tier="background",
                     cpu_weight_min=20, cpu_weight_max=150,
                     mem_high_floor=128 * MIB, mem_high_ceiling=512 * MIB,
                     min_interval_s=0)


def policy_of(*comps, safety=None):
    return Policy(components=list(comps),
                  safety=safety or SafetyPolicy(),
                  protected_units=frozenset())


# ---------------------------------------------------------------- config


class TestConfig(unittest.TestCase):
    def test_rejects_unknown_tier(self):
        with self.assertRaises(ValueError):
            Component(unit="x", label="x", tier="nonsense")

    def test_rejects_inverted_weight_bounds(self):
        with self.assertRaises(ValueError):
            Component(unit="x", label="x", tier="workload",
                      cpu_weight_min=500, cpu_weight_max=100)

    def test_rejects_ceiling_below_floor(self):
        with self.assertRaises(ValueError):
            Component(unit="x", label="x", tier="workload",
                      mem_high_floor=1024 * MIB, mem_high_ceiling=512 * MIB)

    def test_rejects_duplicate_units(self):
        with self.assertRaises(ValueError):
            Policy(components=[agent(), agent()], safety=SafetyPolicy())

    def test_rejects_managing_a_protected_unit(self):
        with self.assertRaises(ValueError) as ctx:
            Policy(components=[agent("gateway")], safety=SafetyPolicy(),
                   protected_units=frozenset({"gateway"}))
        self.assertIn("protect", str(ctx.exception))

    def test_rejects_empty_policy(self):
        with self.assertRaises(ValueError):
            Policy(components=[], safety=SafetyPolicy())

    def test_rejects_absurdly_small_reserve(self):
        with self.assertRaises(ValueError):
            SafetyPolicy(reserve_bytes=8 * MIB)

    def test_rejects_too_frequent_sampling(self):
        with self.assertRaises(ValueError):
            Policy(components=[agent()], safety=SafetyPolicy(),
                   sampling_interval_s=1)

    def test_shipped_example_policy_is_valid(self):
        p = load_policy("config/policy.example.toml")
        self.assertGreaterEqual(len(p.components), 1)
        self.assertIn("hermes-gateway.service",
                      [c.unit for c in p.components])
        # The example must not ship miners enabled that do not exist.
        self.assertEqual([c for c in p.components if c.tier == "mining"], [])


# --------------------------------------------------------------- sampling


class TestSamplerParsing(unittest.TestCase):
    def test_parses_cpu_stat(self):
        usage, periods = parse_cpu_stat(
            "usage_usec 123456\nuser_usec 100\nnr_periods 42\n")
        self.assertEqual(usage, 123456)
        self.assertEqual(periods, 42)

    def test_tolerates_empty_cpu_stat(self):
        self.assertEqual(parse_cpu_stat(""), (0, 0))

    def test_tolerates_garbage(self):
        self.assertEqual(parse_cpu_stat("usage_usec notanumber"), (0, 0))


# --------------------------------------------------------------- CPU alloc


class TestCpuAllocation(unittest.TestCase):
    def test_idle_miner_gets_no_protection_boost(self):
        # An idle miner falls to its own minimum: the mining floor is a
        # floor for *working* miners, not a permanent subsidy.
        p = policy_of(miner(), agent())
        s = {"miner-1": sample("miner-1", cpu=0.0),
             "gateway": sample("gateway", cpu=0.5)}
        plan = plan_allocation(p, s, HOST)
        m = next(a for a in plan.allocations if a.unit == "miner-1")
        self.assertLess(m.demand, 0.01)
        self.assertEqual(m.cpu_weight, 200, "idle miner was boosted by the floor")

    def test_working_miner_holds_the_floor(self):
        p = policy_of(miner(), agent(), bg())
        s = {"miner-1": sample("miner-1", cpu=1.0, cpu_w=100),
             "gateway": sample("gateway", cpu=1.0),
             "indexer": sample("indexer", cpu=0.2)}
        plan = plan_allocation(p, s, HOST)
        m = next(a for a in plan.allocations if a.unit == "miner-1")
        self.assertGreaterEqual(m.cpu_weight, MINER_WEIGHT_FLOOR,
                                "an actively mining miner was throttled below floor")

    def test_miner_never_exceeds_ceiling(self):
        # Even with nothing else running, a miner must not take the whole box.
        p = policy_of(miner())
        s = {"miner-1": sample("miner-1", cpu=12.0)}
        plan = plan_allocation(p, s, HOST)
        m = plan.allocations[0]
        self.assertLessEqual(m.cpu_weight, MINER_WEIGHT_CEILING)

    def test_critical_outranks_background_at_equal_demand(self):
        a, b = agent(), bg()
        p = policy_of(a, b)
        s = {"gateway": sample("gateway", cpu=0.4),
             "indexer": sample("indexer", cpu=0.4)}
        plan = plan_allocation(p, s, HOST)
        aw = next(x.cpu_weight for x in plan.allocations if x.unit == "gateway")
        bw = next(x.cpu_weight for x in plan.allocations if x.unit == "indexer")
        self.assertGreater(aw, bw)

    def test_busy_component_outranks_idle_one_within_a_tier(self):
        p = policy_of(bg("a"), bg("b"))
        s = {"a": sample("a", cpu=1.0), "b": sample("b", cpu=0.0)}
        plan = plan_allocation(p, s, HOST)
        aw = next(x.cpu_weight for x in plan.allocations if x.unit == "a")
        bw = next(x.cpu_weight for x in plan.allocations if x.unit == "b")
        self.assertGreater(aw, bw)

    def test_hysteresis_suppresses_small_changes(self):
        # First learn the target with hysteresis disabled, then present a
        # current value just 2% away and require that no change is made.
        def make(hyst):
            return Component(unit="svc", label="svc", tier="workload",
                            min_interval_s=0, hysteresis=hyst)
        s_idle = sample("gateway", cpu=0.0, cpu_w=100)
        s_busy = sample("svc", cpu=0.5, cpu_w=100)
        target = plan_allocation(
            policy_of(make(0.0), agent()), {"svc": s_busy, "gateway": s_idle}, HOST
        ).allocations[0].cpu_weight
        self.assertIsNotNone(target)

        cur = int(target * 1.02)
        plan = plan_allocation(
            policy_of(make(0.50), agent()),
            {"svc": sample("svc", cpu=0.5, cpu_w=cur), "gateway": s_idle}, HOST,
        )
        svc = plan.allocations[0]
        self.assertEqual(svc.cpu_weight, cur,
                         "changed despite being inside the 50% hysteresis band")
        self.assertTrue(any("hysteresis" in k for k in svc.skipped))

    def test_cooldown_suppresses_recent_change(self):
        c = Component(unit="svc", label="svc", tier="workload",
                      min_interval_s=3600, hysteresis=0.0)
        p = policy_of(c, agent())
        s = {"svc": sample("svc", cpu=0.9, cpu_w=100),
             "gateway": sample("gateway", cpu=0.0)}
        plan = plan_allocation(p, s, HOST, {"last_change": {"svc": time.time()}})
        svc = next(a for a in plan.allocations if a.unit == "svc")
        self.assertEqual(svc.cpu_weight, 100,
                         "changed a unit that is still inside its cooldown")
        self.assertTrue(any("cooldown" in k for k in svc.skipped))

    def test_inactive_unit_gets_no_action(self):
        p = policy_of(agent())
        s = {"gateway": sample("gateway", active=False, cpu_w=100)}
        plan = plan_allocation(p, s, HOST)
        self.assertEqual(plan.allocations[0].actions, [])

    def test_missing_sample_does_not_crash(self):
        p = policy_of(agent(), miner())
        plan = plan_allocation(p, {}, HOST)
        self.assertEqual(plan.allocations, [])

    def test_weights_stay_in_systemd_range(self):
        p = policy_of(agent(), miner(), bg())
        s = {c.unit: sample(c.unit, cpu=12.0) for c in p.components}
        for a in plan_allocation(p, s, HOST).allocations:
            self.assertGreaterEqual(a.cpu_weight, 1)
            self.assertLessEqual(a.cpu_weight, 10000)


# ------------------------------------------------------------ memory alloc


class TestMemorySafety(unittest.TestCase):
    def _many_bg(self, n):
        return policy_of(*[bg(f"bg{i}") for i in range(n)])

    def test_reserve_is_withheld_from_the_managed_set(self):
        # 8 background components each actually resident in 1.5Gi: the managed
        # set alone is 12Gi on a 7.4GiB box. The director must shrink the
        # non-mining ceilings and, if it still cannot fit, reject the plan.
        p = self._many_bg(8)
        s = {f"bg{i}": sample(f"bg{i}", mem=1500 * MIB, peak=1500 * MIB)
             for i in range(8)}
        plan = plan_allocation(p, s, HOST)
        self.assertFalse(plan.ok, "an impossible memory plan was accepted")
        # Everything the director could still give back is already at its floor.
        for a in plan.allocations:
            c = p.by_unit(a.unit)
            self.assertLessEqual(a.memory_high, c.mem_high_floor,
                                 f"{a.unit} was left with reclaim headroom")

    def test_memory_high_tracks_observed_peak(self):
        p = policy_of(agent())
        s = {"gateway": sample("gateway", mem=300 * MIB, peak=300 * MIB)}
        plan = plan_allocation(p, s, HOST)
        a = plan.allocations[0]
        self.assertGreater(a.memory_high, 300 * MIB)
        self.assertLessEqual(a.memory_high, 768 * MIB, "exceeded mem_high_ceiling")

    def test_no_raise_when_mem_available_is_low(self):
        p = policy_of(agent())
        host = dict(HOST, mem_available=256 * MIB)
        s = {"gateway": sample("gateway", mem=700 * MIB, peak=700 * MIB)}
        plan = plan_allocation(p, s, host)
        a = plan.allocations[0]
        self.assertEqual(a.memory_high, -1, "raised a limit under memory pressure")
        self.assertTrue(any("pause floor" in k for k in a.skipped))

    def test_never_sets_memory_max_on_a_miner(self):
        c = miner()
        p = Policy(components=[c], safety=SafetyPolicy(
            allow_memory_max_on_miners=False), protected_units=frozenset())
        c = Component(**{**c.__dict__, "mem_max_enabled": True})
        p = Policy(components=[c], safety=SafetyPolicy())
        s = {"miner-1": sample("miner-1", cpu=1.0, mem=900 * MIB)}
        plan = plan_allocation(p, s, HOST)
        a = plan.allocations[0]
        self.assertIsNone(a.memory_max, "MemoryMax was set on a miner")
        self.assertTrue(any("OOM" in k for k in a.skipped))

    def test_oversubscribed_floors_are_rejected_not_oversubscribed(self):
        # Each component resident in 2Gi: five of them cannot fit on 7.4GiB
        # no matter how the ceilings are arranged.
        comps = [Component(unit=f"s{i}", label=f"s{i}", tier="workload",
                           mem_high_floor=100 * MIB, mem_high_ceiling=1500 * MIB,
                           min_interval_s=0)
                 for i in range(5)]
        p = policy_of(*comps)
        s = {f"s{i}": sample(f"s{i}", mem=2000 * MIB, peak=2000 * MIB)
             for i in range(5)}
        plan = plan_allocation(p, s, HOST)
        self.assertFalse(plan.ok, "an over-committed plan was accepted")
        self.assertIn("over by", plan.violations[0])

    def test_absurd_reserve_rejects_the_plan(self):
        p = policy_of(agent(), safety=SafetyPolicy(
            reserve_bytes=7500 * MIB, max_managed_mem_high_fraction=0.70))
        s = {"gateway": sample("gateway", mem=300 * MIB)}
        plan = plan_allocation(p, s, HOST)
        self.assertFalse(plan.ok)
        for a in plan.allocations:
            self.assertEqual(a.memory_high, -1, "limits changed on a rejected plan")

    def test_memory_high_is_never_lowered_on_demand_alone(self):
        # memory.peak is a historical high-water mark. A service that idles
        # now may burst later; the director must not ratchet its limit down
        # and OOM-kill it when it does.
        p = policy_of(agent())
        s = {"gateway": sample("gateway", mem=100 * MIB, peak=100 * MIB,
                               mem_high=768 * MIB)}
        a = plan_allocation(p, s, HOST).allocations[0]
        self.assertEqual(a.memory_high, 768 * MIB,
                         "limit was lowered purely because usage dropped")
        self.assertTrue(any("hold" in k for k in a.skipped))

    def test_memory_high_still_raises_on_growth(self):
        p = policy_of(agent())
        s = {"gateway": sample("gateway", mem=600 * MIB, peak=600 * MIB,
                               mem_high=256 * MIB)}
        a = plan_allocation(p, s, HOST).allocations[0]
        self.assertGreater(a.memory_high, 600 * MIB,
                           "did not raise for a component that outgrew its limit")

    def test_memory_high_never_below_floor(self):
        p = policy_of(agent())
        s = {"gateway": sample("gateway", mem=1 * MIB, peak=1 * MIB)}
        a = plan_allocation(p, s, HOST).allocations[0]
        self.assertGreaterEqual(a.memory_high, 256 * MIB)


# ----------------------------------------------------------------- the mix


class TestMiningRigSafety(unittest.TestCase):
    """The scenario that matters: 4 miners + agent + desktop on 7.4GiB."""

    def build(self, safety=None):
        comps = [miner(f"miner-{i}") for i in range(1, 5)]
        comps += [agent(), bg("baloo"), bg("bhm")]
        return policy_of(*comps, safety=safety or RIG_SAFETY)

    def test_four_miners_need_a_wider_envelope_than_the_default(self):
        # Documents the arithmetic a rig owner actually needs: the default
        # 0.70/1536Mi envelope cannot hold four 1GiB miners, and the director
        # says so instead of quietly handing out unsatisfiable limits.
        p = self.build(safety=SafetyPolicy())  # the shipped defaults
        s = {f"miner-{i}": sample(f"miner-{i}", cpu=1.0, mem=1000 * MIB)
             for i in range(1, 5)}
        plan = plan_allocation(p, s, HOST)
        self.assertFalse(plan.ok)
        self.assertIn("over by", plan.violations[0])

    def test_four_miners_and_an_agent_fit_in_budget(self):
        # The realistic rig: 4 miners at 1.0Gi each + agent + 2 background
        # processes ~= 4.6Gi resident, which fits the rig safety envelope.
        p = self.build()
        s = {}
        for i in range(1, 5):
            s[f"miner-{i}"] = sample(f"miner-{i}", cpu=1.0, mem=1000 * MIB)
        s["gateway"] = sample("gateway", cpu=0.6, mem=400 * MIB)
        s["baloo"] = sample("baloo", cpu=0.3, mem=200 * MIB)
        s["bhm"] = sample("bhm", cpu=0.05, mem=100 * MIB)
        plan = plan_allocation(p, s, HOST)
        self.assertTrue(plan.ok, plan.violations)
        budget = int(HOST["mem_total"] * 0.80) - 1024 * MIB
        self.assertLessEqual(plan.total_observed_mem(), budget,
                             "rig exceeds its own memory safety budget")

    def test_all_four_miners_survive_the_allocation_pass(self):
        p = self.build()
        s = {f"miner-{i}": sample(f"miner-{i}", cpu=1.0, mem=1100 * MIB)
             for i in range(1, 5)}
        plan = plan_allocation(p, s, HOST)
        for a in plan.allocations:
            if a.tier != "mining":
                continue
            self.assertGreaterEqual(a.cpu_weight, MINER_WEIGHT_FLOOR,
                                    f"{a.unit} throttled while mining")
            self.assertLessEqual(a.memory_high, 1200 * MIB,
                                 f"{a.unit} MemoryHigh exceeded its ceiling")
            self.assertIsNone(a.memory_max, f"MemoryMax set on {a.unit}")

    def test_miners_keep_their_ceiling_under_pressure(self):
        # Deliberately over-committed: the agent's ceiling must give way, the
        # miner's must not.
        p = Policy(
            components=[miner("m1"), bg("indexer")],
            safety=SafetyPolicy(reserve_bytes=4000 * MIB,
                                max_managed_mem_high_fraction=0.70),
            protected_units=frozenset())
        s = {"m1": sample("m1", cpu=1.0, mem=2000 * MIB, peak=2000 * MIB),
             "indexer": sample("indexer", cpu=0.5, mem=2000 * MIB, peak=2000 * MIB)}
        plan = plan_allocation(p, s, HOST)
        m = next(a for a in plan.allocations if a.unit == "m1")
        i = next(a for a in plan.allocations if a.unit == "indexer")
        self.assertLessEqual(m.memory_high, 1200 * MIB,
                             "a miner's MemoryHigh was raised under pressure")
        self.assertTrue(any("over-committed" in k for k in i.skipped),
                        f"the background component absorbed no pressure: {i.skipped}")

    def test_agent_outweighs_background_at_equal_load(self):
        p = self.build()
        s = {f"miner-{i}": sample(f"miner-{i}", cpu=0.0) for i in range(1, 5)}
        s["gateway"] = sample("gateway", cpu=0.3)
        s["baloo"] = sample("baloo", cpu=0.3)
        plan = plan_allocation(p, s, HOST)
        g = next(a.cpu_weight for a in plan.allocations if a.unit == "gateway")
        b = next(a.cpu_weight for a in plan.allocations if a.unit == "baloo")
        self.assertGreater(g, b)

    def test_director_never_starves_miners_for_background_work(self):
        # Saturate the machine with background components; miners must still
        # outrank them, because tier priority is not a share budget.
        p = self.build()
        s = {f"miner-{i}": sample(f"miner-{i}", cpu=1.0) for i in range(1, 5)}
        s["baloo"] = sample("baloo", cpu=6.0)
        s["bhm"] = sample("bhm", cpu=6.0)
        plan = plan_allocation(p, s, HOST)
        for a in plan.allocations:
            if a.tier == "mining" and a.demand > 0.01:
                self.assertGreaterEqual(a.cpu_weight, MINER_WEIGHT_FLOOR)

    def test_tier_priority_table_is_the_documented_one(self):
        self.assertEqual(TIER_PRIORITY["critical"], 3.0)
        self.assertGreater(TIER_PRIORITY["mining"], TIER_PRIORITY["workload"])
        self.assertGreater(TIER_PRIORITY["workload"], TIER_PRIORITY["background"])


# ----------------------------------------------------------------- applier


class TestApplierGuards(unittest.TestCase):
    def test_dry_run_never_touches_systemd(self):
        from crd.applier import apply_plan
        p = policy_of(agent())
        s = {"gateway": sample("gateway", cpu=1.0, mem=400 * MIB, cpu_w=100)}
        plan = plan_allocation(p, s, HOST)
        res = apply_plan(plan, p, dry_run=True)
        self.assertTrue(all("(dry-run)" in v for _, _, v in res.applied))
        self.assertEqual(res.failed, [])

    def test_violating_plan_applies_nothing(self):
        from crd.applier import apply_plan
        comps = [Component(unit=f"s{i}", label=f"s{i}", tier="workload",
                           mem_high_floor=100 * MIB, mem_high_ceiling=1500 * MIB,
                           min_interval_s=0)
                 for i in range(5)]
        p = policy_of(*comps)
        s = {f"s{i}": sample(f"s{i}", mem=2000 * MIB, peak=2000 * MIB)
             for i in range(5)}
        plan = plan_allocation(p, s, HOST)
        self.assertFalse(plan.ok)
        res = apply_plan(plan, p)
        self.assertEqual(res.applied, [], "a rejected plan still touched systemd")
        self.assertEqual(res.failed, [])
        self.assertTrue(res.skipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
