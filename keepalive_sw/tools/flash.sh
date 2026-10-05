#!/bin/sh
# Firmware flashen (USB-HID-Bootloader)
#
#   ./flash.sh                    # patchiertes Image flashen
#   ./flash.sh --stream-pace-ms 15   # langsamer, falls Rahmen verloren gehen
#
# Voraussetzungen: Display per USB angesteckt und versorgt. Laeuft die
# Applikation, holt das Werkzeug sie selbst in den Bootloader.
#
set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python3" ]; then
    PY=".venv/bin/python3"
else
    PY="${PY:-python3}"
fi

IMG="firmware/stm32f105_bms_control.bin"
if [ ! -f "$IMG" ]; then
    echo "FEHLER: $IMG fehlt -- erst ./patch.sh laufen lassen." >&2
    exit 2
fi

exec "$PY" tools/stm_display_fw.py flash "$IMG" "$@"
