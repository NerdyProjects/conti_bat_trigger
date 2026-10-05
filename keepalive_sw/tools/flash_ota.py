#!/usr/bin/env python3
"""keepalive_sw (ESP32-C3) per WiFi-Web-OTA aktualisieren.

Laedt ein App-Image (keepalive_sw.bin) an den OTA-Endpunkt ``/update`` des
ESP32-Webservers. Der ESP32 oeffnet den Access Point "AkkuController"
(Standard-IP 192.168.4.1). Voraussetzung: Der Rechner ist mit diesem AP
verbunden bzw. kann die IP erreichen.

Beispiele:
    tools/flash_ota.py                       # SuperMini-Image -> 192.168.4.1
    tools/flash_ota.py --variant oled        # OLED-Variante flashen
    tools/flash_ota.py --wait 120            # bis zu 2 min auf das Geraet warten
    tools/flash_ota.py --bin build_oled/keepalive_sw.bin
    tools/flash_ota.py --no-verify           # nicht auf den Neustart warten

Das Skript benoetigt nur die Python-Standardbibliothek.
"""

from __future__ import annotations

import argparse
import http.client
import os
import socket
import sys
import time
from typing import Optional

DEFAULT_HOST = "192.168.4.1"
DEFAULT_PORT = 80
OTA_PATH = "/update"
CHUNK = 4096


def find_default_bin(variant: str) -> Optional[str]:
    """Sucht das Varianten-Image im dist-Layout oder im Quellbaum."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "esp32", f"keepalive_sw_{variant}.bin"),      # dist
        os.path.join(here, os.pardir, f"build_{variant}",
                     "keepalive_sw.bin"),                                # Quelle
        os.path.join(here, "esp32", "keepalive_sw.bin"),                 # alt/dist
        os.path.join(here, os.pardir, "build", "keepalive_sw.bin"),      # alt/Quelle
        os.path.join(here, "keepalive_sw.bin"),                          # daneben
        os.path.join(os.getcwd(), f"build_{variant}", "keepalive_sw.bin"),
        os.path.join(os.getcwd(), "build", "keepalive_sw.bin"),
    )
    for cand in candidates:
        cand = os.path.normpath(cand)
        if os.path.isfile(cand):
            return cand
    return None


def wait_for_host(host: str, port: int, timeout: float) -> bool:
    """Wartet bis host:port eine TCP-Verbindung annimmt."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError as exc:
            attempt += 1
            if attempt % 5 == 1:
                print(f"  ... warte auf {host}:{port} ({exc}). Zum Akku-Controller Wifi verbunden?")
            time.sleep(1)
    return False


def wait_for_reboot(host: str, port: int, timeout: float) -> bool:
    """Wartet, bis das Geraet kurz weg war und wieder antwortet."""
    deadline = time.monotonic() + timeout
    went_down = False
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                pass
            time.sleep(0.5)
        except OSError:
            went_down = True
            break
    if not went_down:
        return False
    return wait_for_host(host, port, timeout)


def upload(host: str, port: int, path: str, connect_timeout: float,
           timeout: float,
           quiet: bool) -> tuple[Optional[int], str, int, int]:
    """Sendet die Datei an /update. Rueckgabe: (status, body, sent, size)."""
    size = os.path.getsize(path)
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    sent = 0
    progress = False
    try:
        conn.putrequest("POST", OTA_PATH)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()   # baut die Verbindung mit connect_timeout auf

        # Fuer die eigentliche Uebertragung (inkl. Flashvorgang) gilt das
        # grosszuegigere --timeout, nicht das kurze Verbindungs-Timeout.
        if conn.sock is not None:
            conn.sock.settimeout(timeout)

        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                if not quiet:
                    progress = True
                    pct = int(sent * 100 / size)
                    sys.stdout.write(
                        f"\r  Sende {pct:3d}%  ({sent}/{size} B)")
                    sys.stdout.flush()

        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace").strip()
        return resp.status, body, sent, size
    except (http.client.HTTPException, OSError) as exc:
        return None, str(exc), sent, size
    finally:
        if progress and not quiet:
            sys.stdout.write("\n")
        conn.close()


def check_status(host: str, port: int, timeout: float) -> bool:
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        resp.read()
        code = resp.status
        conn.close()
        return code == 200
    except OSError:
        return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ESP32 keepalive_sw per Web-OTA aktualisieren.")
    parser.add_argument("--bin", dest="bin_path",
                        help="App-Image (Standard: automatisch gesucht)")
    parser.add_argument("--variant", choices=("supermini", "oled"),
                        default="supermini",
                        help="Hardware-Variante (Standard: supermini)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"ESP-IP (Standard: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP-Port (Standard: {DEFAULT_PORT})")
    parser.add_argument("--wait", type=float, default=30.0,
                        help="Sekunden auf das Geraet warten (0 = nicht warten)")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="HTTP-Timeout in Sekunden")
    parser.add_argument("--connect-timeout", type=float, default=5.0,
                        help="Timeout fuer den Verbindungsaufbau in Sekunden")
    parser.add_argument("--reboot-timeout", type=float, default=30.0,
                        help="Sekunden auf den Neustart warten")
    parser.add_argument("--no-verify", action="store_true",
                        help="Nicht auf den Neustart warten/pruefen")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Weniger Ausgabe")
    args = parser.parse_args(argv)

    bin_path = args.bin_path or find_default_bin(args.variant)
    if not bin_path or not os.path.isfile(bin_path):
        print("FEHLER: Firmware-Image nicht gefunden.", file=sys.stderr)
        print("  --bin <pfad> angeben (z.B. build_supermini/keepalive_sw.bin).",
              file=sys.stderr)
        return 2
    size = os.path.getsize(bin_path)
    if not args.quiet:
        print(f"Variante: {args.variant}")
        print(f"Image: {bin_path} ({size} B)")
        print(f"Ziel : http://{args.host}:{args.port}{OTA_PATH}")

    if args.wait > 0:
        if not wait_for_host(args.host, args.port, args.wait):
            print(f"FEHLER: {args.host}:{args.port} nicht erreichbar.",
                  file=sys.stderr)
            return 1

    status, body, sent, size = upload(
        args.host, args.port, bin_path, args.connect_timeout, args.timeout,
        args.quiet)

    if status == 200:
        print(f"OK: {body or 'Update angenommen'}")
    elif status is not None:
        print(f"FEHLER: HTTP {status} - {body}", file=sys.stderr)
        return 1
    elif sent == 0:
        print(f"FEHLER: keine Verbindung zu {args.host}:{args.port}: {body}",
              file=sys.stderr)
        return 1
    elif sent >= size:
        print("Upload vollstaendig, Verbindung wurde dabei geschlossen.")
        print("Das ist das normale Neustart-Verhalten des ESP32.")
    else:
        print(f"FEHLER: Verbindung nach {sent}/{size} B abgebrochen: {body}",
              file=sys.stderr)
        return 1

    if args.no_verify:
        return 0

    print("Warte auf Neustart ...")
    if not wait_for_reboot(args.host, args.port, args.reboot_timeout):
        print("WARNUNG: Geraet nach dem Neustart nicht erreichbar.",
              file=sys.stderr)
        return 1
    if not check_status(args.host, args.port, 5):
        print("WARNUNG: /api/status antwortet nicht.", file=sys.stderr)
        return 1
    print("Geraet laeuft wieder und antwortet auf /api/status.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
