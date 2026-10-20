#!/usr/bin/env bash
# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
set -euo pipefail

# Delegate the io cgroup controller into the user slice so IOWeight works.
# Root required. Idempotent.

USER_SLICE="user-1000.slice"

echo "=== Before ==="
systemctl show "$USER_SLICE" -p Delegate --no-pager
cat "/sys/fs/cgroup/user.slice/${USER_SLICE}/cgroup.controllers" 2>/dev/null || echo "(no cgroup.controllers file)"

echo ""
echo "=== Delegating io controller ==="
systemctl set-property "$USER_SLICE" Delegate=io

echo ""
echo "=== After ==="
systemctl show "$USER_SLICE" -p Delegate --no-pager
cat "/sys/fs/cgroup/user.slice/${USER_SLICE}/cgroup.controllers" 2>/dev/null || echo "(no cgroup.controllers file)"

echo ""
echo "=== Verifying io is now available to user units ==="
if systemctl show "$USER_SLICE" -p Delegate | grep -q "io"; then
    echo "OK: io controller delegated to $USER_SLICE"
else
    echo "WARN: io not showing in Delegate property yet — may need re-login"
fi
