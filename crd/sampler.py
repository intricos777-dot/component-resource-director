# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
"""Sample real resource usage for each managed unit.

Design constraint: the director runs on a 7.4GiB box shared with 4 miners, so
sampling must be cheap. Everything here reads cgroup files directly -- no
`systemctl show` per unit, no `ps`, no `nvidia-smi` unless GPU accounting is on.
One `systemctl show` burst is done once per unit at discovery time and cached.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Component

CGROUP_ROOT = Path("/sys/fs/cgroup")


def _systemd_escape(name: str) -> str:
    """Escape a unit name the way systemd does for cgroup paths.

    systemd replaces every byte outside [A-Za-z0-9] with a literal
    backslash followed by \\xNN. e.g. 'dbus-:1.2-org.kde' ->
    'dbus-\\x3a1.2-org.kde'.
    """
    out = []
    for ch in name:
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            out.append(ch)
        else:
            out.append(f"\\x{ord(ch):02x}")
    return "".join(out)


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return default


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def _parse_size(v: str, default: int = -1) -> int:
    """systemd reports sizes as 'infinity' or a bare byte count."""
    v = v.strip()
    if not v or v == "infinity":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def parse_cpu_stat(stat_text: str) -> tuple[int, int]:
    """Return (usage_usec, nr_periods) from cgroup cpu.stat."""
    usage = periods = 0
    for line in stat_text.splitlines():
        k, _, v = line.partition(" ")
        if k == "usage_usec":
            usage = _read_int_int(v)
        elif k == "nr_periods":
            periods = _read_int_int(v)
    return usage, periods


def _read_int_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


@dataclass
class Sample:
    unit: str
    cgroup: str | None
    timestamp: float
    # Smoothed CPU utilisation as a fraction of all logical CPUs (0.0 - ncpu).
    cpu_frac: float
    cpu_weight_current: int
    mem_current: int
    mem_high_current: int
    mem_max_current: int
    mem_peak: int
    io_weight_current: int
    allowed_cpus: list[int] | None
    pids: int
    active: bool
    oom_kills: int
    # GPU (MiB) attributed to this unit's pids, if GPU accounting is enabled.
    gpu_mem_mib: int = 0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d


class Sampler:
    """Caches unit->cgroup-path mappings and refreshes them lazily.

    A unit whose ControlGroup is not yet visible (inactive, or transiently
    reloading) is reported as `active=False` rather than raising.
    """

    def __init__(self, units: list[str], gpu_pids: bool = False) -> None:
        self._units = list(dict.fromkeys(units))
        self._cg: dict[str, str] = {}
        self._state: dict[str, str] = {}
        self._prev_usage: dict[str, tuple[int, int]] = {}
        self._prev_time: dict[str, float] = {}
        self._gpu_enabled = gpu_pids
        self._ncpu = os.cpu_count() or 1
        self._refresh_cgroups()

    # ---------- discovery ----------

    PROPS = ("Id", "ControlGroup", "ActiveState", "LoadState")

    def _run_show(self) -> dict[str, dict[str, str]]:
        """One systemctl call for all units. Returns unit -> properties.

        `systemctl show` with several units emits one property block per
        unit, each starting with Id=. A single -p list is applied to all of
        them, so a flat key=value parse would silently keep only the last
        unit's block.
        """
        out: dict[str, dict[str, str]] = {}
        try:
            p = subprocess.run(
                ["systemctl", "--user", "show", *self._units,
                 "-p", "Id", "-p", "ControlGroup", "-p", "ActiveState",
                 "-p", "LoadState", "--no-pager"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return out
        cur: dict[str, str] = {}
        for line in p.stdout.splitlines():
            k, _, v = line.partition("=")
            if not k:
                continue
            if k == "Id":
                # A new block begins. Flush the previous one.
                if cur.get("Id"):
                    out[cur["Id"]] = cur
                cur = {}
            cur[k] = v
        if cur.get("Id"):
            out[cur["Id"]] = cur
        return out

    def _refresh_cgroups(self) -> None:
        for unit, props in self._run_show().items():
            self._state[unit] = props.get("ActiveState", "")
            cg = props.get("ControlGroup", "")
            if not cg or cg == "/":
                continue
            # ControlGroup= is a path *relative to the cgroup v2 mount
            # point*, e.g. /user.slice/.../foo.service -- not a real path.
            p = CGROUP_ROOT / cg.lstrip("/")
            if p.exists():
                self._cg[unit] = str(p)
        # Fall back to the conventional path for any unit systemctl didn't
        # report (e.g. transient scopes) so a missing entry isn't fatal.
        for unit in self._units:
            if unit not in self._cg:
                guess = self._guess_cgroup(unit)
                if guess:
                    self._cg[unit] = guess

    def _user_base(self) -> Path:
        uid = os.getuid()
        return (CGROUP_ROOT / f"user.slice/user-{uid}.slice/user@{uid}.service")

    def _guess_cgroup(self, unit: str) -> str | None:
        """Best-effort cgroup path for a unit systemctl did not report.

        systemd escapes unit names in the cgroup filesystem: everything that
        is not [A-Za-z0-9] becomes a backslash-escaped \\xNN sequence, and
        the scope suffix is dropped (a .service is not a .scope).
        """
        base = self._user_base()
        esc = _systemd_escape(unit)
        # A unit lands in app.slice, background.slice, or a user-type slice
        # depending on the unit's Slice= property. Search them all.
        slices = ("app.slice", "background.slice", "session.slice", "")
        candidates = []
        for s in slices:
            head = base / s if s else base
            candidates += [head / esc, head / f"{esc}.scope"]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    def known_units(self) -> list[str]:
        return [u for u in self._units if u in self._cg]

    # ---------- sampling ----------

    def sample(self, comp: Component) -> Sample:
        now = time.monotonic()
        cg = self._cg.get(comp.unit)
        if cg is None:
            self._refresh_cgroups()
            cg = self._cg.get(comp.unit)
        if cg is None:
            return Sample(
                unit=comp.unit, cgroup=None, timestamp=now, cpu_frac=0.0,
                cpu_weight_current=0, mem_current=0, mem_high_current=-1,
                mem_peak=0, mem_max_current=-1, io_weight_current=0,
                allowed_cpus=None,
                pids=0, active=False, oom_kills=0,
            )

        d = Path(cg)
        usage, periods = parse_cpu_stat(_read_text(d / "cpu.stat", ""))
        prev_u, prev_t = self._prev_usage.get(comp.unit, (usage, now))
        dt = max(now - self._prev_time.get(comp.unit, now), 1e-6)
        self._prev_usage[comp.unit] = (usage, now)
        self._prev_time[comp.unit] = now
        # clamp: a unit restarted mid-window shows a usage drop
        delta = max(0, usage - prev_u)
        cpu_frac = (delta / 1e6) / dt

        mem_high = _parse_size(_read_text(d / "memory.high", ""), -1)
        mem_max = _parse_size(_read_text(d / "memory.max", ""), -1)
        ev = _read_text(d / "memory.events", "")
        oom = 0
        for line in ev.splitlines():
            if line.startswith("oom_kill "):
                oom = _read_int_int(line.split(" ")[1])

        gpu = 0
        if self._gpu_enabled:
            gpu = self._gpu_for_cgroup(d)

        return Sample(
            unit=comp.unit,
            cgroup=cg,
            timestamp=now,
            cpu_frac=cpu_frac,
            cpu_weight_current=_read_int(d / "cpu.weight", comp.cpu_weight_min),
            mem_current=_read_int(d / "memory.current"),
            mem_high_current=mem_high,
            mem_max_current=mem_max,
            mem_peak=_read_int(d / "memory.peak"),
            io_weight_current=_read_int(d / "io.weight", -1),
            allowed_cpus=self._read_cpuset(d),
            pids=_read_int(d / "pids.current"),
            active=self._state.get(comp.unit, "") in ("active", "activating"),
            oom_kills=oom,
            gpu_mem_mib=gpu,
        )

    def _read_cpuset(self, d: Path) -> list[int] | None:
        raw = _read_text(d / "cpuset.cpus.effective", "")
        if not raw:
            return None
        cpus: set[int] = set()
        for part in raw.split(","):
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    cpus.update(range(int(a), int(b) + 1))
                except ValueError:
                    continue
            else:
                try:
                    cpus.add(int(part))
                except ValueError:
                    continue
        return sorted(cpus)

    def _gpu_for_cgroup(self, d: Path) -> int:
        pids_text = _read_text(d / "cgroup.procs", "")
        pids = {p for p in pids_text.split() if p.isdigit()}
        if not pids:
            return 0
        return _nvidia_mem_by_pid(pids)


_NV_CACHE: dict[str, int] = {}


def _nvidia_mem_by_pid(pids: set[str]) -> int:
    """GPU MiB used by the given pids. Cached for a few seconds by the caller."""
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    for line in p.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[0] in pids:
            try:
                total += int(float(parts[1]))
            except ValueError:
                pass
    return total


# ---------- host facts ----------


def host_facts() -> dict:
    """Machine-wide facts used by the safety floor."""
    info: dict[str, str] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = v.strip()
    except OSError:
        pass

    def kb(k: str) -> int:
        try:
            return int(info.get(k, "0").split()[0]) * 1024
        except (ValueError, IndexError):
            return 0

    root_ctrl = _read_text(CGROUP_ROOT / "cgroup.controllers")
    uid = os.getuid()
    # What the user slice has actually delegated to its children. This, not
    # the root controller list, determines which knobs a user unit can set.
    subtree = _read_text(
        CGROUP_ROOT / f"user.slice/user-{uid}.slice/user@{uid}.service/cgroup.subtree_control"
    )
    subtree_parts = subtree.split()
    return {
        "ncpu": os.cpu_count() or 1,
        "mem_total": kb("MemTotal"),
        "mem_available": kb("MemAvailable"),
        "mem_free": kb("MemFree"),
        "swap_total": kb("SwapTotal"),
        "root_controllers": sorted(root_ctrl.split()),
        "user_subtree_control": sorted(subtree_parts),
        # IOWeight only has effect if the io controller is delegated into the
        # user slice's subtree. On stock Arch it is NOT.
        "io_controlled": "io" in subtree_parts,
    }


def load_state(path: str | Path) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: str | Path, data: dict) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)
