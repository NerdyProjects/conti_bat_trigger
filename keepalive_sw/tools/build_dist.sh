#!/bin/sh
# Baut das Release-Paket dist/cebs_display_bms_patch/ (+ .zip) aus den Quellen
# im Repo zusammen. dist/ ist per .gitignore ausgenommen, deshalb liegen alle
# Paket-Bestandteile im Repo:
#
#   tools/*.py, o2_cave.*, PROTOCOL.md, 99-*.rules, install-udev.sh
#                                            -> <paket>/tools/
#   tools/patch.sh, flash.sh, requirements.txt, README.md
#                                            -> <paket>/          (Wurzel)
#   data/BMS_Patch.md, data/STM_Display_*.md -> <paket>/docs/
#   data/stm32f105_*.{bin,hex}               -> <paket>/firmware/
#
# Verwendung:  tools/build_dist.sh
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)
out="$root/dist"
pkg="$out/cebs_display_bms_patch"
zipname="cebs_display_bms_patch.zip"

die() { echo "FEHLER: $*" >&2; exit 1; }

copy() { # copy <quelle> <ziel>
    [ -f "$1" ] || die "Quelldatei fehlt: $1"
    cp -p "$1" "$2"
}

echo "[dist] baue $pkg"
rm -rf "$pkg" "$out/$zipname"
mkdir -p "$pkg/tools" "$pkg/docs" "$pkg/firmware"

# --- Werkzeuge ---------------------------------------------------------------
for f in patch_bms.py emu.py cebs_ble.py stm_display_fw.py \
         o2_cave.s o2_cave.ld PROTOCOL.md 99-continental-ebike.rules \
         install-udev.sh; do
    copy "$here/$f" "$pkg/tools/$f"
done
chmod +x "$pkg/tools/install-udev.sh"

# --- Paket-Wurzel ------------------------------------------------------------
for f in patch.sh flash.sh requirements.txt README.md; do
    copy "$here/$f" "$pkg/$f"
done
chmod +x "$pkg/patch.sh" "$pkg/flash.sh"

# --- Doku + Firmware aus data/ ----------------------------------------------
copy "$root/data/BMS_Patch.md" "$pkg/docs/BMS_Patch.md"
copy "$root/data/STM_Display_Firmwareupdate_Analyse.md" \
     "$pkg/docs/STM_Display_Firmwareupdate_Analyse.md"

for f in stm32f105_conti.bin stm32f105_conti.hex \
         stm32f105_bms_control.bin stm32f105_bms_control.hex; do
    copy "$root/data/$f" "$pkg/firmware/$f"
done

# --- Zip ---------------------------------------------------------------------
( cd "$out" && zip -r -X -q "$zipname" "cebs_display_bms_patch" )

echo "[dist] fertig: $out/$zipname"
