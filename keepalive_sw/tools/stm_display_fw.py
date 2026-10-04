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
from typing import Dict, List, Optional, Tuple

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

# Rueckgabe des CRC-Befehls (0x31 / Unterkommando 0x10202).
# Der Handler 0x08001414 vergleicht CRC32(0x08008000 + 0x00037FFC) mit dem
# Wort bei 0x0803FFFC und legt das Ergebnis als Statusbyte in die Antwort.
# Der Antwortbau 0x0800130C schickt **0 = gleich** und **1 = ungleich**
# (im Emulator gegen den echten Bootloadercode bestaetigt).
CRC_OK = 0
CRC_MISMATCH = 1

# Geraeterekord: letzte 2-KiB-Seite des Bootloaders und die einzige
# Flash-Adresse, die der Bootloader ueberhaupt auslesen kann (Kommando 0x22,
# Handler 0x08000F54). Im Original leer (0xFF).
RECORD_ADDR = 0x08007800
RECORD_MAGIC = 0xF15B

VID_CONTINENTAL = 0x2A8A            # aus lspci/dmesg-Dump
PID_APP = 0x0010                    # "Continental eBike System"
PRODUCT_BOOTLOADER = "CEBS Bootloader"   # Produktstring des Bootloaders
PRODUCT_APP = "Continental eBike System"  # Produktstring der Applikation
REPORT_SIZE = 64

# Frame-Header Host -> Geraet: 02 21 <len> <payload...>
FRAME_HDR0 = 0x02
FRAME_HDR1 = 0x21

# --------------------------------------------------------------------------
# App-HID-Protokoll (rueckentwickelt aus data/stm32f105_conti.dis)
#
# Der App-Report ist 64 Byte gross und hat keine Report-ID. Aufbau:
#
#   [0]     Sequenzzaehler  muss dem Zaehler *(0x20000BAC) der App entsprechen
#   [1]     Rahmentyp       1 = ein Tunnel-/CAN-Frame, 0 = bis zu 5 Frames
#                           a 11 Byte, 2 = Variante, 3 = Datenkanal,
#                           0xFD/0xFE/0xFF = Steuerrahmen
#   [2]     0x3D            feste Marke (nur Typ 1)
#   [3..4]  Kanal-ID        u16 little-endian, nur freigegebene IDs
#   [5]     Nutzlaenge      <= 58
#   [6..]   Nutzdaten
#
# Belege im Disassembly: Sender 0x0801D1DA, Typverteilung 0x0801D634
# (Typ 1 ab 0x0801D75C), Verpackung 0x0801D5C2, FIFO-Auswertung 0x0801D900,
# ID-Freigabe 0x0801D4E2 (Tabelle 0x08037B3A). Der Block, den 0x0801D5C2
# erzeugt, ist [id_lo][id_hi][len][daten] und wird von 0x0801D900 als
# Nachricht ans Nachrichtenmodul gegeben (Objekt *(0x2000092C), [+16] =
# Block+3 = Nutzdaten, [+12] = Laenge).
# --------------------------------------------------------------------------
SYNC_TYPE = 0xFE            # Typ 0xFE setzt den Zaehler der App auf 0
FRAME_TYPE_TUNNEL = 0x01
FRAME_MARK = 0x3D
FRAME_MAX_PAYLOAD = 58

# Kanal-IDs, die 0x0801D4E2 durchlaesst (Flash-Tabelle 0x08037B3A)
HOST_CHANNEL_IDS = (0x0550, 0x0552, 0x0422, 0x0425,
                    0x0101, 0x0331, 0x0668, 0x0300)
HOST_CHANNEL_DEFAULT = 0x0550


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
    """Minimaler Intel-HEX-Parser. Liefert (base, data).

    `ext` wird als bereits vollstaendig verschobene Basisadresse gefuehrt:
      Typ 02 (Segment)  -> ext = payload << 4
      Typ 04 (linear)   -> ext = payload << 16
    und dann mit `ext + addr` kombiniert.
    """
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
            full = ext + addr
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


def build_app_frame(payload: bytes, seq: int = 0,
                    channel: int = HOST_CHANNEL_DEFAULT,
                    ftype: int = FRAME_TYPE_TUNNEL) -> bytes:
    """Echten App-Rahmen als 64-Byte-Report bauen (siehe Protokoll oben).

    Fuer ``ftype == FRAME_TYPE_TUNNEL`` entsteht
    ``[seq][0x01][0x3D][id_lo][id_hi][len][payload...]``; fuer Steuerrahmen
    (z. B. ``SYNC_TYPE``) nur ``[seq][typ][...]``.
    """
    if len(payload) > FRAME_MAX_PAYLOAD:
        raise ValueError(f"Nutzlast zu gross (max {FRAME_MAX_PAYLOAD} Byte)")
    body = bytearray(REPORT_SIZE)
    body[0] = seq & 0xFF
    body[1] = ftype & 0xFF
    if ftype == FRAME_TYPE_TUNNEL:
        if channel not in HOST_CHANNEL_IDS:
            raise ValueError(
                f"Kanal {channel:#06x} nicht freigegeben; erlaubt: "
                + " ".join(f"{c:#06x}" for c in HOST_CHANNEL_IDS))
        body[2] = FRAME_MARK
        body[3] = channel & 0xFF
        body[4] = (channel >> 8) & 0xFF
        body[5] = len(payload)
        body[6:6 + len(payload)] = payload
    else:
        body[2:2 + len(payload)] = payload
    return bytes(body)


def build_sync_frame(seq: int = 0) -> bytes:
    """Typ-0xFE-Steuerrahmen: die App setzt ihren Sequenzzaehler auf 0.

    0x0801D744 (Zweig fuer Typ 0xFE) schreibt bedingungslos 0 nach
    *(0x20000BAC) - damit ist der naechste Rahmen mit Sequenz 0 gueltig.
    """
    return build_app_frame(b"", seq=seq, ftype=SYNC_TYPE)


def parse_app_frame(report: bytes) -> Optional[Tuple[int, int, int, bytes]]:
    """App-Rahmen zerlegen -> ``(seq, typ, kanal, nutzdaten)`` oder ``None``."""
    if len(report) < 6 or report[1] != FRAME_TYPE_TUNNEL:
        return None
    if report[2] != FRAME_MARK:
        return None
    n = report[5]
    if n > FRAME_MAX_PAYLOAD:
        return None
    return (report[0], report[1], report[3] | (report[4] << 8),
            bytes(report[6:6 + n]))


def ack_payload(report: Optional[bytes]) -> Optional[bytes]:
    """Payload einer Antwort -> ``bytes`` oder ``None`` (unbrauchbare Antwort).

    Das eigentliche ACK steht NICHT am Anfang des Reports, sondern im
    App-Rahmen an Offset 6::

        00 01 3d 51 05 01 77 ...
        \\__/ \\__/ \\__/ \\__/ |  |
         seq  01   3d  0551  |  Nutzlast (1 Byte) = 0x77 = 0x37 | 0x40
                             Laenge
    """
    pf = parse_app_frame(report) if report else None
    return pf[3] if pf else None


def _label_for(d) -> Optional[str]:
    """Produktstring eines HID-Eintrags klassifizieren."""
    low = (d.get("product_string") or "").lower()
    if PRODUCT_BOOTLOADER.lower() in low:
        return "BOOTLOADER"
    if PRODUCT_APP.lower() in low:
        return "APP"
    return None


def watch_devices(seconds: float = 10.0, interval: float = 0.05) -> int:
    """Beobachtet, ob App und Bootloader auftauchen und wieder verschwinden.

    Erkennt eine Reset-Schleife: kommt 'Continental eBike System' periodisch
    wieder, laeuft die App in einem Reboot-Zyklus. Wichtig: ein Reset, der
    NICHT ueber SFTRST (= NVIC_SystemReset) laeuft, landet laut
    Boot-Entscheidung (0x08001A92) wieder in der App - nur SFTRST bleibt im
    Bootloader.
    """
    hid = _import_hid()
    t0 = time.time()
    present: Dict[str, str] = {}
    starts: List[float] = []
    n_bl = 0
    print(f"[watch] beobachte {seconds:.0f} s (Abtastung {interval * 1000:.0f} ms)")
    while True:
        elapsed = time.time() - t0
        if elapsed >= seconds:
            break
        cur: Dict[str, str] = {}
        for d in hid.enumerate(0, 0):
            if d.get("vendor_id") != VID_CONTINENTAL:
                continue
            lab = _label_for(d)
            if lab:
                cur[str(d["path"])] = lab
        for path, lab in cur.items():
            if present.get(path) != lab:
                print(f"  {elapsed * 1000:9.1f} ms  + {lab:<11}{path}")
                if lab == "APP":
                    starts.append(elapsed)
                else:
                    n_bl += 1
        for path, lab in present.items():
            if path not in cur:
                print(f"  {elapsed * 1000:9.1f} ms  - {lab:<11}{path}")
        present = cur
        time.sleep(interval)

    print(f"[watch] App-Erscheinungen = {len(starts)}, "
          f"Bootloader-Erscheinungen = {n_bl}")
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    if gaps:
        print(f"[watch] mittlerer Abstand = {sum(gaps) / len(gaps) * 1000:.0f} ms")
    if n_bl:
        print("[watch] Bootloader war sichtbar -> 'upload' kann direkt laufen.")
    elif len(starts) > 1:
        print("[watch] App kommt immer wieder -> Reset-Schleife.")
        print("        Solche Resets sind kein SFTRST, deshalb landet der BL")
        print("        wieder in der App. Fuer den BL muss die App selbst per")
        print("        NVIC_SystemReset (0x08017378) neu starten.")
    return 0


def reset_to_bootloader(channel: int = HOST_CHANNEL_DEFAULT,
                        msg: bytes = TRIGGER_MSG,
                        seq: Optional[int] = None,
                        wait_s: float = 20.0, repeat: int = 12,
                        interval_ms: int = 150,
                        legacy_wire: Optional[str] = None,
                        verbose: bool = True) -> int:
    """App per HID-Rahmen in den Bootloader bewegen.

    Ablauf (jeder Schritt aus dem Dump belegt):

    1. App-HID oeffnen (VID 0x2A8A/PID 0x0010, "Continental eBike System").
    2. Steuerrahmen Typ 0xFE senden -> die App setzt ihren Sequenzzaehler
       0x20000BAC auf 0 (Empfaengerpfad 0x0801D744).
    3. Rahmen Typ 1 senden: Sequenz 0, Kanal-ID, Nutzlast ``11 03``.
       0x0801D75C prueft Sequenz/Marke/Laenge und gibt ihn an 0x0801D5C2;
       von dort geht er in FIFO A, 0x0801D900 macht daraus eine Nachricht
       fuers Nachrichtenmodul. Index 4 -> Tabelle 0x08036F40 -> fnTable[3]
       = 0x08017378 -> SCB->AIRCR = 0x05FA0004 (SYSRESETREQ).
       Nur SFTRST laesst den Bootloader aktiv (0x08001A92), deshalb ist
       genau dieser Weg noetig.
    4. Auf "CEBS Bootloader Mode" warten.

    Der Steuerrahmen wird vor jedem Versuch erneut gesendet, damit Schritt 3
    unabhaengig vom aktuellen Zaehlerstand immer gueltig ist.

    Rueckgabe: 0 = Bootloader aktiv, 3 = kein App-HID, 4 = kein Wechsel,
               5 = Rechte-/Oeffnungsfehler.
    """
    if legacy_wire:
        frames = [build_app_report(msg, legacy_wire)]
        print(f"[reset] Legacy-Report ({legacy_wire}, 64 B): "
              f"{frames[0][:8].hex(' ')} ...")
    else:
        s = 0 if seq is None else (seq & 0xFF)
        sync = build_sync_frame(0)
        trig = build_app_frame(msg, seq=s, channel=channel)
        frames = [sync, trig]
        print(f"[reset] Kanal {channel:#06x}, Nutzlast {msg.hex(' ')}")
        print(f"[reset] Sync-Rahmen : {sync[:8].hex(' ')} ...")
        print(f"[reset] Trigger     : {trig[:8].hex(' ')} ...")

    deadline = time.time() + wait_s
    attempt = 0
    opened_once = False
    while time.time() < deadline and attempt < repeat:
        attempt += 1
        hits = find_app(verbose=bool(verbose and attempt == 1))
        if hits:
            try:
                tp = Transport(hits[0])
            except Exception as exc:
                print(f"[reset] {exc}")
                return 5
            try:
                for fr in frames:
                    tp.send(fr)
                    time.sleep(0.02)
                opened_once = True
                if verbose:
                    print(f"[reset] Versuch {attempt}: "
                          f"{len(frames)} Rahmen gesendet")
            finally:
                tp.close()
        if find_bootloader(verbose=False):
            print(f"[reset] Bootloader aktiv nach Versuch {attempt} "
                  f"- kann geflasht werden.")
            return 0
        time.sleep(interval_ms / 1000.0)

    if not opened_once:
        print(f"[reset] keine App-HID-Schnittstelle ({PRODUCT_APP!r}) gefunden.")
        print("        Sichtbar?  python3 tools/stm_display_fw.py info")
        return 3
    print("[reset] Bootloader ist nicht aufgetaucht.")
    print("        Schleife ansehen:      watch")
    print("        Rechte (udev) pruefen: siehe tools/99-continental-ebike.rules")
    return 4


def probe_protocol(listen_s: float = 1.0, wait_s: float = 1.0,
                   verbose: bool = True) -> int:
    """Reine Lese-Diagnose des Bootloader-Protokolls.

    Es wird **nichts** in den Flash geschrieben. Gesendet werden nur
    0x37 (Sequencer-Reset), 0x10 (Modus setzen) und 0x34 (Adresse setzen) --
    alle drei veraendern nur RAM-Zustand. Ziel ist zu sehen, wie das Geraet
    tatsaechlich antwortet (volles 64-Byte-Report, mit Zeitstempel).
    """
    hits = find_bootloader(verbose=verbose)
    if not hits:
        print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
        return 3
    tp = Transport(hits[0])
    try:
        print(f"\n[probe] Phase 1: {listen_s:.1f} s nur zuhoeren ...")
        t0 = time.time()
        nspont = 0
        shown: List[bytes] = []
        while time.time() - t0 < listen_s:
            r = tp.recv(100)
            if r:
                nspont += 1
                if len(shown) < 3:
                    shown.append(r)
        print(f"[probe] spontane Reports: {nspont} in {listen_s:.1f} s")
        for r in shown:
            print(f"        {r.hex(' ')}")

        tests = [
            ("0x37 Sequencer-Reset", bytes([0x37])),
            ("0x10 Modus setzen", bytes([0x10, 0x03])),
            ("0x34 Adresse setzen",
             bytes([0x34, 0x03, 0x00]) + struct.pack(">I", APP_BASE) + bytes(4)),
        ]
        acks = 0
        for name, payload in tests:
            rep = bytes([len(payload)]) + payload
            want = (payload[0] | 0x40) & 0xFF
            print(f"\n[probe] {name}\n        -> {rep[:3].hex(' ')} | "
                  f"{payload.hex(' ')}")
            tp.send(rep)
            t0 = time.time()
            got = 0
            while time.time() - t0 < wait_s:
                r = tp.recv(100)
                if not r:
                    continue
                got += 1
                head = r[0] if r[0] else (r[1] if len(r) > 1 else 0)
                is_ack = head == want
                acks += 1 if is_ack else 0
                print(f"        [{'ACK' if is_ack else '   '}] {r.hex(' ')}")
            if got == 0:
                print("        (keine Antwort)")
        print(f"\n[probe] kommandospezifische ACKs: {acks}/{len(tests)}")
        if acks == 0:
            print("[probe] ERGEBNIS: Das Geraet bestaetigt die Kommandos NICHT.")
            print("        -> Das rekonstruierte Protokoll passt nicht; mit")
            print("           'upload' wird nichts geschrieben. Siehe PROTOCOL.md")
            print("           (Handshake dort als [R] = rekonstruiert markiert).")
            return 1
        print("[probe] ERGEBNIS: Kommandos werden bestaetigt, Protokoll passt.")
        return 0
    finally:
        tp.close()


def sweep_framing(wait_s: float = 0.6, verbose: bool = True) -> int:
    """Probiert mehrere Rahmungen fuer Kommando 0x37 durch.

    Erwartete Quittung waere 0x77 (0x37 | 0x40) irgendwo in der Antwort.
    Schreibt nichts in den Flash -- 0x37 setzt nur den Sequencer zurueck.
    """
    hits = find_bootloader(verbose=False)
    if not hits:
        print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
        return 3
    variants = [
        ("len + payload", bytes([0x01, 0x37])),
        ("02 21 len + payload", bytes([0x02, 0x21, 0x01, 0x37])),
        ("nur 0x37", bytes([0x37])),
        ("len=2, 37", bytes([0x02, 0x37])),
        ("len=0x3f + payload", bytes([0x3F, 0x37])),
        ("0x37 + 63 Fuellbytes", bytes([0x37]) + bytes(63)),
        ("Muell aa bb cc", bytes([0xAA, 0xBB, 0xCC])),
    ]
    tp = Transport(hits[0])
    try:
        print("[sweep] Kommando 0x37, erwartete Quittung 0x77\n")
        for name, rep in variants:
            rep = rep + bytes(max(0, REPORT_SIZE - len(rep)))
            print(f"[sweep] {name}\n        -> {rep[:6].hex(' ')} …")
            tp.send(rep)
            t0 = time.time()
            n = 0
            while time.time() - t0 < wait_s:
                r = tp.recv(100)
                if not r:
                    continue
                n += 1
                pos = [i for i, b in enumerate(r) if b == 0x77]
                tag = f"0x77 bei Offset {pos}" if pos else "kein 0x77"
                print(f"        [{tag}] {r[:24].hex(' ')}")
            if n == 0:
                print("        (keine Antwort)")
            print()
    finally:
        tp.close()
    return 0


def resettest(wait_s: float = 3.0, verbose: bool = True) -> int:
    """Prueft, ob der Bootloader unsere Kommandos wirklich *versteht*.

    Die Kanal-Antwort ist bei jeder Eingabe identisch (siehe `sweep`), taugt
    also nicht als Signal. Verwertbar ist dagegen der **Selbst-Reset**:

      * `0x3E 0x80` (Abschluss) loest bei verstandenem Kommando einen
        Software-Reset aus -> das Geraet verschwindet als Bootloader und
        erscheint als App.
      * Ein **ungueltiges** Kommando darf das *nicht* tun.

    Nur wenn (1) resettet und (2) nicht, laeuft der Kommando-Dispatcher und
    damit auch der Flash-Schreibpfad. Schreibt selbst nichts in den Flash.
    """
    if not find_bootloader(verbose=False):
        print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
        print("Bitte das Geraet zuerst in den Bootloader bringen.")
        return 3

    plan = [
        ("ungueltiges Kommando 0xAA", bytes([0x01, 0xAA]), False),
        ("0x3E 0x80 (Abschluss)", bytes([0x02, 0x3E, 0x80]), True),
    ]
    for name, payload, expect_reset in plan:
        hits = find_bootloader(verbose=False)
        if not hits:
            print(f"[resettest] Bootloader schon vor '{name}' weg -- Abbruch.")
            return 4
        tp = Transport(hits[0])
        try:
            rep = bytes([len(payload)]) + payload
            rep = rep + bytes(max(0, REPORT_SIZE - len(rep)))
            print(f"[resettest] {name}\n        -> {rep[:5].hex(' ')} …")
            tp.send(rep)
            t0 = time.time()
            got = 0
            while time.time() - t0 < 0.8:
                r = tp.recv(100)
                if r:
                    got += 1
        finally:
            tp.close()
        time.sleep(wait_s)
        still_bl = bool(find_bootloader(verbose=False))
        app = bool(find_app(verbose=False))
        print(f"        Antworten={got}  Bootloader_da={still_bl}  App={app}")
        if expect_reset:
            if not still_bl:
                print("[resettest] ERGEBNIS: 0x3E 0x80 hat resettet -> das "
                      "Geraet VERSTEHT unsere Kommandos.")
                return 0
            print("[resettest] ERGEBNIS: 0x3E 0x80 hat NICHT resettet -> "
                  "Kommandos werden nicht verarbeitet.")
            return 1
        if not still_bl:
            print("[resettest] WARNUNG: auch das ungueltige Kommando hat "
                  "resettet -> der Reset haengt nicht am Kommando "
                  "(z. B. Inaktivitaets-Timeout).")
            return 2
        print("[resettest] OK: ungueltiges Kommando resettet nicht.\n")
    return 1


def _drain(tp, seconds: float, verbose: bool = True) -> int:
    """Alle eingehenden Reports fuer `seconds` sammeln und ausgeben."""
    t0 = time.time()
    n = 0
    while time.time() - t0 < seconds:
        r = tp.recv(100)
        if not r:
            continue
        n += 1
        if verbose:
            print(f"        <- {r[:16].hex(' ')}")
    return n


def probe_app_frame(wait_s: float = 1.0, verbose: bool = True) -> int:
    """Sendet Sync + Datenrahmen im **App-Rahmenformat** an den Bootloader.

    Beleg: der Bootloader-Loader 0x08003ABC prueft genau

        [0]    Sequenz       muss *(0x20000171) entsprechen
        [1]    0x01          Rahmentyp (1 = Tunnel/Daten)
        [2]    0x3D          feste Marke
        [3]    0x50          Kanal 0x0550, low
        [4]    0x05          Kanal 0x0550, high
        [5]    Laenge        <= 0x3A (58)
        [6..]  Nutzdaten     Nutzdaten[0] = Kommandocode

    Typ 0xFE (Sync) setzt den Sequenzzaehler auf 0. Nur lesend: 0x37 setzt
    den Sequencer zurueck, 0x10 nur den Modus -- kein Flash-Zugriff.
    """
    hits = find_bootloader(verbose=False)
    if not hits:
        print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
        return 3
    tp = Transport(hits[0])
    try:
        sync = build_sync_frame(0)
        print(f"[appframe] Sync (Typ 0xFE) -> {sync[:6].hex(' ')} …")
        tp.send(sync)
        _drain(tp, 0.4, verbose)

        for seq, payload in ((0, bytes([0x37])), (1, bytes([0x10, 0x03]))):
            rep = build_app_frame(payload, seq=seq)
            want = (payload[0] | 0x40) & 0xFF
            print(f"[appframe] seq={seq} -> {rep[:8].hex(' ')} …")
            tp.send(rep)
            t0 = time.time()
            acks = 0
            while time.time() - t0 < wait_s:
                r = tp.recv(100)
                if not r:
                    continue
                pf = parse_app_frame(r)
                pl = pf[3] if pf else b""
                is_ack = bool(pl) and pl[0] == want
                acks += 1 if is_ack else 0
                print(f"        [{'ACK' if is_ack else '   '}] {r[:20].hex(' ')}")
            print(f"        kommandospezifische ACKs: {acks}")
            if acks == 0:
                print("[appframe] keine passende Quittung -- Rahmen passen "
                      "noch nicht.")
                return 1
        print("[appframe] ERGEBNIS: Der Bootloader quittiert die Rahmen "
              "kommandospezifisch -> Protokoll passt!")
        return 0
    finally:
        tp.close()


PERM_HINT = """[hid] Zugriff auf {path} verweigert.

hidraw-Geraete gehoeren root. Einmalig eine udev-Regel installieren:

    sudo cp tools/99-continental-ebike.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger

Danach das Geraet ab- und wieder anstecken."""


def _open_hid(path):
    """HID-Geraet oeffnen - unterstuetzt beide verbreiteten Bindungen.

    * klassische hidapi-Bindung : ``hid.device()`` + ``open_path()``
    * cython-hidapi (neu)       : ``hid.Device(path=...)`` (kein hid.device())
    """
    hid = _import_hid()
    if hasattr(hid, "device"):
        dev = hid.device()
        dev.open_path(path)
        dev.set_nonblocking(0)
        return dev
    if hasattr(hid, "Device"):
        dev = hid.Device(path=path)
        try:
            dev.nonblocking = 0
        except Exception:                      # pragma: no cover
            pass
        return dev
    names = sorted(n for n in dir(hid) if not n.startswith("_"))
    raise RuntimeError(f"Unbekannte hid-Bindung (kein hid.device/hid.Device): {names}")


class Transport:
    """Kapselt das Senden/Empfangen von 64-Byte-HID-Reports."""

    def __init__(self, device_info) -> None:
        path = device_info["path"]
        self.log: List[Tuple[str, bytes]] = []
        try:
            self.dev = _open_hid(path)
        except Exception as exc:               # Rechte, belegt, ...
            msg = str(exc)
            if "denied" in msg.lower() or "permission" in msg.lower():
                raise RuntimeError(PERM_HINT.format(path=path)) from exc
            raise

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
                 wire: str = "app",
                 block_ack: Optional[bool] = None) -> None:
        self.tp = tp
        self.verbose = verbose
        # "app"  = [seq][0x01][0x3D][id_lo][id_hi][len][payload]  <-- richtig
        # "raw"  = [len][payload]            (falsch, nie bestaetigt)
        # "hdr"  = [0x02][0x21][len][payload] (anderer Kanal)
        self.wire = wire
        self.seq = 1
        self.fseq = 0               # Rahmensequenz des App-Formats
        self.channel = HOST_CHANNEL_DEFAULT
        # Der Bootloader quittiert 0x36-Bloecke nicht
        # einzeln, sondern erst am Ende des Bildes.
        self.block_ack = (wire != "app") if block_ack is None \
            else block_ack
        self.addr = APP_BASE

    # -- Frames ------------------------------------------------------------
    def frame(self, payload: bytes) -> bytes:
        """USB-HID-Report bauen.

        Der Bootloader liest die Laenge aus [0x20000206] und den Payload ab
        +1 -- das ist der komplette 64-Byte-HID-Block (Byte 0 = Laenge).
        Mit wire='hdr' wird die im CAN-Pfad (0x08000AFC) geprüfte Variante
        '02 21 <len> <payload>' gesendet.
        """
        if self.wire == "app":
            rep = build_app_frame(payload, seq=self.fseq, channel=self.channel)
            self.fseq = (self.fseq + 1) & 0xFF
            return rep
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

    def expect_ack_strict(self, cmd: int, timeout_ms: int = 1000,
                          tries: int = 4) -> Optional[bytes]:
        """Wartet auf ein kommandospezifisches ACK (Payload[0] == cmd|0x40).

        Der Bootloader streut zwischen den Kommandos leere bzw. fremde Reports
        ein; ein einzelnes ``recv()`` liefert dann nicht das ACK. Fehlerantworten
        haben die Form ``7f <cmd> <errcode>`` und werden gemeldet.
        """
        want = (cmd | 0x40) & 0xFF
        for _ in range(tries):
            r = self.tp.recv(timeout_ms)
            if not r:
                return None
            pl = ack_payload(r)
            if pl is None:
                if self.verbose:
                    print(f"  <- [verworfen] {r[:12].hex(' ')}")
                continue
            if pl[0] == 0x7F:
                err = pl[2] if len(pl) > 2 else 0
                print(f"  <- [FEHLER 0x{err:02x}] {r[:14].hex(' ')}")
                return None
            if self.verbose:
                tag = "ACK" if pl[0] == want else "verworfen"
                print(f"  <- [{tag}] {r[:14].hex(' ')}")
            if pl[0] == want:
                return r
        return None

    # -- Kommandos ---------------------------------------------------------
    def cmd_sync(self) -> None:
        """Typ-0xFE-Rahmen: Sequenzzaehler des Geraets auf 0 setzen.

        Im Bootloader: 0x08003B54 schreibt 0 nach *(0x20000171). Danach ist der
        erste Datenrahmen mit Sequenz 0 gueltig.
        """
        self.fseq = 0
        rep = build_sync_frame(0)
        if self.verbose:
            print(f"  -> Sync     {rep[:6].hex(' ')} …")
        self.tp.send(rep)
        self.expect_ack(timeout_ms=250)

    def cmd_erase(self, addr: int, length: int) -> Optional[bytes]:
        """0x31 = Flash loeschen (Unterkommando 0x1FF00).

        Der Handler 0x080013A4 liest aus payload[1..3] einen 24-Bit-Wert
        (Big-Endian) und verteilt darauf:

            0x1FF00  -> 0x080013FE -> 0x0800148A  -> 0x08001964(addr, len)
            0x10202  -> 0x08001404  CRC der Applikation pruefen

        Auf Hardware bestaetigt: Praefix ``31 01 ff 00 00`` wird mit ``71``
        (= 0x31 | 0x40) quittiert. Uebrige Praefixe liefern Fehler 0x12.

        0x0800148A verlangt Payload-Laenge **13**, Adresse aus payload[5..8]
        und Laenge aus payload[9..12], beide Big-Endian; die Startadresse muss
        auf eine 2-KiB-Seite ausgerichtet sein.
        """
        if addr % 2048:
            raise ValueError("Adresse muss auf 2-KiB-Seite ausgerichtet sein")
        payload = (bytes([0x31, 0x01, 0xFF, 0x00, 0x00])
                   + struct.pack(">I", addr) + struct.pack(">I", length))
        assert len(payload) == 13, len(payload)
        self.send_frame(payload)
        return self.expect_ack_strict(0x31)

    def cmd_app_crc(self, tries_len: Tuple[int, ...] = (8, 4)) -> Optional[int]:
        """0x31 mit Unterkommando 0x10202 = Applikations-CRC pruefen.

        Handler 0x08001404 -> 0x08001414. Er verlangt
        ([0x2000001E] == 8 und Modus == 1) ODER ([0x2000001E] == 4 und
        Modus == 2); Modus = *(0x2000001D). Die CRC laeuft fest ueber
        0x08008000 + 0x00037FFC und wird mit dem gespeicherten Wert bei
        0x0803FFFC verglichen.

        Rueckgabe: CRC_OK (0) = CRC stimmt, CRC_MISMATCH (1) = stimmt nicht,
                   None = Befehl nicht akzeptiert.

        Das Statusbyte steht in der Antwort an Index 4 (die Bytes 1..3 sind
        der wiederholte Kommandokopf ``31 01 02 02``).
        """
        for ln in tries_len:
            payload = bytes([0x31, 0x01, 0x02, 0x02]) + bytes(ln - 4)
            self.send_frame(payload)
            r = self.tp.recv(2500)
            if r and self.verbose:
                print(f"  <- {r[:20].hex(' ')}")
            pl = ack_payload(r)
            if pl is None:
                continue
            if pl[0] == 0x7F:
                if len(pl) > 2 and pl[2] == 0x13:
                    if self.verbose:
                        print(f"     Laenge {ln} passt nicht zum Modus")
                    continue
                return None
            if pl[0] == 0x71 and len(pl) > 4:
                return pl[4]
        return None

    def cmd_read_record(self, magic: int = RECORD_MAGIC,
                        tries: int = 4) -> Optional[bytes]:
        """0x22 = Geraeterekord lesen -- der EINZIGE lesende Befehl.

        Dispatcher 0x08000EAE (Payload-Laenge 3) liest payload[1..2] als
        Big-Endian-Magic und verteilt:

            0xF100 -> 0x08000EF2  Quittung
            0xF101 -> 0x08000E3C  Statusantwort
            0xF15B -> 0x08000F54  Daten aus dem Flash

        0x08000F54 kopiert aus der **festen** Adresse 0x08007800 (Literal
        *(0x08000FE0)) 6 Byte (Kanal 1) bzw. 8 Byte (Kanal 2) in die Antwort.
        Antwortpayload auf Kanal 1: ``62 <magic-hi> <magic-lo> <6 Datenbytes>``,
        die Daten stehen also ab Index 3. Magic 0xF15A ist **das Schreibmagic**
        von 0x2E und liefert hier Fehler 0x13 (im Emulator bestaetigt).

        Rueckgabe: Record-Bytes (Kanal 1: 6 Byte), None = nicht beantwortet.
        """
        payload = bytes([0x22, (magic >> 8) & 0xFF, magic & 0xFF])
        assert len(payload) == 3, len(payload)
        self.send_frame(payload)
        for _ in range(max(1, tries)):
            r = self.tp.recv(1200)
            if not r:
                return None
            pl = ack_payload(r)
            if pl is None or len(pl) < 4:
                if self.verbose:
                    print(f"  <- [verworfen] {r[:12].hex(' ')}")
                continue
            if pl[0] == 0x7F:
                err = pl[2] if len(pl) > 2 else 0
                if self.verbose:
                    print(f"  <- [FEHLER 0x{err:02x}] Magic 0x{magic:04x}")
                return None
            if self.verbose:
                print(f"  <- {r[:16].hex(' ')}")
            if pl[0] == 0x62:
                return bytes(pl[3:9])
        return None

    def verify_against_device(self, img: Image, deep: bool = False) -> int:
        """Prueft den Applikationsbereich im Flash gegen das Image.

        Ohne ``deep`` **nur lesend** (CRC-Abfrage 0x31/0x10202): der Bootloader
        vergleicht CRC32(0x08008000 + 0x00037FFC) mit dem Wort bei
        0x0803FFFC. Das belegt die *Selbstkonsistenz* des Bereichs -- nicht,
        dass es genau dieses Image ist (ein anderes, in sich stimmiges Image
        liefert ebenfalls "stimmt").

        Mit ``deep`` **schreibend**: die letzte Seite 0x0803F800..0x0803FFFF
        wird geloescht und aus dem Image neu geschrieben (damit steht das
        CRC-Wort des Images im Flash), danach wird die CRC abgefragt. Nur dann
        beweist "stimmt", dass der **gesamte** Bereich genau diesem Image
        entspricht. Der alte Inhalt der Seite ist nicht lesbar und damit auch
        nicht sicherbar; der Bootloader darunter ist durch die Firmware
        geschuetzt (Schreibsperre < 0x08007800).

        Rueckgabe: 0 = stimmt, 1 = stimmt nicht, 2 = Fehler beim Schreiben.
        """
        if deep:
            page = APP_CRC_ADDR & ~0x7FF
            blob = img.slice(page, 0x800)
            print(f"[verify] deep (SCHREIBT): Seite 0x{page:08x} loeschen und "
                  f"{len(blob)} Bytes aus dem Image schreiben")
            if self.cmd_erase(page, 0x800) is None:
                print("[verify] deep: Loeschen fehlgeschlagen")
                return 2
            self.cmd_sequencer_start()
            self.cmd_set_address(page)
            failed = 0
            for i in range(0, len(blob), 56):
                if self.cmd_write_block(blob[i:i + 56]):
                    failed += 1
            if failed:
                print(f"[verify] deep: {failed} Block/Bloecke nicht geschrieben")
                return 2
            self.tp.recv(4000)          # Abschlussquittung bei 0x08040000
        res = self.cmd_app_crc()
        if res is None:
            print("[verify] Geraet hat die CRC-Abfrage nicht akzeptiert.")
            return 2
        return 0 if res == CRC_OK else 1

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

    def cmd_write_block(self, data: bytes, tries: int = 3) -> int:
        """0x36: bis zu 56 Datenbytes schreiben, Schreibzeiger laeuft weiter.

        Rueckgabe: 0 = ok, sonst Fehlercode des Geraets. Der Geraetesequencer
        [0x20000038] wird nur bei Erfolg weitergezaehlt, deshalb wird derselbe
        Block mit **unveraenderter** Sequenznummer wiederholt.
        """
        if len(data) > 56:
            raise ValueError("max 56 Bytes pro 0x36-Block")
        payload = bytes([0x36, self.seq]) + data
        if not self.block_ack:
            # Streaming: keine Einzelquittung. Anfallende Antworten nur
            # abholen, damit der Puffer leer bleibt; Fehlermeldungen melden.
            self.send_frame(payload)
            self.seq = self.seq + 1 if self.seq < 255 else 1
            self.addr += len(data)
            while True:
                r = self.tp.recv(0)
                if not r:
                    break
                pl = ack_payload(r)
                if pl and pl[0] == 0x7F:
                    return pl[2] if len(pl) > 2 else -2
            return 0
        last_err = 0
        for _ in range(tries):
            self.send_frame(payload)
            r = self.expect_ack(timeout_ms=400)
            pl = ack_payload(r)
            if pl is None:
                last_err = -1
                continue
            if pl[0] == 0x7F:
                last_err = pl[2] if len(pl) > 2 else -2
                continue
            if pl[0] == (0x36 | 0x40):
                self.seq = self.seq + 1 if self.seq < 255 else 1
                self.addr += len(data)
                return 0
            last_err = -3
        return last_err

    def cmd_finish(self) -> None:
        """0x3E = Abschluss (Payload-Laenge 2, Arg 0x00 oder 0x80).

        Arg 0x80 -> keine Antwort; der Bootloader laeuft danach in seinen
        Timeout und macht einen Software-Reset (SCB->AIRCR = 0x05FA0004),
        wodurch die App startet. Arg 0x00 antwortet mit 0x7E.
        """
        self.send_frame(bytes([0x3E, 0x80]))

    # -- Gesamt-Upload -----------------------------------------------------
    def upload(self, img: Image, region: str = "app", chunk: int = 56,
               do_erase: bool = True) -> None:
        if region == "app":
            start, stop = APP_BASE, APP_CRC_ADDR + 4
        elif region == "all":
            start, stop = FLASH_BASE, FLASH_BASE + len(img.data)
        else:
            raise ValueError("region muss 'app' oder 'all' sein")
        blob = img.slice(start, stop - start)
        print(f"[upload] {region}: 0x{start:08x}..0x{stop - 1:08x} ({len(blob)} Bytes)")
        if self.wire == "app":
            self.cmd_sync()
        self.cmd_enter_program()
        if do_erase:
            # f_801998 ist reines Programmieren -- ohne vorheriges Loeschen
            # antwortet 0x36 mit Fehler 0x72.
            a = self.cmd_erase(start, stop - start)
            if a is None and self.wire == "app":
                raise IOError("Loeschen fehlgeschlagen -- Upload abgebrochen, "
                              "die Applikation ist noch unveraendert.")
        self.cmd_sequencer_start()
        self.cmd_set_address(start)
        sent = 0
        failed = []
        for i in range(0, len(blob), chunk):
            blk = blob[i:i + chunk]
            rc = self.cmd_write_block(blk)
            if rc:
                failed.append((start + i, rc))
            sent += len(blk)
            if sent % (chunk * 8) == 0:
                pct = 100 * sent // len(blob)
                print(f"         {sent}/{len(blob)}  {pct}%"
                      f"{f'  ({len(failed)} Fehler)' if failed else ''}",
                      flush=True)
        if not failed and not self.block_ack:
            # Der Bootloader antwortet einmal, wenn der Zeiger 0x08040000
            # erreicht hat.
            r = self.tp.recv(4000)
            pl = ack_payload(r)
            if pl and pl[0] == (0x36 | 0x40):
                print("[upload] Abschlussquittung 0x76 erhalten.")
            elif pl and pl[0] == 0x7F:
                print(f"[upload] Geraet meldet Fehler 0x{pl[2]:02x}.")
                failed.append((stop, pl[2] if len(pl) > 2 else -2))
            else:
                print("[upload] Keine Abschlussquittung erhalten.")
        if failed:
            print(f"[upload] ABBRUCH: {len(failed)} Block/Blöcke nicht "
                  f"geschrieben, erster Fehler bei 0x{failed[0][0]:08x} "
                  f"(rc={failed[0][1]}). KEIN Reset ausgeloest.")
            raise IOError("Flash-Schreiben fehlgeschlagen")
        self.cmd_finish()
        print("[upload] alle Bloecke quittiert, Reset ausgeloest.")


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

    sp = sub.add_parser("flash",
                        help="Kombimodus: auf Geraet warten, Bootloader holen, flashen")
    sp.add_argument("image", type=Path, help="Image (.bin oder .hex)")
    sp.add_argument("--region", choices=["app", "all"], default="app")
    sp.add_argument("--wait", type=float, default=120.0,
                    help="Sekunden auf das Geraet warten (Standard 120)")
    sp.add_argument("--no-reset", action="store_true",
                    help="nicht selbst in den Bootloader springen")
    sp.add_argument("-q", "--quiet", action="store_true")

    sp = sub.add_parser("patch", help="Bytes/Worte patchen")
    sp.add_argument("image", type=Path)
    sp.add_argument("-o", "--out", type=Path, required=True)
    sp.add_argument("-s", "--set", action="append", default=[], metavar="ADDR=HEX",
                    help="z. B. -s 0x08012340=90,90 -s 0x08012350=DEADBEEF")
    sp.add_argument("--fix-crc", action="store_true", help="App-CRC neu berechnen")

    sp = sub.add_parser("fixcrc", help="nur App-CRC neu berechnen")
    sp.add_argument("image", type=Path)
    sp.add_argument("-o", "--out", type=Path, required=True)

    sp = sub.add_parser("verify",
                        help="Image und (falls vorhanden) Geraeteinhalt pruefen")
    sp.add_argument("image", type=Path)
    sp.add_argument("--offline", action="store_true",
                    help="nur die Datei pruefen, kein Geraet anfassen")
    sp.add_argument("--deep", action="store_true",
                    help="exakter Beweis -- SCHREIBT die letzte 2-KiB-Seite "
                         "(verlangt -y)")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Schreiben bei --deep bestaetigen")

    sp = sub.add_parser("dump",
                        help="Geraeterekord lesen (0x22, 8 Byte @ 0x08007800)")
    sp.add_argument("target", nargs="?", choices=["rec", "app"], default="rec",
                    help="rec = Geraeterekord (einzige lesbare Stelle), "
                         "app = nicht moeglich (erklaert)")
    sp.add_argument("--raw", action="store_true",
                    help="weniger Ausgabe")

    sp = sub.add_parser("diff", help="zwei Images vergleichen")
    sp.add_argument("a", type=Path)
    sp.add_argument("b", type=Path)

    sp = sub.add_parser("crc",
                        help="Applikations-CRC pruefen (0x31/0x10202)")
    sp.add_argument("--wire", choices=["app", "raw", "hdr"],
                    default="app")

    sp = sub.add_parser("erase",
                        help="Flash-Bereich im Bootloader loeschen (0x31)")
    sp.add_argument("--addr", type=lambda x: int(x, 0), required=True)
    sp.add_argument("--len", type=lambda x: int(x, 0), required=True)
    sp.add_argument("--wire", choices=["app", "raw", "hdr"], default="app")

    sp = sub.add_parser("appframe",
                        help="nur lesen: App-Rahmenformat am Bootloader testen")
    sp.add_argument("--wait", type=float, default=1.0,
                    help="Wartezeit je Rahmen, Default %(default)s")

    sp = sub.add_parser("resettest",
                        help="nur lesen: versteht der Bootloader unsere Kommandos?")
    sp.add_argument("--wait", type=float, default=3.0,
                    help="Sekunden auf den Reset warten, Default %(default)s")

    sp = sub.add_parser("sweep",
                        help="nur lesen: Rahmungen fuer ein Kommando durchprobieren")
    sp.add_argument("--wait", type=float, default=0.6,
                    help="Wartezeit je Variante, Default %(default)s")

    sp = sub.add_parser("probe",
                        help="nur lesen: Antwortformat des Bootloaders pruefen")
    sp.add_argument("--listen", type=float, default=1.0,
                    help="Sekunden nur zuhoeren, Default %(default)s")
    sp.add_argument("--wait", type=float, default=1.0,
                    help="Wartezeit je Kommando, Default %(default)s")

    sp = sub.add_parser("reset", help="App per HID zum Bootloader-Reset bewegen")
    sp.add_argument("--channel", type=lambda s: int(s, 0),
                    default=HOST_CHANNEL_DEFAULT,
                    help="Kanal-ID u16, Default %(default)#06x; freigegeben: "
                         + " ".join(f"{c:#06x}" for c in HOST_CHANNEL_IDS))
    sp.add_argument("--seq", type=lambda s: int(s, 0), default=None,
                    help="Sequenzbyte; Default = Steuerrahmen 0xFE voran + 0")
    sp.add_argument("--legacy-wire", choices=["len", "raw"], default=None,
                    help="alte, unbestaetigte Huelle senden (nur zum Testen)")
    sp.add_argument("--msg", default=TRIGGER_MSG.hex(" "),
                    help="Trigger-Nutzlast, Default '%(default)s'")
    sp.add_argument("--wait", type=float, default=20.0,
                    help="Sekunden auf den Bootloader warten")
    sp.add_argument("--repeat", type=int, default=12,
                    help="Anzahl Trigger-Versuche (Default 12)")
    sp.add_argument("--interval", type=int, default=150,
                    help="Millisekunden zwischen den Versuchen")

    sp = sub.add_parser("watch", help="App/Bootloader zeitlich beobachten")
    sp.add_argument("--seconds", type=float, default=10.0)
    sp.add_argument("--interval", type=float, default=0.05,
                    help="Abtastintervall in Sekunden")

    sp = sub.add_parser("upload", help="per HID-Bootloader hochladen")
    sp.add_argument("image", type=Path)
    sp.add_argument("--region", choices=["app", "all"], default="app")
    sp.add_argument("--wire", choices=["app", "raw", "hdr"], default="app",
                    help="app = [seq][0x01][0x3D][id][len][payload] "
                         "(am Geraet verifiziert), "
                         "raw = [len][payload], hdr = 02 21 <len> <payload>")
    sp.add_argument("--dry-run", action="store_true",
                    help="nur anzeigen, nichts senden")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="Sicherheitsabfrage ueberspringen")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.add_argument("--no-erase", action="store_true",
                    help="Bereich nicht vorher loeschen (nur wenn schon leer)")

    sp = sub.add_parser("raw", help="rohen 64-Byte-Report senden (Protokoll testen)")
    sp.add_argument("hexbytes", help="z. B. '02 21 02 34 03'")
    sp.add_argument("--app-frame", action="store_true",
                    help="hexbytes als Nutzlast nehmen und daraus einen echten "
                         "App-Rahmen (Typ 1) bauen")
    sp.add_argument("--channel", type=lambda s: int(s, 0),
                    default=HOST_CHANNEL_DEFAULT,
                    help="Kanal-ID fuer --app-frame (Default %(default)#06x)")
    sp.add_argument("--seq", type=lambda s: int(s, 0), default=0,
                    help="Sequenzbyte fuer --app-frame (Default %(default)#04x)")
    sp.add_argument("--sync", action="store_true",
                    help="vorher Steuerrahmen Typ 0xFE senden (Zaehler = 0)")
    sp.add_argument("--listen", type=int, default=0, metavar="MS",
                    help="danach MS Millisekunden IN-Reports der App mitlesen")
    sp.add_argument("--app", action="store_true",
                    help="an die Applikation senden statt an den Bootloader")

    args = p.parse_args(argv)

    if args.cmd == "flash":
        # Ein Aufruf fuer alles: auf das Geraet warten, bei Bedarf in den
        # Bootloader springen (Software-Reset, damit der Bootloader aktiv
        # bleibt), flashen und auf den Neustart der App warten.
        t0 = time.time()
        last = 0.0
        while time.time() - t0 < args.wait:
            if find_bootloader(verbose=False) or find_app(verbose=False):
                break
            now = time.time() - t0
            if now - last >= 5.0:
                last = now
                print(f"  ... warte auf Geraet ({now:5.1f}s von "
                      f"{args.wait:.0f}s)", flush=True)
            time.sleep(0.5)
        else:
            print("[flash] kein Geraet gefunden (Akku/Display an?). "
                  "Sichtbar?  python3 tools/stm_display_fw.py info")
            return 3

        if not find_bootloader(verbose=False):
            if args.no_reset:
                print("[flash] Applikation laeuft; --no-reset gesetzt.")
                return 3
            print("[flash] Applikation laeuft -> Software-Reset in den "
                  "Bootloader ...")
            rc = reset_to_bootloader(verbose=not args.quiet)
            if rc != 0:
                print("[flash] Bootloader konnte nicht aktiviert werden "
                      f"(rc={rc}).")
                return rc

        boot = find_bootloader(verbose=False)
        if not boot:
            print("[flash] Bootloader nicht gefunden.")
            return 3
        print(f"[flash] Bootloader bereit: {boot[0].get('path')}")

        img = load_image(args.image)
        print(f"[flash] Image {args.image}: base=0x{img.base:08x} "
              f"size={len(img.data)}")
        verify_app_crc(img)

        tp = Transport(boot[0])
        try:
            proto = Protocol(tp, verbose=not args.quiet, wire="app")
            proto.upload(img, region=args.region)
        finally:
            tp.close()

        print("[flash] warte auf Neustart der Applikation ...")
        t0 = time.time()
        while time.time() - t0 < 20.0:
            if find_app(verbose=False):
                print("[flash] fertig -- Applikation laeuft.")
                return 0
            time.sleep(0.5)
        print("[flash] Applikation ist nicht aufgetaucht; das Geraet bleibt "
              "dann im Bootloader\n"
              "        (z.B. wenn die Applikations-CRC nicht stimmt) und kann "
              "einfach erneut\n        geflasht werden.")
        return 0

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
        local_ok = verify_app_crc(img)
        if args.offline:
            return 0 if local_ok else 1
        if args.deep:
            # Immer warnen -- nicht nur beim Ablehnen, sondern auch beim Tun.
            print("[verify] --deep SCHREIBT in den Flash:\n"
                  "         * 2 KiB letzte Applikationsseite 0x0803f800..0x0803ffff\n"
                  "           loeschen und aus dem Image neu schreiben (inkl. CRC-Wort).\n"
                  "         * Der alte Inhalt dieser Seite ist nicht lesbar und damit\n"
                  "           auch nicht sicherbar. Passt er nicht zum Image, ist er weg.\n"
                  "         * Der Bootloader liegt darunter und ist mit dem Protokoll\n"
                  "           nicht erreichbar (Firmware-Sperre < 0x08007800).\n"
                  "         * Bricht der Vorgang nach dem Loeschen ab, startet die\n"
                  "           Applikation nicht mehr; das Geraet bleibt im Bootloader\n"
                  "           und kann einfach neu geflasht werden.")
            if not args.yes:
                print("         Nichts gesendet. Zum Ausfuehren:  verify <image> --deep -y\n"
                      "         Nur lesen (Standard):        verify <image>")
                return 2
        hits = find_bootloader(verbose=False)
        if not hits:
            if args.deep:
                print("[verify] kein Bootloader gefunden -- die --deep-Pruefung "
                      "konnte nicht laufen.")
                return 3
            print("[verify] kein Bootloader gefunden -> nur die Datei geprueft.")
            return 0 if local_ok else 1
        tp = Transport(hits[0])
        try:
            proto = Protocol(tp, verbose=True, wire="app")
            proto.cmd_sync()
            proto.cmd_enter_program()      # setzt das "gueltig"-Flag
            rc = proto.verify_against_device(img, deep=args.deep)
        finally:
            tp.close()
        if rc == 0:
            if args.deep:
                print("[verify] Geraet: Applikationsbereich entspricht GENAU diesem "
                      "Image (CRC32 ueber die ganze Region).")
            else:
                print("[verify] Geraet: Applikationsbereich ist selbstkonsistent "
                      "(CRC32 passt zum gespeicherten CRC-Wort).")
                print("[verify] Hinweis: das belegt noch nicht, dass es genau dieses "
                      "Image ist;\n         dafuer 'verify <image> --deep -y' "
                      "(schreibt 2 KiB).")
            return 0
        if rc == 1:
            if args.deep:
                print("[verify] Geraet: Applikationsbereich entspricht NICHT diesem "
                      "Image.")
            else:
                print("[verify] Geraet: Selbstkonsistenz verletzt -- Inhalt und/oder "
                      "CRC-Wort passt nicht.")
                print("[verify] '--deep -y' unterscheidet: Inhalt abweichend oder nur "
                      "das CRC-Wort.")
            return 1
        print("[verify] Pruefung fehlgeschlagen (siehe Meldungen oben).")
        return 2

    if args.cmd == "dump":
        if args.target == "app":
            print("[dump] Der Bootloader hat **keinen** Lese-Befehl. Sein\n"
                  "       Dispatcher kennt nur 10 Kommandos (Erase, Adresse,\n"
                  "       Block schreiben, Sequencer, Finish, Modus, Record,\n"
                  "       Challenge) -- keines liest Flash. Antworten tragen\n"
                  "       nur Status, keine Daten.\n"
                  "       Auslesbar ist ausschliesslich der Geraeterekord:\n"
                  "         python3 tools/stm_display_fw.py dump rec\n"
                  "       Inhalt pruefen statt lesen:\n"
                  "         python3 tools/stm_display_fw.py verify <image>")
            return 2
        hits = find_bootloader(verbose=False)
        if not hits:
            print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
            return 3
        tp = Transport(hits[0])
        try:
            proto = Protocol(tp, verbose=not args.raw, wire="app")
            proto.cmd_sync()
            rec = proto.cmd_read_record()
        finally:
            tp.close()
        if rec is None:
            print("[dump] Geraet hat den Lese-Befehl (0x22) nicht beantwortet.")
            return 1
        print(f"[dump] Record 0x{RECORD_ADDR:08x}: {rec.hex(' ')}")
        if rec == b"\xff" * len(rec):
            print("       (leer -- im Originalzustand unbeschrieben)")
        elif not args.raw:
            print(f"       Datum (JJ MM TT): {rec[0]:02d}-{rec[1]:02d}-{rec[2]:02d}"
                  f"   Rest: {rec[3:].hex(' ')}")
        print("[dump] Hinweis: das ist der einzige auslesbare Flash-Bereich;")
        print("       den Applikationsbereich kann nur 'verify' pruefen.")
        return 0

    if args.cmd == "diff":
        return 0 if diff(load_image(args.a), load_image(args.b)) else 0

    if args.cmd == "crc":
        hits = find_bootloader(verbose=False)
        if not hits:
            print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
            return 3
        tp = Transport(hits[0])
        try:
            proto = Protocol(tp, verbose=True, wire=args.wire)
            proto.cmd_sync()
            proto.cmd_enter_program()
            res = proto.cmd_app_crc()
            if res is None:
                print("[crc] Geraet hat die CRC-Pruefung nicht akzeptiert.")
                return 1
            if res == CRC_OK:
                print("[crc] Status 0: CRC stimmt -> Applikationsbereich ist "
                      "selbstkonsistent (unveraendert bzw. vollstaendig "
                      "geschrieben).")
            else:
                print(f"[crc] Status {res}: CRC stimmt NICHT -> der Flash "
                      "wurde veraendert oder ist leer.")
        finally:
            tp.close()
        return 0

    if args.cmd == "erase":
        if args.addr < APP_BASE:
            print("Abbruch: Adresse liegt im Bootloader (< 0x08008000).")
            return 2
        hits = find_bootloader(verbose=False)
        if not hits:
            print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
            return 3
        tp = Transport(hits[0])
        try:
            proto = Protocol(tp, verbose=True, wire=args.wire)
            proto.cmd_sync()
            proto.cmd_enter_program()
            ack = proto.cmd_erase(args.addr, args.len)
            if ack is None:
                print("[erase] keine Antwort.")
                return 1
            print(f"[erase] 0x{args.len:x} Bytes @ 0x{args.addr:08x} -> "
                  f"{ack[:16].hex(' ')}")
        finally:
            tp.close()
        return 0

    if args.cmd == "appframe":
        return probe_app_frame(wait_s=args.wait)

    if args.cmd == "resettest":
        return resettest(wait_s=args.wait)

    if args.cmd == "sweep":
        return sweep_framing(wait_s=args.wait)

    if args.cmd == "probe":
        return probe_protocol(listen_s=args.listen, wait_s=args.wait)

    if args.cmd == "reset":
        msg = bytes(int(b, 16) for b in args.msg.split())
        return reset_to_bootloader(channel=args.channel, msg=msg, seq=args.seq,
                                   wait_s=args.wait, repeat=args.repeat,
                                   interval_ms=args.interval,
                                   legacy_wire=args.legacy_wire)

    if args.cmd == "watch":
        return watch_devices(seconds=args.seconds, interval=args.interval)

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
            proto.upload(img, region=args.region, do_erase=not args.no_erase)
        finally:
            if proto is not None and getattr(proto, "tp", None) is not None:
                proto.tp.close()
        return 0

    if args.cmd == "raw":
        data = bytes(int(b, 16) for b in args.hexbytes.split())
        use_app = args.app or args.app_frame or args.sync
        if use_app:
            hits = find_app(verbose=False)
            if not hits:
                print(f"Keine App-HID-Schnittstelle ({PRODUCT_APP!r}) gefunden.")
                return 3
        else:
            hits = find_bootloader(verbose=False)
            if not hits:
                print("Kein Bootloader gefunden.")
                return 3
        tp = Transport(hits[0])
        try:
            if args.sync:
                sync = build_sync_frame(0)
                tp.send(sync)
                print(f"sync -> {sync[:8].hex(' ')} ...")
                time.sleep(0.02)
            if args.app_frame:
                rep = build_app_frame(data, seq=args.seq, channel=args.channel)
            else:
                rep = data + bytes(max(0, REPORT_SIZE - len(data)))
            tp.send(rep)
            print(f"out  -> {rep[:16].hex(' ')} ...")
            if args.app_frame:
                print(f"        typ=0x{rep[1]:02x} marke=0x{rep[2]:02x} "
                      f"kanal=0x{(rep[3] | (rep[4] << 8)):04x} len={rep[5]}")
            if args.listen:
                end = time.time() + args.listen / 1000.0
                n = 0
                while time.time() < end:
                    r = tp.recv(100)
                    if not r:
                        continue
                    n += 1
                    p = parse_app_frame(r)
                    extra = (f"  typ=0x{p[1]:02x} kanal={p[2]:#06x} "
                             f"seq={p[0]} payload={p[3].hex(' ')}") if p else ""
                    print(f"in   <- {r[:20].hex(' ')}{extra}")
                if not n:
                    print("       (keine IN-Reports empfangen)")
            else:
                r = tp.recv(1000)
                print(f"in   <- {r.hex(' ') if r else '(keine Antwort)'}")
        finally:
            tp.close()
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
