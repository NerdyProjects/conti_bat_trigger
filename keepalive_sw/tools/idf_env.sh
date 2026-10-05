#!/bin/sh
# Gemeinsame ESP-IDF-Erkennung für build_esp32.sh und flash_esp32_usb.sh.
# Das Skript wird *eingebunden* (". tools/idf_env.sh"), nicht ausgeführt.
#
# Ist IDF_PATH bereits gesetzt (z.B. durch die Shell der ESP-IDF-Extension),
# bleibt alles unverändert. Ansonsten wird das von EIM erzeugte
# Aktivierungsskript unter ~/.espressif/tools/activate_idf_v*.sh geladen
# (bevorzugt die höchste Version).

load_idf_env() {
    if [ -n "${IDF_PATH:-}" ] && [ -d "${IDF_PATH:-}" ]; then
        return 0
    fi

    act=""
    acts=""
    for f in "$HOME"/.espressif/tools/activate_idf_v*.sh; do
        [ -f "$f" ] || continue
        acts="$acts $f"
    done
    if [ -n "$acts" ]; then
        # Bei mehreren Installationen die höchste Version wählen.
        # shellcheck disable=SC2086
        act=$(printf '%s\n' $acts | sort -V | tail -1)
    fi
    [ -n "$act" ] || return 1

    # Das EIM-Skript erkennt per $0, ob es gesourct wurde, und beendet sich
    # sonst mit exit 1. Deshalb in einer Sub-Shell mit Shell-artigem $0 laden
    # und die exportierte Umgebung übernehmen.
    env_dump=$(sh -c '. "$1" >/dev/null 2>&1; export -p' -bash "$act") || return 1
    eval "$env_dump" || return 1
    [ -n "${IDF_PATH:-}" ] && [ -d "${IDF_PATH:-}" ]
}

# Python für idf.py/esptool: IDF-Venv bevorzugen, sonst System-Python.
idf_python() {
    if [ -n "${IDF_PYTHON_ENV_PATH:-}" ] && [ -x "$IDF_PYTHON_ENV_PATH/bin/python" ]; then
        printf '%s\n' "$IDF_PYTHON_ENV_PATH/bin/python"
    else
        printf '%s\n' "${PYTHON:-python3}"
    fi
}
