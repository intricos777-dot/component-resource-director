# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Copyright (c) 2026 Living Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Licensed under Living Sin Proprietary License (LSPL-1.0)
# See LICENSE file for details.
"""Apply a plan to systemd, or explain why it cannot be applied.

Two application modes, deliberately separated:

  runtime  (default) `systemctl --user set-property --runtime <unit> ...`
           Writes drop-ins under /run/user/$UID/systemd/user.control/. Survives
           until reboot or an explicit `revert`. This is the safe default: a
           bad plan self-heals at next login.

  persist            Writes drop-ins under ~/.config/systemd/user/<unit>.d/.
           Survives reboot. Requires policy.director.persist = true AND an
           explicit --persist flag, so a typo in a policy can never silently
           become permanent.

Nothing here invokes sudo. Anything needing root is reported, not performed.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .allocator import Allocation, Plan
from .config import Policy

USER_UNIT_DIR = Path("~/.config/systemd/user").expanduser()
# The director's own overhead budget, in bytes, withheld from the managed set
# so monitoring can never be the thing that OOMs or starves the miners.
DIRECTOR_FOOTPRINT = 48 * 1024 * 1024


@dataclass
class ApplyResult:
    applied: list[tuple[str, str, str]] = field(default_factory=list)  # unit, prop, value
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    needs_root: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.needs_root

    def as_dict(self) -> dict:
        return {
            "applied": [{"unit": u, "prop": p, "value": v} for u, p, v in self.applied],
            "failed": [{"unit": u, "prop": p, "error": v} for u, p, v in self.failed],
            "skipped": self.skipped,
            "needs_root": self.needs_root,
        }


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stderr or p.stdout).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as e:
        return 127, str(e)


def set_property(unit: str, prop: str, value, persist: bool) -> tuple[bool, str]:
    """Set a single cgroup property on a user unit."""
    if prop == "AllowedCPUs":
        val = ",".join(str(c) for c in value)
    elif prop in ("MemoryHigh", "MemoryMax"):
        val = str(value)
    else:
        val = str(value)
    cmd = ["systemctl", "--user", "set-property"]
    if not persist:
        cmd.append("--runtime")
    cmd += [unit, f"{prop}={val}"]
    rc, err = _run(cmd)
    if rc != 0:
        # Fall back to a real drop-in when set-property is unavailable
        # (older systemd, or a unit systemd refuses to control directly).
        return _write_dropin(unit, prop, val, persist, err)
    return True, ""


def _write_dropin(
    unit: str, prop: str, val: str, persist: bool, prev_err: str
) -> tuple[bool, str]:
    base = USER_UNIT_DIR if persist else Path(f"/run/user/{os.getuid()}/systemd/user")
    d = base / f"{unit}.d"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "50-director.conf").write_text(f"[Service]\n{prop}={val}\n")
    except OSError as e:
        return False, f"set-property failed ({prev_err}); drop-in failed: {e}"
    rc, err = _run(["systemctl", "--user", "daemon-reload"])
    if rc != 0:
        return False, f"daemon-reload failed: {err}"
    return True, ""


def revert_all(policy: Policy) -> ApplyResult:
    """Remove every director drop-in and systemd's runtime control files."""
    res = ApplyResult()
    for c in policy.components:
        rc, err = _run(["systemctl", "--user", "revert", c.unit])
        if rc != 0:
            res.failed.append((c.unit, "revert", err))
        else:
            res.applied.append((c.unit, "revert", "drop-ins removed"))
        d = USER_UNIT_DIR / f"{c.unit}.d"
        f = d / "50-director.conf"
        if f.exists():
            try:
                f.unlink()
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError as e:
                res.failed.append((c.unit, "unlink", str(e)))
    _run(["systemctl", "--user", "daemon-reload"])
    return res


def apply_plan(
    plan: Plan, policy: Policy, persist: bool = False, dry_run: bool = False
) -> ApplyResult:
    res = ApplyResult()
    if not plan.ok:
        res.skipped.append("plan has safety violations; nothing applied")
        return res

    for a in plan.allocations:
        if not a.active:
            res.skipped.append(f"{a.unit}: inactive, no action")
            continue
        if policy.by_unit(a.unit) is None:
            res.skipped.append(f"{a.unit}: not in policy")
            continue
        for prop, new, cur in (
            ("CPUWeight", a.cpu_weight, a.cpu_weight_current),
            ("MemoryHigh", a.memory_high, a.memory_high_current),
            ("IOWeight", a.io_weight, a.io_weight_current),
        ):
            if new is None or new == cur:
                continue
            if prop == "IOWeight" and not plan.host.get("io_controlled", False):
                res.skipped.append(f"{a.unit}: {prop} no-op (io controller not delegated)")
                continue
            if dry_run:
                res.applied.append((a.unit, prop, f"{cur} -> {new} (dry-run)"))
                continue
            ok, err = set_property(a.unit, prop, new, persist)
            if ok:
                res.applied.append((a.unit, prop, f"{cur} -> {new}"))
            else:
                res.failed.append((a.unit, prop, err))
        if a.allowed_cpus and not dry_run:
            ok, err = set_property(a.unit, "AllowedCPUs", a.allowed_cpus, persist)
            (res.applied if ok else res.failed).append(
                (a.unit, "AllowedCPUs", str(a.allowed_cpus) if ok else err)
            )
    return res


def record_change(plan: Plan, history: dict) -> dict:
    """Stamp cooldown timers for units we just changed."""
    now = time.time()
    last = dict(history.get("last_change", {}))
    for a in plan.changed_units():
        last[a.unit] = now
    smoothed = dict(history.get("smoothed", {}))
    for a in plan.allocations:
        smoothed[a.unit] = a.demand
    return {"smoothed": smoothed, "last_change": last}
