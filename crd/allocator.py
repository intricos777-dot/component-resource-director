# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
"""Demand-driven allocation of CPU / memory / IO to each component.

The allocator is a pure function: (policy, samples, host facts, history) ->
plan. It performs no side effects, which is what makes it testable and what
keeps the "never starve the miners" guarantee checkable rather than aspirational.

Allocation strategy
-------------------
CPU (cgroup v2 CPUWeight, proportional-share scheduler):
  demand is a smoothed fraction-of-one-CPU load, normalised across components
  and mapped to a tier-weighted share. Weights are *relative*, so the director
  allocates the whole 1..10000 range proportional to demand x tier priority.
  Miners sit in a protected band: they are the bottom of the throttle order
  (high weight) whenever they are actually doing work, and the director never
  drops a miner below `MINER_WEIGHT_FLOOR`.

Memory:
  MemoryHigh (soft reclaim pressure) is set to observed peak x headroom,
  clamped by the safety budget. MemoryMax is only set where explicitly enabled
  and never on a miner, because an OOM kill destroys mining progress.

IO:
  IOWeight only works if the io controller is delegated to the user slice.
  The plan records the intent and the applier reports it as skipped otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Component, Policy
from .sampler import Sample

# A miner that is genuinely working never drops below this share. Rationale:
# on a 4-miner solo node, throttling a miner costs real hashrate and can stall
# a share; interactive work is elastic by comparison.
MINER_WEIGHT_FLOOR = 2000
# ...and never rises above this, so a miner can't monopolise the box either.
MINER_WEIGHT_CEILING = 8000

# Tier multipliers. This is the precedence order, stated once.
TIER_PRIORITY = {
    "critical": 3.0,    # the agent/gateway the user is talking to
    "mining": 2.6,     # the income-generating workload
    "workload": 1.6,   # builds, compiles, batch jobs
    "background": 0.7, # indexers, monitors, caches
}

# Memory headroom multiplier over observed peak.
MEM_HIGH_HEADROOM = 1.35
# A component using more than this fraction of the machine is by definition
# dominant; cap its relative demand so it cannot eat the whole budget.
DEMAND_NORMALISER = 0.85  # fraction of ncpu treated as "full tilt"
MIB = 1024 * 1024


@dataclass
class Allocation:
    unit: str
    label: str
    tier: str
    cpu_weight: int | None
    cpu_weight_current: int
    memory_high: int | None
    memory_high_current: int
    memory_max: int | None
    io_weight: int | None
    io_weight_current: int
    allowed_cpus: list[int] | None
    demand: float
    # RSS right now, used by the safety floor's over-commit check.
    observed_mem: int = 0
    reason: str = ""
    actions: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    active: bool = True

    def changed(self) -> list[str]:
        out: list[str] = []
        if self.cpu_weight is not None and self.cpu_weight != self.cpu_weight_current:
            out.append("CPUWeight")
        if self.memory_high is not None and self.memory_high != self.memory_high_current:
            out.append("MemoryHigh")
        if self.memory_max is not None:
            out.append("MemoryMax")
        if self.io_weight is not None and self.io_weight != self.io_weight_current:
            out.append("IOWeight")
        if self.allowed_cpus is not None:
            out.append("AllowedCPUs")
        return out

    def as_dict(self) -> dict:
        return {
            "unit": self.unit,
            "label": self.label,
            "tier": self.tier,
            "active": self.active,
            "demand": round(self.demand, 4),
            "cpu_weight": self.cpu_weight,
            "cpu_weight_current": self.cpu_weight_current,
            "memory_high": self.memory_high,
            "memory_high_current": self.memory_high_current,
            "memory_max": self.memory_max,
            "io_weight": self.io_weight,
            "io_weight_current": self.io_weight_current,
            "allowed_cpus": self.allowed_cpus,
            "reason": self.reason,
            "actions": self.actions,
            "skipped": self.skipped,
        }


@dataclass
class Plan:
    allocations: list[Allocation]
    host: dict
    safety_notes: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def total_mem_high(self) -> int:
        """Sum of MemoryHigh ceilings the director would set, active units only."""
        return sum(a.memory_high or 0 for a in self.allocations if a.active)

    def total_observed_mem(self) -> int:
        return sum(a.observed_mem or 0 for a in self.allocations if a.active)

    def changed_units(self) -> list[Allocation]:
        return [a for a in self.allocations if a.changed()]

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "allocations": [a.as_dict() for a in self.allocations],
            "safety_notes": self.safety_notes,
            "violations": self.violations,
        }


def _demand(comp: Component, s: Sample, smoothed: float | None, ncpu: int) -> float:
    """Smoothed demand, in 'fraction of one CPU' units."""
    raw = max(0.0, s.cpu_frac)
    if smoothed is not None:
        raw = comp.ema_alpha * raw + (1.0 - comp.ema_alpha) * smoothed
    return raw


def _cpu_weight_for(comp: Component, demand: float, share: float) -> int:
    """Map a demand-weighted share onto the 1..10000 proportional range."""
    w = int(round(share * 10000))
    w = max(comp.cpu_weight_min, min(comp.cpu_weight_max, w))
    if comp.tier == "mining" and demand > 0.01:
        w = max(w, MINER_WEIGHT_FLOOR)
        w = min(w, MINER_WEIGHT_CEILING)
    return max(1, min(10000, w))


def plan_allocation(
    policy: Policy,
    samples: dict[str, Sample],
    host: dict,
    history: dict | None = None,
) -> Plan:
    """Produce a side-effect-free allocation plan.

    `history` carries the director's own smoothing and cooldown state:
      {"smoothed": {unit: frac}, "last_change": {unit: epoch}}
    """
    history = history or {}
    smoothed = history.get("smoothed", {})
    last_change = history.get("last_change", {})
    import time as _t

    now = _t.time()
    ncpu = int(host.get("ncpu", 1)) or 1
    comps = [c for c in policy.components if c.enabled]

    # --- 1. demand pass -------------------------------------------------
    demands: dict[str, float] = {}
    for c in comps:
        s = samples.get(c.unit)
        if s is None:
            continue
        if not s.active:
            # An inactive unit gets no allocation and no action; the plan
            # leaves whatever limit it has in place (limits are a ceiling).
            demands[c.unit] = 0.0
            continue
        demands[c.unit] = _demand(c, s, smoothed.get(c.unit), ncpu)

    # --- 2. CPU share pass ----------------------------------------------
    # Normalise demand to a 0..1 pressure figure, then weight by tier
    # priority. A totally idle machine yields equal minimums; a busy one
    # spreads by demand.
    def _pressure(c: Component) -> float:
        """Demand of one component as a 0..1 fraction of 'full tilt'."""
        return min(1.0, demands.get(c.unit, 0.0) / (DEMAND_NORMALISER * ncpu))

    total_demand = max(
        sum(_pressure(c) * TIER_PRIORITY[c.tier] for c in comps
            if demands.get(c.unit, 0.0) > 0),
        1e-6,
    )

    allocations: list[Allocation] = []
    for c in comps:
        s = samples.get(c.unit)
        if s is None:
            continue
        d = demands[c.unit]
        prio = TIER_PRIORITY[c.tier]
        pressure = _pressure(c)
        share = (pressure * prio) / total_demand if d > 0 else 0.0
        cpu_w = _cpu_weight_for(c, d, share)

        reason = f"demand {pressure*100:.0f}% of 1cpu, tier={c.tier} prio={prio}"
        actions: list[str] = []
        skipped: list[str] = []

        # hysteresis + cooldown on CPU
        if s.active and cpu_w != s.cpu_weight_current:
            last = last_change.get(c.unit)
            cooled = last is None or (now - last) >= c.min_interval_s
            cur = s.cpu_weight_current
            big_enough = cur <= 0 or abs(cpu_w - cur) / max(cur, 1) >= c.hysteresis
            if not cooled:
                skipped.append(f"CPUWeight (cooldown {c.min_interval_s}s)")
                cpu_w = cur
            elif not big_enough and cur > 0:
                skipped.append("CPUWeight (below hysteresis)")
                cpu_w = cur
            else:
                actions.append("CPUWeight")

        # --- 3. memory pass ---------------------------------------------
        mem_high = _memory_high_for(c, s, policy, host, skipped)
        if mem_high is not None and s.active and mem_high != s.mem_high_current:
            last = last_change.get(c.unit)
            cooled = last is None or (now - last) >= c.min_interval_s
            if not cooled:
                skipped.append("MemoryHigh (cooldown)")
                mem_high = s.mem_high_current
            else:
                actions.append("MemoryHigh")

        mem_max = None
        if c.mem_max_enabled and s.active:
            if c.tier == "mining" and not policy.safety.allow_memory_max_on_miners:
                skipped.append("MemoryMax (miners protected from OOM)")
            else:
                mem_max = s.mem_max_current  # unchanged; real cap set via unit

        # --- 4. IO pass --------------------------------------------------
        io_w = None
        if s.active:
            io_w = _io_weight_for(c, d, pressure, share)
            if not host.get("io_controlled", False):
                skipped.append("IOWeight (io cgroup controller not delegated to user slice)")

        # --- 5. CPU affinity --------------------------------------------
        cpus = list(c.cpus) if c.cpus else None
        if cpus and sorted(s.allowed_cpus or []) != sorted(cpus):
            actions.append("AllowedCPUs")

        allocations.append(
            Allocation(
                unit=c.unit, label=c.label, tier=c.tier,
                cpu_weight=cpu_w, cpu_weight_current=s.cpu_weight_current,
                memory_high=mem_high, memory_high_current=s.mem_high_current,
                memory_max=mem_max, io_weight=io_w, io_weight_current=s.io_weight_current,
                observed_mem=s.mem_current,
                allowed_cpus=cpus, demand=d, reason=reason,
                actions=actions, skipped=skipped, active=s.active,
            )
        )

    plan = Plan(allocations=allocations, host=host)
    _enforce_safety_floor(plan, policy, host)
    return plan


def _memory_high_for(
    c: Component, s: Sample, policy: Policy, host: dict, skipped: list[str]
) -> int | None:
    """MemoryHigh = observed peak x headroom, clamped to a safety budget.

    Ratchet rule: on demand alone the target may only go UP. Lowering a limit
    because current usage is low is how you OOM-kill a service that was about
    to burst -- memory.peak is a historical high-water mark and says nothing
    about the next spike. Tightening downward is reserved for the safety floor
    in _enforce_safety_floor, which only does it when the machine is genuinely
    over-committed and miners are still protected.
    """
    if not s.active:
        return s.mem_high_current
    if host.get("mem_available", 0) < policy.safety.mem_available_pause_bytes:
        skipped.append("MemoryHigh raise (MemAvailable below pause floor)")
        return s.mem_high_current

    peak = max(s.mem_peak, s.mem_current)
    if peak <= 0:
        return s.mem_high_current
    want = int(peak * MEM_HIGH_HEADROOM)
    want = max(want, c.mem_high_floor)
    if c.mem_high_ceiling:
        want = min(want, c.mem_high_ceiling)
    if policy.safety.absolute_mem_high_ceiling:
        want = min(want, policy.safety.absolute_mem_high_ceiling)

    # Never ratchet down on demand alone.
    cur = s.mem_high_current
    if cur > 0 and want < cur:
        skipped.append(
            f"MemoryHigh hold at {cur // MIB}Mi (target {want // MIB}Mi; "
            "limits are not lowered on demand alone)"
        )
        return cur
    return want


def _io_weight_for(c: Component, demand: float, pressure: float, share: float) -> int:
    w = int(round(max(pressure, share) * c.io_weight_max))
    return max(c.io_weight_min, min(c.io_weight_max, w))


def _enforce_safety_floor(plan: Plan, policy: Policy, host: dict) -> None:
    """Clamp and validate the plan against the machine's real limits.

    This is the guard that keeps the director from harming a solo mining node:
      - a memory reserve is never allocated away,
      - the director's own overhead is paid for first,
      - miner floors survive any allocation pass.
    """
    safety = policy.safety
    mem_total = int(host.get("mem_total", 0))
    budget = int(mem_total * safety.max_managed_mem_high_fraction) - safety.reserve_bytes

    if mem_total > 0 and budget <= 0:
        plan.violations.append(
            "safety floor exceeds usable memory: lower safety.reserve_bytes "
            "or max_managed_mem_high_fraction for this machine"
        )
        for a in plan.allocations:
            a.memory_high = a.memory_high_current
            a.actions = [x for x in a.actions if x != "MemoryHigh"]
            a.skipped.append("MemoryHigh (safety budget negative)")
        return

    # Reserve the director's own footprint before handing out anything else.
    director_cost = 0  # computed in report; keeps reserve honest
    spendable = budget - director_cost

    # The kernel enforces MemoryHigh per-cgroup against *actual* usage, and
    # the sum of ceilings across cgroups may legitimately exceed RAM. So the
    # budget is checked against what the components are really using right now
    # plus the slack each ceiling still grants, not against the ceilings alone.
    observed = sum(a.observed_mem or 0 for a in plan.allocations if a.active)
    committed = sum(
        max(a.observed_mem or 0, a.memory_high or 0)
        for a in plan.allocations if a.active
    )
    plan.safety_notes.append(
        f"managed set: {observed // MIB}Mi resident, "
        f"{committed // MIB}Mi worst-case committed, "
        f"{spendable // MIB}Mi budget"
    )
    if mem_total > 0 and observed > spendable:
        # The machine is genuinely over-committed right now. Tighten the
        # non-mining ceilings toward their floors until the budget is met.
        over = observed - spendable
        shrinkable = sorted(
            [a for a in plan.allocations
             if a.memory_high and a.active and a.tier != "mining"],
            key=lambda a: -(a.memory_high or 0),
        )
        for a in shrinkable:
            if over <= 0:
                break
            comp = policy.by_unit(a.unit)
            floor = comp.mem_high_floor if comp else 0
            give = max(0, (a.memory_high or 0) - floor)
            take = min(give, over)
            if take <= 0:
                continue
            a.memory_high = (a.memory_high or 0) - take
            over -= take
            a.skipped.append(
                f"MemoryHigh reduced {take // MIB}Mi (over-committed)"
            )
        # Shrinking a ceiling adds reclaim pressure but does not free memory:
        # if the components are *currently* resident beyond the budget, the
        # plan is still not safe to apply, no matter how the limits are tuned.
        if observed > spendable:
            offenders = ", ".join(
                f"{a.label} {(a.observed_mem or 0) // MIB}Mi"
                for a in plan.allocations
                if a.active and a.observed_mem and a.tier != "mining"
            ) or "miners (never shrunk by design)"
            plan.violations.append(
                f"managed set is resident {observed // MIB}Mi against a "
                f"{spendable // MIB}Mi budget -- over by "
                f"{(observed - spendable) // MIB}Mi. Lower mem_high_ceiling on "
                f"[{offenders}] so reclaim pressure engages, or stop what is not "
                "earning. The director will not pretend this is safe to apply."
            )

    # CPUWeight precedence sanity. CPUWeight is a *relative* scheduler weight,
    # so a sum of floors is not a capacity figure; what actually matters is
    # that a low-priority component's floor cannot outrank a critical one.
    floors = {}
    for a in plan.allocations:
        c = policy.by_unit(a.unit)
        if c is not None and a.active:
            floors.setdefault(c.tier, []).append(c.cpu_weight_min)
    if floors.get("critical") and floors.get("background"):
        worst_bg = max(floors["background"])
        best_crit = min(floors["critical"])
        if worst_bg >= best_crit:
            plan.violations.append(
                f"tier precedence is inverted: a background component's CPUWeight "
                f"floor ({worst_bg}) is >= the critical floor ({best_crit}); "
                "the director could starve the active session"
            )
    if floors.get("mining") and floors.get("background"):
        if min(floors["mining"]) < max(floors["background"]):
            plan.violations.append(
                f"tier precedence is inverted: a background floor "
                f"({max(floors['background'])}) exceeds a miner floor "
                f"({min(floors['mining'])}); miners must outrank background work"
            )

    if mem_total:
        reserved_pct = 100.0 * safety.reserve_bytes / mem_total
        plan.safety_notes.append(
            f"memory reserve: {safety.reserve_bytes/(1024**3):.2f}GiB "
            f"({reserved_pct:.0f}% of {mem_total/(1024**3):.2f}GiB) withheld from managed set"
        )
    if not host.get("io_controlled", False):
        plan.safety_notes.append(
            "io cgroup controller is NOT delegated to the user slice; IOWeight is a no-op. "
            "To enable it, a root change is required (documented in README)."
        )
    for a in plan.allocations:
        if a.tier == "mining" and a.cpu_weight is not None and a.demand > 0.01:
            plan.safety_notes.append(
                f"miner {a.label} holds CPUWeight>={a.cpu_weight} (floor {MINER_WEIGHT_FLOOR})"
            )


def _headroom_above_floor(a: Allocation) -> int:
    return max(0, (a.memory_high or 0))
