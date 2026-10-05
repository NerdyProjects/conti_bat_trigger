#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cebs_ble.py -- kleines BLE-POC-Werkzeug fuer das Continental "CEBS"-Display.

Idee
----
Das Display stellt per BLE (GATT) unter der Continental-Basis-UUID
``0000xxxx-006c-6174-6e65-6e69746e6f43`` (der 96-Bit-Teil ist rueckwaerts
gelesenes ASCII "Continental") eine Telemetrie-Service bereit.

Das Werkzeug verbindet sich mit dem Geraet, nimmt **den ersten Vendor-Service
und darin die erste Charakteristik**. Diese 20 Byte sind die Verkettung der
drei BMS-CAN-Rahmen 0x404 (6 B) + 0x405 (8 B) + 0x406 (6 B); die Zuordnung
wurde aus der STM-Firmware rekonstruiert (USART-Nachricht Index 1) und gegen
CAN-Mitschnitte (data/can_log*.csv) geprueft:

    Byte 1..2   Stromstaerke        int16  LE   [mA]   (0x404 b0-1)
    Byte 3..4   Spannung            uint16 LE   [mV]   (0x404 b2-3)
    Byte 5      Ladezustand SOC     uint8       [%]    (0x404 b4, 0xFF = ungueltig)
    Byte 6      SOH                 uint8       [%]    (0x404 b5, Vermutung)
    Byte 7..8   RemainingCapacity   uint16 LE   [mAh]  (0x405 b0-1)
    Byte 9..10  FullChargeCapacity  uint16 LE   [mAh]  (0x405 b2-3)
    Byte 11..14 (unbekannt)                             (0x405 b4-7)
    Byte 15..16 Stromstaerke (dup.) int16  LE   [mA]   (0x406 b0-1)
    Byte 17..20 (unbekannt)                             (0x406 b2-5)

Alle Werte stammen aus einem BQ34Z100-FuelGauge im BMS. Temperatur, Zyklen,
Flags usw. stehen NICHT in dieser Charakteristik (19,5 Grad Raumtemperatur
liessen sich in keinem Feld wiederfinden) -- sie liegen in anderen Rahmen/
Charakteristiken (z. B. CAN 0x415 -> BLE-Charakteristik 0x0a08).

Damit die Auswahl reproduzierbar ist und nicht versehentlich bei Generic
Access (0x1800) landet, wird der *erste Service mit Continental-Basis-UUID*
genommen; innerhalb des Service die Charakteristik mit dem kleinsten
ATT-Handle (="erste"). Mit ``--service``/``--char`` laesst sich alles
uebersteuern, ``--list`` zeigt vorher den GATT-Baum.

Beispiel
--------
    python3 tools/cebs_ble.py                 # suchen, verbinden, 1x/s ausgeben
    python3 tools/cebs_ble.py --interval 0.5  # 2x/s
    python3 tools/cebs_ble.py --json          # eine JSON-Zeile je Messwert
    python3 tools/cebs_ble.py --list          # nur GATT-Baum anzeigen
    python3 tools/cebs_ble.py --address D2:11:09:D2:3B:CF

Voraussetzung
-------------
    pip install bleak

Hinweis: Die BLE-Adresse des Displays ist eine Zufallsadresse und kann sich
nach jedem Neustart aendern -- deshalb ist Scannen (ueber den Namen "CEBS")
der Standard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import time
from typing import List, Optional, Sequence, Tuple

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
except ImportError:  # pragma: no cover - reine Nutzerhilfe
    sys.stderr.write(
        "Fehler: das Modul 'bleak' fehlt.\n"
        "Installation:  pip install bleak\n"
    )
    raise SystemExit(2)

# --------------------------------------------------------------------------
# Konstanten
# --------------------------------------------------------------------------
DEFAULT_NAME = "CEBS"
# Continental-Basis-UUID: der untere Teil ist ASCII "Continental" rueckwaerts.
VENDOR_SUFFIX = "006c-6174-6e65-6e69746e6f43"
DEFAULT_SCAN_S = 10.0
DEFAULT_CONNECT_S = 20.0
DEFAULT_INTERVAL = 1.0
# 0x404 Byte 5 = SOC; die Firmware (f_195A4) schreibt 0xFF als "Daten ungueltig".
SOC_INVALID = 0xFF


# --------------------------------------------------------------------------
# UUID-Helfer
# --------------------------------------------------------------------------
def _norm(uuid: object) -> str:
    """UUID als klein geschriebener String ohne 0x-Praefix."""
    return str(uuid).strip().lower().removeprefix("0x")


def _is_vendor(uuid: object) -> bool:
    """True, wenn die UUID auf der Continental-Basis liegt."""
    return _norm(uuid).endswith(VENDOR_SUFFIX)


def _uuid_matches(uuid: object, spec: str) -> bool:
    """Vergleich erlaubt volle UUID, Kurzform ``0a01`` oder ``0x0a01``."""
    cand = _norm(uuid)
    want = _norm(spec)
    if not want:
        return False
    if len(want) <= 4:                      # Kurzform: auf 16-Bit-Teil pruefen
        return cand.split("-")[0].endswith(want)
    return cand == want


def _hnd(obj: object) -> int:
    """ATT-Handle eines Service/Charakteristik, 0 falls unbekannt."""
    return int(getattr(obj, "handle", 0) or 0)


def _sort_by_handle(items: Sequence[object]) -> List[object]:
    return sorted(items, key=_hnd)


# --------------------------------------------------------------------------
# Geraete-Suche
# --------------------------------------------------------------------------
async def find_cebs(name: str, timeout: float) -> Optional[BLEDevice]:
    """Scannt und liefert das Geraet mit passendem Namen (exakt > Teilstring)."""
    print(f"[scan] suche '{name}' ({timeout:.0f} s) ...", flush=True)
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    exact: Optional[BLEDevice] = None
    partial: Optional[BLEDevice] = None
    for dev, adv in found.values():
        label = adv.local_name or ""
        if label.upper() == name.upper():
            exact = dev
            break
        if partial is None and name.upper() in label.upper():
            partial = dev

    dev = exact or partial
    if dev is not None:
        print(f"[scan] gefunden: {dev.address}  '{name}'", flush=True)
    else:
        print(f"[scan] kein Geraet mit Namen '{name}' gefunden.", flush=True)
    return dev


# --------------------------------------------------------------------------
# Auswahl: erster Vendor-Service + erste Charakteristik
# --------------------------------------------------------------------------
def print_tree(client: BleakClient) -> None:
    """Gibt den kompletten GATT-Baum aus (fuer --list)."""
    for svc in _sort_by_handle(list(client.services)):
        tag = " <- Continental-Vendor" if _is_vendor(svc.uuid) else ""
        print(f"[svc ] {svc.uuid}  handle={_hnd(svc):#06x}{tag}")
        for ch in _sort_by_handle(list(svc.characteristics)):
            print(f"  [chr] {ch.uuid}  handle={_hnd(ch):#06x}  props={ch.properties}")


def select_target(
    client: BleakClient,
    service_spec: Optional[str],
    char_spec: Optional[str],
) -> Tuple[object, object]:
    """Waehlt Service und Charakteristik.

    Reihenfolge:
      * Service: ``--service`` (falls gesetzt), sonst der erste
        **Vendor-Service** (Continental-Basis), sonst der erste Service mit
        Charakteristiken.
      * Charakteristik: ``--char`` (falls gesetzt), sonst die erste
        (kleinstes ATT-Handle) des gewaehlten Service.
    """
    services = _sort_by_handle(list(client.services))

    svc = None
    if service_spec:
        svc = next((s for s in services if _uuid_matches(s.uuid, service_spec)), None)
        if svc is None:
            raise SystemExit(f"Service {service_spec} nicht gefunden (--list zeigt alle).")
    if svc is None:
        svc = next((s for s in services if _is_vendor(s.uuid)), None)
    if svc is None:
        svc = next((s for s in services if list(s.characteristics)), None)
    if svc is None:
        raise SystemExit("Kein Service mit Charakteristiken gefunden.")

    chars = _sort_by_handle(list(svc.characteristics))
    if not chars:
        raise SystemExit(f"Service {svc.uuid} hat keine Charakteristiken.")

    ch = None
    if char_spec:
        ch = next((c for c in chars if _uuid_matches(c.uuid, char_spec)), None)
        if ch is None:
            raise SystemExit(f"Charakteristik {char_spec} in {svc.uuid} nicht gefunden.")
    if ch is None:
        ch = chars[0]
    return svc, ch


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------
def decode_sample(data: bytes) -> dict:
    """20-Byte-Block (BMS 0x404+0x405+0x406) in benannte Felder zerlegen."""
    if len(data) < 4:
        raise ValueError(f"Antwort zu kurz ({len(data)} Byte, mind. 4 noetig)")
    out = {
        "current_ma": struct.unpack_from("<h", data, 0)[0],   # 0x404 b0-1
        "voltage_mv": struct.unpack_from("<H", data, 2)[0],   # 0x404 b2-3
        "soc_percent": None,
        "soh_percent": None,
        "remaining_mah": None,
        "full_mah": None,
        "extra": [],
        "hex": data.hex(" "),
    }
    if len(data) >= 6:
        soc = data[4]
        out["soc_percent"] = None if soc == SOC_INVALID else soc   # 0x404 b4
        out["soh_percent"] = data[5]                               # 0x404 b5
    if len(data) >= 10:
        out["remaining_mah"] = struct.unpack_from("<H", data, 6)[0]  # 0x405 b0-1
        out["full_mah"] = struct.unpack_from("<H", data, 8)[0]       # 0x405 b2-3
    if len(data) >= 20:
        # Unbekannte Restfelder: 0x405 b4-5, 0x405 b6-7, 0x406 b2-3, 0x406 b4-5
        out["extra"] = [struct.unpack_from("<H", data, o)[0] for o in (10, 12, 16, 18)]
    return out


def format_sample(m: dict, json_mode: bool, stamp: bool) -> str:
    if json_mode:
        return json.dumps(
            {
                "t": time.time(),
                "current_ma": m["current_ma"],
                "voltage_v": None if m["voltage_mv"] is None else round(m["voltage_mv"] / 1000.0, 3),
                "soc_percent": m["soc_percent"],
                "soh_percent": m["soh_percent"],
                "remaining_mah": m["remaining_mah"],
                "full_mah": m["full_mah"],
                "extra": m["extra"],
                "hex": m["hex"],
            },
            separators=(",", ":"),
        )
    prefix = time.strftime("%H:%M:%S") + ".%03d " % (time.time() % 1 * 1000,) if stamp else ""
    fields = [f"I={m['current_ma']:+5d} mA"]
    if m["voltage_mv"] is not None:
        fields.append(f"U={m['voltage_mv'] / 1000.0:7.3f} V")
    if m["soc_percent"] is not None:
        fields.append(f"SOC={m['soc_percent']:3d}%")
    if m["soh_percent"] is not None:
        fields.append(f"SOH={m['soh_percent']:3d}%")
    if m["remaining_mah"] is not None:
        fields.append(f"Rem={m['remaining_mah']:5d} mAh")
    if m["full_mah"] is not None:
        fields.append(f"Full={m['full_mah']:5d} mAh")
    return f"{prefix}{'  '.join(fields)}   | {m['hex']}"


# --------------------------------------------------------------------------
# Hauptablauf
# --------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    # --- Geraet finden / verbinden ---------------------------------------
    if args.address:
        target: object = args.address
        print(f"[ble] verbinde mit {args.address} ...", flush=True)
    else:
        dev = await find_cebs(args.name, args.scan_time)
        if dev is None:
            return 3
        target = dev

    async with BleakClient(target, timeout=args.connect_timeout) as client:
        print(f"[ble] verbunden: {client.address}", flush=True)

        if args.list:
            print_tree(client)
            return 0

        svc, ch = select_target(client, args.service, args.char)
        print(f"[ble] Service       : {svc.uuid}")
        print(f"[ble] Charakteristik: {ch.uuid}  props={ch.properties}")

        notify = ("notify" in ch.properties) or ("indicate" in ch.properties)
        print(f"[ble] Modus: {'NOTIFY' if notify else 'POLL'} alle {args.interval:.2f} s")
        print("[ble] Felder: I[0:2] mA, U[2:4] mV, SOC[4] %, SOH[5] %, "
              "Rem[6:8] mAh, Full[8:10] mAh (Little-Endian)")

        count = 0
        next_deadline = time.monotonic()

        def emit(data: bytes) -> None:
            nonlocal count
            try:
                sample = decode_sample(data)
            except ValueError as exc:
                print(f"[warn] {exc}: {data.hex(' ')}", flush=True)
                return
            print(format_sample(sample, args.json, args.timestamp), flush=True)
            count += 1

        if notify:
            await client.start_notify(ch, lambda _c, d: emit(d))
            try:
                while args.count == 0 or count < args.count:
                    await asyncio.sleep(0.2)
            finally:
                try:
                    await client.stop_notify(ch)
                except Exception:  # noqa: BLE001
                    pass
        else:
            while args.count == 0 or count < args.count:
                data = bytes(await client.read_gatt_char(ch))
                emit(data)
                next_deadline += args.interval
                delay = next_deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_deadline = time.monotonic()

    print(f"[ble] fertig ({count} Messwerte).", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cebs_ble.py",
        description="CEBS per BLE verbinden und BMS-Telemetrie kontinuierlich ausgeben (POC).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--name", default=DEFAULT_NAME, help="BLE-Geraetename fuer die Suche")
    p.add_argument("--address", default=None,
                   help="BLE-Adresse direkt (ueberspringt das Scannen)")
    p.add_argument("--service", default=None,
                   help="Service-UUID (voll, oder Kurzform wie 0a00) statt Auto-Auswahl")
    p.add_argument("--char", default=None,
                   help="Charakteristik-UUID (voll, oder Kurzform wie 0a01)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help="Poll-Intervall in Sekunden")
    p.add_argument("--count", type=int, default=0,
                   help="Anzahl Messwerte, 0 = unbegrenzt")
    p.add_argument("--scan-time", type=float, default=DEFAULT_SCAN_S,
                   help="Suchdauer beim Scan in Sekunden")
    p.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_S,
                   help="Verbindungs-Timeout in Sekunden")
    p.add_argument("--json", action="store_true",
                   help="eine JSON-Zeile je Messwert (fuer Skripte)")
    p.add_argument("--no-timestamp", dest="timestamp", action="store_false",
                   help="ohne HH:MM:SS.mmm-Praefix")
    p.add_argument("--list", action="store_true",
                   help="nur den GATT-Baum anzeigen, nichts lesen")
    p.set_defaults(timestamp=True)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[ble] abgebrochen.", flush=True)
        return 130
    except Exception as exc:  # noqa: BLE001 - POC: klare Meldung statt Traceback
        print(f"[fehler] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
