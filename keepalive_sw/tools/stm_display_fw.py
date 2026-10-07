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

# Wartezeit auf die Loeschquittung (0x31/0x1FF00). Der Bootloader quittiert
# erst, wenn die letzte Seite geloescht ist, und schleift dabei synchron durch
# alle Seiten (App-Bereich = 224 KiB = 112 Seiten). Ein kurzes Zeitfenster
# fuehrt zu "keine Antwort", obwohl das Loeschen laeuft: am 2026-10-04 waren
# 4 s zu kurz (Quittung kam nach ~2,5-4,5 s), der Bereich war danach trotzdem
# leer. Am Geraet gemessen: 2,5 s bei bereits geloeschtem Bereich.
ERASE_TIMEOUT_MS = 60000

# Statusbyte der Loeschquittung: der Antwortbauer 0x0800130C bildet den
# Erfolgswert 1 auf **0** und den Fehlerwert 2 auf **1** ab
# (0x0800134C..0x0800135A: strb r6/r3 mit r6=0, r3=1). Auf Hardware
# bestaetigt: erfolgreiches Loeschen -> Status 0.
ERASE_OK = 0

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


# Die klassische hidapi-Bindung (``hid.device.read``, z. B. hidapi 0.15.0 auf
# PyPI) liest bei ``timeout_ms <= 0`` ueber ``hid_read()`` -- das ist der
# **blockierende** Aufruf: er wartet unbegrenzt auf einen Report. Im
# Datenstrom sendet das Geraet aber nichts (Erfolg wird nicht quittiert),
# ein ``read(64, 0)`` haengt also fuer immer. Am Stromanfang -- direkt nach
# dem ersten ``0x36`` -- blieb das Werkzeug genau dort stehen. Ein Timeout
# von 0 soll "kurz nachsehen" heissen, nicht "ewig warten"; deshalb wird auf
# mindestens 1 ms aufgerundet und auf int gebracht (``read`` verlangt int).
HID_POLL_MIN_MS = 1


def _hid_timeout(timeout_ms: float) -> int:
    """Lese-Timeout fuer hidapi: nie <= 0, immer int (sonst blockiert/hakt es)."""
    return max(HID_POLL_MIN_MS, int(timeout_ms))


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
        """Report lesen -- mit einer Wiederholung bei einem Lese-/Schreibfehler.

        Waehrend ein Flash-Befehl laeuft, schaltet die Bootloader-Firmware den
        Systemtakt auf HSI und PLL/HSE ab (Unlock-Routine Blob +0x2C,
        ``RCC_CR &= 0xFEF2FFFF`` / ``RCC_CFGR = 0x9F0000``). Der USB-Takt kommt
        vom PLL -- das Geraet bleibt zwar am Bus (dmesg schweigt), antwortet
        aber kurz nicht; hidapi wirft dann OSError/HIDException. Ein zweiter
        Versuch nach kurzer Pause faengt das meist ab.
        """
        timeout_ms = _hid_timeout(timeout_ms)
        try:
            data = self.dev.read(REPORT_SIZE, timeout_ms)
        except Exception as exc:                   # noqa: BLE001
            self.log.append(("ERR", f"{type(exc).__name__}: {exc}".encode()))
            time.sleep(0.02)
            try:
                data = self.dev.read(REPORT_SIZE, timeout_ms)
            except Exception as exc2:              # noqa: BLE001
                self.log.append(("ERR",
                                 f"{type(exc2).__name__}: {exc2}".encode()))
                # hidapi meldet hier oft "Success" (errno 0) -- das ist kein
                # Erfolg, sondern ein stummer/gestallter Endpunkt oder ein
                # abgerissener Knoten. Einmal den Knoten neu oeffnen und ein
                # letztes Mal lesen; danach **None** zurueckgeben, damit der
                # Aufrufer entscheiden kann (kein Absturz mitten im Vorgang).
                if not getattr(self, "_reopening", False):
                    self._reopening = True
                    try:
                        if self.reopen(3.0):
                            try:
                                data = self.dev.read(REPORT_SIZE, timeout_ms)
                            except Exception as exc3:   # noqa: BLE001
                                self.log.append(
                                    ("ERR", f"{type(exc3).__name__}: "
                                            f"{exc3}".encode()))
                                return None
                        else:
                            return None
                    finally:
                        self._reopening = False
                else:
                    return None
        if not data:
            return None
        data = bytes(data)
        self.log.append(("IN", data))
        return data

    def poll(self, timeout_ms: int = 0) -> Optional[bytes]:
        """Lesen **ohne** Fehlerbehandlung -- Transportproblem -> ``None``.

        Fuer den Datenstrom: dort gibt es ausser Fehlerrahmen und der
        Endquittung nichts zu lesen, und hidapi meldet beim Pollen auf einem
        gerade beschaeftigten Geraet sporadisch ``-1`` mit errno 0
        (``HIDException: Success`` -- hidraw-Poll ohne ``POLLIN``/
        ``POLLERR``). Das darf den Strom nicht abbrechen, deshalb wird hier
        weder neu geoeffnet noch eine Ausnahme geworfen; die Zaehler
        ``soft_errors``/``stall_reopens``/``lost_reopens`` machen es sichtbar.

        Das Timeout geht durch ``_hid_timeout()``: ein 0-Timeout waere in der
        klassischen hidapi-Bindung der blockierende ``hid_read()``.
        """
        timeout_ms = _hid_timeout(timeout_ms)
        try:
            data = self.dev.read(REPORT_SIZE, timeout_ms)
        except Exception as exc:                   # noqa: BLE001
            self.log.append(("ERR", f"{type(exc).__name__}: {exc}".encode()))
            self.soft_errors = getattr(self, "soft_errors", 0) + 1
            return None
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

    def reopen(self, timeout_s: float = 30.0) -> bool:
        """Nach einem Transportfehler wieder sprechbar werden.

        Zwei Faelle, die hier unterschieden werden:

        * **USB-Stall** (Normalfall): das Geraet ist weiter am Bus (der Kernel
          meldet nichts, ``dmesg`` bleibt leer), antwortet aber kurz nicht --
          die Firmware schaltet fuer Flash-Befehle den Systemtakt um, der
          USB-Takt (PLL) faellt dabei weg. Dann genuegt es, denselben Knoten
          neu zu oeffnen.
        * **echtes Abstecken/Neu-Anmelden**: der Knoten ist verschwunden, dann
          wird auf das Wiederauftauchen gewartet (udev-Regel kann beim frischen
          Knoten kurz fehlen, daher mehrere Versuche).
        """
        self.close()
        present = False
        try:
            present = bool(find_bootloader(verbose=False))
        except Exception:                          # noqa: BLE001
            present = False
        # Art des Neuaufbaus merken: "stall" = Geraet haengt weiter am Bus,
        # nur der Handle ist tot (Taktumschaltung der Firmware waehrend eines
        # Flash-Befehls). Das ist harmlos und die Firmware-Zustaende (Sitzung,
        # Schreibzeiger) bleiben erhalten. "lost" = Knoten war wirklich weg.
        self.reopen_kind = "stall" if present else "lost"
        if present:
            print("[usb] Geraet haengt weiter am Bus -- Knoten neu oeffnen "
                  "(Taktumschaltung der Firmware)", flush=True)
        else:
            print("[usb] Knoten ist verschwunden -- auf Neuanmeldung warten "
                  "...", flush=True)
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout_s:
            try:
                for d in find_bootloader(verbose=False) or ():
                    try:
                        self.dev = _open_hid(d["path"])
                        self.reopens = getattr(self, "reopens", 0) + 1
                        self.stall_reopens = getattr(self, "stall_reopens", 0) + \
                            (1 if self.reopen_kind == "stall" else 0)
                        self.lost_reopens = getattr(self, "lost_reopens", 0) + \
                            (1 if self.reopen_kind == "lost" else 0)
                        return True
                    except Exception as exc:        # noqa: BLE001
                        last = f"{type(exc).__name__}: {exc}"
            except Exception as exc:                # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        print(f"[usb] kein Geraet gefunden ({last})")
        return False


# --------------------------------------------------------------------------
# Protokoll (rekonstruiert aus dem Disassembly)
# --------------------------------------------------------------------------
def _is_transport_error(exc: BaseException) -> bool:
    """Ist das ein USB-/Transportfehler (kein Protokoll-/Logikfehler)?

    Typisch: ``OSError``/``HIDException`` beim Lesen oder Schreiben, wenn die
    Firmware waehrend eines Flash-Befehls den Systemtakt umschaltet und der
    USB-Takt (PLL) kurz wegfaellt. Das Geraet bleibt dabei am Bus -- der
    Kernel meldet nichts.
    """
    return isinstance(exc, (IOError, OSError)) or \
        "HID" in type(exc).__name__


class DryTransport:
    """Fuer --dry-run: sendet nichts, protokolliert nur."""

    def send(self, report: bytes) -> None:
        return None

    def recv(self, timeout_ms: int = 500):
        return None

    def poll(self, timeout_ms: float = 0):
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
                 block_ack: Optional[bool] = None,
                 erase_timeout_ms: int = ERASE_TIMEOUT_MS,
                 erase_chunk: int = 32768,
                 erase_pause_ms: int = 60) -> None:
        self.tp = tp
        self.verbose = verbose
        self.erase_timeout_ms = erase_timeout_ms
        self.erase_chunk = erase_chunk
        self.erase_pause_ms = erase_pause_ms
        self.last_erase_status: Optional[int] = None
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

    def cmd_erase(self, addr: int, length: int,
                  timeout_ms: Optional[int] = None,
                  progress: bool = True) -> Optional[bytes]:
        """0x31 = Flash loeschen (Unterkommando 0x1FF00).

        Der Handler 0x080013A4 liest aus payload[1..3] einen 24-Bit-Wert
        (Big-Endian) und verteilt darauf:

            0x1FF00  -> 0x080013FE -> 0x0800148A  -> 0x08001964(addr, len)
            0x10202  -> 0x08001404  CRC der Applikation pruefen

        Uebrige Praefixe liefern Fehler 0x12.

        0x0800148A verlangt Payload-Laenge **13**, Adresse aus payload[5..8]
        und Laenge aus payload[9..12], beide Big-Endian; die Startadresse muss
        auf eine 2-KiB-Seite ausgerichtet sein.

        **Antwortzeit**: der Bootloader quittiert erst, wenn die letzte Seite
        geloescht ist -- er arbeitet dabei synchron alle Seiten ab (App-Bereich
        = 112 Seiten). Deshalb wartet diese Funktion ``ERASE_TIMEOUT_MS``
        (60 s) und meldet den Fortschritt; ein kurzes Fenster liefert sonst
        scheinbar "keine Antwort", obwohl das Loeschen gelaufen ist
        (genau das ist am 2026-10-04 passiert).

        Antwort: ``71 01 ff 00 <status>``; ausgewertet wird ``status``
        (0 = ok, sonst Fehler).

        Rueckgabe: Antwortreport, oder ``None`` bei fehlender/fehlerhafter
        Quittung.
        """
        if addr % 2048:
            raise ValueError("Adresse muss auf 2-KiB-Seite ausgerichtet sein")
        if timeout_ms is None:
            timeout_ms = self.erase_timeout_ms
        self.last_erase_status: Optional[int] = None
        payload = (bytes([0x31, 0x01, 0xFF, 0x00, 0x00])
                   + struct.pack(">I", addr) + struct.pack(">I", length))
        assert len(payload) == 13, len(payload)
        self.send_frame(payload)
        t0 = time.time()
        last = -1
        while True:
            left = timeout_ms - int((time.time() - t0) * 1000)
            if left <= 0:
                if progress:
                    print(f"  ... {timeout_ms / 1000:.0f} s ohne Quittung "
                          f"verstrichen", flush=True)
                return None
            r = self.tp.recv(min(1000, left))
            if not r:
                if progress:
                    sec = int(time.time() - t0)
                    if sec and sec != last and sec % 2 == 0:
                        last = sec
                        print(f"  ... Loeschen laeuft, warte auf Quittung "
                              f"({sec} s von {timeout_ms / 1000:.0f} s)",
                              flush=True)
                continue
            pl = ack_payload(r)
            if pl is None or len(pl) < 5:
                if self.verbose:
                    print(f"  <- [verworfen] {r[:12].hex(' ')}")
                continue
            if pl[0] == 0x7F:
                err = pl[2] if len(pl) > 2 else 0
                print(f"  <- [FEHLER 0x{err:02x}] {r[:14].hex(' ')}")
                return None
            if pl[0] != (0x31 | 0x40) or pl[1:3] != b"\x01\xff":
                if self.verbose:
                    print(f"  <- [verworfen] {r[:14].hex(' ')}")
                continue
            status = pl[4]
            self.last_erase_status = status
            if self.verbose:
                print(f"  <- [ACK] {r[:14].hex(' ')}  Status {status}"
                      f"{' (ok)' if status == ERASE_OK else ' (FEHLER)'}")
            if status != ERASE_OK:
                print(f"[erase] Geraet meldet Fehlerstatus {status} "
                      f"(0 = ok; 1 = Adresse/Laenge/WRP/Busy).")
                return None
            return r

    def cmd_flash_unlock(self, timeout_ms: int = 2000) -> bool:
        """0x31/0x10203 = Flash entsperren (Handler 0x080013DA).

        Der Handler ruft **nur** die Unlock-Routine des RAM-Blobs auf
        (Trampolin 0x08001004 -> Blob +0x2C: HSI/RCC einrichten, dann
        FLASH_KEYR = KEY1/KEY2) und verlangt Payload-Laenge 4 sowie das
        'gueltig'-Flag aus ``0x10``.

        Die Erase- und Programm-Wrapper rufen denselben Unlock zwar selbst
        auf, aber der offizielle Ablauf des Bootloaders hat dieses Kommando --
        es schadet nicht und stellt sicher, dass das Flash-Interface offen ist
        (vermutlich der Grund, warum nach einem USB-Neuaufbau des Geraets
        Loeschen/Programmieren scheinbar wirkungslos blieben).
        """
        self.send_frame(bytes([0x31, 0x01, 0x02, 0x03]))
        t0 = time.time()
        while time.time() - t0 < timeout_ms / 1000.0:
            r = self.tp.recv(300)
            if not r:
                continue
            pl = ack_payload(r)
            if pl is None:
                continue
            if pl[0] == 0x7F:
                err = pl[2] if len(pl) > 2 else 0
                print(f"  <- [FEHLER 0x{err:02x}] beim Entsperren 0x10203")
                return False
            if pl[0] == 0x71 and pl[1:4] == b"\x01\x02\x03":
                if self.verbose:
                    print(f"  <- [ACK] 0x10203 Flash entsperrt")
                return True
        print("[proto] 0x10203 (Flash entsperren) wurde nicht quittiert.")
        return False

    def erase_range(self, addr: int, length: int, chunk: int = 0x800,
                    pause_ms: int = 60, progress: bool = True) -> bool:
        """Bereich loeschen -- in Bloecken mit Statuspruefung nach jedem Block.

        Ein einziger Riesen-Auftrag (0x38000) hat zwei Nachteile: waehrend der
        ~2,5 s spricht das Geraet kein USB (der Host sieht einen Abriss), und
        ein Status wird nur *einmal* am Ende geprueft. In Bloecken zu
        ``chunk`` Bytes (Standard 32 KiB = 16 Seiten) bleibt der Host in
        Kontakt und faellt ein Teilfehler (Status 1) **sofort** auf -- statt
        erst, wenn der Schreibvorgang danach in Fehler 0x72 laeuft.
        """
        chunk = max(0x800, chunk & ~0x7FF)
        todo = list(range(addr, addr + length, chunk))
        for n, a in enumerate(todo, 1):
            ln = min(chunk, addr + length - a)
            for attempt in (1, 2):
                if progress:
                    print(f"[erase] Block {n}/{len(todo)}: 0x{a:08x} "
                          f"({ln} Bytes)"
                          f"{' (Wiederholung)' if attempt > 1 else ''}",
                          flush=True)
                if self.cmd_erase(a, ln, progress=False) is not None:
                    if pause_ms:
                        time.sleep(pause_ms / 1000.0)
                    break
                # Kein gueltiges ACK: entweder echter Fehler (Status 1 = Flash
                # gesperrt/Fehlerbit) oder das Geraet hat sich neu am USB
                # angemeldet. Beides einmal mit frischer Sitzung versuchen.
                print(f"[erase] Block {n} ohne gueltige Quittung -- neu "
                      f"verbinden (Versuch {attempt}/2)", flush=True)
                if not self.open(30.0) or not self.start_session():
                    return False
            else:
                return False
        return True

    def flash_canary(self, timeout_ms: int = 3000,
                     verbose: bool = True) -> bool:
        """Prueft den kompletten Flash-Pfad -- **vor** dem App-Schreiben.

        Die Geraeterekord-Seite (0x08007800) liegt im Bootloader-Bereich (nicht
        in der App-CRC) und ist ueber ``0x2E`` beschreibbar sowie ueber
        ``0x22``/``0xF15B`` wieder **lesbar**. Damit ist sie der einzige Ort,
        an dem sich Unlock + Erase + Programm + Verify funktional pruefen
        lassen: Datensatz schreiben und zuruecklesen.

        Warum das wichtig ist: meldet der Erase-Befehl auch dann Erfolg, wenn
        das Flash-Interface gesperrt ist (er prueft nur BSY, nicht WRPRTERR),
        dann trifft der erste Programmierversuch **belegten** Flash, setzt ein
        Fehlerbit und laesst danach gar nichts mehr zu (siehe
        ``tools/emu.py blsticky``). Mit dieser Sonde merkt man das *bevor*
        der App-Bereich angefasst wird -- und ein Power-Cycle genuegt.

        Rueckgabe: True, wenn Schreiben *und* Zuruecklesen geklappt haben.
        """
        self._canary_n = getattr(self, "_canary_n", 0) + 1
        # Bei jedem Aufruf ANDERE Daten: sonst sieht ein fehlgeschlagener
        # Schreibvorgang wie Erfolg aus, weil der alte Inhalt gleich waere.
        data = bytes([26, (self._canary_n % 12) + 1, (self._canary_n % 28) + 1,
                      0x11, 0x22, 0x33, self._canary_n & 0xFF])
        payload = bytes([0x2E, 0xF1, 0x5A]) + data           # Laenge exakt 10
        assert len(payload) == 10, len(payload)
        self.send_frame(payload)
        ack = None
        t0 = time.time()
        while time.time() - t0 < timeout_ms / 1000.0:
            r = self.tp.recv(300)
            if not r:
                continue
            pl = ack_payload(r)
            if pl is None:
                continue
            if pl[0] == 0x6E:                                # ACK von 0x2E
                ack = pl
                break
            if pl[0] == 0x7F:
                if verbose:
                    print(f"[canary] 0x2E meldet Fehler 0x{pl[2]:02x} -- das "
                          f"Flash-Interface nimmt keine Schreibzugriffe an.")
                return False
        if ack is None:
            if verbose:
                print("[canary] 0x2E wurde nicht quittiert.")
            return False
        back = self.cmd_read_record()
        ok = back is not None and back[:6] == data[:6]
        if verbose:
            print(f"[canary] geschrieben {data[:6].hex(' ')} / gelesen "
                  f"{back.hex(' ') if back else '-'} -> "
                  f"{'Flash-Pfad ok' if ok else 'FEHLER: Schreiben wirkungslos '}"
                  f"{'oder Zugriff gesperrt' if not ok else ''}", flush=True)
        return ok

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
                if pl[1:4] != b"\x01\x02\x02":
                    # Z. B. eine verspaetete Loeschquittung (Unterkommando
                    # 0x1FF00) -- deren Statusbyte ist kein CRC-Ergebnis.
                    if self.verbose:
                        print(f"  <- [verworfen, anderes Unterkommando] "
                              f"{r[:14].hex(' ')}")
                    continue
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
                print("[verify] deep: keine gueltige Loeschquittung -- die "
                      "Seite ist danach\n"
                      "         moeglicherweise leer; das Geraet bleibt im "
                      "Bootloader und kann\n"
                      "         einfach neu geflasht werden.")
                return 2
            self.cmd_sequencer_start()
            self.cmd_set_address(page)
            failed = 0
            # Stromverfahren: nur der erste Rahmen traegt Kommando + Sequenz,
            # sonst wuerde der Bootloader ab dem zweiten Rahmen die Nutzlast
            # wortwoertlich (mit Kommandobyte!) ab dem Zeiger programmieren.
            rest = 0x08040000 - (page + len(blob))
            first = True
            for i in range(0, len(blob), 56):
                blk = blob[i:i + 56]
                if first:
                    if self.cmd_write_block(blk):
                        failed += 1
                    first = False
                else:
                    self.cmd_write_stream(blk)
            # Bis 0x08040000 auffuellen -- nur dann quittiert der Bootloader
            # (0x76) und ist wieder im Kommandomodus.
            while rest > 0:
                n = min(56, rest)
                self.cmd_write_stream(b"\xff" * n)
                rest -= n
            if failed:
                print(f"[verify] deep: {failed} Block/Bloecke nicht geschrieben")
                return 2
            ack = self.tp.recv(4000)     # Abschlussquittung bei 0x08040000
            if ack and ack_payload(ack) and ack_payload(ack)[0] != 0x76:
                print(f"[verify] deep: Abschluss war "
                      f"{ack_payload(ack).hex(' ')}, erwartet 0x76")
        res = self.cmd_app_crc()
        if res is None:
            print("[verify] Geraet hat die CRC-Abfrage nicht akzeptiert.")
            return 2
        return 0 if res == CRC_OK else 1

    def cmd_enter_program(self) -> bool:
        """0x10 = Modus setzen (Payload-Laenge 2, Arg 2/3/0x82/0x83).

        Nur Arg 2 und 3 (bzw. 0x82/0x83) setzen das 'Gueltig'-Flag
        [0x20000028]; 0x34 verlangt dieses Flag. Antwort: ``50 03 01 f4 03 e8``
        (Emulator wie Hardware).

        Rueckgabe: True, wenn das ACK 0x50 kam.
        """
        self.send_frame(bytes([0x10, 0x03]))
        ack = self.expect_ack_strict(0x10, timeout_ms=1500, tries=6)
        if ack is None:
            print("[proto] 0x10 (Modus setzen) wurde nicht quittiert.")
            return False
        pl = ack_payload(ack)
        if self.verbose and pl:
            print(f"     Modus/Info = {pl.hex(' ')}")
        return True

    def cmd_sequencer_start(self) -> bool:
        """0x37 = Sequencer zuruecksetzen (Seq = 1, Zeiger = 0). Antwort 0x77.

        Rueckgabe: True, wenn das ACK 0x77 kam.
        """
        self.send_frame(bytes([0x37]))
        return self.expect_ack_strict(0x37, timeout_ms=1500, tries=6) is not None

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
        return self.expect_ack_strict(0x34, timeout_ms=1500, tries=6) is not None

    # -- Sitzung, Reconnect, Fehlerrahmen ---------------------------------
    def close(self) -> None:
        try:
            self.tp.close()
        except Exception:                      # noqa: BLE001
            pass

    def open(self, timeout_s: float = 30.0) -> bool:
        """Transport wieder sprechbar machen (nach einem USB-Stall).

        Fuer Flash-Befehle schaltet die Firmware den Systemtakt auf HSI und
        PLL/HSE ab; der USB-Takt kommt vom PLL, also bleibt das Geraet kurz
        stumm und hidapi meldet einen Fehler, obwohl der Knoten weiter existiert
        (``dmesg`` bleibt leer). Kann der Transport das selbst (``reopen``, z. B.
        im Emulator), wird er gefragt; sonst wird ein neues Geraet gesucht.
        """
        fn = getattr(self.tp, "reopen", None)
        if callable(fn):
            try:
                return bool(fn(timeout_s))
            except Exception as exc:                # noqa: BLE001
                print(f"[proto] Reconnect fehlgeschlagen: {exc}")
                return False
        self.close()
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout_s:
            try:
                for d in find_bootloader(verbose=False) or ():
                    try:
                        self.tp = Transport(d)
                        if self.verbose:
                            print(f"  [dev] verbunden: {d.get('path')}")
                        return True
                    except Exception as exc:    # noqa: BLE001
                        last = f"{type(exc).__name__}: {exc}"
            except Exception as exc:            # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        print(f"[proto] kein Geraet gefunden ({last})")
        return False

    def start_session(self) -> bool:
        """0xFE-Sync + 0x10 03 + Flash entsperren (0x10203).

        Voraussetzung fuer 0x34/0x31/0x36. Das explizite Entsperren gehoert
        zum dokumentierten Ablauf und macht den Flash-Zugriff unabhaengig
        davon, ob ein vorheriges Kommando das Interface wieder gesperrt hat.
        """
        self.cmd_sync()
        if not self.cmd_enter_program():
            return False
        return self.cmd_flash_unlock()

    def resync(self, addr: int) -> bool:
        """Schreibzeiger absolut setzen: 0x37 (Seq = 1) + 0x34 (Adresse).

        Damit haengt der Zeiger des Bootloaders nicht an der Host-Rechnung:
        er wird vor jedem Blockbuendel neu gesetzt.
        """
        self.seq = 1
        return self.cmd_sequencer_start() and self.cmd_set_address(addr)

    def drain_errors(self, settle_ms: int = 40) -> List[int]:
        """Anstehende Fehlerrahmen (``7f 36 xx``) einsammeln.

        Der Bootloader quittiert erfolgreiche 0x36-Bloecke nicht einzeln,
        meldet Fehler aber sofort. ``settle_ms`` ist die Wartezeit, in der
        spaete Antworten noch eingesammelt werden.
        """
        errs: List[int] = []
        end = time.time() + settle_ms / 1000.0
        while True:
            left = int((end - time.time()) * 1000)
            if left < 0:
                break
            r = self.tp.recv(min(100, max(0, left)))
            if not r:
                if time.time() >= end:
                    break
                continue
            pl = ack_payload(r)
            if pl is None:
                continue
            if pl[0] == 0x7F:
                code = pl[2] if len(pl) > 2 else -2
                errs.append(code)
                if self.verbose:
                    print(f"  <- [FEHLER 0x{code:02x}] {r[:14].hex(' ')}")
            elif self.verbose:
                print(f"  <- [ACK] {r[:14].hex(' ')}")
        return errs

    def cmd_write_block(self, data: bytes, tries: int = 3) -> int:
        """0x36: bis zu 56 Datenbytes schreiben, Schreibzeiger laeuft weiter.

        Rueckgabe: 0 = ok, sonst Fehlercode des Geraets. Der Geraetesequencer
        [0x20000038] wird nur bei Erfolg weitergezaehlt, deshalb wird derselbe
        Block mit **unveraenderter** Sequenznummer wiederholt.
        """
        if len(data) > 56:
            raise ValueError("max 56 Bytes pro 0x36-Block")
        if len(data) % 2:
            # Die Blob-Programm-Routine rechnet die Laenge in HALBWOERTER um
            # (0x0800526E: ``lsrs r0,#1``) und vergleicht anschliessend genau
            # diese Anzahl. Ein ungerades Byte am Ende wird also stillschweigend
            # NICHT geschrieben -- und die Verifikation faellt nicht auf, weil
            # sie dasselbe (verkuerzte) Fenster prueft.
            raise ValueError(f"0x36-Blocklaenge muss gerade sein, ist "
                             f"{len(data)} -- sonst wird das letzte Byte "
                             f"stillschweigend nicht programmiert")
        payload = bytes([0x36, self.seq]) + data
        if not self.block_ack:
            # Streaming: keine Einzelquittung. Anfallende Antworten nur
            # abholen, damit der Puffer leer bleibt; Fehlermeldungen melden.
            # Dafuer ``poll()`` statt ``recv(0)``: ein Lesefehler ist hier
            # normal (hidraw-Poll-Artefakt waehrend des Programmierens) und
            # darf den Handle **nicht** mitten im Strom neu aufbauen.
            self.send_frame(payload)
            self.seq = self.seq + 1 if self.seq < 255 else 1
            self.addr += len(data)
            while True:
                r = self.tp.poll(0)
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

    def cmd_write_stream(self, data: bytes) -> None:
        """Datenblock in der **Stromphase** -- ohne Kommando-/Sequenzbyte.

        Im Image (0x0800168A) steht: nach dem *ersten* erfolgreichen
        ``0x36``-Kommando setzt der Bootloader ``[0x2000001C] = 1``. Der
        Empfangspfad (0x08000B34) schickt jede weitere Nutzlast dann NICHT
        mehr an den Kommando-Dispatcher, sondern an 0x0800168A, und der
        programmiert sie **wortwoertlich** (Nutzlaenge = Anzahl Bytes) ab dem
        Schreibzeiger.

        Zurueck in den Kommandomodus kommt er nur, wenn der Schreibzeiger
        genau 0x08040000 erreicht (Antwort ``0x76``) oder ein
        Programmierfehler den Strom beendet (Antwort ``0x72``, danach ist
        ``[0x2000001C]`` wieder 0).

        Deshalb: Erfolgreiche Stromrahmen werden **nicht** quittiert --
        Fehler aber sofort gemeldet.
        """
        if not 1 <= len(data) <= 58:
            raise ValueError("1..58 Bytes je Stromrahmen")
        if len(data) % 2:
            # Siehe cmd_write_block: die Blob-Routine programmiert len/2
            # Halbwoerter, ein ungerades Byte bleibt unbemerkt liegen.
            raise ValueError(f"Stromrahmen-Laenge muss gerade sein, ist "
                             f"{len(data)}")
        self.send_frame(bytes(data))
        self.addr += len(data)

    def cmd_finish(self) -> None:
        """0x3E = Abschluss (Payload-Laenge 2, Arg 0x00 oder 0x80).

        Arg 0x80 -> keine Antwort; der Bootloader laeuft danach in seinen
        Timeout und macht einen Software-Reset (SCB->AIRCR = 0x05FA0004),
        wodurch die App startet. Arg 0x00 antwortet mit 0x7E.
        """
        self.send_frame(bytes([0x3E, 0x80]))

    # -- Gesamt-Upload -----------------------------------------------------
    def _verify_and_finish(self) -> None:
        """Applikations-CRC pruefen und bei Erfolg den Reset ausloesen."""
        crc_res = self.cmd_app_crc()
        if crc_res == CRC_OK:
            print("[upload] App-CRC stimmt (Status 0) -- Bereich "
                  "vollstaendig und konsistent.")
        elif crc_res is None:
            print("[upload] App-CRC nicht abfragbar -- fahre trotzdem fort.")
        else:
            print(f"[upload] WARNUNG: App-CRC stimmt nicht (Status {crc_res}) "
                  "-- KEIN Reset ausgeloest.\n"
                  "         Es fehlen Bloecke. NICHT einfach nochmal ohne "
                  "Loeschen schreiben (das\n"
                  "         wuerde auf belegten Flash programmieren), sondern "
                  "erneut flashen -- das\n"
                  "         Tool loescht dann vorher.")
            raise IOError("CRC-Pruefung nach dem Schreiben fehlgeschlagen")
        self.cmd_finish()
        print("[upload] fertig, Reset ausgeloest.")

    def _stream_frame(self, blk: bytes, first: bool) -> None:
        """Einen Stromrahmen senden -- bei Schreibfehler Handle erneuern.

        Der Schreibfehler ist meist ein toter Handle (hidraw), nicht ein
        fehlender Rahmen: das Geraet haengt weiter am Bus. Weil nicht sicher
        ist, ob der Rahmen angekommen ist, wird er nach dem Neuaufbau
        **wiederholt** (die Firmware verwirft unvollstaendige Reports).
        """
        for attempt in (1, 2):
            try:
                if first:
                    # Nur der erste Rahmen traegt Kommando + Sequenz (== 1
                    # nach 0x37). Genau dieser Aufruf setzt [0x2000001C] = 1.
                    self.seq = 1
                    rc = self.cmd_write_block(blk)
                    if rc:
                        raise IOError(
                            f"erster 0x36-Rahmen abgelehnt (0x{rc:02x})")
                else:
                    self.cmd_write_stream(blk)
                return
            except Exception as exc:               # noqa: BLE001
                if attempt == 2 or not _is_transport_error(exc):
                    raise
                print("[stream] Schreibfehler -- Handle neu oeffnen und "
                      "Rahmen wiederholen.", flush=True)
                if not self.tp.reopen(10.0):
                    raise IOError("Geraet kommt nicht zurueck") from exc

    def upload_stream(self, img: Image, region: str = "app", chunk: int = 56,
                      do_erase: bool = True, poll_ms: float = 3.0,
                      end_wait_ms: float = 8000.0, pace_ms: float = 4.0,
                      batch_frames: int = 64,
                      clear_ms: float = 2.0) -> None:
        """Image im **Datenstrom** schreiben -- das vom Bootloader erwartete
        Verfahren.

        Der Bootloader quittiert nur den **ersten** Rahmen eines Stromes
        (``0x36`` mit Sequenzbyte); alles danach muss als reiner Datenrahmen
        kommen. Genau daran ist das blockweise Senden gescheitert: ab Block 2
        wurde jeder Kommandorahmen ``36 <seq> <56>`` als *Nutzlast* gewertet
        und mit 58 statt 56 Byte ab dem Zeiger programmiert. Nach einem
        ``0x37``/``0x34`` (Wiederaufsetzen) lag der Zeiger dann hinter dem
        tatsaechlichen Stand -- also auf schon beschriebenem Flash: PGERR,
        Pruefsummenfehler in der Routine, Antwort ``0x72`` und Verriegelung.

        Endmarke: erreicht der Zeiger exakt 0x08040000 (Ende des App-Bereichs),
        antwortet der Bootloader ``76 <seq>`` und ist wieder im
        Kommandomodus. Das ist gleichzeitig der Beweis, dass der Zeiger exakt
        mitgerechnet hat.
        """
        if region == "app":
            start, stop = APP_BASE, APP_CRC_ADDR + 4
        elif region == "all":
            # Das Flash-Interface des Blobs prueft addr >= 0x08007800
            # (0x080019A6) -- der Bootloader-Bereich ist damit grundsaetzlich
            # nicht ueber 0x36 beschreibbar.
            raise ValueError(
                "region 'all' ist nicht schreibbar: der Bootloader lehnt "
                "Adressen < 0x08007800 ab (Schutz des eigenen Bereichs). "
                "Nur 'app' flashen.")
        else:
            raise ValueError("region muss 'app' oder 'all' sein")
        if start < 0x08007800:
            raise ValueError(
                f"Startadresse 0x{start:08x} liegt unterhalb von 0x08007800 "
                "-- dort lehnt der Bootloader das Programmieren ab.")
        blob = bytearray(img.slice(start, stop - start))
        rest = len(blob) % chunk
        if rest:
            # Der Strom endet erst bei 0x08040000; 0xFF programmieren ist auf
            # geloeschtem Flash erlaubt (kein 0 -> 1), kostet nur Zeit.
            blob += b"\xff" * (chunk - rest)
        total = len(blob)
        nblk = total // chunk
        print(f"[stream] {region}: 0x{start:08x}..0x{stop - 1:08x} "
              f"({total} Bytes) = 1+{nblk - 1} Rahmen a {chunk} Byte")
        if self.wire == "app":
            if not self.start_session():
                raise IOError("Sitzung liess sich nicht starten (0x10).")
        if not self.cmd_flash_unlock() and self.wire == "app":
            raise IOError("Flash liess sich nicht entsperren (0x10203).")
        if do_erase:
            self._prepare_flash_erase(start, stop)
        if not self.resync(start):
            raise IOError("Resync 0x37/0x34 nicht quittiert")
        t0 = time.time()
        pos = 0
        err: Optional[int] = None
        end_seen = False
        stalls0 = getattr(self.tp, "stall_reopens", 0)
        losts0 = getattr(self.tp, "lost_reopens", 0)
        stalls_total = 0
        pace_s = max(0.0, pace_ms) / 1000.0
        batch = max(1, batch_frames)
        reads = 0
        while pos < total:
            blk = bytes(blob[pos:pos + chunk])
            self._stream_frame(blk, first=(pos == 0))
            pos += len(blk)
            # Nur selten lesen: im Strom kommt (ausser Fehlern und der
            # Endquittung) nichts zurueck, und hidapi meldet beim Pollen auf
            # einem gerade beschaeftigten Geraet sporadisch
            # ``HIDException: Success`` (hidraw-Poll ohne POLLIN/POLLERR).
            # Das darf den Strom nicht abbrechen -- deshalb Schuebe senden und
            # nur am Schubende kurz nachsehen.
            if pos % (batch * chunk) and pos < total:
                if pace_s:
                    time.sleep(pace_s)
            else:
                reads += 1
                stalls = getattr(self.tp, "stall_reopens", 0) - stalls0
                losts = getattr(self.tp, "lost_reopens", 0) - losts0
                if losts:
                    # Knoten war wirklich weg (Neu-Anmeldung). Der
                    # Schreibzeiger des Geraets ist damit unbekannt --
                    # weiterzuschreiben wuerde Flash beschreiben, dessen
                    # Adresse nicht mehr sicher ist. Deshalb: abbrechen.
                    raise IOError(
                        f"USB-Knoten ist bei 0x{start + pos:08x} "
                        f"verschwunden und neu angemeldet "
                        f"({pos}/{total} Bytes).\n"
                        "        Der Schreibzeiger des Geraets ist jetzt "
                        "unbekannt. Kein Reset ausgeloest --\n"
                        "        einfach erneut flashen (das Tool loescht "
                        "vorher).")
                if stalls:
                    stalls_total += stalls
                    stalls0 = getattr(self.tp, "stall_reopens", 0)
                    print(f"[stream] USB-Handle bei "
                          f"0x{start + pos:08x} neu geoeffnet "
                          f"(Stall, {stalls_total}. Mal) -- Strom laeuft "
                          f"weiter.", flush=True)
                    if stalls_total > 40:
                        raise IOError(
                            "mehr als 40x USB-Handle neu geoeffnet -- das "
                            "Geraet ist nicht stabil.\n"
                            "        Bitte Kabel/Port pruefen; kein Reset "
                            "ausgeloest.")
                r = self.tp.poll(clear_ms)
                pl = ack_payload(r) if r else None
                if pl is not None:
                    if pl[0] == 0x7F:
                        err = pl[2] if len(pl) > 2 else -2
                        break
                    if pl[0] == 0x76:
                        # Zeiger hat 0x08040000 erreicht -- nur am Ende.
                        if pos < total:
                            print(f"[stream] Hinweis: Geraet meldet "
                                  f"bereits bei 0x{start + pos:08x} "
                                  f"'fertig' ({pos}/{total} Bytes).")
                        end_seen = True
                        break
                if pace_s:
                    time.sleep(pace_s)
            if pos % (chunk * 512) == 0:
                dt = time.time() - t0
                rate = pos / dt if dt > 0 else 0
                print(f"[stream] {pos}/{total}  {100 * pos // total}%  "
                      f"({rate / 1024:.1f} KiB/s)", flush=True)
        if err is not None:
            if err in (0x11, 0x12, 0x13, 0x24, 0x73):
                raise IOError(
                    f"Fehler 0x{err:02x} im Datenstrom bei "
                    f"0x{start + pos - chunk:08x}: das Geraet hat einen "
                    f"Datenrahmen als\n"
                    "        Kommando gelesen -- der Datenstrom ist also "
                    "beendet (Flag zurueckgesetzt).\n"
                    "        Typische Ursache: Zwischenzeitliche "
                    "USB-Neuanmeldung oder ein Rahmen ging\n"
                    "        verloren. Kein Reset ausgeloest -- einfach "
                    "erneut flashen.")
            raise IOError(
                f"Programmierfehler 0x{err:02x} im Datenstrom bei "
                f"0x{start + pos - chunk:08x}.\n"
                "        Der Strom ist damit beendet (Flag zurueckgesetzt), "
                "weitere Rahmen\n"
                "        wuerden als Kommandos gelesen. Nicht wiederholen -- "
                "das Geraet erneut\n"
                "        flashen (das Tool loescht vorher).")
        dt = time.time() - t0
        print(f"[stream] {pos}/{total} Bytes geschrieben in {dt:.1f} s "
              f"({pos / dt / 1024:.1f} KiB/s)")
        # Endquittung: nur wenn der Zeiger exakt 0x08040000 erreicht hat.
        got76 = None
        tries = 0
        while not end_seen and tries < 40:
            tries += 1
            r = self.tp.recv(200)
            if not r:
                continue
            pl = ack_payload(r)
            if pl is None:
                continue
            if pl[0] == 0x76:
                got76 = pl
                break
            if pl[0] == 0x7F:
                raise IOError(f"Fehlerrahmen nach dem Strom: "
                              f"0x{(pl[2] if len(pl) > 2 else 0):02x}")
        if got76 is None and end_seen:
            got76 = bytes([0x76, 0])
        if got76 is None:
            print("[stream] WARNUNG: keine 0x76-Endquittung -- der Zeiger hat "
                  "0x" f"{stop:08x} nicht exakt erreicht.")
        else:
            print(f"[stream] Endquittung 0x76 -- Schreibzeiger steht exakt "
                  f"auf 0x{stop:08x}.")
        self._verify_and_finish()

    def _prepare_flash_erase(self, start: int, stop: int) -> None:
        """Bereich seitenweise loeschen und danach die Sitzung neu aufbauen.

        Die Flash-Befehle schalten den Systemtakt um (Unlock-Routine des
        Blobs), der USB-Takt faellt dabei kurz weg -- kein Abstecken, aber
        der alte Handle ist unbrauchbar.
        """
        print(f"[upload] loesche 0x{start:08x}..0x{stop - 1:08x} "
              f"({stop - start} Bytes) in Bloecken ...", flush=True)
        a = self.erase_range(start, stop - start,
                             chunk=self.erase_chunk,
                             pause_ms=self.erase_pause_ms)
        if not a and self.wire == "app":
            raise IOError(
                "Loeschen fehlgeschlagen (Status 1) -- der Flash-Controller "
                "ist gesperrt\n"
                "        oder hat ein Fehlerbit. Es wurde NICHT geschrieben; "
                "das Geraet bleibt\n"
                "        im Bootloader. Abhilfe: Stromversorgung trennen "
                "(Akku/Display abziehen,\n"
                "        kurz warten) und erneut flashen.")
        if self.wire == "app":
            print("[upload] Sitzung neu aufbauen (Stall nach dem Flash-Befehl) "
                  "...", flush=True)
            time.sleep(0.5)
            if not self.open(30.0) or not self.start_session():
                raise IOError("Geraet antwortet nach dem Loeschen nicht mehr.")

    def upload(self, img: Image, region: str = "app", chunk: int = 56,
               do_erase: bool = True, burst_blocks: int = 8,
               retries: int = 6, pause_ms: float = 150,
               settle_ms: float = 120, pace_ms: float = 50,
               stream: bool = False, batch_frames: int = 64,
               stream_pace_ms: float = 4.0) -> None:
        """Image schreiben -- blockweise, fehlerfest und ohne Doppelbeschreiben.

        Mit ``stream=True`` wird stattdessen ``upload_stream`` benutzt: das ist
        das Verfahren, das der Bootloader tatsaechlich erwartet (ein
        Kommandorahmen, danach reine Datenrahmen, Ende bei 0x08040000). Der
        blockweise Weg hier ist nur noch fuer Vergleichsmessungen da -- er
        scheitert am Geraet, weil der Bootloader nach dem ersten 0x36 auf den
        Datenstrom umschaltet (siehe ``upload_stream``).

        Warum nicht einfach durchstreamen:

        * Der Bootloader quittiert erfolgreiche 0x36-Bloecke **nicht**
          einzeln, Fehler aber sofort (``7f 36 73`` Sequenz, ``7f 36 72``
          Programmieren). Ein blindes Durchsenden merkt einen Fehler erst am
          Ende -- dann fehlen Bloecke und die Applikations-CRC passt nicht.
        * Nach dem Loeschen (und gelegentlich mitten im Schreiben) meldet sich
          das Geraet am USB neu an; der alte Handle ist tot.

        **Wichtig**: der Transportfehler ist meist *kein* Abstecken. Fuer
        Flash-Befehle schaltet die Firmware den Systemtakt auf HSI und PLL/HSE
        ab (Unlock-Routine des Blobs); der USB-Takt kommt vom PLL, also bleibt
        das Geraet kurz stumm -- der Kernel meldet nichts (``dmesg`` leer).
        Deshalb wird der Knoten nur neu geoeffnet und danach weitergearbeitet,
        und zwischen den Kommandos bleibt eine Pause (``--pace-ms``,
        ``--settle-ms``).

        **Niemals einen Block zweimal schreiben**: der STM setzt beim
        Programmieren auf nicht-geloeschte Halbwoerter ein Fehlerbit im
        FLASH_SR, das die Eintrittspruefung des Flash-Blobs (``0x080051E0``,
        ``0x0800527A``: SR bit 4/2) dauerhaft fehlschlagen laesst -- danach
        meldet *jede* Loeschung Status 1 und *jeder* Block 0x72, auch auf
        leerem Bereich. Im Emulator nachgestellt mit ``tools/emu.py blsticky``;
        Software entfernt das Bit nicht (die W1C-Schreiboperation liegt hinter
        der Pruefung) -- es hilft nur ein Power-Cycle. Deshalb:

        Ablauf je Blockbuendel (``burst_blocks`` * 56 Byte):

        1. ``0x37`` + ``0x34`` -- Sequencer und Adresse absolut setzen,
        2. ab dem Wiederaufsetzpunkt senden,
        3. Fehlerrahmen einsammeln (``settle_ms``),
        4. ``0x72`` (Programmierfehler, Flash-Fehlerbit) -> **sofortiger
           Abbruch** mit Hinweis auf Power-Cycle,
        5. ``0x24`` (Datenmodus) -> Sitzung neu aufbauen und weiter,
        6. ``0x73`` (Sequenz) -> der erste abgelehnte Block und alles danach
           wurden **nicht** geschrieben: genau dort wieder aufsetzen
           (davor liegende Bloecke werden nie wiederholt),
        7. bei USB-Abriss: neu verbinden, Sitzung neu aufbauen, weiter.

        Am Ende entscheidet die Applikations-CRC (0x31/0x10202): erst bei
        Status 0 wird der Reset (0x3E) ausgeloest.
        """
        if region == "app":
            start, stop = APP_BASE, APP_CRC_ADDR + 4
        elif region == "all":
            start, stop = FLASH_BASE, FLASH_BASE + len(img.data)
        else:
            raise ValueError("region muss 'app' oder 'all' sein")
        blob = img.slice(start, stop - start)
        print(f"[upload] {region}: 0x{start:08x}..0x{stop - 1:08x} "
              f"({len(blob)} Bytes), {burst_blocks} Bloecke je Buendel")
        if stream:
            # Der Bootloader schaltet nach dem ersten 0x36 auf einen reinen
            # Datenstrom um -- blockweises Senden ist damit unmoeglich.
            self.upload_stream(img, region=region, chunk=chunk,
                               do_erase=do_erase,
                               pace_ms=stream_pace_ms,
                               batch_frames=batch_frames)
            return
        if self.wire == "app" and not self.start_session():
            raise IOError("Sitzung liess sich nicht starten (0x10).")
        if self.wire == "app" and not self.flash_canary():
            raise IOError(
                "Flash-Sonde fehlgeschlagen: die Geraeterekord-Seite liess "
                "sich nicht\n"
                "        schreiben bzw. nicht zuruecklesen. Das heisst, dass das "
                "Flash-Interface\n"
                "        gesperrt ist (z. B. nach einem Takt-Umschalt-Stall) -- "
                "der Erase-Befehl\n"
                "        meldet dann trotzdem Erfolg! Es wurde NICHTS im "
                "Applikationsbereich\n"
                "        geschrieben. Abhilfe: Stromversorgung trennen "
                "(Akku/Display abziehen,\n"
                "        kurz warten) und erneut flashen.")

        if do_erase:
            # f_801998 ist reines Programmieren -- ohne vorheriges Loeschen
            # antwortet 0x36 mit Fehler 0x72.
            print(f"[upload] loesche 0x{start:08x}..0x{stop - 1:08x} "
                  f"({stop - start} Bytes) in Bloecken ...", flush=True)
            a = self.erase_range(start, stop - start,
                                 chunk=self.erase_chunk,
                                 pause_ms=self.erase_pause_ms)
            if not a and self.wire == "app":
                raise IOError(
                    "Loeschen fehlgeschlagen (Status 1) -- der Flash-Controller "
                    "ist gesperrt\n"
                    "        oder hat ein Fehlerbit. Es wurde NICHT geschrieben; "
                    "das Geraet bleibt\n"
                    "        im Bootloader. Abhilfe: Stromversorgung trennen "
                    "(Akku/Display abziehen,\n"
                    "        kurz warten) und erneut flashen.")
            if self.wire == "app":
                # Nach dem Loeschen kurz beruhigen und Sitzung neu aufbauen:
                # die Flash-Befehle schalten den Systemtakt um, der USB-Takt
                # (PLL) faellt dabei kurz weg (Stall, kein Abstecken).
                print("[upload] Sitzung neu aufbauen (Stall nach dem "
                      "Flash-Befehl) ...", flush=True)
                time.sleep(0.5)
                if not self.open(30.0) or not self.start_session():
                    raise IOError("Geraet antwortet nach dem Loeschen nicht "
                                  "mehr.")
                if not self.flash_canary():
                    raise IOError(
                        "Nach dem Loeschen nimmt das Flash-Interface keine "
                        "Schreibzugriffe\n"
                        "        mehr an (Sonde ueber die Datensatzseite "
                        "fehlgeschlagen). Es wurde\n"
                        "        NICHTS im Applikationsbereich geschrieben. "
                        "Power-Cycle noetig.")

        burst = max(chunk, burst_blocks * chunk)
        nburst = (len(blob) + burst - 1) // burst
        retried = 0
        skipped = 0
        for b in range(nburst):
            off = b * burst
            piece = blob[off:off + burst]
            resume = 0            # Bytes des Buendels, die schon geschrieben sind
            done = False
            for attempt in range(1, retries + 1):
                try:
                    if self.wire == "app" and \
                            not self.resync(start + off + resume):
                        raise IOError("Resync 0x37/0x34 nicht quittiert")
                    nblk = 0
                    errs: List[int] = []
                    need_resync = False
                    for i in range(resume, len(piece), chunk):
                        blk = piece[i:i + chunk]
                        if all(b == 0xFF for b in blk):
                            # Nicht schreiben: 0xFF laesst sich auf belegtem
                            # Flash nicht programmieren (0 -> 1 unmoeglich) und
                            # setzt PGERR. Steht dort wirklich Dateninhalt,
                            # bleibt er so erhalten -- die CRC-Endkontrolle
                            # meldet das anschliessend, ohne den Latch.
                            skipped += 1
                            need_resync = True
                            continue
                        if need_resync:
                            if self.wire == "app" and \
                                    not self.resync(start + off + i):
                                raise IOError("Resync nach uebersprungenem "
                                              "Block nicht quittiert")
                            need_resync = False
                        rc = self.cmd_write_block(blk)
                        nblk += 1
                        if rc:
                            errs.append(rc)
                    errs += self.drain_errors(settle_ms)
                except Exception as exc:            # noqa: BLE001
                    if not _is_transport_error(exc):
                        raise
                    print(f"  [retry] Buendel 0x{start+off:08x}: "
                          f"{type(exc).__name__}: {exc} -- Transport neu "
                          f"aufbauen (Versuch {attempt}/{retries})", flush=True)
                    try:
                        self.close()
                    except Exception:               # noqa: BLE001
                        pass
                    if not self.open(30.0) or not self.start_session():
                        raise IOError("Geraet kommt nicht zurueck") from exc
                    if self.wire == "app" and not self.flash_canary():
                        raise IOError(
                            "Nach dem Transportfehler nimmt das Flash-Interface "
                            "keine\n"
                            "        Schreibzugriffe mehr an (Sonde ueber die "
                            "Datensatzseite fehlgeschlagen).\n"
                            "        Es wurde nichts weiter geschrieben -- "
                            "Power-Cycle noetig, danach\n"
                            "        erneut flashen.") from exc
                    continue
                if not errs:
                    done = True
                    break
                retried += len(errs)
                codes = " ".join(f"0x{c:02x}" for c in errs[:6])
                if 0x72 in errs:
                    # FATAL: der Flash-Controller nimmt jetzt nichts mehr an
                    # (Fehlerbit im FLASH_SR). Weiterschreiben ist sinnlos und
                    # wuerde nur weiter schaden -- sofort abbrechen.
                    raise IOError(
                        f"Programmierfehler 0x72 bei "
                        f"0x{start + off + resume:08x}.\n"
                        "        Ursache: es wurde auf NICHT geloeschten Flash "
                        "geschrieben (Loeschen\n"
                        "        unvollstaendig oder Block doppelt). Der "
                        "Flash-Controller setzt dabei\n"
                        "        ein Fehlerbit und nimmt danach WEDER Loeschen "
                        "noch Programmieren an\n"
                        "        (Erase meldet Status 1), auch auf leerem "
                        "Bereich. Im Emulator\n"
                        "        nachgestellt mit tools/emu.py blsticky; "
                        "Software loescht das Bit nicht.\n"
                        "        Abhilfe: Stromversorgung trennen (Akku/Display "
                        "abziehen, kurz warten),\n"
                        "        danach erneut flashen.")
                if 0x24 in errs:
                    print("  [retry] Datenmodus fehlt (0x24) -- Sitzung neu "
                          "aufbauen", flush=True)
                    if not self.start_session():
                        raise IOError("Sitzung liess sich nicht neu aufbauen")
                    continue
                # 0x73 = Sequenzfehler: der erste abgelehnte Block und alles
                # danach wurden NICHT geschrieben, die Bloecke davor schon.
                # Also genau ab dort wieder aufsetzen -- niemals Bloecke
                # wiederholen, die schon geschrieben sind (das setzt PGERR!).
                skip = max(0, nblk - len(errs))
                print(f"  [retry] Buendel 0x{start+off:08x}: {len(errs)} "
                      f"Fehlerrahmen ({codes}), {skip} Block/Bloecke davor ok "
                      f"-- weiter ab 0x{start+off+resume+skip*chunk:08x} "
                      f"(Versuch {attempt}/{retries})", flush=True)
                resume = min(len(piece), resume + skip * chunk)
                time.sleep(pause_ms / 1000.0)
            if not done:
                raise IOError(
                    f"Blockbuendel 0x{start + off:08x} liess sich nach "
                    f"{retries} Versuchen nicht schreiben.\n"
                    "        Das Geraet bleibt im Bootloader und kann erneut "
                    "geflasht werden.")
            if pace_ms:
                time.sleep(pace_ms / 1000.0)
            if b % 16 == 15 or off + burst >= len(blob):
                n = min(len(blob), off + len(piece))
                print(f"         {n}/{len(blob)}  {100 * n // len(blob)}%"
                      f"{f'  ({retried} Wiederholungen)' if retried else ''}"
                      f"{f'  ({skipped} 0xFF-Bloecke uebersprungen)' if skipped else ''}",
                      flush=True)

        # Endkontrolle VOR dem Reset: das ist der eigentliche Nachweis, dass
        # Erase + Schreiben funktioniert haben (CRC-Wort steckt im Image).
        if skipped:
            print(f"[upload] {skipped} Block/Bloecke waren im Image nur 0xFF "
                  f"und wurden NICHT geschrieben.")
        crc_res = self.cmd_app_crc()
        if crc_res == CRC_OK:
            print("[upload] App-CRC stimmt (Status 0) -- Bereich "
                  "vollstaendig und konsistent.")
        elif crc_res is None:
            print("[upload] App-CRC nicht abfragbar -- fahre trotzdem fort.")
        else:
            print(f"[upload] WARNUNG: App-CRC stimmt nicht (Status {crc_res}) "
                  "-- KEIN Reset ausgeloest.\n"
                  "         Es fehlen Bloecke. NICHT einfach nochmal ohne "
                  "Loeschen schreiben (das\n"
                  "         wuerde auf belegten Flash programmieren), sondern "
                  "erneut flashen -- das\n"
                  "         Tool loescht dann vorher.")
            raise IOError("CRC-Pruefung nach dem Schreiben fehlgeschlagen")
        self.cmd_finish()
        print("[upload] fertig, Reset ausgeloest.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _add_burst_options(sp) -> None:
    """Gemeinsame Upload-Optionen: Stromverfahren + Blockbuendel."""
    sp.add_argument("--legacy-blocks", action="store_true",
                    help="altes blockweises Verfahren (scheitert am Geraet: "
                         "der Bootloader schaltet nach dem ersten 0x36 auf "
                         "den Datenstrom um)")
    sp.add_argument("--stream-pace-ms", type=float, default=4.0,
                    metavar="MS",
                    help="Pause je Datenrahmen im Strom (Standard "
                         "%(default)g; der Flash-Controller braucht je "
                         "Rahmen einige ms -- bei CRC-Fehler erhoehen)")
    sp.add_argument("--stream-batch", type=int, default=64, metavar="N",
                    help="Rahmen je Schub; erst am Schubende wird gelesen "
                         "(Standard %(default)s)")
    sp.add_argument("--resync", type=int, default=8, metavar="N",
                    help="(nur --legacy-blocks) Bloecke je Buendel")
    sp.add_argument("--retries", type=int, default=6, metavar="N",
                    help="Versuche je Buendel bei Fehlerrahmen (Standard "
                         "%(default)s)")
    sp.add_argument("--pause-ms", type=float, default=150.0, metavar="MS",
                    help="Pause vor einem Wiederholungsversuch in ms "
                         "(Standard %(default)g)")
    sp.add_argument("--settle-ms", type=float, default=120.0, metavar="MS",
                    help="Wartezeit je Buendel zum Einsammeln spaeter "
                         "Fehlerrahmen in ms (Standard %(default)g; deckt auch "
                         "den kurzen USB-Stall nach Flash-Befehlen ab)")
    sp.add_argument("--pace-ms", type=float, default=50.0, metavar="MS",
                    help="Pause nach jedem Buendel in ms (Standard "
                         "%(default)g; entlastet den Flash-Controller)")


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
    sp.add_argument("--erase-timeout", type=float, default=ERASE_TIMEOUT_MS / 1000.0,
                    metavar="S",
                    help="Sekunden auf die Loeschquittung warten (Standard "
                         "%(default)g); 224 KiB Loeschen dauert Sekunden")
    sp.add_argument("--erase-chunk", type=lambda x: int(x, 0), default=0x800,
                    metavar="B",
                    help="Loeschen in Bloecken von B Bytes (Standard "
                         "%(default)#x = 1 Seite); nach jedem Block wird "
                         "der Status geprueft")
    sp.add_argument("--erase-pause-ms", type=float, default=60.0, metavar="MS",
                    help="Pause zwischen zwei Loeschbefehlen in ms (Standard "
                         "%(default)g; eine Seite braucht bis zu ~25 ms)")
    _add_burst_options(sp)
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

    sp = sub.add_parser("wptest",
                        help="WRP testen: dieselbe App-Seite zweimal loeschen")
    sp.add_argument("--addr", type=lambda x: int(x, 0), default=0x08030000,
                    help="Seite in der Applikationsregion (Standard "
                         "%(default)#010x)")
    sp.add_argument("--wire", choices=["app", "raw", "hdr"], default="app")

    sp = sub.add_parser("writetest",
                        help="Schreibpfad 0x34/0x36 auf der Datensatzseite "
                             "pruefen")
    sp.add_argument("--readback", action="store_true",
                    help="nur die Datensatzseite lesen (nach Power-Cycle)")
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
    sp.add_argument("--erase-timeout", type=float, default=ERASE_TIMEOUT_MS / 1000.0,
                    metavar="S",
                    help="Sekunden auf die Loeschquittung warten (Standard "
                         "%(default)g)")
    sp.add_argument("--erase-chunk", type=lambda x: int(x, 0), default=0x800,
                    metavar="B",
                    help="Loeschen in Bloecken von B Bytes (Standard "
                         "%(default)#x = 1 Seite)")
    sp.add_argument("--erase-pause-ms", type=float, default=60.0, metavar="MS",
                    help="Pause zwischen zwei Loeschbefehlen in ms (Standard "
                         "%(default)g)")
    _add_burst_options(sp)

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
            proto = Protocol(tp, verbose=not args.quiet, wire="app",
                             erase_timeout_ms=int(args.erase_timeout * 1000),
                             erase_chunk=args.erase_chunk,
                             erase_pause_ms=int(args.erase_pause_ms))
            proto.upload(img, region=args.region,
                         burst_blocks=args.resync, retries=args.retries,
                         pause_ms=int(args.pause_ms),
                         settle_ms=int(args.settle_ms),
                         pace_ms=int(args.pace_ms),
                         stream=(not args.legacy_blocks),
                         batch_frames=args.stream_batch,
                         stream_pace_ms=args.stream_pace_ms)
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

    if args.cmd == "wptest":
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
            proto.start_session()
            # Vorher/nachher die Applikations-CRC abfragen: solange der Bereich
            # mit seinem CRC-Wort uebereinstimmt (Status 0), zeigt ein Wechsel
            # auf Status 1, dass die Loeschung tatsaechlich etwas veraendert hat
            # (PM0075: WRPRTERR wird gesetzt, wenn eine geschuetzte Seite
            # geloescht/programmiert werden soll -- der Wrapper prueft nur BSY
            # und meldet deshalb trotzdem Erfolg).
            res0 = proto.cmd_app_crc()
            print(f"[wptest] App-CRC vor dem Loeschen : Status {res0} "
                  f"(0 = Bereich ist selbstkonsistent)")
            print(f"[wptest] Seite 0x{args.addr:08x} zweimal loeschen "
                  f"(schreibt nichts):")
            st = []
            for n in (1, 2):
                proto.cmd_erase(args.addr, 0x800, timeout_ms=20000)
                st.append(proto.last_erase_status)
                print(f"[wptest] Versuch {n}: Status = {st[-1]}")
                time.sleep(0.2)
            res1 = proto.cmd_app_crc()
            print(f"[wptest] App-CRC nach dem Loeschen: Status {res1}")
        finally:
            tp.close()
        print()
        if res0 == 0 and res1 == 0:
            print("[wptest] Die Applikations-CRC war vorher und ist nachher "
                  "konsistent -> die\n"
                  "         Loeschung hat den Bereich NICHT veraendert.")
        elif res0 == 0 and res1 == 1:
            print("[wptest] Die Applikations-CRC war konsistent und ist es "
                  "nicht mehr -> die\n"
                  "         Loeschung hat den Bereich sehr wohl veraendert.")
        if st[0] == ERASE_OK and st[1] == ERASE_OK:
            print("[wptest] Beide Loeschungen akzeptiert -> kein WRPRTERR "
                  "gesetzt.\n"
                  "         Die Seite ist also NICHT schreibgeschuetzt; ein "
                  "wirkungsloses\n"
                  "         Loeschen haette eine andere Ursache.")
        elif st[0] == ERASE_OK and st[1] == 1:
            print("[wptest] Erste Loeschung akzeptiert, zweite mit Status 1 "
                  "abgelehnt ->\n"
                  "         die Seite ist SCHREIBGESCHUETZT (WRP): das "
                  "Loeschen setzt WRPRTERR,\n"
                  "         ohne es zu melden (der Wrapper prueft nur BSY).\n"
                  "         Damit kann der Bootloader den Applikationsbereich "
                  "nicht aendern --\n"
                  "         die Option Bytes muessen per SWD (ST-Link) "
                  "geaendert werden.")
        else:
            print("[wptest] Erstes Loeschen schon mit Status 1 -> es ist "
                  "bereits ein Fehlerbit\n"
                  "         gesetzt (Latch aus einem frueheren Versuch). "
                  "Power-Cycle, dann erneut.")
        return 0

    if args.cmd == "writetest":
        # Probe auf der EINZIGEN lesbaren Flash-Seite (Geraeterekord
        # 0x08007800). Sie klaert drei Fragen auf einmal:
        #  1. wirkt das Loeschen?       -> gelesen == ff ff ff ff ff ff
        #  2. wirkt 0x34/0x36 (2 Byte)? -> nach Power-Cycle steht dort A0 A1
        #  3. wirkt der Stromrahmen?    -> die naechsten Bytes stehen ab
        #     0x08007802 (wortwoertlich, ohne Kommando-/Sequenzbyte)
        # Der erste Block muss **gerade** Laenge haben: die Blob-Routine
        # rechnet in Halbwoertern, ein einzelnes Byte wird stillschweigend
        # nicht programmiert (am Geraet am 2026-10-04 genau so beobachtet).
        # Das Lesen geht nur, solange der Bootloader Kommandos annimmt; nach
        # dem ersten 0x36 schaltet er auf den Datenstrom um (siehe
        # ``upload_stream``). Deshalb: Ausgabe lesen, dann warten (der
        # Bootloader setzt sich nach seinem Timeout selbst zurueck) oder
        # Power-Cycle und ``writetest --readback``.
        rec = 0x08007800
        leer = b"\xff" * 6
        hits = find_bootloader(verbose=False)
        if not hits:
            print("Kein Bootloader gefunden ('CEBS Bootloader Mode').")
            return 3
        tp = Transport(hits[0])
        try:
            proto = Protocol(tp, verbose=True, wire=args.wire)
            if args.readback:
                back = proto.cmd_read_record()
                print(f"[writetest] Datensatzseite 0x{rec:08x}: "
                      f"{back.hex(' ') if back else 'keine Antwort'}")
                if back == leer:
                    print("         -> geloescht/leer (0xFF = leer!). "
                          "Ein vorher geschriebenes Muster ist nicht da.")
                elif back:
                    b = back
                    notes = []
                    if b[:2] == b"\xa0\xa1":
                        notes.append("Byte 0..1 = Kommandorahmen 0x36 hat "
                                     "geschrieben (A0 A1)")
                    if b[2:6] == bytes([0xB1, 0xB2, 0xB3, 0xB4]):
                        notes.append("Byte 2..5 = Stromrahmen hat "
                                     "wortwoertlich geschrieben (B1..B4)")
                    if b[2:4] == b"\x22\xf1":
                        notes.append("Byte 2..3 = der Leseversuch '22 f1 5b' "
                                     "wurde als Stromdatum geschrieben "
                                     "(Kommandos wurden nicht mehr gelesen)")
                    print("         -> " + ("; ".join(notes) if notes else
                          "unbekanntes Muster (kein Testmuster vom "
                          "letzten Lauf)"))
                print("         Muster bei sauberem Lauf: 'a0 a1 b1 b2 b3 b4'")
                return 0
            if not proto.start_session():
                print("[writetest] Sitzung (0x10) nicht gestartet.")
                return 4
            proto.cmd_flash_unlock()
            print(f"[writetest] 1) Datensatzseite 0x{rec:08x} loeschen ...")
            proto.cmd_erase(rec, 0x800, timeout_ms=20000)
            got = proto.cmd_read_record()
            if got == leer:
                bew = "LEER -- Loeschen wirkt"
            elif got is None:
                bew = "keine Antwort (Geraet im Strommodus)"
            else:
                bew = "nicht leer"
            print(f"[writetest] 2) gelesen: "
                  f"{got.hex(' ') if got else 'keine Antwort'} -> {bew}")
            print("[writetest] 3) 0x37 + 0x34 + Kommandorahmen '36 01 A0 A1' "
                  "(2 Byte) ...")
            if not proto.resync(rec):
                print("[writetest]    Resync nicht quittiert.")
            rc = proto.cmd_write_block(bytes([0xA0, 0xA1]))
            errs = proto.drain_errors(300)
            print(f"[writetest]    rc={rc} Fehler={[hex(e) for e in errs]}")
            print("[writetest]    -> ab hier ist der Bootloader im "
                  "Datenstrom: Kommandos werden nicht mehr gelesen.")
            # WICHTIG: der Datenrahmen kommt VOR dem Leseversuch. Der
            # Leseversuch (3 Byte) wird naemlich selbst als Stromdatum
            # programmiert -- aber nur 3>>1 = 1 Halbwort, waehrend der Zeiger um
            # die volle Laenge (+3, ungerade!) weiterlaeuft. Danach landet der
            # naechste Rahmen auf einer ungeraden Adresse und wirkt nicht mehr
            # (am Geraet am 2026-10-04 genau so beobachtet).
            print("[writetest] 4) reiner Datenrahmen 'B1..B6' (6 Byte) ...")
            proto.cmd_write_stream(
                bytes([0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6]))
            print("[writetest] 5) Leseversuch (wird im Strom als Daten "
                  "geschluckt) ...")
            got = proto.cmd_read_record()
            if got is None:
                print("[writetest]    keine Antwort -- wie erwartet, das "
                      "Geraet ist im Strommodus.")
            else:
                print(f"[writetest]    {got.hex(' ')} (!) das Geraet nimmt "
                      "noch Kommandos an")
            print("[writetest] Jetzt warten -- der Bootloader setzt sich nach "
                  "seinem Timeout selbst zurueck -- oder Strom trennen, "
                  "danach 'writetest --readback'.")
            print("[writetest] Erwartung: 'a0 a1 b1 b2 b3 b4' "
                  "(Byte 0..1 Kommandorahmen, Byte 2..5 Stromrahmen).")
            print("[writetest] Steht dort 'a0 a1 22 f1 …', stammt das vom "
                  "alten Aufbau: dann wurde der Leseversuch '22 f1 5b' als "
                  "Stromdatum geschrieben.")
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
                             wire=args.wire,
                             erase_timeout_ms=int(args.erase_timeout * 1000),
                             erase_chunk=args.erase_chunk,
                             erase_pause_ms=int(args.erase_pause_ms))
        else:
            proto = Protocol(DryTransport(), verbose=not args.quiet,  # type: ignore
                             wire=args.wire,
                             erase_timeout_ms=int(args.erase_timeout * 1000),
                             erase_chunk=args.erase_chunk,
                             erase_pause_ms=int(args.erase_pause_ms))

        if not args.dry_run and not args.yes:
            print(f"ACHTUNG: schreibt 0x{args.region}-Region in den Flash. Fortsetzen? [j/N] ",
                  end="")
            if input().strip().lower() not in ("j", "y", "ja", "yes"):
                return 2
        try:
            proto.upload(img, region=args.region, do_erase=not args.no_erase,
                         burst_blocks=args.resync, retries=args.retries,
                         pause_ms=int(args.pause_ms),
                         settle_ms=int(args.settle_ms),
                         pace_ms=int(args.pace_ms),
                         stream=(not args.legacy_blocks),
                         batch_frames=args.stream_batch,
                         stream_pace_ms=args.stream_pace_ms)
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
