# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
"""Command-line entry point.

Subcommands, in increasing order of authority:
  report   observe and print a plan. Read-only. Default command.
  validate check the policy file and the live system, print what is
           available and what is not. Read-only.
  apply    write the plan. Runtime-scoped unless --persist is given.
  revert   remove all director drop-ins from every managed unit.
  explain  show the cgroup layout and current limits for one unit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .allocator import plan_allocation
from .applier import apply_plan, record_change, revert_all
from .config import DEFAULT_POLICY, load_policy
from .report import render_report
from .sampler import Sampler, host_facts, load_state, save_state

STATE_PATH = Path("~/.local/state/component-resource-director/state.json")


def _collect(policy, gpu: bool):
    units = [c.unit for c in policy.components]
    s = Sampler(units, gpu_pids=gpu)
    samples = {c.unit: s.sample(c) for c in policy.components}
    return s, samples


def _print_apply_result(res, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res.as_dict(), indent=2))
        return
    for u, p, v in res.applied:
        print(f"  set {u:<30} {p:<12} {v}")
    for u, p, e in res.failed:
        print(f"  FAIL {u:<30} {p:<12} {e}", file=sys.stderr)
    for n in res.skipped:
        print(f"  skip {n}")
    for n in res.needs_root:
        print(f"  NEEDS ROOT (not done): {n}")


def cmd_validate(args, policy) -> int:
    host = host_facts()
    print(f"policy: {policy.source_path}  ({len(policy.components)} components)")
    print(f"cgroup v2 root controllers : {','.join(host['root_controllers'])}")
    print(f"user slice subtree_control : {','.join(host['user_subtree_control'])}")
    print(f"io controller usable        : {host['io_controlled']}")
    print(f"ram total / available      : {host['mem_total'] >> 20}Mi / "
          f"{host['mem_available'] >> 20}Mi")
    s, samples = _collect(policy, gpu=False)
    print()
    missing = []
    for c in policy.components:
        smp = samples[c.unit]
        if smp.cgroup is None:
            missing.append(c.unit)
            print(f"  MISSING cgroup  {c.unit}")
        else:
            print(f"  ok  {c.unit:<30} cgroup={smp.cgroup} active={smp.active} "
                  f"rss={smp.mem_current >> 20}Mi")
    if missing:
        print(f"\n{len(missing)} unit(s) not found. A unit only appears once it has "
              "been started at least once. Start it, or remove it from the policy.")
    if not host["io_controlled"]:
        print("\nNOTE: IOWeight is a no-op on this system. The io controller is not "
              "delegated into the user slice. See README 'Root-only setup'.")
    return 0


def cmd_report(args, policy) -> int:
    host = host_facts()
    _, samples = _collect(policy, gpu=args.gpu)
    hist = load_state(args.state)
    plan = plan_allocation(policy, samples, host, hist)
    if args.json:
        print(json.dumps(plan.as_dict(), indent=2, default=str))
    else:
        print(render_report(plan, samples, {}))
    return 0 if plan.ok else 2


def cmd_apply(args, policy) -> int:
    host = host_facts()
    _, samples = _collect(policy, gpu=args.gpu)
    hist = load_state(args.state)
    plan = plan_allocation(policy, samples, host, hist)
    if not plan.ok:
        print("PLAN REJECTED by safety floor; nothing applied:", file=sys.stderr)
        for v in plan.violations:
            print(f"  !! {v}", file=sys.stderr)
        return 2
    persist = args.persist and policy.persist
    if args.persist and not policy.persist:
        print("  note: policy.director.persist is false; applying runtime-only",
              file=sys.stderr)
    res = apply_plan(plan, policy, persist=persist, dry_run=args.dry_run)
    _print_apply_result(res, args.json)
    if not args.dry_run and res.applied:
        save_state(args.state, record_change(plan, hist))
    return 0 if res.ok else 1


def cmd_revert(args, policy) -> int:
    res = revert_all(policy)
    _print_apply_result(res, args.json)
    st = Path(args.state).expanduser()
    if st.exists():
        st.unlink()
        print("  cleared director state")
    return 0 if res.ok else 1


def cmd_explain(args, policy) -> int:
    c = policy.by_unit(args.unit)
    _, samples = _collect(policy, gpu=False)
    smp = samples.get(args.unit)
    if smp is None:
        print(f"unit {args.unit!r} is not in the policy", file=sys.stderr)
        return 1
    d = Path(smp.cgroup) if smp.cgroup else None
    if d and d.exists():
        for f in ("cpu.weight", "memory.current", "memory.high", "memory.max",
                  "memory.peak", "memory.events", "pids.current",
                  "cpuset.cpus.effective", "io.weight", "cgroup.controllers"):
            p = d / f
            if p.exists():
                print(f"{f:<24} {p.read_text().strip()[:200]}")
    print()
    print(json.dumps(smp.as_dict(), indent=2, default=str))
    if c:
        print(f"\ntier={c.tier} kind={c.kind} label={c.label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="crd",
        description="Demand-driven resource allocation for systemd user services.",
    )
    ap.add_argument("--version", action="version", version=f"crd {__version__}")
    ap.add_argument("-p", "--policy", default=None,
                    help=f"policy TOML (default: {DEFAULT_POLICY})")
    ap.add_argument("--state", default=str(STATE_PATH), help="state file path")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--gpu", action="store_true",
                    help="also account GPU VRAM per unit (runs nvidia-smi; "
                         "costs ~50ms, so keep it off on a 15s loop)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("report", help="observe and print a plan (read-only)")
    sub.add_parser("validate", help="check policy + live system (read-only)")
    a = sub.add_parser("apply", help="write the plan")
    a.add_argument("--dry-run", action="store_true", help="print, change nothing")
    a.add_argument("--persist", action="store_true",
                   help="write drop-ins under ~/.config/systemd/user (survives reboot)")
    r = sub.add_parser("revert", help="remove all director drop-ins")
    e = sub.add_parser("explain", help="show cgroup detail for one unit")
    e.add_argument("unit")

    args = ap.parse_args(argv)
    cmd = args.cmd or "report"

    try:
        policy = load_policy(args.policy)
    except (FileNotFoundError, ValueError) as exc:
        print(f"policy error: {exc}", file=sys.stderr)
        return 3

    return {
        "report": cmd_report,
        "validate": cmd_validate,
        "apply": cmd_apply,
        "revert": cmd_revert,
        "explain": cmd_explain,
    }[cmd](args, policy)


if __name__ == "__main__":
    sys.exit(main())
