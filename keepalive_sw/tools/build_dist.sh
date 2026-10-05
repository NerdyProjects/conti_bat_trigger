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
# Zusaetzlich wird die ESP32-C3-Firmware (keepalive_sw) mit ESP-IDF fuer beide
# Hardware-Varianten gebaut und mit den Flash-Werkzeugen ins Paket gelegt:
#
#   build_supermini/keepalive_sw.bin -> <paket>/esp32/keepalive_sw_supermini.bin
#   build_oled/keepalive_sw.bin      -> <paket>/esp32/keepalive_sw_oled.bin
#   bootloader/partition-table/ota_data (variantenunabhaengig) -> <paket>/esp32/
#   tools/flash_ota.py, flash_esp32_usb.sh -> <paket>/          (Wurzel)
#
# Verwendung:  tools/build_dist.sh [--no-esp]
#   --no-esp   ESP32-Build ueberspringen (kein ESP-IDF noetig)
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

esp=1
for arg in "$@"; do
    case "$arg" in
        --no-esp) esp=0 ;;
        -h|--help) echo "Verwendung: $0 [--no-esp]"; exit 0 ;;
        *) die "unbekannte Option: $arg" ;;
    esac
done

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

# --- ESP32-C3 Firmware -------------------------------------------------------
if [ "$esp" -eq 1 ]; then
    echo "[dist] baue ESP32-Firmware (keepalive_sw)"
    "$here/build_esp32.sh"

    mkdir -p "$pkg/esp32"
    copy "$root/build_supermini/keepalive_sw.bin"  "$pkg/esp32/keepalive_sw_supermini.bin"
    copy "$root/build_oled/keepalive_sw.bin"        "$pkg/esp32/keepalive_sw_oled.bin"
    copy "$root/build_supermini/bootloader/bootloader.bin"           "$pkg/esp32/bootloader.bin"
    copy "$root/build_supermini/partition_table/partition-table.bin" "$pkg/esp32/partition-table.bin"
    copy "$root/build_supermini/ota_data_initial.bin"                "$pkg/esp32/ota_data_initial.bin"
    copy "$root/build_supermini/flasher_args.json"                   "$pkg/esp32/flasher_args.json"
else
    echo "[dist] ESP32-Build uebersprungen (--no-esp)"
fi

# --- ESP32-Werkzeuge ---------------------------------------------------------
for f in flash_ota.py flash_esp32_usb.sh idf_env.sh; do
    copy "$here/$f" "$pkg/$f"
done
chmod +x "$pkg/flash_ota.py" "$pkg/flash_esp32_usb.sh"

# --- Zip ---------------------------------------------------------------------
( cd "$out" && zip -r -X -q "$zipname" "cebs_display_bms_patch" )

echo "[dist] fertig: $out/$zipname"
