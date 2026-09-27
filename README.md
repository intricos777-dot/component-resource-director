# component-resource-director

A resource director that allocates **CPU, memory and I/O to individual services
according to observed demand**, using systemd user units and cgroup v2.

It replaces nothing and is not a replacement for the existing monitors on this
box (`zen-healer`, `business-health-monitor`, `system-resource-optimizer`).
Those *report and suggest*; this one **sets limits per component** and is the
only one of the four that does per-component allocation.

Built for this specific machine: an Arch laptop, i5-12450H (8c/12t), 7.4GiB
RAM, RTX 3050 Mobile, running a **4-miner solo mining set**. That constraint
drives every design decision below.

---

## Quick start

```bash
cd /home/livings/Projects/component-resource-director
./install.sh                      # installs the unit, does NOT start it
                                  # runs the test suite + live validation

python3 -m crd.cli validate       # what can this box actually do?
python3 -m crd.cli report         # observe + show the plan (read-only)
python3 -m crd.cli apply          # set it, runtime-scoped (clears at reboot)
python3 -m crd.cli revert         # undo everything
```

To run it continuously:

```bash
systemctl --user enable --now component-resource-director
journalctl --user -u component-resource-director -f
```

Nothing here needs root. Nothing here ever invokes `sudo`.

---

## How it works

```
 cgroup v2 files  ──►  Sampler   ──►  plan_allocation()  ──►  applier  ──► systemd
 (cpu.stat,            (direct        (pure function:           (set-property
  memory.current,       file reads,    policy + samples          --runtime,
  memory.peak,          1 systemctl     -> plan, with             or drop-ins)
  cpuset, pids)         call)          safety floor)

                          ▲                    │
                          └──── history ───────┘
                          (EWMA smoothing, per-unit cooldown)
```

`plan_allocation()` is a **pure function**. It performs no side effects, which
is what lets the safety properties be tested directly rather than asserted
hopefully — see `tests/test_director.py::TestMiningRigSafety`.

### What it controls

| Knob | Mechanism | Notes |
|---|---|---|
| `CPUWeight` | cgroup v2 proportional scheduler | Relative weight 1–10000, not a percentage |
| `MemoryHigh` | soft reclaim pressure | The director **only raises** it (see below) |
| `MemoryMax` | hard limit | Off by default; **never** on a miner |
| `IOWeight` | cgroup v2 IO scheduler | **Inert on this box** — see below |
| `AllowedCPUs` | CPU affinity | Optional pin, e.g. keep a miner on cores 8–11 |

### Tiers and precedence

| Tier | Priority | Who |
|---|---|---|
| `critical` | 3.0 | the agent/gateway you are actively talking to |
| `mining` | 2.6 | income-generating miners |
| `workload` | 1.6 | builds, compiles, batch jobs |
| `background` | 0.7 | indexers, monitors, caches |

`critical > mining > workload > background`. This is stated once, in
`TIER_PRIORITY`, and the safety floor **rejects any policy that inverts it** —
a background component whose `cpu_weight_min` is >= the critical floor is a
configuration error, not a tuning choice.

---

## Safety design (why this can't hurt the mining set)

This is the part that matters on a 4-miner node, so it is enforced in code
rather than left to configuration discipline.

**1. Miners are protected, always.**
- `MemoryMax` is never set on a miner. An OOM kill destroys in-flight work.
- A miner's `MemoryHigh` is never lowered by the safety floor. Only non-mining
  components absorb pressure.
- A miner showing real load never drops below `CPUWeight` 2000
  (`MINER_WEIGHT_FLOOR`) and never exceeds 8000 (`MINER_WEIGHT_CEILING`), so it
  can neither be throttled to a stall nor allowed to monopolise the desktop.
- The floor applies only to miners **doing work**. An idle miner falls to its
  own `cpu_weight_min` — the floor is a floor, not a permanent subsidy.

**2. Memory limits only ever ratchet up.**
`memory.peak` is a historical high-water mark. A service that is quiet now may
burst later, so lowering its limit based on current usage is how you OOM-kill
something that was about to matter. The director raises `MemoryHigh` when a
component outgrows its limit and **holds** it otherwise. Lowering is reserved
for the safety floor below, which only engages when the machine is genuinely
over-committed.

**3. A reserve the director cannot touch.**
`safety.reserve_bytes` (default 1.5GiB) is withheld from the managed set to
cover kwin_wayland, Xwayland, plasmashell, page-cache headroom, and the
director itself. `max_managed_mem_high_fraction` (default 0.70) caps the rest.

**4. Over-commitment is reported, not tuned away.**
The budget is checked against *actually resident* memory. If the managed set is
resident beyond the budget, shrinking a ceiling does not free anything — so the
director **rejects the plan and applies nothing**, naming the components to
stop. It will not pretend an unsatisfiable allocation is fine.

**5. The director starves first.**
Its own unit carries `Nice=19`, `CPUQuota=15%`, `CPUWeight=10`,
`MemoryHigh=192M`, `MemoryMax=256M`, `TasksMax=32`, `IOAccounting`-class idle
I/O scheduling, and `StartLimitBurst=3`. Measured on this box over a 100s run:
**0.1% CPU, 18MB RSS.** Monitoring must never be what causes the problem.

**6. A rejected plan changes nothing.**
`apply_plan()` returns early if the plan has violations. There is no partial
application.

**7. Anti-thrash.**
Per-component EWMA smoothing (`ema_alpha`), a per-unit cooldown
(`min_interval_s`), and a relative-change band (`hysteresis`). The director
will not oscillate limits on a noisy signal.

**8. Runtime-scoped by default.**
`systemctl --user set-property --runtime` writes to
`/run/user/$UID/systemd/user.control/`, which **vanishes at reboot**. A bad
policy cannot silently become permanent. `--persist` is required, *and*
`director.persist` must be `true` in the policy, to write real drop-ins under
`~/.config/systemd/user/`.

---

## Known limitation on this machine: IOWeight is a no-op

```
cgroup v2 root controllers : cpu,cpuset,dmem,hugetlb,io,memory,misc,pids,rdma
user slice subtree_control : cpu,memory,pids      <-- no `io`
```

The `io` controller is **not delegated into the user slice**, so a user unit
cannot set `IOWeight`. Note the trap: `systemctl --user set-property
IOWeight=50` **exits 0 and silently does nothing** — no `io.weight` file ever
appears in the cgroup. The director detects this from
`cgroup.subtree_control` and reports it as skipped rather than pretending it
worked. It is not a bug in the director; it is a fact about this kernel config.

### Root-only setup (for the user to run, if IO throttling is wanted)

```bash
sudo systemctl edit user@1000.service
```

```ini
[Service]
Delegate=cpu cpuset io memory pids
```

Then reboot or `systemctl daemon-reexec`. The director will pick it up on the
next pass and start applying `IOWeight` automatically — no policy change
needed. Verify with `python3 -m crd.cli validate`, which prints
`io controller usable : True`.

This was **not** run. It needs root and is the user's call.

---

## Policy

`~/.config/component-resource-director/policy.toml` (see
`config/policy.example.toml`). Only units listed here are visible to the
director; it never discovers and mutates units implicitly.

```toml
[safety]
reserve_bytes = "1536M"
max_managed_mem_high_fraction = 0.70
mem_available_pause_bytes = "1024M"
allow_memory_max_on_miners = false

[[components]]
unit = "hermes-gateway.service"
label = "Hermes Gateway"
tier = "critical"
cpu_weight_min = 300
mem_high_floor = "256M"
mem_high_ceiling = "768M"
min_interval_s = 60
hysteresis = 0.20
```

### Sizing the memory envelope for 4 miners

The default envelope **does not fit four 1GiB miners**, and the director will
tell you so rather than hand out unsatisfiable limits:

```
budget = mem_total * max_managed_mem_high_fraction - reserve_bytes
       = 7550Mi * 0.70 - 1536Mi = 3749Mi
but 4 miners @ 1GiB + agent + background ~= 4700Mi resident   -> over by 951Mi
```

For a 4×1GiB rig, use a wider envelope:

```toml
[safety]
reserve_bytes = "1024M"
max_managed_mem_high_fraction = 0.80     # 7550Mi*0.80 - 1024Mi = 5016Mi
```

This is asserted in
`test_four_miners_need_a_wider_envelope_than_the_default`.

### Adding your miners

The example ships the miner blocks **commented out**, because the director must
not manage units that do not exist. Replace the names with your real ones:

```toml
[[components]]
unit = "your-miner-1.service"
label = "Monero Miner 1"
tier = "mining"
kind = "miner"
cpu_weight_min = 2000
mem_high_floor = "512M"
mem_high_ceiling = "1200M"
```

---

## Commands

| Command | Effect |
|---|---|
| `crd report` | observe + print the plan. **Read-only.** |
| `crd validate` | policy + live system check. **Read-only.** |
| `crd explain <unit>` | full cgroup state for one unit. **Read-only.** |
| `crd apply [--dry-run] [--persist]` | write the plan. Runtime-scoped by default. |
| `crd revert` | remove all director drop-ins, clear state. |

Global flags: `-p/--policy`, `--state`, `--json`, `--gpu`.

`--gpu` runs `nvidia-smi` (~50ms) to attribute VRAM per unit. Leave it **off**
on a 15s loop; use it for diagnosis. GPU VRAM is *reported*, not enforced —
per-process VRAM limits need root or a cgroup device driver.

---

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

40 tests, all passing. Coverage is concentrated where the risk is:

- `TestConfig` — malformed policies are rejected, including managing a
  protected unit, inverted tiers, and a sub-5s sampling interval.
- `TestMemorySafety` — the reserve is withheld, over-commitment is rejected
  rather than tuned away, no `MemoryMax` on miners, limits never ratchet down.
- `TestMiningRigSafety` — the 4-miner scenario: all four miners keep their
  ceiling, no `MemoryMax`, floors intact, and background work cannot starve
  them.
- `TestApplierGuards` — a rejected plan touches nothing; dry-run changes
  nothing.

The service file is verified with
`systemd-analyze --user verify` (clean).

---

## Do NOT enable `system-resource-optimizer`'s `auto_optimizer.py`

The other project in `_inventory/` is a near-miss. Its `setup.sh` installs a
unit with `ExecStart=... auto_optimizer.py --daemon --apply`,
`Restart=always`, `RestartSec=300`, and the daemon loops **every 300 seconds,
forever**. In that loop, unattended, it will:

```python
# auto_optimizer.py:44    install packages, no prompt, no review
sudo -S -p "" pacman -S --noconfirm ...
# auto_optimizer.py:71-81 create a zram device and swap on it
sudo -S -p "" modprobe zram num_devices=1
sudo -S -p "" mkswap /dev/zram0 ; sudo -S -p "" swapon /dev/zram0
# auto_optimizer.py:110-118 APPEND a tmpfs /tmp entry to /etc/fstab
# auto_optimizer.py:128    wipe the entire pacman cache
sudo -S -p "" pacman -Scc --noconfirm
# auto_optimizer.py:132    delete every file in /tmp older than 7 days
find /tmp -type f -mtime +7 -delete
```

Why this must stay uninstalled on this box:

1. **Unattended passwordless root.** `sudo -S -p ""` with `--noconfirm` lets a
   heuristic-triggered loop install packages with no prompt and no review. On a
   machine whose GPU also runs 4 miners, a library or driver change can silently
   alter clocks, thermals, or hashrate.
2. **It rewrites `/etc/fstab`.** The appended `tmpfs /tmp` entry survives every
   subsequent boot, and the only backup is `/etc/fstab.backup-optimizer`, which
   nothing restores automatically. A mis-sized tmpfs `/tmp` takes memory *away*
   from the miners.
3. **It creates and swaps zram on its own.** zram consumes RAM to provide
   compressed swap. On a 7.4GiB box with 4 miners resident, that is memory taken
   from mining, introduced automatically.
4. **Destructive cleanup on a timer.** `pacman -Scc` wipes the full package
   cache (so a rollback becomes impossible) and `find /tmp -delete` removes
   anything over 7 days old, every 5 minutes, forever. The intended
   `rm -rf ~/.cache/thumbnails/*` is itself a bug -- the glob is quoted inside
   `Path.home() / ".../*"`, so it never expands and deletes nothing. That is the
   quality of destructive code path the rest of that file is full of.
5. **It is machine-wide, not per-component.** It has no notion of a budget, so
   it cannot know which process deserves memory, and it cannot protect a miner
   because it does not know a miner exists.
6. **It does not compose with this director.** Both want to own resource policy,
   and the older one's changes are invisible to the newer one's observations.

If you want its *reporting*, use `crd report`. If you want one of its ideas,
run that single function by hand, once, after reading exactly what it does.

---

## Layout

```
component-resource-director/
├── crd/
│   ├── config.py       policy loading + validation (fail-closed)
│   ├── sampler.py      cgroup reads, host facts, cgroup path resolution
│   ├── allocator.py    the pure planning function + safety floor
│   ├── applier.py      systemd writes, runtime vs persistent, revert
│   ├── report.py       human-readable rendering
│   ├── cli.py          report / validate / apply / revert / explain
│   └── daemon.py       the systemd service loop
├── config/policy.example.toml
├── systemd/component-resource-director.service
├── tests/test_director.py
└── install.sh
```

No third-party dependencies. Python 3.11+ stdlib only (`tomllib`).
