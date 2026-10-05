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

P5 0x08009492  Abschalt-Timer ausgebaut.
               Die Verzweigung wird auf "immer nachladen" umgebogen, der
               Zaehler erreicht die 0 also nie mehr. (Wird von P7 abgeloest.)

P6 0x0801B944  CAN: ABOM = automatische Bus-Off-Erholung.

P7 0x080093D8  O2 "Selbstversorgung": Trampolin in den freien Block
               0x08038184..0x0803FFF3 (31,6 KiB, geloescht). Der Block setzt
               Latch [0x2000087E] = 1 und die 0x201-Nutzlast [0x20000A0C] =
               0x0100 ("0x201 mit Fahrt-Byte", wie das Motor-Keepalive) und
               holt den ersetzten Zustandsmaschinen-Aufruf nach. Damit sieht
               die Firmware dauerhaft Aktivitaet (f_0F390() = 256/10 = 25 >
               10) -> der Original-Code laedt den 5-Minuten-Timer nach und
               setzt 0x555 = 1. Ersetzt P5 (und macht P1 redundant).

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
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
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
class Segment:
    """Ein zusammenhaengender Patch-Bereich."""
    addr: int
    accept: Tuple[bytes, ...]   # zulaessige Ist-Zustaende (Original zuerst)
    new: bytes                  # Zielzustand


@dataclass(frozen=True)
class Patch:
    pid: str
    addr: int
    accept: Tuple[bytes, ...]   # zulaessige Ist-Zustaende (Original zuerst)
    new: bytes                  # Zielzustand
    title: str
    detail: str
    extra: Tuple[Segment, ...] = ()      # weitere Bereiche (z. B. Code-Cave)
    superseded_by: Optional[str] = None  # wird uebersprungen, wenn X aktiv ist

    @property
    def size(self) -> int:
        return len(self.new)

    @property
    def old(self) -> bytes:
        """Der kanonische Ausgangszustand (fuer die Anzeige)."""
        return self.accept[0]

    @property
    def segments(self) -> Tuple[Segment, ...]:
        return (Segment(self.addr, self.accept, self.new),) + self.extra


# --------------------------------------------------------------------------
# O2: Code-Cave -- "0x201 mit Fahrt" dauerhaft vorbelegen
# --------------------------------------------------------------------------
O2_TRAMPOLINE = 0x080093D8      # `bl 0x8016822` im 100-ms-Task 0x080093D2
O2_CAVE = 0x0803FF00            # freie Flaeche -- siehe Pruefung unten!
O2_STATE = 0x08016822           # ersetzter Aufruf: Zustandsmaschine
O2_LATCH_SET = 0x080167E4       # f_167E4: setzt [0x2000087E] = 1 (0x201-Post-Call)
O2_X201_SLOT = 0x20000A0C       # 0x201-Nutzlast (Signal 14, DLC 4)


def _thumb_bl(addr: int, target: int) -> bytes:
    """Thumb-2-BL (T1) kodieren -- gegen acht Spruenge im Original verifiziert.

    Wird nur fuer den **Vergleichswert** des Trampolins gebraucht (die
    Originalbytes `bl 0x08016822`); erzeugt wird der neue Code vom Assembler.
    Beispiel: `bl 0x08016822` bei 0x080093D8 ergibt `0d f0 23 fa`.
    """
    imm = (target - (addr + 4)) & 0x1FFFFFF
    s = (imm >> 24) & 1
    i1, i2 = (imm >> 23) & 1, (imm >> 22) & 1
    imm10, imm11 = (imm >> 12) & 0x3FF, (imm >> 1) & 0x7FF
    j1, j2 = (i1 ^ s) ^ 1, (i2 ^ s) ^ 1
    return ((0xF000 | (s << 10) | imm10).to_bytes(2, "little")
            + (0xD000 | (j1 << 13) | (j2 << 11) | imm11).to_bytes(2, "little"))


O2_ASM = Path(__file__).resolve().parent / "o2_cave.s"
O2_LD = Path(__file__).resolve().parent / "o2_cave.ld"
AS = "arm-none-eabi-as"
LD = "arm-none-eabi-ld"
OBJCOPY = "arm-none-eabi-objcopy"


@lru_cache(maxsize=1)
def assemble_o2() -> Tuple[bytes, bytes]:
    """Assembliert tools/o2_cave.s und liefert (Cave, Trampolin).

    Quellcode steht in `tools/o2_cave.s`, die Platzierung in `tools/o2_cave.ld`
    (0x08038184 = Cave, 0x080093D8 = Trampolin). Die Adressen der Aufrufziele
    kommen als ``--defsym`` von hier -- eine Quelle der Wahrheit fuer Adressen.

    Ergebnis wird geprueft: Laengen 28/4 Byte, Literal == O2_X201_SLOT und der
    Trampolin-BL gegen den verifizierten Python-Encoder.
    """
    for tool in (AS, LD, OBJCOPY):
        if shutil.which(tool) is None:
            raise SystemExit(
                f"{tool} nicht gefunden. O2/P7 braucht die ARM-Toolchain:\n"
                f"     sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi"
            )
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        obj, elf = td / "o2_cave.o", td / "o2_cave.elf"
        cave_bin, tramp_bin = td / "cave.bin", td / "tramp.bin"
        subprocess.run(
            [AS, "-mthumb", "-o", str(obj), str(O2_ASM),
             f"--defsym=F_167E4={O2_LATCH_SET:#x}",
             f"--defsym=F_16822={O2_STATE:#x}",
             f"--defsym=O2_X201_SLOT={O2_X201_SLOT:#x}"],
            check=True, capture_output=True, text=True)
        subprocess.run([LD, "-T", str(O2_LD), "-o", str(elf), str(obj)],
                       check=True, capture_output=True, text=True)
        for sec, out in ((".cave", cave_bin), (".tramp", tramp_bin)):
            subprocess.run([OBJCOPY, "-O", "binary", "-j", sec, str(elf),
                            str(out)], check=True, capture_output=True,
                           text=True)
        cave, tramp = cave_bin.read_bytes(), tramp_bin.read_bytes()

    # Platzierung aus tools/o2_cave.ld gegen die Konstanten hier abgleichen --
    # so gibt es genau eine Adresse je Abschnitt, und ein Vergessen faellt auf.
    text = O2_LD.read_text(encoding="utf-8")
    for name, want in ((".tramp", O2_TRAMPOLINE), (".cave", O2_CAVE)):
        m = re.search(rf"\{name}\s+(0x[0-9A-Fa-f]+)", text)
        if not m:
            raise SystemExit(f"{O2_LD}: Abschnitt {name} nicht gefunden")
        if int(m.group(1), 16) != want:
            raise SystemExit(
                f"{O2_LD}: {name} = {m.group(1)} passt nicht zu "
                f"{want:#010x} in patch_bms.py")

    want_tramp = _thumb_bl(O2_TRAMPOLINE, O2_CAVE)
    if not cave or not tramp:
        raise SystemExit("O2: Assembler lieferte leere Abschnitte")
    if tramp != want_tramp:
        raise SystemExit(f"O2-Trampolin: Assembler {tramp.hex(' ')} != erwartet "
                         f"{want_tramp.hex(' ')}")
    lit_at = cave.rfind(O2_X201_SLOT.to_bytes(4, "little"))
    if lit_at < 0:
        raise SystemExit("O2-Cave: Literal 0x20000A0C fehlt")
    if len(cave) % 4 or len(tramp) != 4:
        raise SystemExit(f"O2: unerwartete Laengen {len(cave)}/{len(tramp)}")
    return cave, tramp


O2_CAVE_BYTES, O2_TRAMP_BYTES = assemble_o2()


def check_cave_free(img: Image) -> None:
    """Prueft, dass die Cave in wirklich freiem Flash liegt.

    Lehre aus dem ersten Versuch: 0xFF heisst **nicht** automatisch "unbenutzt".
    Der Block ab 0x08038184 ist zwar geloescht, wird aber von einem Deskriptor
    im Datenbereich referenziert (0x08037F54 -> 0x08038184) -- die Stelle wird
    also moeglicherweise als Wert gelesen. Deshalb:

      1. der Cave-Bereich selbst muss im Eingangsimage 0xFF sein,
      2. **kein** 4-Byte-Wort im Image darf in die 2-KiB-Seite der Cave zeigen
         (Pointer, Deskriptoren, Literal-Pools -- alles 4-Byte-aligned).
    """
    off = O2_CAVE - img.base
    n = len(O2_CAVE_BYTES)
    if off < 0 or off + n > len(img.data):
        raise SystemExit(f"O2: Cave 0x{O2_CAVE:08X} liegt ausserhalb des Images")
    have = bytes(img.data[off:off + n])
    if have != b"\xff" * n:
        raise SystemExit(f"O2: Cave-Bereich 0x{O2_CAVE:08X} ist nicht leer:\n"
                         f"     {have.hex(' ')}")
    page = O2_CAVE & ~0x7FF
    hits = []
    for i in range(0, len(img.data) - 4, 4):
        w = int.from_bytes(img.data[i:i + 4], "little")
        if not page <= w < page + 0x800:
            continue
        if APP_CRC_ADDR <= w < APP_CRC_ADDR + 4:
            continue            # legitimer Zeiger auf das App-CRC-Wort
        hits.append((img.base + i, w))
    if hits:
        raise SystemExit(
            f"O2: {len(hits)} Zeiger zeigen in die Cave-Seite 0x{page:08X}:\n"
            + "\n".join(f"     0x{a:08X} -> 0x{w:08X}" for a, w in hits[:8])
            + "\n     Andere Adresse in tools/o2_cave.ld waehlen!")
    print(f"[ccheck ok ] O2-Cave 0x{O2_CAVE:08X} frei (Seite 0x{page:08X} "
          f"unreferenziert)")


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
        "     Pfad `[0x200008FF] = 1` (0x080094B6) vollstaendig.\n"
        "     P7 stellt stattdessen die Aktivitaet selbst her -- dann meldet die\n"
        "     echte Formel f_0F390() = 25 und der Zaehler laedt von allein nach.",
        superseded_by="P7",
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
    Patch(
        "P7",
        O2_TRAMPOLINE,
        (_thumb_bl(O2_TRAMPOLINE, O2_STATE),),      # Original: bl 0x8016822
        O2_TRAMP_BYTES,                             # -> in den freien Block
        "O2: Firmware glaubt dauerhaft an '0x201 mit Fahrt'",
        "Ersetzt im 100-ms-Task den Aufruf `bl 0x8016822` (Zustandsmaschine)\n"
        "     durch einen Sprung in den freien Block 0x08038184. Der Block setzt\n"
        "     das Latch [0x2000087E] = 1, schreibt die 0x201-Nutzlast\n"
        "     [0x20000A0C] = 0x0100 (= Wire {00 01}, wie das Motor-Keepalive) und\n"
        "     holt den ersetzten Zustandsmaschinen-Aufruf nach.\n"
        "     Wirkung: f_0F390() = 0x0100/10 = 25 > 10 -> Aktivitaet erkannt,\n"
        "     der 5-Minuten-Timer laedt nach (P5 wird unnoetig und automatisch\n"
        "     uebersprungen); der Original-Rampencode setzt 0x555 = 1, weil das\n"
        "     Latch gesetzt ist (P1 damit redundant, bleibt aber als Absicherung).\n"
        "     Quellcode: tools/o2_cave.s (Bauen: tools/patch_bms.py --print-o2).",
        extra=(Segment(O2_CAVE, (b"\xff" * len(O2_CAVE_BYTES),), O2_CAVE_BYTES),),
    ),
)


def apply_patches(img: Image, patches: Sequence[Patch]) -> List[str]:
    """Prueft die Ausgangsbytes und traegt die Patches ein. Idempotent:
    bereits gepatchte Stellen werden erkannt und akzeptiert.

    Liefert je Patch 'changed' oder 'already'."""
    result: List[str] = []
    for p in patches:
        status = "already"
        for seg in p.segments:
            off = seg.addr - img.base
            if off < 0 or off + len(seg.new) > len(img.data):
                raise SystemExit(f"[{p.pid}] ausserhalb des Images: "
                                 f"0x{seg.addr:08X}")
            have = bytes(img.data[off:off + len(seg.new)])
            if have == seg.new:
                continue                    # schon gepatcht -- nichts zu tun
            if have not in seg.accept:
                raise SystemExit(
                    f"[{p.pid}] Bytes an 0x{seg.addr:08X} unerwartet!\n"
                    f"     akzeptiert: {' | '.join(b.hex(' ') for b in seg.accept)}\n"
                    f"     gefunden  : {have.hex(' ')}\n"
                    f"     Falsche Firmware oder fremder Patch."
                )
            img.data[off:off + len(seg.new)] = seg.new
            status = "changed"
        result.append(status)
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
    ap.add_argument("--print-o2", action="store_true",
                    help="O2-Code (tools/o2_cave.s) assemblieren und anzeigen")
    ap.add_argument("--no-crc", action="store_true",
                    help="App-CRC nicht neu berechnen (nicht empfohlen)")
    args = ap.parse_args(argv)

    if args.list:
        for p in PATCHES:
            print(f"{p.pid}  0x{p.addr:08X}  "
                  f"{p.old.hex(' '):<24} -> {p.new.hex(' '):<24} {p.title}")
            for seg in p.extra:
                print(f"      + 0x{seg.addr:08X}  {len(seg.new)} Byte  {seg.new.hex(' ')}")
        return 0

    if args.print_o2:
        cave, tramp = O2_CAVE_BYTES, O2_TRAMP_BYTES
        print(f"Quelle   : {O2_ASM}")
        print(f"Platzung : {O2_LD}")
        print(f"Aufrufe  : f_167E4={O2_LATCH_SET:#010x} (Latch), "
              f"f_16822={O2_STATE:#010x} (Zustandsmaschine), "
              f"Slot={O2_X201_SLOT:#010x}")
        print(f"\nTrampolin 0x{O2_TRAMPOLINE:08X} ({len(tramp)} Byte):\n"
              f"   {tramp.hex(' ')}   (ersetzt {_thumb_bl(O2_TRAMPOLINE, O2_STATE).hex(' ')})")
        print(f"\nCave      0x{O2_CAVE:08X} ({len(cave)} Byte):\n"
              f"   {cave.hex(' ')}")
        print("\nDisassembly:")
        with tempfile.TemporaryDirectory() as td:
            obj, elf = Path(td) / "o2.o", Path(td) / "o2.elf"
            subprocess.run([AS, "-mthumb", "-o", str(obj), str(O2_ASM),
                            f"--defsym=F_167E4={O2_LATCH_SET:#x}",
                            f"--defsym=F_16822={O2_STATE:#x}",
                            f"--defsym=O2_X201_SLOT={O2_X201_SLOT:#x}"], check=True)
            subprocess.run([LD, "-T", str(O2_LD), "-o", str(elf), str(obj)],
                           check=True)
            subprocess.run(["arm-none-eabi-objdump", "-d", str(elf)], check=True)
        return 0

    skip = {s.upper() for s in args.without}
    unknown = skip - {p.pid for p in PATCHES}
    if unknown:
        raise SystemExit(f"Unbekannte Patch-IDs: {', '.join(sorted(unknown))}")
    active = [p for p in PATCHES if p.pid not in skip]
    # Abgeloeste Patches (z. B. P5 durch P7) automatisch auslassen.
    active_ids = {p.pid for p in active}
    dropped = [p for p in active
               if p.superseded_by and p.superseded_by in active_ids]
    active = [p for p in active if p not in dropped]

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

    if any(p.pid == "P7" for p in active):
        check_cave_free(img)

    status = apply_patches(img, active)
    print()
    smap = dict(zip([p.pid for p in active], status))
    for p in PATCHES:
        if p.pid in skip:
            note = "uebersprungen"
        elif p in dropped:
            note = f"durch {p.superseded_by} abgeloest"
        elif smap.get(p.pid) == "already":
            note = "war schon gesetzt"
        else:
            note = "angewendet"
        print(f"[{p.pid}] 0x{p.addr:08X}  {p.new.hex(' '):<24} {p.title}  ({note})")
        for seg in p.extra:
            print(f"      + 0x{seg.addr:08X}  {len(seg.new)} Byte  (Code-Cave)")
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
    print("\nFlashen (USB anstecken -- die App darf laufen):")
    print(f"  python3 tools/stm_display_fw.py flash {out_bin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
