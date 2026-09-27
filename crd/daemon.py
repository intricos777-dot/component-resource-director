"""Continuous director loop, run by the systemd user service.

Loop contract:
  - one allocation pass per policy.sampling_interval_s
  - hard wall-clock cap per pass; if a pass overruns, skip rather than
    accumulate backlog (a director that falls behind is worse than none)
  - self-limits: Nice/IOWeight so the director is always the first thing
    throttled on a loaded box
  - exits non-zero if the policy becomes invalid, so systemd restarts it
    visibly instead of silently doing nothing
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .allocator import plan_allocation
from .applier import DIRECTOR_FOOTPRINT, apply_plan, record_change
from .config import load_policy
from .sampler import Sampler, host_facts, load_state, save_state

log = logging.getLogger("crd")

# A pass that takes longer than this is a bug or a pathological system; do not
# keep up a backlog.
PASS_BUDGET_S = 10.0
def _default_state() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "component-resource-director" / "state.json"


STATE = _default_state()


def _run_once(policy, args) -> dict:
    host = host_facts()
    units = [c.unit for c in policy.components]
    sampler = Sampler(units, gpu_pids=args.gpu)
    samples = {c.unit: sampler.sample(c) for c in policy.components}
    hist = load_state(args.state)
    plan = plan_allocation(policy, samples, host, hist)

    summary = {
        "ts": time.time(),
        "ok": plan.ok,
        "violations": plan.violations,
        "changed": [a.unit for a in plan.changed_units()],
        "resident_mib": plan.total_observed_mem() // (1024 * 1024),
    }
    if not plan.ok:
        for v in plan.violations:
            log.error("SAFETY VIOLATION: %s", v)
        return summary

    res = apply_plan(plan, policy, persist=args.persist, dry_run=args.dry_run)
    if res.applied:
        log.info("applied %d change(s): %s", len(res.applied),
                 ", ".join(f"{u}:{p}" for u, p, _ in res.applied))
    for u, p, e in res.failed:
        log.error("apply failed %s %s: %s", u, p, e)
    if res.applied and not args.dry_run:
        save_state(args.state, record_change(plan, hist))
    return summary


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="crd-daemon")
    ap.add_argument("-p", "--policy", default=None)
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--interval", type=int, default=None,
                    help="override policy sampling_interval_s")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(message)s")

    stop = False

    def _stop(signum, frame):
        nonlocal stop
        stop = True
        log.info("signal %s received; finishing current pass", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    policy = load_policy(args.policy)
    interval = args.interval or policy.sampling_interval_s
    log.info(
        "director started: %d component(s), %ds interval, reserve %dMi, "
        "director footprint budget %dMi",
        len(policy.components), interval,
        policy.safety.reserve_bytes // (1024 * 1024),
        DIRECTOR_FOOTPRINT // (1024 * 1024),
    )
    if not host_facts().get("io_controlled", False):
        log.warning("io cgroup controller is not delegated to the user slice; "
                    "IOWeight will be reported as skipped (see README)")

    # A first pass always needs two samples to compute a rate, so prime it.
    while not stop:
        t0 = time.monotonic()
        try:
            summary = _run_once(policy, args)
            if args.json:
                print(json.dumps(summary), flush=True)
        except Exception:
            log.exception("pass failed; continuing")
        if args.once:
            return 0
        elapsed = time.monotonic() - t0
        if elapsed > PASS_BUDGET_S:
            log.warning("pass took %.1fs (budget %.0fs); skipping ahead",
                        elapsed, PASS_BUDGET_S)
        sleep_for = max(1, interval - elapsed)
        deadline = time.monotonic() + sleep_for
        while not stop and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))
    log.info("director stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
