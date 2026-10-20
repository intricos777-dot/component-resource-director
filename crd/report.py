# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Copyright (c) 2026 Living Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Licensed under Living Sin Proprietary License (LSPL-1.0)
# See LICENSE file for details.
"""Human-readable reporting. Kept separate from the planner so `report` can
run with zero chance of mutating anything."""

from __future__ import annotations

from .allocator import Plan
from .sampler import Sample

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024


def _mib(b: int | None) -> str:
    if b is None:
        return "-"
    if b < 0 or b >= 2**63 - 1:
        return "inf"
    if b >= GIB:
        return f"{b / GIB:.2f}Gi"
    if b >= MIB:
        return f"{b // MIB}Mi"
    return f"{b}B"


def _weight(w: int | None) -> str:
    return "-" if w is None else str(w)


def render_report(plan: Plan, samples: dict[str, Sample], samples_raw: dict) -> str:
    h = plan.host
    L: list[str] = []
    L.append("=" * 78)
    L.append("COMPONENT RESOURCE DIRECTOR -- observation + allocation plan")
    L.append("=" * 78)
    L.append(
        f"host: {h.get('ncpu')} cpus | "
        f"RAM {h.get('mem_total', 0) // GIB}.{h.get('mem_total', 0) % GIB // (100 * MIB)}Gi "
        f"total / {h.get('mem_available', 0) // MIB}Mi available | "
        f"swap {h.get('swap_total', 0) // MIB}Mi"
    )
    ctrls = ",".join(h.get("user_subtree_control", [])) or "(none)"
    L.append(f"user slice cgroup controllers: {ctrls}")
    L.append("")

    L.append(f"{'COMPONENT':<26} {'TIER':<11} {'CPU%':>6} {'CPUWeight':>12} {'RSS':>9} {'MemoryHigh':>11}")
    L.append("-" * 78)
    ncpu = max(1, int(h.get("ncpu", 1)))
    for a in plan.allocations:
        s = samples.get(a.unit)
        rss = s.mem_current if s else 0
        pct = 100.0 * (a.demand / ncpu) if a.demand else 0.0
        cpu_cell = f"{a.cpu_weight_current}->{_weight(a.cpu_weight)}"
        if a.memory_high is not None and a.memory_high != a.memory_high_current:
            mem_cell = f"{_mib(a.memory_high_current)}->{_mib(a.memory_high)}"
        else:
            mem_cell = _mib(a.memory_high_current)
        L.append(
            f"{a.label[:25]:<26} {a.tier:<11} {pct:>5.1f}% {cpu_cell:>12} "
            f"{_mib(rss):>9} {mem_cell:>11}"
        )
        if a.allowed_cpus:
            L.append(f"{'':<26} affinity: cpus {a.allowed_cpus}")
        if s and s.gpu_mem_mib:
            L.append(f"{'':<26} gpu: {s.gpu_mem_mib}Mi VRAM")
    L.append("")

    changed = plan.changed_units()
    if changed:
        L.append("PLANNED CHANGES")
        for a in changed:
            for prop, cur, new in (
                ("CPUWeight", a.cpu_weight_current, a.cpu_weight),
                ("MemoryHigh", a.memory_high_current, a.memory_high),
                ("IOWeight", a.io_weight_current, a.io_weight),
            ):
                if new is not None and new != cur:
                    L.append(
                        f"  {a.label:<24} {prop:<12} {_weight(cur)} -> {_weight(new)}   [{a.reason}]"
                    )
            if a.allowed_cpus and sorted(a.allowed_cpus) != sorted(
                samples[a.unit].allowed_cpus or []
            ):
                L.append(f"  {a.label:<24} AllowedCPUs  -> {a.allowed_cpus}")
    else:
        L.append("PLANNED CHANGES: none (steady state)")
    L.append("")

    sk = [(a, s) for a in plan.allocations for s in a.skipped]
    if sk:
        L.append("SKIPPED / DEFERRED")
        for a, s in sk:
            L.append(f"  {a.label:<24} {s}")
        L.append("")

    if plan.safety_notes:
        L.append("SAFETY NOTES")
        for n in plan.safety_notes:
            L.append(f"  - {n}")
        L.append("")

    if plan.violations:
        L.append("!! SAFETY VIOLATIONS -- PLAN REJECTED, NOTHING WILL BE APPLIED")
        for v in plan.violations:
            L.append(f"  !! {v}")
    else:
        L.append("Safety floor: satisfied. Managed MemoryHigh total "
                 f"{_mib(plan.total_mem_high())} within budget.")
    L.append("=" * 78)
    return "\n".join(L)
