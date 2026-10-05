#!/bin/sh
# udev-Regel installieren: Zugriff auf das Display (VID 0x2A8A) ohne root.
# Danach das Geraet einmal ab- und wieder anstecken.
#
set -e
cd "$(dirname "$0")"

RULE="99-continental-ebike.rules"
if [ ! -f "$RULE" ]; then
    echo "FEHLER: $RULE nicht gefunden." >&2
    exit 2
fi

sudo cp "$RULE" /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

echo
echo "Regel installiert. Display jetzt einmal ab- und wieder anstecken."
echo "Kontrolle:  python3 tools/stm_display_fw.py info"
