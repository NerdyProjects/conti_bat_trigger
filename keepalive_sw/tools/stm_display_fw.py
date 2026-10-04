#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stm_display_fw.py -- Werkzeug fuer das STM32F105 "Display" (Continental / CEBS).

Funktionen
----------
* Firmware patchen:  Bytes / 16- oder 32-Bit-Worte an festen Adressen aendern.
* CRC32 (STM32-Hardware-CRC, Poly 0x4C11DB7) ueber den Applikationsbereich
  0x08008000..0x0803FFFB neu berechnen und Big-Endian bei 0x0803FFFC eintragen.
* Pruefen: CRC gegen gespeicherten Wert testen.
* Upload ueber den USB-HID-Bootloader "CEBS Bootloader Mode" (rekonstruiertes
  Protokoll, siehe PROTOCOL.md -- Handshake noch nicht 100% verifiziert).
* Raw-Modus: einzelne 64-Byte-Reports senden, um das Protokoll zu verifizieren.

Bild-Layout (aus dem Dump verifiziert)
--------------------------------------
    0x08000000..0x08007FFF   Bootloader  ("CEBS Bootloader Mode", USB-HID, IAP)
    0x08008000..0x0803FFFB   Applikation ("Continental eBike System")
    0x0803FFFC..0x0803FFFF   CRC32(App) Big-Endian

Voraussetzungen
---------------
    pip install hidapi          # Modulname: hid
Optional (nur fuer .hex):
    intelhex                    # wird hier nicht gebraucht, Parser ist eingebaut.

Autorenhinweis: Die Adressen/Offsets stammen aus dem Disassembly
(data/stm32f105_conti.dis bzw. data/STM_Display_Firmwareupdate_Analyse.md).
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------
# Layout-Konstanten
# --------------------------------------------------------------------------
FLASH_BASE = 0x08000000
FLASH_SIZE = 0x40000  # 256 KiB
BOOTLOADER_BASE = 0x08000000
BOOTLOADER_END = 0x08008000  # exklusiv
APP_BASE = 0x08008000
APP_CRC_ADDR = 0x0803FFFC
APP_CRC_LEN = APP_CRC_ADDR - APP_BASE  # Bytes, ueber die die CRC laeuft

VID_CONTINENTAL = 0x2A8A            # aus lspci/dmesg-Dump
PID_APP = 0x0010                    # "Continental eBike System"
PRODUCT_BOOTLOADER = "CEBS Bootloader"   # Produktstring des Bootloaders
PRODUCT_APP = "Continental eBike System"  # Produktstring der Applikation
REPORT_SIZE = 64

# Frame-Header Host -> Geraet: 02 21 <len> <payload...>
FRAME_HDR0 = 0x02
FRAME_HDR1 = 0x21


# --------------------------------------------------------------------------
# STM32 Hardware CRC32
# --------------------------------------------------------------------------
def crc32_stm32(data: bytes) -> int:
    """CRC32 wie das STM32 CRC-Peripheral (Poly 0x4C11DB7, Init 0xFFFFFFFF,
    keine Reflektion, kein finales XOR). Wortweise little-endian."""
    crc = 0xFFFFFFFF
    n = len(data) & ~3
    for i in range(0, n, 4):
        w = struct.unpack_from("<I", data, i)[0]
        crc ^= w
        for _ in range(32):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x4C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    # Restbytes (sollte bei uns nicht vorkommen) wie Hardware nullen auffuellen
    for b in data[n:]:
        crc ^= b
        for _ in range(32):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x4C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc & 0xFFFFFFFF


# --------------------------------------------------------------------------
# Image laden/speichern (.bin und Intel-HEX)
# --------------------------------------------------------------------------
@dataclass
class Image:
    data: bytearray          # gesamter Bereich ab FLASH_BASE
    base: int = FLASH_BASE

    def __getitem__(self, addr: int) -> int:
        off = addr - self.base
        if off < 0 or off >= len(self.data):
            raise IndexError(hex(addr))
        return self.data[off]

    def slice(self, addr: int, length: int) -> bytes:
        off = addr - self.base
        return bytes(self.data[off:off + length])


def parse_intel_hex(text: str) -> Tuple[int, bytearray]:
    """Minimaler Intel-HEX-Parser. Liefert (base, data)."""
    bytes_out = {}
    ext = 0
    base = None
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith(":"):
            continue
        raw = bytes.fromhex(line[1:])
        count, addr, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
        payload = raw[4:4 + count]
        if rtype == 0x00:
            full = (ext << 16) | addr
            if base is None:
                base = full
            for i, b in enumerate(payload):
                bytes_out[full + i] = b
        elif rtype == 0x01:
            break
        elif rtype == 0x02:
            ext = int.from_bytes(payload, "big") << 4
        elif rtype == 0x04:
            ext = int.from_bytes(payload, "big") << 16
        elif rtype == 0x03 or rtype == 0x05:
            continue
    if base is None:
        raise ValueError("Keine Daten im HEX-File")
    size = max(bytes_out) - base + 1
    data = bytearray(size)
    for a, b in bytes_out.items():
        data[a - base] = b
    return base, data


def load_image(path: Path) -> Image:
    raw = path.read_bytes()
    if path.suffix.lower() == ".hex" or raw.lstrip().startswith(b":"):
        text = raw.decode("ascii", errors="ignore")
        base, data = parse_intel_hex(text)
        return Image(data, base)
    return Image(bytearray(raw), FLASH_BASE)


def save_image(img: Image, path: Path) -> None:
    path.write_bytes(bytes(img.data))


# --------------------------------------------------------------------------
# Patch- und CRC-Operationen
# --------------------------------------------------------------------------
def patch(img: Image, addr: int, payload: bytes) -> None:
    off = addr - img.base
    if off < 0 or off + len(payload) > len(img.data):
        raise ValueError(f"Patch ausserhalb des Images: {addr:#010x}")
    img.data[off:off + len(payload)] = payload


def fix_app_crc(img: Image, verbose: bool = True) -> int:
    """Berechnet CRC32 ueber die Applikation und schreibt sie Big-Endian an
    0x0803FFFC. Gibt die CRC zurueck."""
    if img.base != FLASH_BASE:
        raise ValueError("Image muss bei 0x08000000 beginnen (voller Flash-Dump).")
    app = img.slice(APP_BASE, APP_CRC_LEN)
    crc = crc32_stm32(app)
    struct.pack_into(">I", img.data, APP_CRC_ADDR - img.base, crc)
    if verbose:
        print(f"[crc] App 0x{APP_BASE:08x}..0x{APP_BASE + APP_CRC_LEN - 1:08x} "
              f"-> CRC32 = 0x{crc:08x} (geschrieben @0x{APP_CRC_ADDR:08x})")
    return crc


def verify_app_crc(img: Image) -> bool:
    app = img.slice(APP_BASE, APP_CRC_LEN)
    calc = crc32_stm32(app)
    stored = struct.unpack_from(">I", img.data, APP_CRC_ADDR - img.base)[0]
    ok = calc == stored
    print(f"[crc] berechnet=0x{calc:08x} gespeichert=0x{stored:08x} -> "
          f"{'OK' if ok else 'MISMATCH'}")
    return ok


def diff(img_a: Image, img_b: Image, limit: int = 40) -> int:
    n = min(len(img_a.data), len(img_b.data))
    count = 0
    for i in range(n):
        if img_a.data[i] != img_b.data[i]:
            count += 1
            if count <= limit:
                a = img_a.base + i
                print(f"  0x{a:08x}: {img_a.data[i]:02x} -> {img_b.data[i]:02x}")
    print(f"[diff] {count} abweichende Bytes")
    return count


# --------------------------------------------------------------------------
# HID-Transport
# --------------------------------------------------------------------------
def _import_hid():
    try:
        import hid  # type: ignore
        return hid
    except Exception:  # pragma: no cover
        print("FEHLER: Python-Modul 'hid' (hidapi) fehlt.  ->  pip install hidapi",
              file=sys.stderr)
        raise


def find_devices(product_substr: str, verbose: bool = True):
    """Sucht HID-Geraete anhand des Produktstrings (VID/PID nicht noetig)."""
    hid = _import_hid()
    found = []
    for d in hid.enumerate(0, 0):
        product = (d.get("product_string") or "")
        manufacturer = (d.get("manufacturer_string") or "")
        if product_substr.lower() in product.lower():
            found.append(d)
        elif verbose:
            print(f"  [hid] VID={d.get('vendor_id'):#06x} PID={d.get('product_id'):#06x} "
                  f"{manufacturer!r} {product!r}")
    return found


def find_bootloader(verbose: bool = True):
    """Bootloader ("CEBS Bootloader Mode")."""
    return find_devices(PRODUCT_BOOTLOADER, verbose)


def find_app(verbose: bool = True):
    """Applikation ("Continental eBike System", HID fuer Diagnose/Update-Trigger)."""
    return find_devices(PRODUCT_APP, verbose)


# --------------------------------------------------------------------------
# Reset in den Bootloader (App -> NVIC_SystemReset)
# --------------------------------------------------------------------------
# Die App wertet eingehende Nachrichten im Modul 0x08018xxx aus. Der Matcher
# 0x08018652 bildet ueber die Selector-Tabelle 0x08036E7C und die Definitions-
# Tabelle 0x08036EC4 einen Tabellenindex in 0x20000941:
#     payload[0] = 0x11  -> Definition 1 (Basis-Index 3)
#     payload[1] = 0x01  -> Index 3, id 0x0203 -> fnTable[2]
#     payload[1] = 0x03  -> Index 4, id 0x0304 -> fnTable[3]
# Der Dispatcher 0x08018D04 springt dann ueber Tabelle 0x08036F40 (Byte +13
# = High-Byte der id) auf fnTable[3] = 0x08017378; das schreibt
# SCB->AIRCR = 0x05FA0004 (SYSRESETREQ) -> Neustart. Der Bootloader bleibt
# danach anhand von RCC_CSR/App-CRC im USB-HID-Bootloader ("CEBS Bootloader
# Mode"). Verifiziert: tools/emu.py msgprobe 0x11 bzw. hidtrigger.
TRIGGER_MSG = bytes([0x11, 0x03])


def build_app_report(payload: bytes, wire: str = "len") -> bytes:
    """64-Byte-HID-Report fuer die Applikation bauen.

    Der App-HID-Report ist 64 Byte gross und hat **keine** Report-ID
    (Report-Descriptor: Usage Page 0xFF00, Report Size 8, Count 0x40,
    Input + Output). 'len' stellt - wie im Bootloader-Protokoll - ein
    Laengenbyte voran, 'raw' nutzt den Report direkt. Welche Huelle die App
    erwartet, ist ohne Hardware noch nicht endgueltig bewiesen.
    """
    if wire == "raw":
        body = bytes(payload)
    elif wire == "len":
        if len(payload) > 0xFF:
            raise ValueError("Payload zu gross")
        body = bytes([len(payload)]) + bytes(payload)
    else:
        raise ValueError("wire muss 'raw' oder 'len' sein")
    if len(body) > REPORT_SIZE:
        raise ValueError("Report zu gross")
    return body + bytes(REPORT_SIZE - len(body))


def app_payload_from_report(report: bytes, wire: str = "len") -> bytes:
    """Umkehrung von build_app_report (fuer den Emulator-Test)."""
    if wire == "raw":
        return bytes(report)
    if not report:
        return b""
    return bytes(report[1:1 + report[0]])


def reset_to_bootloader(wire: str = "len", msg: bytes = TRIGGER_MSG,
                        wait_s: float = 15.0, verbose: bool = True) -> int:
    """App per HID-Report dazu bringen, in den Bootloader zu springen.

    1. App-HID oeffnen (VID 0x2A8A/PID 0x0010). Die Enumeration durch den
       Host bringt den App-USB-Port auf Zustand 3 - das ist die Bedingung,
       die das Nachrichtenmodul 0x0800B59C fuer die Annahme verlangt.
    2. Trigger-Report senden (Default '11 03').
    3. Warten, bis der Bootloader ('CEBS Bootloader Mode') auftaucht.

    Rueckgabe: 0 = Bootloader aktiv, 3 = kein App-HID, 4 = kein Wechsel.
    """
    hits = find_app(verbose=verbose)
    if not hits:
        print(f"[reset] keine App-HID-Schnittstelle ({PRODUCT_APP!r}) gefunden.")
        return 3
    tp = Transport(hits[0])
    try:
        rep = build_app_report(msg, wire)
        if verbose:
            print(f"[reset] sende {wire}-Report: {rep[:16].hex(' ')} ...")
        tp.send(rep)
        time.sleep(0.2)
    finally:
        tp.close()

    if verbose:
        print(f"[reset] warte bis {wait_s:.0f} s auf {PRODUCT_BOOTLOADER!r} ...")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if find_bootloader(verbose=False):
            print("[reset] Bootloader aktiv - kann geflasht werden.")
            return 0
        time.sleep(0.3)
    print("[reset] Bootloader ist nicht aufgetaucht; ggf. '--wire raw' testen.")
    return 4


class Transport:
    """Kapselt das Senden/Empfangen von 64-Byte-HID-Reports."""

    def __init__(self, device_info) -> None:
        hid = _import_hid()
        self.dev = hid.device()
        self.dev.open_path(device_info["path"])
        self.dev.set_nonblocking(0)
        self.log: List[Tuple[str, bytes]] = []

    # -- low level ---------------------------------------------------------
    def send(self, report: bytes) -> None:
        if len(report) != REPORT_SIZE:
            report = report + b"\x00" * (REPORT_SIZE - len(report))
        self.log.append(("OUT", report))
        self.dev.write(b"\x00" + report)  # Report-ID 0 voranstellen (hidapi)

    def recv(self, timeout_ms: int = 500) -> Optional[bytes]:
        data = self.dev.read(REPORT_SIZE, timeout_ms)
        if not data:
            return None
        data = bytes(data)
        self.log.append(("IN", data))
        return data

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Protokoll (rekonstruiert aus dem Disassembly)
# --------------------------------------------------------------------------
class DryTransport:
    """Fuer --dry-run: sendet nichts, protokolliert nur."""

    def send(self, report: bytes) -> None:
        return None

    def recv(self, timeout_ms: int = 500):
        return None

    def close(self) -> None:
        return None


class Protocol:
    """
    Host -> Geraet:  ein 64-Byte-Report =  [0x02, 0x21, len, payload...]
                     payload[0] = Kommandocode.

    Verifizierte Kommandos (siehe Analyse):
        0x34  Adresse setzen:   payload = 34 LL HH <addr32 BE> ...
              (LL/HH = Nibbles aus dem Original-Handler; addr32 = payload[3..6])
        0x36  Block schreiben:  payload = 36 <seq> <data...>,  len(data) <= 56
              (Schreibzeiger wird automatisch weitergeschoben)
        0x10/0x11/0x22/0x27   Session/Handshake (rekonstruiert, Reihenfolge offen)
        0x37/0x3E             Abschluss/Status

    Geraet -> Host: u. a. 20-Byte-Statusreport beginnend mit 57 01 20,
    oder 64-Byte-Antwort.
    """

    def __init__(self, tp: Transport, verbose: bool = True,
                 wire: str = "raw") -> None:
        self.tp = tp
        self.verbose = verbose
        self.wire = wire            # "raw" (USB) oder "hdr" (CAN-Rahmen)
        self.seq = 1
        self.addr = APP_BASE

    # -- Frames ------------------------------------------------------------
    def frame(self, payload: bytes) -> bytes:
        """USB-HID-Report bauen.

        Der Bootloader liest die Laenge aus [0x20000206] und den Payload ab
        +1 -- das ist der komplette 64-Byte-HID-Block (Byte 0 = Laenge).
        Mit wire='hdr' wird die im CAN-Pfad (0x08000AFC) geprüfte Variante
        '02 21 <len> <payload>' gesendet.
        """
        if self.wire == "hdr":
            if len(payload) > REPORT_SIZE - 3:
                raise ValueError("payload zu gross")
            return bytes([FRAME_HDR0, FRAME_HDR1, len(payload)]) + payload
        if len(payload) > REPORT_SIZE - 1:
            raise ValueError("payload zu gross")
        return bytes([len(payload)]) + payload

    def send_frame(self, payload: bytes) -> None:
        rep = self.frame(payload)
        if self.verbose:
            print(f"  -> {rep[:3].hex(' ')} | {payload.hex(' ')}")
        self.tp.send(rep)

    def expect_ack(self, timeout_ms: int = 800) -> Optional[bytes]:
        r = self.tp.recv(timeout_ms)
        if r and self.verbose:
            print(f"  <- {r[:20].hex(' ')}")
        return r

    # -- Kommandos ---------------------------------------------------------
    def cmd_enter_program(self) -> None:
        """0x10 = Modus setzen (Payload-Laenge 2, Arg 2/3/0x82/0x83).

        Nur Arg 2 und 3 (bzw. 0x82/0x83) setzen das 'Gueltig'-Flag
        [0x20000028]; 0x34 verlangt dieses Flag. Antwort im Emulator:
        '50 03 01 f4 03 e8'.
        """
        self.send_frame(bytes([0x10, 0x03]))
        ack = self.expect_ack()
        if ack and self.verbose:
            print(f"     Modus/Info = {ack[:6].hex(' ')}")

    def cmd_sequencer_start(self) -> None:
        """0x37 = Sequencer zuruecksetzen (Seq = 1, Zeiger = 0). Antwort 0x77."""
        self.send_frame(bytes([0x37]))
        self.expect_ack()

    def cmd_set_address(self, addr: int, nib: int = 0x03) -> None:
        """0x34: Schreibadresse setzen (32 Bit Big-Endian).

        Der Handler prueft eine Payload-Laenge von 11 Bytes (Zaehler == 11 in
        data/PROTOCOL.md). Nutzt payload[1] (Nibbles) und payload[3..6] (Adresse),
        Rest ist Fuellung.
        """
        payload = bytes([0x34, nib, 0x00]) + struct.pack(">I", addr) + bytes(4)
        assert len(payload) == 11
        self.send_frame(payload)
        self.addr = addr
        self.expect_ack()

    def cmd_write_block(self, data: bytes) -> None:
        """0x36: bis zu 56 Datenbytes schreiben, Schreibzeiger laeuft weiter."""
        if len(data) > 56:
            raise ValueError("max 56 Bytes pro 0x36-Block")
        payload = bytes([0x36, self.seq]) + data
        self.send_frame(payload)
        self.seq = self.seq + 1 if self.seq < 255 else 1
        self.addr += len(data)
        self.expect_ack()

    def cmd_finish(self) -> None:
        """0x3E = Abschluss (Payload-Laenge 2, Arg 0x00 oder 0x80).

        Arg 0x80 -> keine Antwort; der Bootloader laeuft danach in seinen
        Timeout und macht einen Software-Reset (SCB->AIRCR = 0x05FA0004),
        wodurch die App startet. Arg 0x00 antwortet mit 0x7E.
        """
        self.send_frame(bytes([0x3E, 0x80]))

    # -- Gesamt-Upload -----------------------------------------------------
    def upload(self, img: Image, region: str = "app", chunk: int = 56) -> None:
        if region == "app":
            start, stop = APP_BASE, APP_CRC_ADDR + 4
        elif region == "all":
            start, stop = FLASH_BASE, FLASH_BASE + len(img.data)
        else:
            raise ValueError("region muss 'app' oder 'all' sein")
        blob = img.slice(start, stop - start)
        print(f"[upload] {region}: 0x{start:08x}..0x{stop - 1:08x} ({len(blob)} Bytes)")
        self.cmd_enter_program()
        self.cmd_sequencer_start()
        self.cmd_set_address(start)
        sent = 0
        for i in range(0, len(blob), chunk):
            self.cmd_write_block(blob[i:i + chunk])
            sent += len(blob[i:i + chunk])
            if sent % (chunk * 20) == 0:
                print(f"         {sent}/{len(blob)}")
        self.cmd_finish()
        print("[upload] fertig (Status vom Geraet pruefen!)")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _patch_spec(spec: str) -> Tuple[int, bytes]:
    """Formate:  '0x08001234=DEADBEEF'  oder  '0x08001234=90,90' """
    addr_s, val_s = spec.split("=", 1)
    addr = int(addr_s, 0)
    val_s = val_s.strip()
    if "," in val_s:
        data = bytes(int(b, 0) for b in val_s.split(","))
    else:
        val_s = val_s.replace(" ", "")
        if len(val_s) % 2:
            val_s = "0" + val_s
        data = bytes.fromhex(val_s)
    return addr, data


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="STM32F105 Display Firmware-Tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("info", help="HID-Geraete auflisten / Bootloader suchen")
    sp.add_argument("-i", "--image", type=Path, help="optional: Image-Info zeigen")

    sp = sub.add_parser("patch", help="Bytes/Worte patchen")
    sp.add_argument("image", type=Path)
    sp.add_argument("-o", "--out", type=Path, required=True)
    sp.add_argument("-s", "--set", action="append", default=[], metavar="ADDR=HEX",
                    help="z. B. -s 0x08012340=90,90 -s 0x08012350=DEADBEEF")
    sp.add_argument("--fix-crc", action="store_true", help="App-CRC neu berechnen")

    sp = sub.add_parser("fixcrc", help="nur App-CRC neu berechnen")
    sp.add_argument("image", type=Path)
    sp.add_argument("-o", "--out", type=Path, required=True)

    sp = sub.add_parser("verify", help="App-CRC pruefen")
    sp.add_argument("image", type=Path)

    sp = sub.add_parser("diff", help="zwei Images vergleichen")
    sp.add_argument("a", type=Path)
    sp.add_argument("b", type=Path)

    sp = sub.add_parser("reset", help="App per HID zum Bootloader-Reset bewegen")
    sp.add_argument("--wire", choices=["len", "raw"], default="len",
                    help="len = [len] 11 03 ... (wie Bootloader-Protokoll), "
                         "raw = 11 03 ... direkt")
    sp.add_argument("--msg", default=TRIGGER_MSG.hex(" "),
                    help="Trigger-Bytes, Default '%(default)s'")
    sp.add_argument("--wait", type=float, default=15.0,
                    help="Sekunden auf den Bootloader warten")

    sp = sub.add_parser("upload", help="per HID-Bootloader hochladen")
    sp.add_argument("image", type=Path)
    sp.add_argument("--region", choices=["app", "all"], default="app")
    sp.add_argument("--wire", choices=["raw", "hdr"], default="raw",
                    help="raw = [len][payload] (USB, emuliert verifiziert), "
                         "hdr = 02 21 <len> <payload> (CAN-Rahmen)")
    sp.add_argument("--dry-run", action="store_true",
                    help="nur anzeigen, nichts senden")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Sicherheitsabfrage ueberspringen")
    sp.add_argument("-q", "--quiet", action="store_true")

    sp = sub.add_parser("raw", help="rohen 64-Byte-Report senden (Protokoll testen)")
    sp.add_argument("hexbytes", help="z. B. '02 21 02 34 03'")

    args = p.parse_args(argv)

    if args.cmd == "info":
        print("Suche HID-Geraete ...")
        all_hits = find_bootloader(verbose=True)
        bl = find_devices(PRODUCT_BOOTLOADER, verbose=False)
        app = find_devices(PRODUCT_APP, verbose=False)
        print(f"\nBootloader ('{PRODUCT_BOOTLOADER}'): {len(bl)}")
        for d in bl:
            print(f"  {d.get('path')}  VID={d.get('vendor_id'):#06x} "
                  f"PID={d.get('product_id'):#06x} {d.get('product_string')!r}")
        print(f"App ('{PRODUCT_APP}'): {len(app)}")
        for d in app:
            print(f"  {d.get('path')}  VID={d.get('vendor_id'):#06x} "
                  f"PID={d.get('product_id'):#06x} {d.get('product_string')!r}")
        if args.image:
            img = load_image(args.image)
            print(f"\nImage {args.image}: base=0x{img.base:08x} size={len(img.data)}")
            verify_app_crc(img)
        return 0

    if args.cmd == "patch":
        img = load_image(args.image)
        for spec in args.set:
            addr, data = _patch_spec(spec)
            patch(img, addr, data)
            print(f"[patch] 0x{addr:08x} <- {data.hex(' ')}")
        if args.fix_crc:
            fix_app_crc(img)
        save_image(img, args.out)
        print(f"[ok] geschrieben: {args.out}")
        return 0

    if args.cmd == "fixcrc":
        img = load_image(args.image)
        fix_app_crc(img)
        save_image(img, args.out)
        print(f"[ok] geschrieben: {args.out}")
        return 0

    if args.cmd == "verify":
        img = load_image(args.image)
        return 0 if verify_app_crc(img) else 1

    if args.cmd == "diff":
        return 0 if diff(load_image(args.a), load_image(args.b)) else 0

    if args.cmd == "reset":
        msg = bytes(int(b, 16) for b in args.msg.split())
        return reset_to_bootloader(wire=args.wire, msg=msg, wait_s=args.wait)

    if args.cmd == "upload":
        img = load_image(args.image)
        if not verify_app_crc(img):
            print("WARNUNG: App-CRC stimmt nicht -- erst 'fixcrc' laufen lassen!")
            if not args.yes and not args.dry_run:
                print("Abbruch (mit -y erzwingen).")
                return 2
        proto = None
        if not args.dry_run:
            hits = find_bootloader(verbose=False)
            if not hits:
                print("Kein Bootloader gefunden. Ist das Geraet im 'CEBS Bootloader Mode'?")
                print("Tipp: Reset der App (NVIC_SystemReset) bzw. SWD-Reset erzwingt ihn.")
                return 3
            proto = Protocol(Transport(hits[0]), verbose=not args.quiet,
                             wire=args.wire)
        else:
            proto = Protocol(DryTransport(), verbose=not args.quiet,  # type: ignore
                             wire=args.wire)

        if not args.dry_run and not args.yes:
            print(f"ACHTUNG: schreibt 0x{args.region}-Region in den Flash. Fortsetzen? [j/N] ",
                  end="")
            if input().strip().lower() not in ("j", "y", "ja", "yes"):
                return 2
        try:
            proto.upload(img, region=args.region)
        finally:
            if proto is not None and getattr(proto, "tp", None) is not None:
                proto.tp.close()
        return 0

    if args.cmd == "raw":
        data = bytes(int(b, 16) for b in args.hexbytes.split())
        hits = find_bootloader(verbose=False)
        if not hits:
            print("Kein Bootloader gefunden.")
            return 3
        tp = Transport(hits[0])
        try:
            print(f"-> {data.hex(' ')}")
            tp.send(data)
            r = tp.recv(1000)
            print(f"<- {r.hex(' ') if r else '(keine Antwort)'}")
        finally:
            tp.close()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
