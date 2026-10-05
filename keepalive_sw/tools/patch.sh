#!/bin/sh
# Firmware patchen: Original -> patchiertes Image (bin + hex)
#
#   ./patch.sh                       # Standardpfade
#   ./patch.sh --without P4          # weitere Aufrufoptionen werden durchgereicht
#
set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python3" ]; then
    PY=".venv/bin/python3"
else
    PY="${PY:-python3}"
fi

exec "$PY" tools/patch_bms.py \
    -i firmware/stm32f105_conti.hex \
    -o firmware/stm32f105_bms_control \
    "$@"
