# Watermark: Living_Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Copyright (c) 2026 Living Sin (intricos777-dot) + AI agents (Hermes, Lia, opencode) - collaborative work
# Licensed under Living Sin Proprietary License (LSPL-1.0)
# See LICENSE file for details.
# Install the director as a systemd USER service. No sudo required.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="${REPO_DIR}/systemd/component-resource-director.service"
UNIT_DST="${HOME}/.config/systemd/user/component-resource-director.service"
CONF_DIR="${HOME}/.config/component-resource-director"
POLICY="${CONF_DIR}/policy.toml"

echo "== component-resource-director installer =="

if [ ! -f "${POLICY}" ]; then
    echo "-- installing example policy to ${POLICY}"
    mkdir -p "${CONF_DIR}"
    cp "${REPO_DIR}/config/policy.example.toml" "${POLICY}"
    echo "   EDIT THIS FILE: add your miner unit names before going live."
else
    echo "-- existing policy at ${POLICY} left untouched"
fi

# The unit file hardcodes the repo path; rewrite it if the repo moved.
sed "s#^WorkingDirectory=.*#WorkingDirectory=${REPO_DIR}#" \
    "${UNIT_SRC}" > "${UNIT_DST}"
sed -i "s#^ExecStart=.*#ExecStart=/usr/bin/python3 -m crd.daemon#" "${UNIT_DST}"
echo "-- installed unit to ${UNIT_DST}"

systemctl --user daemon-reload

echo
echo "-- running tests"
( cd "${REPO_DIR}" && python3 -m unittest discover -s tests -t . -q )

echo
echo "-- validating live system (read-only)"
python3 -m crd.cli -p "${POLICY}" validate

echo
echo "Done. The service is installed but NOT started."
echo
echo "Next steps:"
echo "  1. Edit ${POLICY}"
echo "     - list your 4 miner units under [[components]] with tier=\"mining\""
echo "     - set safety.reserve_bytes / max_managed_mem_high_fraction for the rig"
echo "  2. Preview:            python3 -m crd.cli -p ${POLICY} report"
echo "  3. Apply once, runtime-scoped (clears at reboot):"
echo "       python3 -m crd.cli -p ${POLICY} apply"
echo "  4. When happy:         systemctl --user enable --now component-resource-director"
echo "  5. To undo everything: systemctl --user disable --now component-resource-director"
echo "                        python3 -m crd.cli -p ${POLICY} revert"
