# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Copyright (c) 2026 Living Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Licensed under Living Sin Proprietary License (LSPL-1.0)
# See LICENSE file for details.
"""Configuration loading and validation for the Component Resource Director.

Policy lives in a single TOML file (stdlib ``tomllib``, no third-party deps).
Everything the director is allowed to touch must be declared there; the
director never discovers and mutates units implicitly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_POLICY = Path("~/.config/component-resource-director/policy.toml").expanduser()

# systemd cgroup knobs this director knows how to reason about. Anything else
# in a component's `limits` block is rejected loudly rather than passed to
# systemd verbatim.
SUPPORTED_KEYS = frozenset(
    {"CPUWeight", "MemoryHigh", "MemoryMax", "IOWeight", "AllowedCPUs"}
)

TIERS = ("critical", "workload", "background", "mining")


@dataclass(frozen=True)
class Component:
    """One managed systemd user unit and the class of work it represents."""

    unit: str
    label: str
    tier: str
    kind: str = "service"  # "service" | "miner" | "desktop"
    cpu_weight_min: int = 1
    cpu_weight_max: int = 10000
    mem_high_floor: int = 64 * 1024 * 1024
    mem_high_ceiling: int | None = None
    mem_max_enabled: bool = False
    io_weight_min: int = 1
    io_weight_max: int = 10000
    cpus: list[int] | None = None  # AllowedCPUs, e.g. [8, 9, 10, 11]
    # Per-component EWMA smoothing for the observed demand signal.
    ema_alpha: float = 0.3
    # Never change this unit more than once per N seconds.
    min_interval_s: int = 60
    # Fraction change required before a new value is applied (anti-thrash).
    hysteresis: float = 0.20
    enabled: bool = True
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(
                f"component {self.unit!r}: tier must be one of {TIERS}, got {self.tier!r}"
            )
        if not 1 <= self.cpu_weight_min <= self.cpu_weight_max <= 10000:
            raise ValueError(f"component {self.unit!r}: invalid CPUWeight bounds")
        if not 1 <= self.io_weight_min <= self.io_weight_max <= 10000:
            raise ValueError(f"component {self.unit!r}: invalid IOWeight bounds")
        if not 0.0 < self.ema_alpha <= 1.0:
            raise ValueError(f"component {self.unit!r}: ema_alpha must be in (0, 1]")
        if not 0.0 <= self.hysteresis < 1.0:
            raise ValueError(f"component {self.unit!r}: hysteresis must be in [0, 1)")
        if self.mem_high_floor < 0:
            raise ValueError(f"component {self.unit!r}: mem_high_floor must be >= 0")
        if self.mem_high_ceiling is not None and self.mem_high_ceiling < self.mem_high_floor:
            raise ValueError(f"component {self.unit!r}: mem_high_ceiling < mem_high_floor")
        if self.min_interval_s < 0:
            raise ValueError(f"component {self.unit!r}: min_interval_s must be >= 0")
        if self.cpus is not None and not self.cpus:
            raise ValueError(f"component {self.unit!r}: cpus=[] is meaningless; omit the key")


@dataclass(frozen=True)
class SafetyPolicy:
    """Hard limits on total director authority. These are not negotiable by
    the allocator; violating them makes a plan invalid rather than clamped."""

    # Bytes never handed out to managed components. Covers the desktop
    # compositor, Xwayland, page cache headroom and the director itself.
    reserve_bytes: int = 1536 * 1024 * 1024
    # Fraction of total RAM the managed set may hold MemoryHigh on at once.
    max_managed_mem_high_fraction: float = 0.70
    # Below this MemAvailable the director refuses to *raise* any MemoryHigh.
    mem_available_pause_bytes: int = 1024 * 1024 * 1024
    # Sum of CPUWeight floor across components. If a plan's floor sum exceeds
    # this, the plan is rejected as under-specified for this machine.
    max_floor_weight_sum: int = 4000
    # A miner is never allowed a MemoryMax here: an OOM kill loses work.
    allow_memory_max_on_miners: bool = False
    # Absolute ceiling on any single component's MemoryHigh.
    absolute_mem_high_ceiling: int | None = None

    def __post_init__(self) -> None:
        if self.reserve_bytes < 256 * 1024 * 1024:
            raise ValueError("reserve_bytes below 256MiB is unsafe for a desktop session")
        if not 0.05 <= self.max_managed_mem_high_fraction <= 0.95:
            raise ValueError("max_managed_mem_high_fraction must be in [0.05, 0.95]")
        if self.mem_available_pause_bytes < 0:
            raise ValueError("mem_available_pause_bytes must be >= 0")


@dataclass(frozen=True)
class Policy:
    components: list[Component]
    safety: SafetyPolicy
    # Units the director must never touch, even if a component names them.
    protected_units: frozenset[str] = frozenset()
    sampling_interval_s: int = 15
    persist: bool = True
    source_path: str = "<memory>"

    def by_unit(self, unit: str) -> Component | None:
        for c in self.components:
            if c.unit == unit:
                return c
        return None

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError(
                "policy defines no components; refusing to run a no-op director"
            )
        seen: set[str] = set()
        for c in self.components:
            if c.unit in seen:
                raise ValueError(f"duplicate component unit: {c.unit!r}")
            seen.add(c.unit)
            if c.unit in self.protected_units:
                raise ValueError(
                    f"component {c.unit!r} is listed in protected_units; "
                    "the director may not manage a unit it also protects"
                )
        if self.sampling_interval_s < 5:
            raise ValueError("sampling_interval_s below 5 risks adding measurable overhead")


def _int_bytes(v: Any, key: str, unit: str) -> int:
    """Parse a TOML size that may be an int (bytes) or a string like '512M'."""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().upper()
        mult = 1
        if s.endswith("B"):
            s = s[:-1]
        for suffix, m in (("K", 1024), ("M", 1024**2), ("G", 1024**3), ("T", 1024**4)):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                mult = m
                break
        try:
            return int(float(s) * mult)
        except ValueError as exc:
            raise ValueError(f"{unit}: cannot parse size {v!r} for {key}") from exc
    raise ValueError(f"{unit}: size for {key} must be int or string, got {type(v).__name__}")


def load_policy(path: str | Path | None = None) -> Policy:
    p = Path(path).expanduser() if path else DEFAULT_POLICY
    if not p.exists():
        raise FileNotFoundError(
            f"policy file not found: {p}\n"
            "Create it from the example: "
            "cp /home/livings/Projects/component-resource-director/"
            "config/policy.example.toml ~/.config/component-resource-director/policy.toml"
        )
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    safety_raw = raw.get("safety", {})
    for k in ("reserve_bytes", "mem_available_pause_bytes", "absolute_mem_high_ceiling"):
        if k in safety_raw:
            safety_raw[k] = _int_bytes(safety_raw[k], k, "safety")
    safety = SafetyPolicy(**safety_raw)

    global_raw = raw.get("director", {})
    components: list[Component] = []
    for entry in raw.get("components", []):
        unknown = set(entry) - set(Component.__dataclass_fields__)
        if unknown:
            raise ValueError(f"component {entry.get('unit')!r}: unknown keys {sorted(unknown)}")
        for k in ("mem_high_floor", "mem_high_ceiling"):
            if k in entry:
                entry[k] = _int_bytes(entry[k], k, str(entry.get("unit")))
        for k in ("cpu_weight_min", "cpu_weight_max", "io_weight_min", "io_weight_max",
                  "min_interval_s"):
            if k in entry:
                entry[k] = int(entry[k])
        components.append(Component(**entry))

    return Policy(
        components=components,
        safety=safety,
        protected_units=frozenset(raw.get("protected_units", [])),
        sampling_interval_s=int(global_raw.get("sampling_interval_s", 15)),
        persist=bool(global_raw.get("persist", True)),
        source_path=str(p),
    )
