#!/usr/bin/env python3
"""Rohdaten-Trace der BLE-Telemetrie-Charakteristik ``0x0a01`` (20 Byte).

Zweck: herausfinden, wie sich die beiden Stromwerte in dem Block verhalten
(Byte 1-2 und Byte 15-16) - welcher reagiert schnell, welcher traege?

Das Werkzeug liest die Charakteristik zyklisch (Standard 10 Hz) *und* schreibt
zusaetzlich jede eintreffende Notification mit. So ist die Abtastrate nicht
durch die Melderate des Displays begrenzt. Ausgabe: eine Zeile je *Aenderung*
im Terminal, alle Abtastwerte in eine CSV.

Beispiel::

    .venv/bin/python tools/cebs_ble_trace.py --seconds 240 --csv trace.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import struct
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bleak import BleakClient, BleakScanner  # noqa: E402

from cebs_ble import DEFAULT_NAME, _sort_by_handle, _uuid_matches, _is_vendor  # noqa: E402

FIELDS = ("i_inst", "u_mv", "soc", "soh", "rem", "full", "x10", "x12", "i_avg", "x16", "x18")


def decode(data: bytes) -> dict:
    """Benannte Felder des 20-Byte-Blocks (Little-Endian, wie tools/README.md)."""

    def h(offset: int) -> Optional[int]:
        return struct.unpack_from("<h", data, offset)[0] if len(data) >= offset + 2 else None

    def u(offset: int) -> Optional[int]:
        return struct.unpack_from("<H", data, offset)[0] if len(data) >= offset + 2 else None

    return {
        "i_inst": h(0),                                  # 0x404 b0-1
        "u_mv": u(2),                                    # 0x404 b2-3
        "soc": data[4] if len(data) >= 5 else None,      # 0x404 b4
        "soh": data[5] if len(data) >= 6 else None,      # 0x404 b5
        "rem": u(6),                                     # 0x405 b0-1
        "full": u(8),                                    # 0x405 b2-3
        "x10": u(10),                                    # 0x405 b4-5
        "x12": u(12),                                    # 0x405 b6-7
        "i_avg": h(14),                                  # 0x406 b0-1
        "x16": u(16),                                    # 0x406 b2-3
        "x18": u(18),                                    # 0x406 b4-5
    }


async def find_cebs(name: str, wait_s: float, scan_s: float = 8.0):
    """Sucht in Schleife, bis das Geraet auftaucht oder die Zeit abgelaufen ist."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        print(f"[scan] suche '{name}' ({scan_s:.0f} s) ...", flush=True)
        found = await BleakScanner.discover(timeout=scan_s, return_adv=True)
        for dev, adv in found.values():
            label = (adv.local_name or "").strip()
            if label.upper() == name.upper():
                return dev
        print(f"[scan] noch nicht da, weiter ...", flush=True)
    return None


async def select_char(client: BleakClient, service_spec: Optional[str], char_spec: Optional[str]):
    """Erster Vendor-Service, darin die erste Charakteristik (wie cebs_ble.py)."""
    services = _sort_by_handle(list(client.services))
    if service_spec:
        svc = next((s for s in services if _uuid_matches(s.uuid, service_spec)), None)
    else:
        svc = next((s for s in services if _is_vendor(s.uuid)), None)
        svc = svc or next((s for s in services if list(s.characteristics)), None)
    if svc is None:
        raise SystemExit("Kein passender Service gefunden (--list in cebs_ble.py hilft).")
    chars = _sort_by_handle(list(svc.characteristics))
    if char_spec:
        ch = next((c for c in chars if _uuid_matches(c.uuid, char_spec)), None)
        if ch is None:
            raise SystemExit(f"Charakteristik {char_spec} nicht in {svc.uuid}.")
    else:
        ch = chars[0]
    return svc, ch


async def trace(args: argparse.Namespace) -> int:
    dev = await find_cebs(args.name, args.wait)
    if dev is None:
        print(f"[fehler] '{args.name}' nicht gefunden.", flush=True)
        return 2

    print(f"[ble] verbinde mit {dev.address} ...", flush=True)
    async with BleakClient(dev) as client:
        svc, ch = await select_char(client, args.service, args.char)
        print(f"[ble] service={svc.uuid} char={ch.uuid}", flush=True)

        queue: asyncio.Queue = asyncio.Queue()

        def on_notify(_sender, data: bytearray) -> None:
            queue.put_nowait(("notify", bytes(data)))

        notify_ok = False
        try:
            await client.start_notify(ch, on_notify)
            notify_ok = True
        except Exception as exc:  # Notification optional - Lesen genuegt
            print(f"[warn] Notification nicht moeglich: {exc}", flush=True)

        print(f"[ble] verbunden (notify={notify_ok}), trace {args.seconds:.0f} s, "
              f"poll {1.0 / args.interval:.1f} Hz\n", flush=True)

        t0 = time.monotonic()
        last_hex: Optional[str] = None
        n_samples = 0
        n_changes = 0

        with open(args.csv, "w", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(["t_s", "src", "hex", *FIELDS])

            def record(src: str, data: bytes) -> None:
                nonlocal last_hex, n_samples, n_changes
                if not data:
                    return
                t = time.monotonic() - t0
                hex_ = data.hex(" ")
                row = decode(data)
                writer.writerow([f"{t:.3f}", src, hex_, *[row[k] for k in FIELDS]])
                n_samples += 1
                if hex_ != last_hex:
                    n_changes += 1
                    print(
                        f"{t:7.2f}s {src:6s} I={row['i_inst']:6d} mA  Iavg={row['i_avg']:6d} mA"
                        f"  U={row['u_mv']} mV  SOC={row['soc']}  | {hex_}",
                        flush=True,
                    )
                    last_hex = hex_

            while time.monotonic() - t0 < args.seconds:
                while not queue.empty():
                    src, data = queue.get_nowait()
                    record(src, data)
                try:
                    record("read", bytes(await client.read_gatt_char(ch)))
                except Exception as exc:
                    print(f"[warn] Lesefehler: {exc}", flush=True)
                    await asyncio.sleep(1.0)
                fh.flush()
                await asyncio.sleep(args.interval)

        print(f"\n[fertig] {n_samples} Abtastwerte, {n_changes} Aenderungen -> {args.csv}",
              flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--name", default=DEFAULT_NAME, help=f"Geraetename (Standard {DEFAULT_NAME})")
    p.add_argument("--seconds", type=float, default=240.0, help="Aufnahmedauer nach Verbinden")
    p.add_argument("--interval", type=float, default=0.1, help="Poll-Intervall in s (Standard 0.1)")
    p.add_argument("--wait", type=float, default=300.0, help="max. Wartezeit auf das Geraet")
    p.add_argument("--csv", default="ble_trace.csv", help="Zieldatei")
    p.add_argument("--service", default=None, help="Service-UUID erzwingen")
    p.add_argument("--char", default=None, help="Charakteristik-UUID erzwingen")
    args = p.parse_args()
    try:
        return asyncio.run(trace(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
