#!/bin/sh
# Baut die ESP32-C3-Firmware (keepalive_sw) mit ESP-IDF - fuer beide
# Hardware-Varianten (SuperMini und OLED).
#
# Verwendung:
#   tools/build_esp32.sh [--variant supermini|oled|both] [--target TARGET] [-- idf.py-Args]
#
# Standard ist --variant both. Jede Variante bekommt ein eigenes
# Build-Verzeichnis samt eigener sdkconfig; die Projekt-sdkconfig bleibt
# unangetastet. Ergebnis:
#   build_supermini/keepalive_sw.bin
#   build_oled/keepalive_sw.bin
#
# Die ESP-IDF-Umgebung wird aus IDF_PATH/IDF_PYTHON_ENV_PATH übernommen oder
# automatisch über ~/.espressif/tools/activate_idf_v*.sh (EIM) geladen.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/.." && pwd)
target="${IDF_TARGET:-esp32c3}"
variant_list=""

# shellcheck source=idf_env.sh
. "$here/idf_env.sh"

usage() {
    cat >&2 <<EOF
usage: $0 [--variant supermini|oled|both] [--target TARGET] [-- <idf.py args>]
EOF
}

idf_args=""
variants=""
while [ $# -gt 0 ]; do
    case "$1" in
        --variant)   variants=$2; shift 2;;
        --variant=*) variants=${1#*=}; shift;;
        --target)    target=$2;    shift 2;;
        --)          shift; idf_args=$*; break;;
        -h|--help)   usage; exit 0;;
        *)           idf_args="$idf_args $1"; shift;;
    esac
done

case "${variants:-both}" in
    both)           variant_list="supermini oled";;
    supermini|oled) variant_list="$variants";;
    *) echo "FEHLER: unbekannte Variante '${variants}' (supermini|oled|both)" >&2
       exit 2;;
esac

if ! load_idf_env; then
    echo "FEHLER: ESP-IDF nicht gefunden." >&2
    echo "  Bitte IDF_PATH (und IDF_PYTHON_ENV_PATH) setzen oder eine" >&2
    echo "  EIM-Installation unter ~/.espressif/tools bereitstellen." >&2
    exit 1
fi

idf_py=$(idf_python)
if [ ! -f "$IDF_PATH/tools/idf.py" ]; then
    echo "FEHLER: $IDF_PATH/tools/idf.py fehlt." >&2
    exit 1
fi

# Schreibt eine variantenspezifische sdkconfig (Basis: Projekt-sdkconfig),
# ohne die Projekt-sdkconfig selbst zu verändern.
write_variant_sdkconfig() {
    var=$1
    dst=$2
    mkdir -p "$(dirname -- "$dst")"
    tmp="$dst.tmp"
    if [ -f "$root/sdkconfig" ]; then
        grep -v -E '^(# )?CONFIG_HW_VARIANT_(SUPERMINI|OLED)' "$root/sdkconfig" > "$tmp" || true
    else
        {
            echo 'CONFIG_IDF_TARGET="esp32c3"'
            echo 'CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y'
            echo 'CONFIG_PARTITION_TABLE_TWO_OTA=y'
        } > "$tmp"
    fi
    if [ "$var" = oled ]; then
        printf 'CONFIG_HW_VARIANT_OLED=y\n# CONFIG_HW_VARIANT_SUPERMINI is not set\n' >> "$tmp"
    else
        printf 'CONFIG_HW_VARIANT_SUPERMINI=y\n# CONFIG_HW_VARIANT_OLED is not set\n' >> "$tmp"
    fi
    if cmp -s "$tmp" "$dst"; then
        rm -f "$tmp"
    else
        mv "$tmp" "$dst"
    fi
}

echo "[esp32] IDF_PATH=$IDF_PATH"

export IDF_TARGET="$target"
for var in $variant_list; do
    bdir="$root/build_$var"
    cfg="$bdir/sdkconfig"
    write_variant_sdkconfig "$var" "$cfg"
    echo "[esp32] Variante $var (target $target) -> $bdir"
    # shellcheck disable=SC2086
    "$idf_py" "$IDF_PATH/tools/idf.py" -C "$root" -B "$bdir" \
        -D SDKCONFIG="$cfg" build $idf_args
    echo "[esp32] fertig: $bdir/keepalive_sw.bin"
done
