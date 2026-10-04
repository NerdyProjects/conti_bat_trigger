#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_bms.py -- BMS-Kontroll-Patch fuer die STM32F105 "Display"-Firmware
(Continental / CEBS, data/stm32f105_conti.hex).

Ziel
----
Das BMS (0x555 = "Spannung an") soll deterministisch dem Display folgen:

    Display laeuft  (Zustand 2)  ->  0x555 = 1   BMS freigegeben
    Display aus     (Zustand 1/3) ->  0x555 = 0   BMS aus

Ausgangslage (aus dem Disassembly verifiziert)
----------------------------------------------
Der Sender 0x0801673C setzt 0x555 nur dann auf 1, wenn ALLE Bedingungen passen:

  1. `[0x2000087E] == 1`  -- dieses Flag wird ausschliesslich beim Empfang von
     CAN 0x201 gesetzt (Handler 0x080167E4) und beim Start geloescht
     (0x08016678). Ohne 0x201 bleibt 0x555 dauerhaft 0.
  2. Der "Rampenzaehler" [0x2000087B] muss exakt 15, 20, 30 oder 60 treffen
     (Wiederholungsversuche). Vorher bleibt 0x555 = 0.
  3. Die Zustandsmaschine (Dispatcher 0x08016822) muss in Zustand 2 sein.

Zusaetzlich gibt es Ruecksetz-/Abschaltpfade (0x200008FF, 0x20000994), die den
Displayszustand in den Endzustand 3 treiben -- Zustand 3 wird nie wieder
verlassen (erst ein Power-Cycle hilft), und 0x555 geht dabei auf 0.

Die Firmware traegt die CAN-Botschaft 0x555 ab Werk periodisch (ca. alle
100 ms) aus dem 32-Bit-Wort bei 0x2000096C aus; dessen Bit 0 ist der Wert.
Es genuegt also, dieses Bit an den Displayszustand zu koppeln.

Patches
-------
P1 0x0801673E  `f_1673C` (wird in Zustand 2 zyklisch aufgerufen) setzt
               0x555 = 1 unbedingt -- ohne 0x201-Flag, ohne Rampenverzoegerung.
               Der Rest der Funktion (Rampe) bleibt unveraendert erhalten.
P2 0x080167BE  `f_167B6` (Abschaltpfad) setzt 0x555 wieder auf 0.
               (Macht den Patch in "edited_stm32f105_always_on_display.hex"
               rueckgaengig, der genau das verhindert hat.)
P3 0x08001A34  Bootloader: App-CRC-Pruefung wieder aktivieren. Sinnvoll, weil
               dieses Werkzeug die CRC korrekt nachrechnet -- ein
               unvollstaendig geflashter Block landet dann im USB-Bootloader
               statt in einer halben Applikation.
P4 0x080168A8  Zustand 2 kann nicht mehr in den Endzustand 3 (Dauer-Aus)
               laufen. Nur der Display-Aus-Modus (Modus 4) verlaesst
               Zustand 2 noch -- Richtung Zustand 1, aus dem es zurueckgeht.

P5 0x08009492  Abschalt-Timer ausgebaut. Der zyklische Task 0x080093D2 laedt
               den Countdown [0x20000100] auf 3000, solange Aktivitaet
               erkannt wird; sonst zaehlt er herunter und setzt bei 0 das
               Abschaltflag [0x200008FF] = 1 (-> Zustand 3, Dauer-Aus).
               Die Verzweigung wird auf "immer nachladen" umgebogen, der
               Zaehler erreicht die 0 also nie mehr.

P6 0x0801B944  CAN: ABOM = automatische Bus-Off-Erholung. Die Firmware liest
               zwar CAN_ESR und erkennt Bus-Off/Error-Passive/Error-Warning,
               setzt aber nie MCR.ABOM. Ohne ABOM bleibt der bxCAN nach einem
               Bus-Off stehen, bis Software MCR.INRQ toggelt -- das passiert
               nur in einem schmalen Init-Pfad. Folge: der STM sendet nichts
               mehr, arbeitet aber weiter (LEDs, Display). Mit ABOM holt sich
               der Controller nach 128x11 rezessiven Bits selbst zurueck.

Alle Patches sind reine Codepfad-Aenderungen:
  * keine Stack-Aenderung (push/pop-Balance bleibt),
  * keine Literal-Pool-Bereiche werden ueberschrieben,
  * keine neuen Sprungziele in unbekannten Code,
  * P4/P5/P6 sind 1-2-Byte-Aenderungen, P1 ersetzt genau eine
    Vergleichssequenz durch einen Aufruf der bereits vorhandenen Funktion.

Aufruf
------
    python3 tools/patch_bms.py                       # alle Patches, data/*.hex
    python3 tools/patch_bms.py --without P4          # nur P1..P3
    python3 tools/patch_bms.py -i data/stm32f105_conti.hex \
                              -o data/stm32f105_bms_control.bin
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# CRC-Funktion und Image-Loader aus dem bestehenden Flash-Tool wiederverwenden.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stm_display_fw import (  # noqa: E402
    APP_BASE,
    APP_CRC_ADDR,
    APP_CRC_LEN,
    FLASH_BASE,
    Image,
    fix_app_crc,
    load_image,
    verify_app_crc,
)

DEFAULT_IN = Path("data/stm32f105_conti.hex")
DEFAULT_OUT = Path("data/stm32f105_bms_control")


# --------------------------------------------------------------------------
# Patch-Definitionen
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Patch:
    pid: str
    addr: int
    accept: Tuple[bytes, ...]   # zulaessige Ist-Zustaende (Original zuerst)
    new: bytes                  # Zielzustand
    title: str
    detail: str

    @property
    def size(self) -> int:
        return len(self.new)

    @property
    def old(self) -> bytes:
        """Der kanonische Ausgangszustand (fuer die Anzeige)."""
        return self.accept[0]


PATCHES: Tuple[Patch, ...] = (
    Patch(
        "P1",
        0x0801673E,
        (bytes.fromhex("2d 48 00 78 01 28 33 d1"),),
        bytes.fromhex("01 20 fc f7 6e fb 00 bf"),
        "0x555 = 1 immer, wenn Zustand 2 (Display laeuft)",
        "ersetzt `ldr r0,[0x2000087E]; ldrb r0,[r0]; cmp r0,#1; bne 0x80167AE`\n"
        "     durch `movs r0,#1; bl 0x8012E20; nop`\n"
        "     -> beseitigt die 0x201-Abhaengigkeit und die Rampenverzoegerung.",
    ),
    Patch(
        "P2",
        0x080167BE,
        (bytes.fromhex("00"), bytes.fromhex("01")),  # Original / Altpatch
        bytes.fromhex("00"),
        "Abschaltpfad setzt 0x555 wieder auf 0",
        "`f_167B6` (Zustand 2 -> 1/3) schreibt 0x555 = 0.\n"
        "     Der alte Patch hatte hier 0x01 stehen -- damit blieb das BMS\n"
        "     auch bei ausgeschaltetem Display an.",
    ),
    Patch(
        "P3",
        0x08001A34,
        (bytes.fromhex("01"), bytes.fromhex("00")),  # Original / Altpatch
        bytes.fromhex("01"),
        "Bootloader: App-CRC-Pruefung aktiv",
        "`movs r5,#1` im CRC-Vergleich 0x08001A02..0x08001A3A.\n"
        "     Der alte Patch hatte hier 0x00 stehen und damit die Pruefung\n"
        "     abgeschaltet. Die CRC wird von diesem Werkzeug korrekt\n"
        "     nachgerechnet, die Pruefung kann also bleiben.",
    ),
    Patch(
        "P4",
        0x080168A8,
        (bytes.fromhex("03 d0"), bytes.fromhex("0a e0")),
        bytes.fromhex("0a e0"),  # b.n 0x080168C0  (statt beq.n 0x080168B2)
        "Zustand 2 laeuft nicht mehr in den Endzustand 3",
        "`beq.n 0x80168B2` -> `b.n 0x80168C0`: ueberspringt `state = 3`\n"
        "     + 0x555 = 0 + Standby. Zustand 3 ist ein Endzustand, der ohne\n"
        "     Power-Cycle nie verlassen wird; er kostet BMS und Display.",
    ),
    Patch(
        "P5",
        0x08009492,
        (bytes.fromhex("04 d1"), bytes.fromhex("ff e7")),
        bytes.fromhex("ff e7"),  # b.n 0x08009494 (statt bne.n 0x0800949E)
        "Abschalt-Timer (Countdown [0x20000100]) entfernt",
        "`bne.n 0x800949E` -> `b.n 0x8009494`: der Zaehler wird immer wieder\n"
        "     auf 3000 geladen und kann nie 0 werden. Damit entfaellt der\n"
        "     Pfad `[0x200008FF] = 1` (0x080094B6) vollstaendig.",
    ),
    Patch(
        "P6",
        0x0801B944,
        (bytes.fromhex("01"), bytes.fromhex("41")),
        bytes.fromhex("41"),  # orr.w r1,r1,#0x41  statt #1
        "CAN: ABOM einschalten (automatische Bus-Off-Erholung)",
        "`MCR |= 1` (nur INRQ) -> `MCR |= 0x41` (INRQ | ABOM) in f_1B93C.\n"
        "     ABOM = Bit 6 von CAN_MCR. Ohne ABOM bleibt der bxCAN nach\n"
        "     einem Bus-Off dauerhaft stumm -- genau das Symptom\n"
        "     \"keine CAN-Nachrichten mehr, aber STM laeuft weiter\".",
    ),
)


def apply_patches(img: Image, patches: Sequence[Patch]) -> List[str]:
    """Prueft die Ausgangsbytes und traegt die Patches ein. Idempotent:
    bereits gepatchte Stellen werden erkannt und akzeptiert.

    Liefert je Patch 'changed' oder 'already'."""
    result: List[str] = []
    for p in patches:
        off = p.addr - img.base
        if off < 0 or off + len(p.new) > len(img.data):
            raise SystemExit(f"[{p.pid}] ausserhalb des Images: 0x{p.addr:08X}")
        have = bytes(img.data[off:off + len(p.new)])
        if have == p.new:
            result.append("already")        # schon gepatcht -- nichts zu tun
            continue
        if have not in p.accept:
            raise SystemExit(
                f"[{p.pid}] Bytes an 0x{p.addr:08X} unerwartet!\n"
                f"     akzeptiert: {' | '.join(b.hex(' ') for b in p.accept)}\n"
                f"     gefunden  : {have.hex(' ')}\n"
                f"     Falsche Firmware oder fremder Patch."
            )
        img.data[off:off + len(p.new)] = p.new
        result.append("changed")
    return result


# --------------------------------------------------------------------------
# Intel-HEX-Ausgabe
# --------------------------------------------------------------------------
def write_intel_hex(data: bytes, base: int, path: Path) -> None:
    """Schreibt die Daten als Intel-HEX (obere Adresshaelfte via Typ-04).

    Bei jedem Wechsel der oberen 16 Adressbits wird ein neuer Typ-04-Record
    ausgegeben, sonst wuerden die Zeilenadressen umlaufen.
    """
    lines: List[str] = []

    def rec(rtype: int, addr: int, payload: bytes) -> None:
        body = bytes([len(payload), (addr >> 8) & 0xFF, addr & 0xFF, rtype]) + payload
        lines.append(":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper())

    upper: Optional[int] = None
    addr = 0
    while addr < len(data):
        abs_addr = base + addr
        up = (abs_addr >> 16) & 0xFFFF
        if up != upper:
            rec(0x04, 0, bytes([(up >> 8) & 0xFF, up & 0xFF]))
            upper = up
        n = min(32, len(data) - addr, 0x10000 - (abs_addr & 0xFFFF))
        rec(0x00, abs_addr & 0xFFFF, data[addr:addr + n])
        addr += n
    lines.append(":00000001FF")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="BMS-Kontroll-Patch fuer die STM32F105 Display-Firmware")
    ap.add_argument("-i", "--in", dest="src", type=Path, default=DEFAULT_IN,
                    help="Original-Firmware (.hex/.bin), Default %(default)s")
    ap.add_argument("-o", "--out", dest="dst", type=Path, default=DEFAULT_OUT,
                    help="Zielbasis ohne Endung, Default %(default)s")
    ap.add_argument("--without", action="append", default=[], metavar="PID",
                    help="Patch weglassen, z. B. --without P4 (mehrfach moeglich)")
    ap.add_argument("--list", action="store_true", help="nur Patchliste zeigen")
    ap.add_argument("--no-crc", action="store_true",
                    help="App-CRC nicht neu berechnen (nicht empfohlen)")
    args = ap.parse_args(argv)

    if args.list:
        for p in PATCHES:
            print(f"{p.pid}  0x{p.addr:08X}  "
                  f"{p.old.hex(' '):<24} -> {p.new.hex(' '):<24} {p.title}")
        return 0

    skip = {s.upper() for s in args.without}
    unknown = skip - {p.pid for p in PATCHES}
    if unknown:
        raise SystemExit(f"Unbekannte Patch-IDs: {', '.join(sorted(unknown))}")
    active = [p for p in PATCHES if p.pid not in skip]

    if not args.src.exists():
        raise SystemExit(f"Quelle nicht gefunden: {args.src}")
    img = load_image(args.src)
    if img.base != FLASH_BASE:
        raise SystemExit(f"Image muss bei 0x{FLASH_BASE:08X} beginnen "
                         f"(base=0x{img.base:08X})")
    if len(img.data) < APP_CRC_ADDR + 4 - FLASH_BASE:
        raise SystemExit(f"Image zu klein: {len(img.data)} Bytes")

    print(f"[in]  {args.src}  base=0x{img.base:08X} size={len(img.data)}")
    verify_app_crc(img)

    status = apply_patches(img, active)
    print()
    smap = dict(zip([p.pid for p in active], status))
    for p in PATCHES:
        if p.pid in skip:
            note = "uebersprungen"
        elif smap.get(p.pid) == "already":
            note = "war schon gesetzt"
        else:
            note = "angewendet"
        print(f"[{p.pid}] 0x{p.addr:08X}  {p.new.hex(' '):<24} {p.title}  ({note})")
        for line in p.detail.splitlines():
            print(f"    {line}")

    if not args.no_crc:
        print()
        fix_app_crc(img)

    out_bin = args.dst.with_suffix(".bin")
    out_hex = args.dst.with_suffix(".hex")
    out_bin.write_bytes(bytes(img.data))
    write_intel_hex(bytes(img.data), img.base, out_hex)
    print(f"\n[ok] {out_bin}")
    print(f"[ok] {out_hex}")

    print("\nKontrolle:")
    verify_app_crc(img)
    print(f"  Image 0x{img.base:08X}..0x{img.base + len(img.data) - 1:08X} "
          f"({len(img.data)} Bytes)")
    print("\nFlashen:")
    print(f"  python3 tools/stm_display_fw.py upload {out_bin} --region app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
