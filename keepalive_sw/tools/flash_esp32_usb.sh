#!/bin/sh
# ESP32-C3 per USB flashen (Bootloader, Partitionstabelle, OTA-Daten, App).
#
# Fuer das erstmalige Bespielen oder die Wiederherstellung per Kabel. Fuer
# Updates im laufenden Betrieb ist der WiFi-Weg (tools/flash_ota.py) einfacher.
#
# Voraussetzung: USB-Kabel am ESP32-C3 (nativer USB-Serial/JTAG) und esptool
# (aus der ESP-IDF-Python-Umgebung oder im PATH, esptool v5 / ESP-IDF v6).
#
# Verwendung: tools/flash_esp32_usb.sh [PORT] [BAUD] [--variant supermini|oled]
#   PORT  z.B. /dev/ttyACM0          (Standard: /dev/ttyACM0)
#   BAUD  Baudrate                   (Standard: 460800)
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

port=""
baud=""
variant="supermini"
while [ $# -gt 0 ]; do
    case "$1" in
        --variant)   variant=$2; shift 2;;
        --variant=*) variant=${1#*=}; shift;;
        -h|--help)
            echo "Verwendung: $0 [PORT] [BAUD] [--variant supermini|oled]" >&2
            exit 0;;
        *)
            if [ -z "$port" ]; then port=$1
            elif [ -z "$baud" ]; then baud=$1
            else echo "FEHLER: unerwartetes Argument: $1" >&2; exit 2
            fi
            shift;;
    esac
done
port=${port:-/dev/ttyACM0}
baud=${baud:-460800}
case "$variant" in
    supermini|oled) ;;
    *) echo "FEHLER: unbekannte Variante '$variant' (supermini|oled)" >&2; exit 2;;
esac

# shellcheck source=idf_env.sh
[ -f "$here/idf_env.sh" ] && . "$here/idf_env.sh"

# --- Firmware-Verzeichnis bestimmen (dist-Layout oder Quellbaum) ------------
if [ -f "$here/esp32/keepalive_sw_$variant.bin" ]; then
    fw="$here/esp32"
    boot="$fw/bootloader.bin";      ptab="$fw/partition-table.bin"
    ota="$fw/ota_data_initial.bin"; app="$fw/keepalive_sw_$variant.bin"
elif [ -f "$here/../build_$variant/keepalive_sw.bin" ]; then
    fw="$here/../build_$variant"
    boot="$fw/bootloader/bootloader.bin"
    ptab="$fw/partition_table/partition-table.bin"
    ota="$fw/ota_data_initial.bin"; app="$fw/keepalive_sw.bin"
elif [ -f "$here/esp32/keepalive_sw.bin" ]; then
    fw="$here/esp32"
    boot="$fw/bootloader.bin";      ptab="$fw/partition-table.bin"
    ota="$fw/ota_data_initial.bin"; app="$fw/keepalive_sw.bin"
elif [ -f "$here/../build/keepalive_sw.bin" ]; then
    fw="$here/../build"
    boot="$fw/bootloader/bootloader.bin"
    ptab="$fw/partition_table/partition-table.bin"
    ota="$fw/ota_data_initial.bin"; app="$fw/keepalive_sw.bin"
elif [ -f "$here/keepalive_sw_$variant.bin" ]; then
    fw="$here"
    boot="$fw/bootloader.bin";      ptab="$fw/partition-table.bin"
    ota="$fw/ota_data_initial.bin"; app="$fw/keepalive_sw_$variant.bin"
else
    echo "FEHLER: keepalive_sw_$variant.bin nicht gefunden - erst tools/build_esp32.sh laufen lassen." >&2
    exit 1
fi

for f in "$boot" "$ptab" "$ota" "$app"; do
    [ -f "$f" ] || { echo "FEHLER: $f fehlt." >&2; exit 1; }
done

# --- esptool bestimmen (IDF-Venv bevorzugt, dann PATH) ----------------------
if [ -z "${ESPTOOL:-}" ]; then
    if load_idf_env 2>/dev/null; then
        py=$(idf_python)
        if "$py" -c "import esptool" >/dev/null 2>&1; then
            ESPTOOL="$py -m esptool"
        fi
    fi
fi
if [ -z "${ESPTOOL:-}" ]; then
    if command -v esptool.py >/dev/null 2>&1; then
        ESPTOOL="esptool.py"
    elif command -v esptool >/dev/null 2>&1; then
        ESPTOOL="esptool"
    else
        echo "FEHLER: esptool nicht gefunden." >&2
        echo "  ESP-IDF aktivieren (export.sh) oder ESPTOOL setzen." >&2
        exit 1
    fi
fi

# shellcheck disable=SC2086
set -- $ESPTOOL
echo "[esp32] flashe $app -> $port (esp32c3, Variante $variant, ${baud} baud)"
exec "$@" --chip esp32c3 --port "$port" --baud "$baud" \
    --before default-reset --after hard-reset \
    write-flash --flash-mode dio --flash-size 4MB --flash-freq 80m \
    0x0 "$boot" 0x8000 "$ptab" 0xd000 "$ota" 0x10000 "$app"
