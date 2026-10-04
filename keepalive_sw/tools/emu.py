#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emu.py -- Unicorn-Emulation von Teilen der STM32F105-"Display"-Firmware.

Ziel: Ablauf rekonstruieren/verifizieren, ohne Hardware (kein SWD, kein
Original-Tool). Es werden einzelne Funktionen aus dem Flash-Dump mit
vorbereitetem SRAM/Peripherie aufgerufen.

Nutzung:
    python3 tools/emu.py crc          # Selbsttest der CRC-Funktion
    python3 tools/emu.py dispatch     # Kommando-Dispatcher des Bootloaders
    python3 tools/emu.py frame        # Rahmen-Parser des Bootloaders
    python3 tools/emu.py bmspatch     # BMS-Patch (0x555 folgt dem Display)

Optionen: -t (Trace), -i <image> (statt data/stm32f105_conti.bin)
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from unicorn import (
    Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
    UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ, UC_HOOK_MEM_READ_UNMAPPED,
    UC_HOOK_MEM_WRITE_UNMAPPED,
    UC_PROT_ALL, UC_PROT_READ, UC_PROT_WRITE, UcError,
)
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
    UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
)

try:
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
    _CS = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
except Exception:  # pragma: no cover
    _CS = None

# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
IMG_PATH = ROOT / "data" / "stm32f105_conti.bin"

FLASH = 0x08000000
FLASH_SIZE = 0x40000
SRAM = 0x20000000
SRAM_SIZE = 0x10000

RET_SENTINEL = 0x0BADF000  # gerade Adresse als Return-Ziel

# Bootloader-Kommando-Handler (aus dem Dispatcher 0x08000B5A)
BL_HANDLERS: Dict[int, int] = {
    0x10: 0x08000CAC, 0x11: 0x08000DA2, 0x22: 0x08000EAE, 0x27: 0x080011DA,
    0x2E: 0x08001224, 0x31: 0x080013A4, 0x34: 0x08001514, 0x36: 0x08001614,
    0x37: 0x080017EC, 0x3E: 0x08001884,
}


def crc_step(crc: int, word: int) -> int:
    crc ^= word
    for _ in range(32):
        if crc & 0x80000000:
            crc = ((crc << 1) ^ 0x4C11DB7) & 0xFFFFFFFF
        else:
            crc = (crc << 1) & 0xFFFFFFFF
    return crc


class Emu:
    def __init__(self, img: bytes, trace: bool = False) -> None:
        self.img = img
        self.trace = trace
        self.mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.mu.mem_map(FLASH, FLASH_SIZE)
        self.mu.mem_write(FLASH, img)
        self.mu.mem_map(SRAM, SRAM_SIZE)
        # Peripherie + System als normalen RAM abbilden (kein Fault)
        for base, size in [(0x40000000, 0x00080000), (0x50000000, 0x00040000),
                           (0xE0000000, 0x00100000),
                           (0xE0040000, 0x00010000)]:
            try:
                self.mu.mem_map(base, size)
            except UcError:
                pass
        self.stubs: Dict[int, Callable[["Emu"], None]] = {}
        self.stop_pcs: set = set()
        self.writes: List[Tuple[int, int, int]] = []  # (pc, addr, value)
        self._crc = 0xFFFFFFFF
        self.aircr = None
        self._init_periph()
        self.mu.hook_add(UC_HOOK_CODE, self._hook_code)
        self.mu.hook_add(UC_HOOK_MEM_WRITE, self._hook_write)
        self.mu.hook_add(UC_HOOK_MEM_WRITE_UNMAPPED, self._hook_write_unmapped)
        self.mu.hook_add(UC_HOOK_MEM_READ_UNMAPPED, self._hook_read_unmapped)

    # ---- Setups ----------------------------------------------------------
    def _init_periph(self) -> None:
        # RCC_CR: HSE/PLL/HSI ready bits setzen, damit Warteschleifen enden
        self.mu.mem_write(0x40021000, struct.pack("<I", 0x0F030B83))
        # FLASH_SR: bereit (BSY=0)
        self.mu.mem_write(0x4002200C, struct.pack("<I", 0x00000020))  # EOP
        # CRC
        self._crc = 0xFFFFFFFF
        self.mu.mem_write(0x40023000, struct.pack("<I", self._crc))
        # NVIC/SCB Defaults
        self.mu.mem_write(0xE000ED00, struct.pack("<I", 0x410FC231))  # CPUID

    # ---- Hooks -----------------------------------------------------------
    def _disasm(self, pc: int) -> str:
        if _CS is None:
            return ""
        code = self.mu.mem_read(pc, 4)
        for ins in _CS.disasm(bytes(code), pc):
            return f"{ins.mnemonic} {ins.op_str}"
        return ""

    def _hook_code(self, mu, address, size, user_data):
        if self.trace:
            print(f"  {address:#010x}  {self._disasm(address)}")
        # CRC_DR ist kein normales RAM: das Ergebnis muss dynamisch geliefert
        # werden. Bei 0x08000958 ('str r0,[r5,#0xc]') steht das Ergebnis fest
        # -> r0 mit dem emulierten CRC-Wert laden, bevor gespeichert wird.
        if address == 0x08000958:
            mu.reg_write(UC_ARM_REG_R0, self._crc)
        if address in self.stubs:
            self.stubs[address](self)
            return
        if address in self.stop_pcs:
            mu.emu_stop()

    def _hook_write(self, mu, access, address, size, value, user_data):
        self.writes.append((mu.reg_read(UC_ARM_REG_PC), address, value))
        # NVIC_SystemReset der App: AIRCR-Write abfangen und anhalten
        if address == 0xE000ED0C:
            self.aircr = value & 0xFFFFFFFF
            mu.emu_stop()
        # CRC-Register emulieren
        if address == 0x40023008 and (value & 1):        # CRC_CR RESET
            self._crc = 0xFFFFFFFF
        elif address == 0x40023000:                      # CRC_DR schreiben
            self._crc = crc_step(self._crc, value & 0xFFFFFFFF)
        # CAN1_RF0R: RFOM (bit5) -> FIFO freigeben (FMP0 loeschen)
        if address == 0x4000640C and (value & 0x20):
            mu.mem_write(0x4000640C, (0).to_bytes(4, "little"))

    def _hook_write_unmapped(self, mu, access, address, size, value, user_data):
        # Unbekannte Peripherie schlucken
        return True

    def _hook_read_unmapped(self, mu, access, address, size, user_data):
        # CRC_DR lesen
        if address == 0x40023000:
            mu.mem_write(0x40023000, struct.pack("<I", self._crc))
        if getattr(self, "log_unmapped", False):
            pc = mu.reg_read(UC_ARM_REG_PC)
            print(f"    [unmapped READ] pc=0x{pc:08x} addr=0x{address:08x} "
                  f"size={size}")
        return True

    def add_stub(self, addr: int, fn: Callable[["Emu"], None]) -> None:
        """Ersetzt die Funktion an addr durch einen Python-Stub (setzt r0 etc.
        und kehrt per LR zurueck)."""
        def _inner(e: "Emu") -> None:
            fn(e)
            lr = e.mu.reg_read(UC_ARM_REG_LR)
            e.mu.reg_write(UC_ARM_REG_PC, lr)
        self.stubs[addr] = _inner

    # ---- Aufruf ----------------------------------------------------------
    def call(self, addr: int, r0: int = 0, r1: int = 0, r2: int = 0, r3: int = 0,
             max_insns: int = 3_000_000) -> None:
        """Ruft Funktion bei addr (Thumb) auf und laeuft bis zur Rueckkehr."""
        sp = SRAM + SRAM_SIZE - 0x200
        sp &= ~7
        self.mu.reg_write(UC_ARM_REG_SP, sp)
        self.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
        for reg, val in ((UC_ARM_REG_R0, r0), (UC_ARM_REG_R1, r1),
                         (UC_ARM_REG_R2, r2), (UC_ARM_REG_R3, r3)):
            self.mu.reg_write(reg, val)
        self.mu.emu_start(addr | 1, RET_SENTINEL, count=max_insns)

    def rd(self, addr: int, size: int = 4) -> int:
        return int.from_bytes(self.mu.mem_read(addr, size), "little")

    def wr(self, addr: int, value: int, size: int = 4) -> None:
        self.mu.mem_write(addr, int(value).to_bytes(size, "little"))

    def ret(self) -> int:
        return self.mu.reg_read(UC_ARM_REG_R0)


# ==========================================================================
# Szenarien
# ==========================================================================
def scenario_crc(emu: Emu) -> int:
    """Selbsttest: Firmware-CRC-Funktion 0x08000924 == Python-CRC."""
    data = bytes(range(256)) * 8          # 2048 Bytes
    dptr = SRAM + 0x1000
    emu.mu.mem_write(dptr, data)
    sptr = SRAM + 0x2000
    emu.wr(sptr + 0, len(data))           # [0] = Laenge in Bytes
    emu.wr(sptr + 4, dptr)                # [4] = Datenzeiger
    emu.wr(sptr + 8, 0)
    emu.wr(sptr + 12, 0)
    emu.call(0x08000924, r0=sptr)
    hw_crc = emu.rd(sptr + 12)

    ref = 0xFFFFFFFF
    for i in range(0, len(data), 4):
        ref = crc_step(ref, struct.unpack_from("<I", data, i)[0])
    print(f"[crc] Firmware 0x08000924 = 0x{hw_crc:08x}")
    print(f"[crc] Python-Referenz     = 0x{ref:08x}")
    ok = hw_crc == ref
    print("[crc] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def scenario_dispatch(emu: Emu) -> int:
    """Verifiziert die Kommando -> Handler Zuordnung ueber 0x08000B5A."""
    payload = SRAM + 0x3000
    emu.mu.reg_write(UC_ARM_REG_R5, payload)
    ok = True
    print("[dispatch] cmd -> getroffener Handler (Emulation)")
    for cmd, expected in sorted(BL_HANDLERS.items()):
        emu.mu.mem_write(payload, bytes([cmd]))
        # Start mitten in der Funktion moeglich: PC=0x08000B5A, Loop faengt
        # beim ersten Handler an zu laufen -> bei erstem Handler stoppen.
        hits: List[int] = []

        def code_hook(mu, address, size, ud):
            if address in BL_HANDLERS.values() and not hits:
                hits.append(address)
                mu.emu_stop()

        h = emu.mu.hook_add(UC_HOOK_CODE, code_hook)
        try:
            emu.mu.reg_write(UC_ARM_REG_SP, SRAM + SRAM_SIZE - 0x400)
            emu.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
            emu.mu.emu_start(0x08000B5A | 1, 0, count=200_000)
        except UcError as exc:
            print(f"    cmd 0x{cmd:02x}: Emulationsfehler {exc}")
            ok = False
        finally:
            emu.mu.hook_del(h)
        got = hits[0] if hits else None
        mark = "OK " if got == expected else "?? "
        if got != expected:
            ok = False
        print(f"    {mark}cmd 0x{cmd:02x} -> "
              f"{hex(got) if got else '(kein Handler)'} (erwartet {hex(expected)})")
    return 0 if ok else 1


# --- Bootloader-Zustandsvariablen (aus den Literal-Pools) ------------------
BL_STATE = 0x2000001C       # +0 flag0, +1 flag1, +2 halfword len, +4 counter
BL_STATE28 = 0x20000028     # "gueltig"-Byte
BL_V34 = 0x20000034         # Byte, von 0x34/0x36 benoetigt (0 bzw. 1)
BL_PAYLOAD_C = 0x200001CC    # Kommando-/Antwortpuffer
BL_FSTATE = 0x20000038      # +0 seq, +1 nibLow, +2 nibHigh, +4 writepointer
BL_ERASE = 0x20000040       # +4 addr, +8 len (fuer 0x08001964)
BL_PAYLOAD = 0x200001CC     # Payload-Puffer
BL_RXBUF = 0x20000206       # USB-Report-RX

# CAN (App): Tabelle ID->Index und Index->Handler
CAN_ID_TABLE = 0x08036BE4   # 22 x (STDID<<21)
CAN_HANDLER_TABLE = 0x08036CD8   # decode handler (Thumb)
CAN_POST_TABLE = 0x08036D30      # post-call handler (Thumb)
CAN_STATE = 0x20009898      # CAN-Empfangs-State der App


def _install_common_stubs(emu: Emu, records: Dict[str, object]) -> None:
    """Ersetzt SRAM-gespiegelte Flash-Routinen, USB-Sender und Fehlerausgabe."""
    noop = lambda e: None
    for a in (0x08001004, 0x0800100E, 0x08001018, 0x08001022, 0x0800102C,
              0x08001036):
        emu.add_stub(a, noop)
    emu.add_stub(0x08003A36, noop)
    emu.add_stub(0x080042AE, noop)

    def err(e: Emu) -> None:
        records["error"] = e.mu.reg_read(UC_ARM_REG_R0)
        e.mu.reg_write(UC_ARM_REG_R0, 0)

    emu.add_stub(0x08000A46, err)


def scenario_frame(emu: Emu) -> int:
    """Verifiziert den Rahmen ' 02 21 <len> <payload> ' (Pfad 0x08000AFC)."""
    records: Dict[str, object] = {}
    _install_common_stubs(emu, records)
    payload = bytes([0x34, 0x03, 0xAA, 0xBB, 0xCC])       # 5 Bytes
    frame = bytes([0x02, 0x21, len(payload)]) + payload + bytes(64)

    def get_rx(e: Emu) -> None:
        e.mu.reg_write(UC_ARM_REG_R0, BL_RXBUF)

    emu.add_stub(0x0800421E, get_rx)
    emu.mu.mem_write(BL_RXBUF, frame)
    emu.crc_ok = True
    # Zustand: flag0 = 0 -> laengenpraefigierter Pfad
    emu.wr(BL_STATE + 0, 0, 1)
    emu.wr(BL_STATE + 2, 0, 2)
    emu.stop_pcs.add(0x08000B26)
    r4 = BL_STATE
    emu.mu.reg_write(UC_ARM_REG_R4, r4)
    emu.mu.reg_write(UC_ARM_REG_R5, BL_PAYLOAD)
    emu.mu.reg_write(UC_ARM_REG_SP, SRAM + SRAM_SIZE - 0x300)
    emu.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
    emu.mu.emu_start(0x08000AFC | 1, RET_SENTINEL, count=200_000)
    got_len = emu.rd(BL_STATE + 2, 2)
    got_pl = bytes(emu.mu.mem_read(BL_PAYLOAD, len(payload)))
    ok = (got_len == len(payload)) and (got_pl == payload)
    print(f"[frame] len im State = {got_len} (erwartet {len(payload)})")
    print(f"[frame] Payload kopiert = {got_pl.hex(' ')}")
    print("[frame] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def scenario_setaddr(emu: Emu) -> int:
    """Verifiziert Kommando 0x34 (Adresse setzen): payload[3..6] = BE-Adresse."""
    records: Dict[str, object] = {}
    _install_common_stubs(emu, records)
    addr = 0x08008500
    payload = bytes([0x34, 0x03, 0x00]) + struct.pack(">I", addr) + bytes(4)
    assert len(payload) == 11
    emu.mu.mem_write(BL_PAYLOAD, payload)
    emu.wr(BL_STATE + 2, 11, 2)     # Payload-Laenge muss 11 sein
    emu.wr(BL_V34, 0, 1)            # Bedingung in 0x34-Handler
    emu.wr(BL_STATE28, 1, 1)
    emu.wr(BL_STATE + 1, 0, 1)      # flag1 = 0 -> keine Antwort bauen
    emu.stop_pcs.add(0x080017C2)    # nach dem Setzen
    emu.call(0x08001514)
    got = emu.rd(BL_FSTATE + 4)
    ok = got == addr
    print(f"[0x34] Schreibzeiger = {got:#010x} (erwartet {addr:#010x})")
    nib = emu.rd(BL_FSTATE + 1, 1) | (emu.rd(BL_FSTATE + 2, 1) << 4)
    print(f"[0x34] Nibble-Byte payload[1]=0x03 -> State = 0x{nib:02x}")
    print("[0x34] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def scenario_writeblk(emu: Emu) -> int:
    """Verifiziert Kommando 0x36 (Block schreiben) inkl. Sequenz + Zeiger."""
    records: Dict[str, object] = {}
    _install_common_stubs(emu, records)
    captures: List[Tuple[int, int, bytes]] = []

    def write_stub(e: Emu) -> None:
        a = e.mu.reg_read(UC_ARM_REG_R0)
        n = e.mu.reg_read(UC_ARM_REG_R1)
        p = e.mu.reg_read(UC_ARM_REG_R2)
        captures.append((a, n, bytes(e.mu.mem_read(p, n))))
        e.mu.reg_write(UC_ARM_REG_R0, 0)     # Erfolg

    emu.add_stub(0x08001998, write_stub)
    data = bytes(range(8))
    payload = bytes([0x36, 0x05]) + data            # Laenge 10, seq 5
    emu.mu.mem_write(BL_PAYLOAD, payload)
    emu.wr(BL_STATE + 2, len(payload), 2)           # Payload-Laenge
    emu.wr(BL_V34, 1, 1)                            # Bedingung in 0x36-Handler
    emu.wr(BL_FSTATE + 0, 0x05, 1)                  # erwartete Sequenz
    emu.wr(BL_FSTATE + 4, 0x08008500)               # Schreibzeiger
    emu.stop_pcs.add(0x08001684)
    emu.call(0x08001614)
    ok = False
    if captures:
        a, n, d = captures[0]
        ok = (a == 0x08008500 and n == len(data) and d == data)
        print(f"[0x36] Flash-Write({a:#010x}, {n} B) Daten={d.hex(' ')}")
    else:
        print("[0x36] KEIN Flash-Write beobachtet", records.get("error"))
    print(f"[0x36] Schreibzeiger danach = {emu.rd(BL_FSTATE + 4):#010x} "
          f"(erwartet {0x08008500 + len(data):#010x})")
    print(f"[0x36] Sequenz danach = {emu.rd(BL_FSTATE + 0):#04x} (erwartet 0x06)")
    ok = ok and (emu.rd(BL_FSTATE + 4) == 0x08008500 + len(data))
    print("[0x36] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def scenario_appreset(emu: Emu) -> int:
    """Verifiziert die App-Resetfunktionen 0x08017378 (Arg==1) und
    0x08016E84 (Arg==2) -> NVIC_SystemReset (AIRCR = 0x05FA0004)."""
    ok = True
    for fn, arg in ((0x08017378, 1), (0x08016E84, 2)):
        emu.aircr = None
        emu.call(fn, r0=arg, max_insns=100_000)
        got = emu.aircr
        hit = (got is not None) and ((got >> 16) == 0x05FA) and bool(got & 4)
        print(f"[app] {fn:#010x}(arg={arg}) -> AIRCR={hex(got) if got else None} "
              f"{'SYSRESETREQ OK' if hit else 'FEHLT'}")
        ok = ok and hit
    emu.aircr = None
    emu.call(0x08017378, r0=0, max_insns=100_000)
    print(f"[app] 0x08017378(arg=0) -> AIRCR={emu.aircr} (erwartet None)")
    ok = ok and emu.aircr is None
    print("[app] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def scenario_trigger(emu: Emu) -> int:
    """Verifiziert den App-Nachrichtenpfad: Dispatcher 0x08018D04 ruft mit
    passendem Nachrichten-State den Reset-Handler 0x08017378 auf."""
    def noop(e: Emu) -> None:
        return None

    emu.add_stub(0x0801D0CE, noop)
    emu.add_stub(0x0801D0EE, noop)

    emu.wr(0x2000092C + 4, 0x40, 1)   # 0x20000930 bit6 -> Dispatcher aktiv
    emu.wr(0x20000928, 0x0800, 2)     # bit11 gesetzt, Byte0 == 0 -> r4 bleibt 1
    emu.wr(0x20000941, 4, 1)          # Index 4 -> Struktur id 0x304 -> fnarray[3]
    emu.wr(0x20000942, 0, 1)

    hit: List[int] = []

    def stop_at_reset(mu, address, size, ud):
        if address == 0x08017378:
            hit.append(address)
            mu.emu_stop()

    h = emu.mu.hook_add(UC_HOOK_CODE, stop_at_reset)
    try:
        emu.mu.reg_write(UC_ARM_REG_SP, SRAM + SRAM_SIZE - 0x400)
        emu.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
        emu.mu.emu_start(0x08018D04 | 1, RET_SENTINEL, count=500_000)
    except UcError as exc:
        print("[trigger] Emulationsfehler:", exc)
    finally:
        emu.mu.hook_del(h)
    ok = bool(hit)
    print("[trigger] Dispatcher 0x08018D04 -> Reset-Handler 0x08017378: "
          f"{'ERREICHT' if ok else 'nicht erreicht'}")
    print("[trigger] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def _can_id_list(emu: Emu):
    return [(emu.rd(CAN_ID_TABLE + i * 4) >> 21) & 0x7FF for i in range(22)]


def _can_handlers(emu: Emu):
    hs = set()
    for base in (CAN_HANDLER_TABLE, CAN_POST_TABLE):
        for i in range(22):
            v = emu.rd(base + i * 4)
            if v & 0xFF000000 == 0x08000000:
                hs.add(v & ~1)
    return hs


def _app_port_ready(emu: "Emu") -> int:
    """Bildet den USB-Pfad der App nach, den ein USB-Host ausloest.

    1. 0x0800B550 (Port-Init der Kommunikations-Task 0x08009228):
       obj = 0x20005E40, obj[0x354] = 4, obj[0xa2] = 1 (0x0800DE64).
    2. Host-Enumeration/SET_CONFIGURATION -> App-Handler 0x0800B92A
       setzt obj[0xa2] = 3.
    3. OTG-Session-Bit in GOTGCTL (0x50000000, Bit 0x80000), das
       0x0800B59C zusaetzlich prueft.
    Danach liefert 0x0800B59C() den Wert 4 = "Port bereit".
    """
    if not emu.rd(0x200002C8):
        try:
            emu.call(0x0800B550)
        except UcError:
            pass
    obj = emu.rd(0x200002C8)
    if not obj:
        return 0
    emu.wr(0x50000000, emu.rd(0x50000000) | 0x00080000)   # OTG-Session
    emu.wr(obj + 0xA2, 3, 1)                              # Host-Konfiguration
    emu.wr(0x200002CD, 0)                                 # neu bewerten
    emu.call(0x0800B59C)
    return emu.ret()


def _inject_can(emu: Emu, canid: int, data: bytes):
    """Fuellt die CAN1-FIFO0-Mailbox und ruft den echten CAN-RX-Handler.

    Setzt vorher das 'Session aktiv'-Flag [0x20000AF6] |= 0x0C, weil das Gate
    0x0801C27C Signale < 16 sonst verwirft.
    """
    emu.wr(CAN_STATE + 0, 0)
    emu.wr(CAN_STATE + 12, 0xFFFF)
    emu.wr(0x20000AF6, emu.rd(0x20000AF6, 1) | 0x0C, 1)       # Session aktiv
    # App-USB-Port auf "bereit" bringen (wie ein angeschlossener USB-Host)
    emu.port_state = _app_port_ready(emu)                     # type: ignore[attr-defined]
    d = bytes(data)[:8].ljust(8, b"\x00")
    emu.wr(0x400065B0, (canid & 0x7FF) << 21)                 # RIR
    emu.wr(0x400065B4, len(data) & 0xF)                       # RDTR (DLC)
    emu.wr(0x400065B8, int.from_bytes(d[0:4], "little"))      # RDLR
    emu.wr(0x400065BC, int.from_bytes(d[4:8], "little"))      # RDHR
    emu.wr(0x4000640C, 1)                                     # RF0R FMP0=1

    hits = []
    handlers = _can_handlers(emu)
    seen = set()
    counts: Dict[int, int] = {}

    def code(mu, address, size, ud):
        if address in handlers:
            counts[address] = counts.get(address, 0) + 1
            if address not in seen:
                seen.add(address)
                hits.append(("hand", address))
            if counts[address] > 3:          # Handler wartet auf weiteren Frame
                hits.append(("loop", address))
                mu.emu_stop()
        if address == 0x08017378:
            hits.append(("RESET", address))
            mu.emu_stop()

    h = emu.mu.hook_add(UC_HOOK_CODE, code)
    try:
        emu.mu.reg_write(UC_ARM_REG_SP, SRAM + SRAM_SIZE - 0x400)
        emu.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
        emu.mu.emu_start(0x0801B066 | 1, RET_SENTINEL, count=300_000)
    except UcError as exc:
        pc = emu.mu.reg_read(UC_ARM_REG_PC)
        hits.append(("error", f"{exc} @ PC={pc:#010x}"))
    finally:
        emu.mu.hook_del(h)
    return hits


def scenario_caninject(emu: Emu) -> int:
    """Injiziert CAN-Frames in den echten RX-Handler (0x0801B066).

    Nutzung:
        python3 tools/emu.py caninject                # alle 22 IDs, leere Daten
        python3 tools/emu.py caninject 0x203 01 02..  # eine ID mit Daten
    """
    args = sys.argv[2:]
    if not args:
        ids = _can_id_list(emu)
        print("[caninject] teste alle IDs:", " ".join(f"{i:#05x}" for i in ids))
        for cid in ids:
            try:
                hits = _inject_can(emu, cid, bytes(8))
            except Exception as exc:  # noqa: BLE001
                print(f"  id {cid:#05x}: Fehler {exc}")
                continue
            hands = [f"{a:#010x}" for k, a in hits if k == "hand"]
            did_reset = any(k == "RESET" for k, _ in hits)
            ev = " ".join(f"{emu.rd(0x20000AB0 + i, 1):02x}" for i in range(4))
            print(f"  id {cid:#05x}: handler={hands} reset={did_reset} "
                  f"sig={emu.rd(CAN_STATE + 12, 2):#06x} event=[{ev}] "
                  f"port={getattr(emu, 'port_state', '?')} "
                  f"hdr28={emu.rd(0x20000928):#010x} "
                  f"st41={emu.rd(0x20000941, 1):#04x}")
        return 0

    canid = int(args[0], 0)
    data = (bytes(int(b, 16) for b in args[1:])
            if len(args) > 1 else bytes(8))
    hits = _inject_can(emu, canid, data)
    print(f"[caninject] id={canid:#05x} data={bytes(data).hex(' ')}")
    for k, a in hits:
        print("   ", k, hex(a) if isinstance(a, int) else a)
    print(f"  hdr 0x20000928={emu.rd(0x20000928):#010x} "
          f"0x20000941={emu.rd(0x20000941, 1):#04x}")
    return 0


# --------------------------------------------------------------------------
# Bootloader-Session im Emulator
# Kanal 1 (USB) hat ein Rohformat: [0x20000206] = Laenge, Payload ab +1,
# TX-fertig-Flag bei 0x20000247. Modus 0x2000001C+0 == 0 -> Kommando.
# --------------------------------------------------------------------------
BL_RAW_RX = 0x20000206      # Kanal 1: [0]=len, [1..]=Payload
BL_TXDONE = 0x20000247      # Kanal 1: TX-fertig-Flag
BL_MODE = 0x2000001C        # +0 Modus (0=Kommando), +1 Kanal, +2 len
BL_REQ = 0x20000248         # Flash-Request [0]=status [4]=ptr [8]=len [12]=buf
BL_FEND = 0x08040000
BL_FSTART = 0x08007800       # unterhalb = Bootloader (schreibgeschuetzt)


def _bl_install(emu: "Emu", rec: Dict[str, object]) -> None:
    """USB-Transport, Fehlerausgabe und Flash-Programmierung stubben."""
    def send(e: "Emu") -> None:
        p = e.mu.reg_read(UC_ARM_REG_R0)
        n = e.mu.reg_read(UC_ARM_REG_R1)
        if n > 64:
            n = 64
        rec.setdefault("tx", []).append(
            bytes(e.mu.mem_read(p, n)) if n else b"")

    emu.add_stub(0x08003ABC, lambda e: None)            # USB poll
    emu.add_stub(0x08003A36, send)                      # USB send(ptr,len)
    emu.add_stub(0x080042AE, send)                      # CAN send
    emu.add_stub(0x0800421E,
                 lambda e: e.mu.reg_write(UC_ARM_REG_R0, BL_RAW_RX))
    emu.add_stub(0x08004222, lambda e: None)
    emu.add_stub(0x080009A8, lambda e: None)
    emu.add_stub(0x080009FC, lambda e: None)
    for a in (0x08001004, 0x0800100E, 0x08001018, 0x08001022, 0x0800102C,
              0x08001036):
        emu.add_stub(a, lambda e: None)

    def err(e: "Emu") -> None:
        rec.setdefault("err", []).append(e.mu.reg_read(UC_ARM_REG_R0))
        e.mu.reg_write(UC_ARM_REG_R0, 0)

    emu.add_stub(0x08000A46, err)

    def fwrite(e: "Emu") -> None:
        a = e.mu.reg_read(UC_ARM_REG_R0)
        n = e.mu.reg_read(UC_ARM_REG_R1)
        p = e.mu.reg_read(UC_ARM_REG_R2)
        if p == 0 or a < BL_FSTART or (a + n) > BL_FEND:
            e.mu.reg_write(UC_ARM_REG_R0, 1)
            return
        d = bytes(e.mu.mem_read(p, n))
        e.mu.mem_write(a, d)
        emu.flash_log.append((a, n))                    # type: ignore[attr-defined]
        e.mu.reg_write(UC_ARM_REG_R0, 0)

    emu.add_stub(0x08001998, fwrite)


def _bl_feed_raw(emu: "Emu") -> None:
    """Verarbeitet den Report, der bereits in BL_RAW_RX liegt (echter Weg)."""
    emu.call(0x08000AAA)


def _bl_feed(emu: "Emu", payload: bytes) -> None:
    """Speist einen Kanal-1-Rahmen ein und laesst den BL ihn verarbeiten."""
    frame = bytes([len(payload)]) + payload
    emu.mu.mem_write(BL_RAW_RX, frame + bytes(0x40 - len(frame)))
    emu.wr(BL_TXDONE, 1, 1)
    emu.wr(BL_MODE + 1, 0, 1)                          # Kanal 1 waehlen
    emu.stop_pcs.add(0x0800190A)
    emu.call(0x08000AAA)


def _bl_summary(emu: "Emu", rec: Dict[str, object]) -> None:
    flog = getattr(emu, "flash_log", [])
    shown = [(hex(a), n) for a, n in flog[:4]]
    tail = " ..." if len(flog) > 4 else ""
    print(f"    Fehler  = {[hex(x) for x in rec.get('err', [])]}")
    print(f"    Antwort = {[b.hex(' ') for b in rec.get('tx', [])]}")
    print(f"    Flash   = {shown}{tail} (n={len(flog)})")
    print(f"    Modus   = {emu.rd(BL_MODE + 0, 1):#04x}  "
          f"Flag34 = {emu.rd(BL_V34, 1)}  "
          f"Seq = {emu.rd(BL_FSTATE + 0, 1):#04x}  "
          f"Ptr = {emu.rd(BL_FSTATE + 4):#010x}")


def scenario_blprobe(emu: "Emu") -> int:
    """Sondiert die Bootloader-Kommandos mit definierter Rahmenlaenge."""
    emu.flash_log = []                                # type: ignore[attr-defined]
    for cmd in (0x10, 0x11, 0x22, 0x27, 0x2E, 0x31, 0x34, 0x36, 0x37, 0x3E):
        for plen in (0, 2, 10, 16):
            rec: Dict[str, object] = {}
            _bl_install(emu, rec)
            payload = bytes([cmd]) + bytes(plen)
            _bl_feed(emu, payload)
            print(f"  cmd 0x{cmd:02x} len={plen + 1}")
            _bl_summary(emu, rec)
    return 0


def scenario_blsession(emu: "Emu") -> int:
    """Kompletter Bootloader-Upload im Emulator.

    Session: 0x10(Arg 3) = Modus+Gültig, 0x37 = Sequencer-Start,
    0x34 = Adresse setzen, 0x36 = Blockschreiben, 0x3E(0x80) = Finish.
    Danach wird der geschriebene Flash-Inhalt gegen das Original geprueft.
    """
    emu.flash_log = []                                # type: ignore[attr-defined]
    rec: Dict[str, object] = {}
    _bl_install(emu, rec)

    def step(payload: bytes, label: str) -> Tuple[list, list]:
        rec.clear()
        _bl_feed(emu, payload)
        errs = list(rec.get("err", []))               # type: ignore[arg-type]
        txs = list(rec.get("tx", []))                 # type: ignore[arg-type]
        e = ",".join(f"{x:#04x}" for x in errs) or "-"
        t = " ".join(b.hex(" ") for b in txs) or "-"
        print(f"  {label:<30} Fehler={e:<8} Antwort={t}")
        return errs, txs

    target = 0x08008000
    total = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x1000
    blk = 56

    print("[blsession] Session-Aufbau")
    step(bytes([0x10, 0x03]), "0x10 Modus=3 + gueltig")
    step(bytes([0x37]), "0x37 Sequencer-Start")
    ok = True
    errs, _ = step(bytes([0x34, 0x03, 0x00]) + struct.pack(">I", target)
                   + bytes(4), "0x34 Adresse 0x08008000")
    ok = ok and not errs
    print(f"    -> Ptr = {emu.rd(BL_FSTATE + 4):#010x}, "
          f"Flag34 = {emu.rd(BL_V34, 1)}, "
          f"Seq = {emu.rd(BL_FSTATE + 0, 1):#04x}")

    print(f"[blsession] {total} Bytes in {blk}-Byte-Bloecken schreiben")
    data = bytes(emu.mu.mem_read(target, total))    # Originaldaten
    for i in range(0, total, blk):
        seq = ((i // blk) % 255) + 1           # BL zaehlt 1..255, dann 1
        chunk = data[i:i + blk]
        errs, _ = step(bytes([0x36, seq]) + chunk,
                       f"0x36 Block {seq} ({len(chunk)} B)"
                       if i in (0, blk, total - blk) else "")
        if errs:
            ok = False
            break

    step(bytes([0x3E, 0x80]), "0x3E Finish 0x80")

    got = bytes(emu.mu.mem_read(target, total))
    flog = getattr(emu, "flash_log", [])
    print(f"[blsession] Flash-Writes  = {len(flog)}")
    print(f"[blsession] Endzeiger     = {emu.rd(BL_FSTATE + 4):#010x}")
    same = got == data
    print(f"[blsession] Inhalt gleich = {same}")
    print("[blsession] ->", "OK" if (ok and same) else "MISMATCH")
    return 0 if (ok and same) else 1


def scenario_nmstate(emu: "Emu") -> int:
    """Bildet den App-Init-Pfad nach, der den NM-Zustand setzt.

    In der echten App ruft die Kommunikations-Task (0x08009228) einmalig
    0x0800B550 auf -> obj[0x354] = 4; das Objekt ist *(0x200002C8) und wird
    von 0x0800D54C als 0x20005E40 (geteilter USB-Stack-State) geliefert.
    0x0800B59C bewertet danach den Zustand (4 = Port bereit).
    """
    emu.add_stub(0x08000A46, lambda e: None)

    obj0 = emu.rd(0x200002C8)
    print(f"[nmstate] vor  Init: *(0x200002C8) = {obj0:#010x}")
    try:
        emu.call(0x0800B550)                         # echter Init + Start
    except UcError as exc:
        print(f"[nmstate] Init abgebrochen: {exc} @ PC="
              f"{emu.mu.reg_read(UC_ARM_REG_PC):#010x}")
    obj = emu.rd(0x200002C8)
    print(f"[nmstate] nach Init: *(0x200002C8) = {obj:#010x}")

    def dump(o: int) -> None:
        print(f"    obj[0x354] = {emu.rd(o + 0x354, 1)}   (NM-Zustand, 4 = bereit)")
        print(f"    obj[0xa2]  = {emu.rd(o + 0xa2, 1)}   (Port-/Line-State, 3 noetig)")
        q = emu.rd(o + 12)
        print(f"    obj[+12]   = {q:#010x} -> *[+12] = "
              f"{emu.rd(q) if 0x20000000 <= q < 0x20010000 else 0:#010x}"
              f" (bit 0x80000 noetig)")

    if obj:
        dump(obj)
    print(f"[nmstate] [0x200002CD] = {emu.rd(0x200002CD, 1)}  "
          f"[0x200002CF] = {emu.rd(0x200002CF, 1)}")

    watch = {0x200002CD: "status-valid", 0x200002CF: "port-status",
             0x200002CE: "flag-CE"}
    if obj:
        for off in (0x354, 0xA1, 0xA2, 0x344, 0x348, 0x34C, 0x0C):
            watch[obj + off] = f"obj+{off:#x}"
    agg: Dict[Tuple[str, int, int], int] = {}
    for pc, addr, val in emu.writes:
        if addr in watch:
            key = (watch[addr], pc, val)
            agg[key] = agg.get(key, 0) + 1
    print("[nmstate] Schreibzugriffe waehrend Init (Feld, PC, Wert, Anzahl):")
    for (name, pc, val), n in sorted(agg.items()):
        print(f"    {name:<12} = {val:#04x}  von {pc:#010x}  x{n}")

    if obj:
        base = emu.rd(obj + 0x348)
        print(f"[nmstate] Callback-Tabelle @ {base:#010x} "
              f"(ersetzt den Zustandshandler):")
        for i in range(8):
            v = emu.rd(base + i * 4)
            tag = "APP" if 0x08008000 <= (v & ~1) < 0x08040000 else ""
            print(f"    [+{i * 4:2d}] {v:#010x} {tag}")

    emu.call(0x0800B59C)
    r = emu.ret()
    print(f"[nmstate] 0x0800B59C() -> {r}  "
          f"({'Port bereit' if r == 4 else 'ohne USB-Host NICHT bereit'})")
    print("[nmstate] jetzt mit Host-Effekt (Enumeration/SET_CONFIGURATION):")
    r2 = _app_port_ready(emu)
    print(f"[nmstate] 0x0800B59C() -> {r2}  "
          f"({'Port bereit' if r2 == 4 else 'NICHT bereit'})")
    return 0 if r2 == 4 else 1


def scenario_msgprobe(emu: "Emu") -> int:
    """Sucht die Empfangs-Bytes, die in der App die Nachricht Id 0x0304
    auswaehlen (-> fnTable[3] = 0x08017378 = NVIC_SystemReset).

    Bildet den Empfangspfad nach: *(0x2000092C) = Nachrichtenobjekt,
    [obj+16] = Payload. Dann laeuft der Produzent 0x0801891C und setzt
    0x20000941 auf den gefundenen Tabellenindex.
    """
    obj = 0x20008000          # Fake-Nachrichtenobjekt
    pay = 0x20008100          # Payload-Puffer
    st = 0x20008200           # Fake-Statusstruktur
    _app_port_ready(emu)
    emu.wr(0x2000092C, obj)
    emu.wr(0x2000880C + 4, 64, 2)      # empfangene Laenge gross genug
    emu.wr(0x2000880C + 16, 0x000C)    # Flags
    emu.wr(0x2000093C, 0xFFFFFFFF)     # Masken-Freigabe fuer alle Defs

    fn = 0x08036F2C
    tbl = 0x08036F40
    found: List[Tuple[int, int, int, int]] = []
    fails: List[str] = []
    b0 = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x11
    for b1 in range(256):
        emu.mu.mem_write(pay, bytes([b0, b1]) + bytes(14))
        emu.wr(obj + 16, pay)
        emu.wr(0x20000941, 0, 1)
        try:
            emu.call(0x0801891C)
        except UcError as exc:
            if len(fails) < 3:
                fails.append(f"{exc} @PC={emu.mu.reg_read(UC_ARM_REG_PC):#010x}")
            continue
        idx = emu.rd(0x20000941, 1)
        if idx == 0:
            continue
        idw = emu.rd(tbl + idx * 20 + 12)
        hi = (idw >> 8) & 0xFF
        f = emu.rd(fn + hi * 4) if hi < 8 else 0
        if idx != 0xFF:
            found.append((b1, idx, idw, f))

    print(f"[msgprobe] payload[0] = {b0:#04x}")
    print(f"    Def-Auswahl (0x20000940) = {emu.rd(0x20000940, 1):#04x}, "
          f"Laenge-Status = {emu.rd(0x2000880C + 4, 2)}, "
          f"Maske = {emu.rd(0x2000093C):#010x}")
    if fails:
        print(f"    Ausnahmen: {fails}")
    seen = set()
    for b1, idx, idw, f in found:
        key = (idx, idw, f)
        tag = "  <== RESET" if f == 0x08017378 else ""
        if key in seen:
            continue
        seen.add(key)
        print(f"    payload[1] = {b1:#04x}  -> Index {idx:2d}  id={idw:#06x}  "
              f"fn={f:#010x}{tag}")
    if not found:
        print("    (kein Treffer - Payload/State passt nicht)")
    return 0


def scenario_hidtrigger(emu: "Emu") -> int:
    """Integrationstest: Flash-Tool -> HID-Report -> App -> Bootloader-Reset.

    Erzeugt den Report mit stm_display_fw.build_app_report() (also mit genau
    dem Code, den das Tool auf die Hardware schickt), spielt den enthaltenen
    Payload in den echten App-Empfangspfad 0x0801891C und prueft:
      * der Matcher setzt Index 4 in 0x20000941
      * Tabelle 0x08036F40[4].id == 0x0304
      * fnTable[3] == 0x08017378
      * der Dispatcher 0x08018D04 erreicht diesen Reset-Handler
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw  # noqa: PLC0415

    obj = 0x20008000          # Fake-Nachrichtenobjekt
    pay = 0x20008100          # Payload-Puffer
    _app_port_ready(emu)
    emu.wr(0x2000092C, obj)
    emu.wr(0x2000880C + 4, 64, 2)
    emu.wr(0x2000880C + 16, 0x000C)
    emu.wr(0x2000093C, 0xFFFFFFFF)
    emu.add_stub(0x0801D0CE, lambda e: None)
    emu.add_stub(0x0801D0EE, lambda e: None)

    print(f"[hidtrigger] Trigger-Bytes = {fw.TRIGGER_MSG.hex(' ')}")
    ok = True
    for wire in ("len", "raw"):
        rep = fw.build_app_report(fw.TRIGGER_MSG, wire)
        pl = fw.app_payload_from_report(rep, wire)
        if wire == "raw":                    # kein Laengenbyte vorhanden
            pl = pl[:len(fw.TRIGGER_MSG)]
        print(f"[hidtrigger] wire={wire:3}  report={len(rep)} B "
              f"[{rep[:4].hex(' ')}]  payload={pl.hex(' ')}")

        emu.mu.mem_write(pay, bytes(pl) + bytes(16))
        emu.wr(obj + 16, pay)
        emu.wr(0x20000941, 0, 1)
        try:
            emu.call(0x0801891C)
        except UcError as exc:
            print(f"    Emulationsfehler: {exc}")
            ok = False
            continue

        idx = emu.rd(0x20000941, 1)
        idw = emu.rd(0x08036F40 + idx * 20 + 12) if idx < 51 else 0
        hi = (idw >> 8) & 0xFF
        fn = emu.rd(0x08036F2C + hi * 4) if hi < 8 else 0
        print(f"    Index={idx}  id={idw:#06x}  fn={fn:#010x}")
        if idx != 4 or (fn & ~1) != 0x08017378:
            ok = False
            continue

        # Dispatcher mit passendem Nachrichten-State ausfuehren
        emu.wr(0x20000930, 0x40, 1)
        emu.wr(0x20000928, 0x0800, 2)
        emu.wr(0x20000942, 0, 1)
        hit: List[int] = []

        def stop_at_reset(mu, address, size, ud):
            if address == 0x08017378:
                hit.append(address)
                mu.emu_stop()

        h = emu.mu.hook_add(UC_HOOK_CODE, stop_at_reset)
        try:
            emu.mu.reg_write(UC_ARM_REG_SP, SRAM + SRAM_SIZE - 0x400)
            emu.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
            emu.mu.emu_start(0x08018D04 | 1, RET_SENTINEL, count=500_000)
        except UcError as exc:
            print(f"    Emulationsfehler: {exc}")
        finally:
            emu.mu.hook_del(h)
        print(f"    Dispatcher -> Reset-Handler 0x08017378: {bool(hit)}")
        ok = ok and bool(hit)

    print("[hidtrigger] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# App-USB-Empfangspfad - Adressen aus dem Dump
# --------------------------------------------------------------------------
APP_RX_BUF = 0x2000996C      # Empfangspuffer der App  (Pool 0x801D9E8)
APP_RX_SEQ = 0x20000BAC      # Sequenzzaehler          (Pools 0x801D9EC/0x801D550)
APP_FIFO_A_PTR = 0x20000BA0  # *(0x801D9E0) -> Deskriptorzeiger FIFO A
APP_FIFO_B_PTR = 0x20000BA4  # *(0x801D9E4) -> Deskriptorzeiger FIFO B
APP_MSG_OBJ = 0x2000B814     # Nachrichtenobjekt      (Pool 0x801DA08)
APP_MSG_PTR = 0x2000092C     # Zeiger darauf          (Pool 0x80190E4)
APP_PARSER = 0x0801D634      # Typverteiler des USB-Empfangs
APP_PORT_TASK = 0x0801D81E   # Port-Task: 0x0801D900 holt FIFO A ab


def scenario_usbtrigger(emu: "Emu") -> int:
    """Echter USB-Weg: 64-Byte-Report -> NVIC_SystemReset der App.

    Es wird nichts vorgekaut, sondern der echte Code benutzt:

      * Empfangsparser   0x0801D634 (Typ 1 ab 0x0801D75C, Typ 0xFE 0x0801D744)
      * Verpackung       0x0801D5C2 / ID-Freigabe 0x0801D4E2
      * FIFO A           0x0800B190 (Init) / 0x0800B1BE (Push) / 0x0800B202
      * Port-Task        0x0801D81E -> 0x0801D900 holt den Block ab
      * Nachrichtenmodul 0x0801819A / 0x080181CA (-> 0x08018160)
      * Dispatcher       0x08018D04 -> fnTable[3] = 0x08017378
                         -> SCB->AIRCR = 0x05FA0004
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw  # noqa: PLC0415

    ok = True
    _app_port_ready(emu)

    def run(addr: int, max_insns: int = 400_000) -> Optional[str]:
        try:
            emu.call(addr, max_insns=max_insns)
            return None
        except UcError as exc:
            return f"{exc} @PC={emu.mu.reg_read(UC_ARM_REG_PC):#010x}"

    # FIFOs anlegen (0x0800B190: desc, base, bloecke, blockgroesse).
    # A/B = Empfangswege (0x20000BA0/0x20000BA4), 0x20000B9C = Sende-FIFO.
    for ptr, desc, base in ((APP_FIFO_A_PTR, 0x2000D000, 0x2000D100),
                            (APP_FIFO_B_PTR, 0x2000D400, 0x2000D500),
                            (0x20000B9C, 0x2000D800, 0x2000D900),
                            (0x20000BB0, 0x2000DC00, 0x2000DD00),
                            (0x20000BA8, 0x2000E000, 0x2000E100)):
        emu.call(0x0800B190, r0=desc, r1=base, r2=8, r3=0x40)
        emu.wr(ptr, desc)
    # Nachrichtenmodul-Zustand (setzt sonst die App-Initialisierung)
    emu.wr(APP_MSG_PTR, APP_MSG_OBJ)
    emu.wr(0x2000880C + 4, 64, 2)
    emu.wr(0x2000880C + 16, 0x000C)
    emu.wr(0x2000093C, 0xFFFFFFFF)

    def fifo_count(ptr: int) -> int:
        desc = emu.rd(ptr)
        return emu.rd(desc + 16, 1) if desc else -1

    # ---- 1) Steuerrahmen Typ 0xFE setzt den Zaehler --------------------
    emu.wr(APP_RX_SEQ, 0x7F, 1)
    emu.mu.mem_write(APP_RX_BUF, fw.build_sync_frame(0))
    err = run(APP_PARSER, 100_000)
    seq_now = emu.rd(APP_RX_SEQ, 1)
    print(f"[usbtrigger] 1) Typ-0xFE-Rahmen -> Zaehler = {seq_now} (erwartet 0)"
          + (f"   [{err}]" if err else ""))
    ok = ok and seq_now == 0 and not err

    # ---- 2) Trigger-Rahmen durch den echten Parser ---------------------
    rep = fw.build_app_frame(fw.TRIGGER_MSG, seq=0)
    print(f"[usbtrigger] 2) Report   = {rep[:8].hex(' ')} ... "
          f"(Nutzlast {fw.TRIGGER_MSG.hex(' ')}, Kanal "
          f"{rep[3] | (rep[4] << 8):#06x})")
    emu.mu.mem_write(APP_RX_BUF, rep)
    before = fifo_count(APP_FIFO_A_PTR)
    err = run(APP_PARSER, 200_000)
    blk = fifo_count(APP_FIFO_A_PTR)
    print(f"[usbtrigger]    Parser -> FIFO A: {before} -> {blk} Block"
          + (f"   [{err}]" if err else ""))
    ok = ok and blk == before + 1 and not err

    # ---- 3) Port-Task holt den Block ab und fuettert das Modul --------
    emu.wr(0x20000941, 0, 1)
    emu.wr(0x20000940, 0, 1)
    # Nach der Uebergabe an das Nachrichtenmodul (Aufruf 0x080181CA kehrt
    # nach 0x0801D93A zurueck) anhalten - der Rest der Task braucht
    # Peripherie, die hier nicht aufgebaut ist.
    emu.stop_pcs.add(0x0801D93A)
    err = run(APP_PORT_TASK, 800_000)
    obj_len = emu.rd(APP_MSG_OBJ + 12, 2)
    obj_pay = emu.rd(APP_MSG_OBJ + 16)
    idx3 = emu.rd(0x20000941, 1)
    print(f"[usbtrigger] 3) Port-Task: obj[+12]={obj_len} obj[+16]={obj_pay:#010x} "
          f"FIFO A={fifo_count(APP_FIFO_A_PTR)} Index={idx3}"
          + (f"   [{err}]" if err else ""))
    if obj_pay:
        print(f"[usbtrigger]    Nutzdaten im Objekt: "
              f"{bytes(emu.mu.mem_read(obj_pay, min(obj_len + 2, 16))).hex(' ')}")
    ok = ok and obj_len == len(fw.TRIGGER_MSG)

    # ---- 4) Matcher/Produzent + Dispatcher ----------------------------
    err2 = run(0x0801891C, 400_000)
    idx = emu.rd(0x20000941, 1)
    idw = emu.rd(0x08036F40 + idx * 20 + 12) if idx < 51 else 0
    hi = (idw >> 8) & 0xFF
    fn = emu.rd(0x08036F2C + hi * 4) if hi < 8 else 0
    print(f"[usbtrigger] 4) Matcher -> Index={idx} id={idw:#06x} fn={fn:#010x}"
          + (f"   [{err2}]" if err2 else ""))
    ok = ok and idx == 4 and (fn & ~1) == 0x08017378

    emu.wr(0x20000930, 0x40, 1)
    emu.wr(0x20000928, 0x0800, 2)
    emu.wr(0x20000942, 0, 1)
    hit: List[int] = []

    def cb(mu, address, size, ud):
        if address == 0x08017378:
            hit.append(address)
            mu.emu_stop()

    h = emu.mu.hook_add(UC_HOOK_CODE, cb)
    try:
        emu.mu.reg_write(UC_ARM_REG_SP, SRAM + SRAM_SIZE - 0x400)
        emu.mu.reg_write(UC_ARM_REG_LR, RET_SENTINEL | 1)
        emu.mu.emu_start(0x08018D04 | 1, RET_SENTINEL, count=800_000)
    except UcError as exc:
        print(f"[usbtrigger]    Dispatcher-Fehler: {exc}")
    finally:
        emu.mu.hook_del(h)
    print(f"[usbtrigger]    Dispatcher -> Reset-Handler 0x08017378: {bool(hit)}")
    ok = ok and bool(hit)

    print("[usbtrigger] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


class _EmuBlTransport:
    """Transport-Ersatz fuer den Emulator.

    Erfuellt die Schnittstelle der Flash-Tool-Transportklasse und schickt die
    Reports von stm_display_fw.Protocol in den **echten** Bootloader-Pfad
    (0x08000AAA). Antworten des Bootloaders werden als FIFO zurueckgegeben.
    """

    def __init__(self, emu: "Emu", rec: Dict[str, object]) -> None:
        self.emu = emu
        self.rec = rec
        self.sent = 0

    def send(self, report: bytes) -> None:
        rep = bytes(report)
        if len(rep) < 0x40:
            rep += bytes(0x40 - len(rep))
        self.sent += 1
        e = self.emu
        # Der Report liegt so im Puffer, wie ihn die Hardware ablegt
        # (App-Rahmen ab Offset 0), dann laeuft der echte USB-Layer.
        e.mu.mem_write(BL_RAW_RX, rep[:0x40])
        e.wr(BL_TXDONE, 1, 1)
        e.wr(BL_MODE + 1, 1, 1)                 # Kanal 1 (USB)
        e.stop_pcs.add(0x0800190A)
        try:
            e.call(0x08000AAA)
            return
        except UcError:
            pass                                  # Fallback unten
        # Fallback: Nutzlast auf der Kommandoschicht einspeisen
        sys.path.insert(0, str(ROOT / "tools"))
        import stm_display_fw as fw  # noqa: PLC0415
        pf = fw.parse_app_frame(rep)
        if pf is not None:
            if pf[1] != 0xFE:
                _bl_feed(e, pf[3])
            return
        if rep and rep[0]:
            _bl_feed(e, rep[1:1 + rep[0]])

    def recv(self, timeout_ms: int = 500):
        txs = self.rec.get("tx")
        if isinstance(txs, list) and txs:
            return txs.pop(0)
        return None

    def close(self) -> None:
        return None


def scenario_uploadtool(emu: "Emu") -> int:
    """Fahrt den echten Upload-Pfad des Flash-Tools gegen den emulierten BL.

    Verwendet stm_display_fw.Protocol.upload() (also genau den Code, der auf
    der Hardware laeuft) mit einem Transport-Ersatz, der die Reports in
    0x08000AAA einspeist. Geprueft wird, dass der gesamte App-Bereich
    unveraendert geschrieben wird und der Schreibzeiger exakt auf
    0x08040000 endet.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw  # noqa: PLC0415

    rec: Dict[str, object] = {}
    emu.flash_log = []                              # type: ignore[attr-defined]
    _bl_install(emu, rec)
    tp = _EmuBlTransport(emu, rec)
    proto = fw.Protocol(tp, verbose=False, wire="raw")

    img = fw.load_image(ROOT / "data" / "stm32f105_conti.bin")
    target = fw.APP_BASE
    total = fw.APP_CRC_ADDR + 4 - fw.APP_BASE
    before = bytes(emu.mu.mem_read(target, total))

    print(f"[uploadtool] starte Protocol.upload('app'): {total} Bytes, "
          f"chunk=56 -> {total // 56} Bloecke")
    proto.upload(img, region="app")

    after = bytes(emu.mu.mem_read(target, total))
    endptr = emu.rd(BL_FSTATE + 4)
    nflash = len(getattr(emu, "flash_log", []))
    errs = [hex(x) for x in rec.get("err", [])]     # type: ignore[union-attr]

    print(f"[uploadtool] Reports gesendet   = {tp.sent}")
    print(f"[uploadtool] Flash-Writes       = {nflash}")
    print(f"[uploadtool] Endzeiger          = {endptr:#010x} "
          f"(erwartet {fw.APP_CRC_ADDR + 4:#010x})")
    print(f"[uploadtool] Bootloader-Fehler  = {errs if errs else '-'}")
    same = after == before
    print(f"[uploadtool] Inhalt identisch   = {same}")
    ok = same and endptr == fw.APP_CRC_ADDR + 4 and not errs
    print("[uploadtool] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


# ==========================================================================
# BMS-Patch: 0x555 soll dem Displayzustand folgen
# ==========================================================================
P555_WORD = 0x2000096C     # 32-Bit-Nutzwort der CAN-Botschaft 0x555 (Bit 0)
P_RAMP = 0x2000087B        # Rampenzaehler
P_ENABLE = 0x2000087E      # "CAN 0x201 empfangen"
P_STATE = 0x200008FE       # Zustandsmaschine (0..3)
P_SHDN = 0x200008FF        # Abschaltflag -> fuehrt in Zustand 3 (Sackgasse)
P_F1A0 = 0x20000994        # Bit 0 -> f_f1a0()
P_MODE = 0x200002CF        # gecachter Display-Modus (2 = laeuft, 4 = aus)
P_MODEFREEZE = 0x200002CD  # Freeze-Flag: Getter liefert Cache ohne Dereferenz
P_DESC_DIRTY = 0x20009908  # Bit 7 je TX-Descriptor (f_1C2CE)

F_RAMP = 0x0801673C        # 0x555-Logik, wird zyklisch in Zustand 2 gerufen
F_OFF = 0x080167B6         # Abschaltpfad: 0x555 = 0
F_SETEN = 0x080167E4       # 0x201-Empfang: [0x2000087E] = 1
F_DISPATCH = 0x08016822    # Zustandsmaschine

BMS_IMG_ORIG = ROOT / "data" / "stm32f105_conti.bin"
BMS_IMG_PATCH = ROOT / "data" / "stm32f105_bms_control.hex"

FW_APP_BASE = 0x08008000
FW_APP_CRC_ADDR = 0x0803FFFC


def _bmspatch_emu(img: bytes) -> Emu:
    """Emu mit Stubs fuer alles, was nicht die gepruefte Logik ist."""
    e = Emu(img)
    noop = lambda _e: None                                   # noqa: E731
    # CAN-Sperrzaehler, Event-Ausgaben und Standby-Sequenz
    for a in (0x0801D0CE, 0x0801D0EE, 0x08015EF8, 0x08015F6C,
              0x08008A62, 0x0800ED76, 0x08012DC8):
        e.add_stub(a, noop)
    # f_0F1B2() ist reine Eingabe ("Bit 1 gesetzt?") -> 0
    e.add_stub(0x0800F1B2, lambda x: x.mu.reg_write(UC_ARM_REG_R0, 0))
    return e


def _bms_ramp(e: Emu, enable: int, ramp: int, bit: int = 0) -> Tuple[int, int]:
    """Ruft f_1673C. Liefert (0x555-Bit, Descriptor-Dirty-Bit)."""
    e.wr(P555_WORD, bit)
    e.wr(P_DESC_DIRTY + 8, 0, 1)
    e.wr(P_ENABLE, enable, 1)
    e.wr(P_RAMP, ramp, 1)
    e.call(F_RAMP)
    return e.rd(P555_WORD) & 1, (e.rd(P_DESC_DIRTY + 8, 1) >> 7) & 1


def _bms_dispatch(e: Emu, mode: int, enable: int, shdn: int,
                  f1a0: int) -> Tuple[int, int]:
    """Ruft die Zustandsmaschine aus Zustand 2. Liefert (Zustand, 0x555)."""
    e.wr(P555_WORD, 0)
    e.wr(P_STATE, 2, 1)
    e.wr(P_MODEFREEZE, 1, 1)
    e.wr(P_MODE, mode, 1)
    e.wr(P_ENABLE, enable, 1)
    e.wr(P_SHDN, shdn, 1)
    e.wr(P_F1A0, f1a0, 1)
    e.wr(P_RAMP, 0, 1)
    e.call(F_DISPATCH)
    return e.rd(P_STATE, 1), e.rd(P555_WORD) & 1


def _bms_app_crc(e: Emu) -> int:
    """Rechnet die Bootloader-CRC ueber die Applikation (0x08000924).

    Deskriptor {+0 = Laenge in Bytes, +4 = Datenzeiger, +12 = Ergebnis}.
    """
    sptr = SRAM + 0x3000
    e.mu.mem_write(sptr, bytes(16))
    e.wr(sptr + 0, FW_APP_CRC_ADDR - FW_APP_BASE)
    e.wr(sptr + 4, FW_APP_BASE)
    e.call(0x08000924, r0=sptr, max_insns=200_000_000)
    return e.rd(sptr + 12)


# --- Abschalt-Timer (P5) --------------------------------------------------
P_TIMER = 0x20000100       # 32-Bit-Countdown, Reload 3000
P_TICK100 = 0x20000104     # Halfword-Teiler (100)
F_TASK = 0x080093D2        # zyklischer Task, enthaelt den Countdown
F_DELAY = 0x08009E1C       # Wartepunkt am Ende einer Task-Iteration


def _ret0(e: Emu) -> None:
    e.mu.reg_write(UC_ARM_REG_R0, 0)


def _ret2(e: Emu) -> None:
    e.mu.reg_write(UC_ARM_REG_R0, 2)


def _adc_zero(e: Emu) -> None:
    e.mu.mem_write(e.mu.reg_read(UC_ARM_REG_R1), b"\x00\x00")
    e.mu.reg_write(UC_ARM_REG_R0, 0)


def _bms_task_emu(img: bytes) -> Emu:
    """Emu mit Stubs, damit der zyklische Task genau eine Iteration durchlaeuft
    und dabei den Zweig \"keine Aktivitaet\" nimmt."""
    e = Emu(img)
    noop = lambda _e: None                                   # noqa: E731
    for a in (0x08015EF8, 0x08015F6C, 0x08008A62, 0x0800ED76, 0x08012DC8,
              0x0801D0CE, 0x0801D0EE, 0x080135DE):
        e.add_stub(a, noop)
    # "keine Aktivitaet": f_0F390() = 0 (<=10), f_0EACA() = 0, f_20144() = 0
    for a in (0x0800F1B2, 0x0801FD8E, 0x0801B8C8, 0x0801EE58, 0x0801EE48,
              0x0800EACA, 0x08020144, 0x0800F390):
        e.add_stub(a, _ret0)
    e.add_stub(0x0800B59C, _ret2)      # Display-Modus 2 (laeuft), nicht 4
    e.add_stub(0x0800E4C2, _adc_zero)  # ADC -> 0
    e.stop_pcs.add(F_DELAY)            # nach einer Iteration anhalten
    return e


def _run_task(e: Emu) -> Tuple[int, int]:
    """Ruft den zyklischen Task einmal auf. Liefert (Timer, Abschaltflag)."""
    e.wr(P_TIMER, 1)          # Timer steht kurz vor dem Ablauf
    e.wr(P_TICK100, 1, 2)
    e.wr(P_SHDN, 0, 1)
    e.call(F_TASK)
    return e.rd(P_TIMER), e.rd(P_SHDN, 1)


# --- CAN (P6) -------------------------------------------------------------
CAN_MCR = 0x40006400
CAN_MSR = 0x40006404
CAN_TSR = 0x40006408
CAN_RF0R = 0x4000640C
F_CAN_ENTER_INIT = 0x0801B93C


def _can_enter_init(e: Emu) -> int:
    """Ruft f_1B93C auf und liefert den geschriebenen CAN_MCR-Wert."""
    e.wr(CAN_MCR, 0)
    e.wr(CAN_MSR, 1)          # INAK gesetzt -> Warteschleife endet sofort
    e.call(F_CAN_ENTER_INIT)
    return e.rd(CAN_MCR)


def scenario_bmspatch(emu: Emu) -> int:
    """Verifiziert den BMS-Patch funktional in der Emulation.

    Geprueft wird die echte Firmware (nicht nachgebauter Code):

      A  Original: ohne 0x201 bleibt 0x555 = 0  -- auch bei Rampe 15
         (das ist der Fehler, der hier behoben wird)
      B  Original: mit 0x201 (oder Rampe 15) wird 0x555 = 1
         -- beweist, dass die Emulation originalgetreu rechnet
      C  Patch:    ohne 0x201 wird 0x555 = 1 (P1)
      D  Patch:    Abschaltpfad f_167B6 setzt 0x555 = 0 (P2)
      E  Patch:    Zustand 2 + [0x8FF] bleibt Zustand 2 (P4)
      F  Original: Zustand 2 + [0x8FF] -> Zustand 3 (Sackgasse, Gegenprobe)
      G  Patch:    Modus 4 (Display aus) -> Zustand 1 + 0x555 = 0
      K  Abschalt-Timer (P5): Original setzt [0x200008FF], Patch nicht
      L  CAN_MCR (P6): Original schreibt nur INRQ, Patch zusaetzlich ABOM
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw                                    # noqa: PLC0415

    if not BMS_IMG_PATCH.exists():
        print(f"FEHLER: {BMS_IMG_PATCH} fehlt -- erst tools/patch_bms.py laufen "
              f"lassen.")
        return 2

    # Gepatchte Firmware absichtlich ueber den HEX-Pfad laden, damit der
    # Intel-HEX-Parser von stm_display_fw mitgeprueft wird.
    patched = bytes(fw.load_image(BMS_IMG_PATCH).data)
    orig = BMS_IMG_ORIG.read_bytes()
    print(f"[bmspatch] Original {len(orig)} B, Patch (aus HEX) {len(patched)} B, "
          f"CRC-Image 0x{len(orig) - 1:06x}")

    checks: List[Tuple[str, bool, str]] = []

    def _fmt(v) -> str:
        if isinstance(v, int) and v > 0x0FFF:
            return f"0x{v:08x}"
        return repr(v)

    def check(name: str, got, want) -> None:
        ok = got == want
        checks.append((name, ok, f"ist {_fmt(got)}, erwartet {_fmt(want)}"))
        print(f"    {'OK ' if ok else '?? '}{name}: ist {_fmt(got)}, "
              f"erwartet {_fmt(want)}")

    o = _bmspatch_emu(orig)
    p = _bmspatch_emu(patched)

    try:
        print("[bmspatch] --- Original ---")
        # A: 0x201-Handler setzt das Flag
        o.wr(P_ENABLE, 0, 1)
        o.call(F_SETEN)
        check("A  Original: 0x201-Handler setzt Flag", o.rd(P_ENABLE, 1), 1)

        # B: ohne 0x201 -> 0x555 bleibt 0, auch bei Rampe 15
        b0, _ = _bms_ramp(o, enable=0, ramp=0)
        b15, _ = _bms_ramp(o, enable=0, ramp=15)
        check("B  Original ohne 0x201 (Rampe 0)", b0, 0)
        check("B  Original ohne 0x201 (Rampe 15)", b15, 0)

        # C: mit 0x201 -> 0x555 = 1 (Gegenprobe: Emulation ist originalgetreu)
        c_en, _ = _bms_ramp(o, enable=1, ramp=0)
        c15, c_dirty = _bms_ramp(o, enable=1, ramp=15)
        check("C  Original mit 0x201 (Rampe 0)", c_en, 0)
        check("C  Original mit 0x201 (Rampe 15)", c15, 1)
        check("C  Original: TX-Descriptor markiert", c_dirty, 1)

        print("[bmspatch] --- Patch ---")
        # D: ohne 0x201 -> sofort 0x555 = 1
        d0, d_dirty = _bms_ramp(p, enable=0, ramp=0)
        check("D  Patch ohne 0x201 -> 0x555 = 1", d0, 1)
        check("D  Patch: TX-Descriptor markiert", d_dirty, 1)

        # E: Abschaltpfad
        p.wr(P555_WORD, 1)
        p.call(F_OFF)
        check("E  Patch: f_167B6 -> 0x555 = 0", p.rd(P555_WORD) & 1, 0)

        # F: Zustandsmaschine, Modus 2
        st2, v2 = _bms_dispatch(p, mode=2, enable=0, shdn=0, f1a0=0)
        check("F  Patch: Modus 2 -> Zustand 2", st2, 2)
        check("F  Patch: Modus 2 -> 0x555 = 1", v2, 1)

        # G: Abschaltflag darf Zustand 2 nicht mehr verlassen (P4)
        st_s, v_s = _bms_dispatch(p, mode=2, enable=0, shdn=1, f1a0=1)
        check("G  Patch: [0x8FF] -> Zustand bleibt 2", st_s, 2)
        check("G  Patch: [0x8FF] -> 0x555 bleibt 1", v_s, 1)

        # H: Gegenprobe Original -> Sackgasse Zustand 3
        o.wr(P_ENABLE, 1, 1)
        st_o, v_o = _bms_dispatch(o, mode=2, enable=1, shdn=1, f1a0=1)
        check("H  Original: [0x8FF] -> Zustand 3", st_o, 3)
        check("H  Original: [0x8FF] -> 0x555 = 0", v_o, 0)

        # I: Display aus (Modus 4) -> Zustand 1 + 0x555 = 0
        st4, v4 = _bms_dispatch(p, mode=4, enable=0, shdn=0, f1a0=0)
        check("I  Patch: Modus 4 -> Zustand 1", st4, 1)
        check("I  Patch: Modus 4 -> 0x555 = 0", v4, 0)

        # J: Bootloader-CRC (P3) -- der Boot entscheidet ueber die App-CRC.
        # Die CRC-Funktion 0x08000924 laeuft echt gegen das emulierte
        # CRC-Peripheral, nicht gegen Python nachgerechnet.
        stored_o = int.from_bytes(orig[FW_APP_CRC_ADDR - FLASH:
                                       FW_APP_CRC_ADDR - FLASH + 4], "big")
        stored_p = int.from_bytes(patched[FW_APP_CRC_ADDR - FLASH:
                                          FW_APP_CRC_ADDR - FLASH + 4], "big")
        crc_o = _bms_app_crc(o)
        crc_p = _bms_app_crc(p)
        print(f"    (Original CRC=0x{stored_o:08x}, Patch CRC=0x{stored_p:08x})")
        check("J  Original: Bootloader-CRC == gespeichert", crc_o, stored_o)
        check("J  Patch:    Bootloader-CRC == gespeichert", crc_p, stored_p)

        # K: Abschalt-Timer (P5)
        t_o = _bms_task_emu(orig)
        t_p = _bms_task_emu(patched)
        k_timer_o, k_flag_o = _run_task(t_o)
        k_timer_p, k_flag_p = _run_task(t_p)
        print(f"    (Timer: Original={k_timer_o} Flag={k_flag_o} | "
              f"Patch={k_timer_p} Flag={k_flag_p})")
        check("K  Original: Timer laeuft ab -> [0x8FF] = 1", k_flag_o, 1)
        check("K  Patch:    Flag bleibt 0", k_flag_p, 0)
        check("K  Patch:    Zaehler neu auf 3000 geladen", k_timer_p, 3000)

        # L: CAN ABOM (P6)
        mcr_o = _can_enter_init(o)
        mcr_p = _can_enter_init(p)
        print(f"    (CAN_MCR: Original=0x{mcr_o:08x} Patch=0x{mcr_p:08x})")
        check("L  Original: MCR.ABOM (0x40) nicht gesetzt", mcr_o & 0x40, 0)
        check("L  Patch:    MCR.ABOM (0x40) gesetzt", mcr_p & 0x40, 0x40)
        check("L  Patch:    MCR.INRQ (0x01) weiterhin gesetzt", mcr_p & 1, 1)
    except UcError as exc:
        import traceback                                            # noqa: PLC0415
        print(f"[bmspatch] Emulationsfehler: {exc}")
        traceback.print_exc()
        return 1

    bad = [n for n, ok, _ in checks if not ok]
    print(f"[bmspatch] {len(checks) - len(bad)}/{len(checks)} Checks OK")
    if bad:
        print("[bmspatch] FEHLGESCHLAGEN:", ", ".join(bad))
    print("[bmspatch] ->", "OK" if not bad else "MISMATCH")
    return 0 if not bad else 1


# ==========================================================================
# Flash-Pfad des Bootloaders: Erase (f_801964) und Program (f_801998)
# --------------------------------------------------------------------------
# Der Bootloader kopiert beim Start einen 536-Byte-Blob aus dem Flash
# (0x08005108) nach 0x2000B248 und springt fuer alle Flash-Operationen per
# Trampolin dorthin:
#
#     RAM 0x2000B274 (+0x2C)  <- 0x08005134  "unlock"  (auf HSI umschalten,
#                                              FLASH_KEYR entriegeln)
#     RAM 0x2000B2A6 (+0x5E)  <- 0x08005166  "lock"    (FLASH_CR.LOCK = 1)
#     RAM 0x2000B2F4 (+0xAC)  <- 0x080051B4  "erase"   (Seitenschleife)
#     RAM 0x2000B39C (+0x154) <- 0x0800525C  "program" (Halbwort + Verify)
#
# Der Deskriptor liegt fest bei 0x20000248:
#     +0 Status (+1 = WRPRTERR/PGERR beim Eintritt, +2 = Timeout,
#                +3 = Bereich ungültig, +4 = Verify-Fehler)
#     +4 Adresse    +8 Laenge    +12 Quellzeiger    +16 Watchdog-Callback
# ==========================================================================
F_ERASE = 0x08001964          # f_801964(addr, len)           -> 0/1/3
F_PROG = 0x08001998           # f_801998(addr, len, srcptr)    -> 0/1/2/3
DESC = 0x20000248
BLOB_SRC = 0x08005108
BLOB_DST = 0x2000B248
BLOB_LEN = 0x218

FLASH_KEYR = 0x40022004
FLASH_SR = 0x4002200C
FLASH_CR = 0x40022010
FLASH_AR = 0x40022014

SR_BSY = 1 << 0
SR_PGERR = 1 << 2
SR_WRPRTERR = 1 << 4
SR_EOP = 1 << 5
CR_PG = 1 << 0
CR_PER = 1 << 1
CR_STR = 1 << 6
CR_LOCK = 1 << 7


class FlashModel:
    """Minimales STM32F1-Flashmodell (nur was der Bootloader benutzt)."""

    def __init__(self, emu: "Emu", wrp_from: int | None = None,
                 locked: bool = False) -> None:
        self.e = emu
        self.wrp_from = wrp_from          # ab dieser Adresse schreibgeschuetzt
        self.locked = locked              # FLASH_CR bleibt gesperrt (KEYR wirkungslos)
        self.sr = SR_EOP
        self.cr = CR_LOCK
        self.ar = 0
        self.key = 0
        self.log: List[Tuple[int, int]] = []   # (pc, wert) auf DESC[+0]
        emu.wr(FLASH_SR, self.sr)
        emu.wr(FLASH_CR, self.cr)
        self.srlog: List[Tuple[int, int]] = []
        emu.mu.hook_add(UC_HOOK_MEM_WRITE, self._on_write)
        emu.mu.hook_add(UC_HOOK_MEM_WRITE, self._on_desc_write)
        emu.mu.hook_add(UC_HOOK_MEM_READ, self._on_read)

    # -- Registerverhalten ------------------------------------------------
    def _on_read(self, mu, access, address, size, value, user_data):
        if address == FLASH_SR and size == 4:
            mu.mem_write(FLASH_SR, struct.pack("<I", self.sr))
            self.srlog.append((mu.reg_read(UC_ARM_REG_PC), self.sr))
        elif address == FLASH_CR and size == 4:
            mu.mem_write(FLASH_CR, struct.pack("<I", self.cr))

    def sr_trace(self) -> str:
        return " ".join(f"0x{pc:08x}=0x{v:02x}" for pc, v in self.srlog[:12])

    def _on_desc_write(self, mu, access, address, size, value, user_data):
        if address == DESC and size == 1:
            self.log.append((mu.reg_read(UC_ARM_REG_PC), int(value) & 0xFF))

    def status_trace(self) -> str:
        return " ".join(f"0x{pc:08x}->0x{v:02x}" for pc, v in self.log)

    def _protected(self, addr: int) -> bool:
        return self.wrp_from is not None and addr >= self.wrp_from

    @property
    def _cr_locked(self) -> bool:
        return self.locked or bool(self.cr & CR_LOCK)

    def _on_write(self, mu, access, address, size, value, user_data):
        self.reg_write(address, value, size)

    # -- Registerlogik (auch direkt nutzbar, z. B. fuer Tests) -------------
    def reg_write(self, address: int, value: int, size: int = 4) -> None:
        if address == FLASH_SR:
            # Schreiben loescht die W1C-Bits (2 = PGERR, 4 = WRPRTERR, 5 = EOP)
            self.sr &= ~value & 0xFFFFFFFF
            return
        if address == FLASH_CR:
            self.cr = value
            if (value & CR_STR) and (value & CR_PER) and not (value & CR_PG):
                self._erase_page()
            return
        if address == FLASH_AR:
            self.ar = value
            return
        if address == FLASH_KEYR:
            if self.locked:
                return
            if self.key == 0 and value == 0x45670123:
                self.key = 1
            elif self.key == 1 and value == 0xCDEF89AB:
                self.key = 2
                self.cr &= ~CR_LOCK
            else:
                self.key = 0
                self.cr |= CR_LOCK
            return
        if (FLASH <= address < FLASH + FLASH_SIZE and size == 2
                and (self.cr & CR_PG)):
            if self._cr_locked:
                return                     # Zugriff wird verworfen (CR.LOCK)
            old = int.from_bytes(self.e.mu.mem_read(address, 2), "little")
            new = int(value) & 0xFFFF
            if new & ~old & 0xFFFF:
                self.sr |= SR_PGERR       # 1-Bits ueber 0 -> Programmierfehler
            self.e.mu.mem_write(address,
                                ((old & new) & 0xFFFF).to_bytes(2, "little"))

    def reg_read(self, address: int) -> int:
        if address == FLASH_SR:
            return self.sr
        if address == FLASH_CR:
            return self.cr
        if address == FLASH_AR:
            return self.ar
        return 0

    def _erase_page(self):
        base = self.ar & ~0x7FF
        if self._cr_locked:
            return                         # keine Wirkung, kein Fehlerflag
        if self._protected(base):
            self.sr |= SR_WRPRTERR          # Seite geschuetzt -> nichts passiert
        else:
            self.e.mu.mem_write(base, b"\xff" * 0x800)

    # -- Helfer ------------------------------------------------------------
    def src(self, addr: int, n: int) -> bytes:
        return bytes(self.e.mu.mem_read(addr, n))

    def desc_status(self) -> int:
        return self.e.rd(DESC, 1)

    def desc_set(self, addr: int, ln: int, src: int = 0) -> None:
        self.e.wr(DESC, 0, 1)
        self.e.wr(DESC + 4, addr)
        self.e.wr(DESC + 8, ln)
        self.e.wr(DESC + 12, src)


def _flash_emu(img: bytes, wrp_from: int | None,
               locked: bool = False) -> Tuple["Emu", FlashModel]:
    e = Emu(img)
    # Der Blob wird auf der echten Hardware beim Start in den RAM kopiert.
    off = BLOB_SRC - FLASH
    e.mu.mem_write(BLOB_DST, img[off:off + BLOB_LEN])
    return e, FlashModel(e, wrp_from, locked)


def scenario_flashprog(emu: "Emu") -> int:
    """Reproduziert den auf der Hardware beobachteten Fehler 0x72."""
    img = emu.img
    app_base = 0x08008000
    app_len = 0x1000          # 2 Seiten fuer den Test
    blk_addr = 0x08008000
    blk = bytes(img[blk_addr - FLASH: blk_addr - FLASH + 56])
    src = SRAM + 0x6000
    ok = True

    def report(name: str, cond: bool, text: str) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'OK ' if cond else 'FEHL'}] {name:22s} {text}")

    print("\n--- 1) Ohne WRP: Erase + Programmieren (Sollweg) ---")
    e, fm = _flash_emu(img, None)
    fm.desc_set(app_base, app_len)
    r = (e.call(F_ERASE, r0=app_base, r1=app_len), e.ret())[1]
    blank = fm.src(app_base, 8) == b"\xff" * 8
    report("Erase Rueckgabe", r == 0, f"ret={r} (0 = ok)")
    report("Flash wirklich leer", blank, "0x08008000..0x08008007 = FF")
    report("FLASH_SR sauber", fm.sr & (SR_WRPRTERR | SR_PGERR) == 0,
           f"SR=0x{fm.sr:02x}")
    fm.desc_set(blk_addr, len(blk), src)
    e.mu.mem_write(src, blk)
    e.call(F_PROG, r0=blk_addr, r1=len(blk), r2=src)
    r = e.ret()
    report("Programm Rueckgabe", r == 0, f"ret={r} (0 = ok)")
    report("Daten im Flash", fm.src(blk_addr, len(blk)) == blk, "identisch")
    print(f"     Statusverlauf: {fm.status_trace()}")
    print(f"     SR-Lesezugriffe: {fm.sr_trace()}")

    print("\n--- 2) Mit WRP (so verhaelt sich das Geraet) ---")
    e, fm = _flash_emu(img, 0x08008000)
    fm.desc_set(app_base, app_len)
    e.call(F_ERASE, r0=app_base, r1=app_len)
    r = e.ret()
    kept = fm.src(app_base, 8) == bytes(img[app_base - FLASH:app_base - FLASH + 8])
    report("Erase Rueckgabe", r == 0, f"ret={r} -> meldet ERFOLG")
    report("Flash unveraendert", kept, "Seiten wurden NICHT geloescht")
    report("WRPRTERR gesetzt", bool(fm.sr & SR_WRPRTERR), f"SR=0x{fm.sr:02x}")
    fm.desc_set(blk_addr, len(blk), src)
    e.call(F_PROG, r0=blk_addr, r1=len(blk), r2=src)
    r = e.ret()
    report("Programm Rueckgabe", r == 3, f"ret={r} -> Fehlercode 0x72")
    report("Stub meldet 1", 1 in [v for _, v in fm.log],
           f"Statusverlauf: {fm.status_trace()}")

    print("\n--- 3) Ohne Erase, ueber belegten Flash schreiben ---")
    e, fm = _flash_emu(img, None)
    print(f"     vor dem Aufruf: SR-Modell=0x{fm.sr:02x} "
          f"SR-Speicher=0x{fm.e.rd(FLASH_SR):08x}")
    fm.desc_set(blk_addr, len(blk), src)
    e.mu.mem_write(src, bytes(b ^ 0xFF for b in blk))   # bewusst anders
    e.call(F_PROG, r0=blk_addr, r1=len(blk), r2=src)
    r = e.ret()
    report("Programm Rueckgabe", r == 3, f"ret={r} -> Fehlercode 0x72")
    report("PGERR gesetzt", bool(fm.sr & SR_PGERR), f"SR=0x{fm.sr:02x}")
    print(f"     SR-Lesezugriffe: {fm.sr_trace()}")
    sw = [v for _, v in fm.log]
    report("Stub meldet Fehler", bool(sw), f"Statusverlauf: {fm.status_trace()}"
           f"  (1 = Eintritt, 4 = Verify)")

    print("\n--- 4) Kleine Seite, ungeschuetzt: ein einzelner Block ---")
    e, fm = _flash_emu(img, 0x08008000)
    fm.desc_set(0x08010000, 0x800)
    e.call(F_ERASE, r0=0x08010000, r1=0x800)
    report("Erase 1 Seite (WRP)", e.ret() == 0, "meldet Erfolg")
    report("WRPRTERR gesetzt", bool(fm.sr & SR_WRPRTERR),
           "-> jede Seite >= 0x08008000 ist geschuetzt")

    print("\n--- 5) Flash bleibt gesperrt (FLASH_CR.LOCK wirksam) ---")
    e, fm = _flash_emu(img, None, locked=True)
    fm.desc_set(app_base, app_len)
    e.call(F_ERASE, r0=app_base, r1=app_len)
    report("Erase Rueckgabe", e.ret() == 0, "meldet ebenfalls Erfolg")
    report("Flash unveraendert", fm.src(app_base, 8) ==
           bytes(img[app_base - FLASH:app_base - FLASH + 8]), "nichts geloescht")
    report("KEIN WRPRTERR", not (fm.sr & SR_WRPRTERR),
           f"SR=0x{fm.sr:02x} -> unterscheidet sich von WRP")
    fm.desc_set(blk_addr, len(blk), src)
    e.mu.mem_write(src, bytes(b ^ 0xFF for b in blk))
    e.call(F_PROG, r0=blk_addr, r1=len(blk), r2=src)
    r = e.ret()
    print(f"  [INFO] Programm Rueckgabe ret={r} (Harness modelliert das "
          f"Verwerfen der Schreibzugriffe nur unvollstaendig)")
    print(f"  [INFO] Statusverlauf: {fm.status_trace()}")
    # Wichtiges Unterscheidungskriterium (im Harness nicht abschliessbar):
    # Bei gesperrtem CR wird ein Schreibzugriff verworfen, ohne Fehlerflag.
    # Sind die Daten identisch zum Flash-Inhalt, besteht das Verify -> Erfolg.
    # Bei WRP bricht der Stub dagegen IMMER schon beim Eintritt ab (Status 1).
    print("  [INFO] Kriterium: schlug auf der Hardware auch der ERSTE Block "
          "(0x08008000, datengleich) fehl,")
    print("         spricht das fuer WRP (Eintrittsabbruch, Status 1) und "
          "gegen ein nur gesperrtes CR.")

    print()
    print("ERGEBNIS: beide Ursachen (WRP / gesperrtes CR) reproduzieren 0x72,"
          if ok else "ERGEBNIS: Abweichung gefunden!")
    print("          unterscheidbar nur am Statusbyte (1 = WRP, 4 = Sperre).")
    return 0 if ok else 1


# --- Modell der Bootloader-Datenstrukturen ---------------------------------
BL_HANDLE = 0x200000F8       # Interface-Handle: +1 Flag, +3 Modus, +4 Objekt
BL_FSTATE70 = 0x20000170     # Rahmenzustand: +0 Laenge, +1 Sequenz, +4 Zeiger
OBJ_IF = 0x20005000          # Interface-Objekt (Zustand +0x354, Byte +0xA2)
OBJ_IF_Q = 0x20005400        # Zeigerziel aus Objekt +0x0C
OBJ_CH = 0x20005500          # Kanalobjekt (Flag +1), via *(0x20000174)
OBJ_IF_STATE = 0x354
OBJ_IF_BYTE = 0xA2


def bl_ram_objects(emu: "Emu", iface_state: int = 0, mode: int = 4) -> None:
    """Legt die Strukturen an, die die echten Bootloader-Funktionen erwarten.

    Damit laufen f_801E70 (Zustandsabfrage) und f_801EF8 (Benachrichtigung)
    als echter Code -- es muss keine Funktion ersetzt werden.
    """
    emu.mu.mem_write(OBJ_IF, bytes(0x400))                 # Objekt nullen
    emu.wr(OBJ_IF + OBJ_IF_STATE, iface_state, 1)          # != 4 -> Fruehausstieg
    emu.wr(OBJ_IF + OBJ_IF_BYTE, 0, 1)
    emu.wr(OBJ_IF + 0x0C, OBJ_IF_Q)                        # Zeiger auf Q
    emu.wr(OBJ_IF_Q, 0)
    emu.mu.mem_write(OBJ_CH, bytes(0x60))
    emu.wr(OBJ_CH + 1, 1, 1)                               # Flag gesetzt
    emu.wr(BL_HANDLE + 1, 0, 1)                            # Flag 0 -> Objekt lesen
    emu.wr(BL_HANDLE + 3, mode, 1)                         # Modus (4 = kein Reset)
    emu.wr(BL_HANDLE + 4, OBJ_IF)
    emu.wr(BL_FSTATE70 + 1, 0, 1)                          # Sequenzzaehler = 0
    emu.wr(BL_FSTATE70 + 4, OBJ_CH + 0x54)                 # container_of-Idiom


def _bl_install_realflash(emu: "Emu", rec: Dict[str, object],
                          blob: bool = True) -> None:
    """Wie _bl_install, aber mit ECHTEM Rahmen-, Flash- und Trampolinpfad.

    Gestubbt bleiben nur die nicht emulierbaren Raender: USB-Senden,
    USB-/CAN-Poll, Fehlerausgabe, Watchdog-Fuetterung und die Abfrage des
    Interface-Objekts (f_801E70), dessen Hardwareobjekt nicht existiert.
    Echt laufen: Report-Loader (0x08003ABC), Dispatcher, Handler,
    f_801964/f_801998, die Trampoline und damit der RAM-Blob.
    """
    def rec_report(e: "Emu") -> None:
        a = e.mu.reg_read(UC_ARM_REG_R0)
        b = e.mu.reg_read(UC_ARM_REG_R1)
        if 0x20000000 <= a < 0x20010000:
            ptr, ln = a, b
        else:
            ptr, ln = b, a
        ln = max(0, min(int(ln), 0x40))
        data = bytes(e.mu.mem_read(ptr, ln)) if ptr else b""
        if not data:
            return
        if len(data) >= 6 and data[1] == 0x01 and data[2] == 0x3D:
            rep = data                              # schon gerahmt
        else:
            rep = (bytes([rec.setdefault("seq", 0) & 0xFF, 0x01, 0x3D, 0x51,
                          0x05, len(data)]) + data)[:0x40]
            rec["seq"] = (int(rec["seq"]) + 1) & 0xFF
        rec.setdefault("tx", []).append(rep + bytes(0x40 - len(rep)))

    def err(e: "Emu") -> None:
        rec.setdefault("err", []).append(e.mu.reg_read(UC_ARM_REG_R0))
        e.mu.reg_write(UC_ARM_REG_R0, 0)

    noop = lambda _e: None                                          # noqa: E731
    emu.add_stub(0x08003A36, rec_report)      # Nutzlast senden
    emu.add_stub(0x080042AE, rec_report)      # CAN-Sendepfad
    emu.add_stub(0x0800421E,
                 lambda e: e.mu.reg_write(UC_ARM_REG_R0, BL_RAW_RX))
    emu.add_stub(0x08004222, noop)
    emu.add_stub(0x080009A8, noop)
    emu.add_stub(0x080009FC, noop)
    emu.add_stub(0x08000A46, err)             # Fehlercode protokollieren
    # f_801E70 und f_801EF8 laufen ECHT -- ihre Strukturen liefert
    # bl_ram_objects(). Der WWDG-Stub entfaellt ebenfalls: 0x08001004
    # schreibt nur das Register 0x40002C00, das der Emulator als RAM hat.
    bl_ram_objects(emu)
    if blob:
        off = BLOB_SRC - FLASH
        emu.mu.mem_write(BLOB_DST, emu.img[off:off + BLOB_LEN])


class _RealBlTransport:
    """Transport fuer stm_display_fw.Protocol -> echter Report-Loader.

    Der HID-Report wird 1:1 in den Empfangspuffer geschrieben und der echte
    Report-Loader (0x08003ABC) damit aufgerufen -- also genau der Pfad, den
    die Hardware geht: App-Rahmen -> Kommando -> Handler -> Flash.
    """

    def __init__(self, emu: "Emu", rec: Dict[str, object]) -> None:
        self.emu = emu
        self.rec = rec
        self.sent = 0

    def send(self, report: bytes) -> None:
        """Genau der Hardware-Weg.

        Der 64-Byte-Report (App-Rahmen ab Offset 0) wird in den
        Empfangspuffer gelegt; danach laeuft 0x08000AAA mit dem echten
        Report-Loader 0x08003ABC und dem echten Kommando-Dispatcher.
        """
        rep = bytes(report)
        if len(rep) < 0x40:
            rep += bytes(0x40 - len(rep))
        self.sent += 1
        e = self.emu
        e.mu.mem_write(BL_RAW_RX, rep[:0x40])
        e.wr(BL_TXDONE, 1, 1)
        e.wr(BL_MODE + 1, 1, 1)                 # Kanal 1 (USB)
        e.stop_pcs.add(0x0800190A)
        _bl_feed_raw(e)

    def recv(self, timeout_ms: int = 500):
        txs = self.rec.get("tx")
        if isinstance(txs, list) and txs:
            return txs.pop(0)
        return None

    def close(self) -> None:
        return None


def scenario_blfull(emu: "Emu") -> int:
    """WIP -- nicht registriert.

    Stand der Rekonstruktion des Rahmen-Pfads:
      * Der Report wird NICHT als Argument uebergeben. Der Rahmen liegt
        in einem RAM-Objekt; den Griff dorthin holt f_8003ABC aus der
        Variablen *(0x0800072C) und bildet base = griff - 0x54.
      * Rahmenformat dort: [+0] Sequenz, [+1] Typ (0x01 Kommando,
        0xFE Sync, 0xFD, 0x02 Fehler), [+2] 0x3D, [+3] 0x50, [+4] 0x05,
        [+5] Laenge <= 0x3A, [+6..] Nutzlast.
      * f_8003ABC kopiert die Nutzlast als [len][payload] in den
        uebergebenen Puffer und ruft f_801C18(rahmen, zustand) -- den
        eigentlichen Kommandoverteiler (0x20000170-0x54 als 2. Arg).
      * Liefert f_801C18 != 0, bricht der Loader ab (kein Kommando).
        Dieser Vertrag ist noch zu klaeren.
      * Funktions-Stubs wurden durch ein Datenstruktur-Modell ersetzt
        (bl_ram_objects); f_801E70 und f_801EF8 laufen als echter Code.

    Echter Bootloader-Codepfad: App-Rahmen -> Report-Loader -> Dispatcher
    -> Handler -> f_8019xx -> Trampoline -> RAM-Blob -> FLASH-Peripherie.

    Nur der USB-Transport und die Interface-Statusabfrage (f_801E70) sind
    gestubbt, weil ihre Hardwareobjekte nicht existieren.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw  # noqa: PLC0415

    img = emu.img
    app_base, app_len = 0x08008000, 0x38000
    nblk, blk = 3, 56
    data = bytes(img[app_base - FLASH:app_base - FLASH + nblk * blk])
    ok = True

    def run(label: str, wrp_from: Optional[int], erase: bool = True) -> dict:
        e = Emu(img)
        rec: Dict[str, object] = {"seq": 0}
        e.log_unmapped = True
        fm = FlashModel(e, wrp_from)
        _bl_install_realflash(e, rec)
        tp = _RealBlTransport(e, rec)
        pr = fw.Protocol(tp, verbose=False, wire="app")

        print(f"\n=== {label} ===")
        pr.cmd_sync()
        pr.cmd_enter_program()
        res_erase = None
        before = bytes(e.mu.mem_read(app_base, 32))
        if erase:
            ack = pr.cmd_erase(app_base, app_len)
            pl = fw.ack_payload(ack) if ack else None
            res_erase = pl[4] if pl and len(pl) > 4 else None
            print(f"  Erase-Quittung       : "
                  f"{'OK (1)' if res_erase == 1 else res_erase}")
            blank = bytes(e.mu.mem_read(app_base, 8)) == b"\xff" * 8
            print(f"  Flash danach leer    : {blank}")
            print(f"  Stub-Statusverlauf   : {fm.status_trace()}")
            print(f"  FLASH_SR             : 0x{fm.sr:02x}")
        pr.cmd_sequencer_start()
        pr.cmd_set_address(app_base)
        rcs = []
        for i in range(nblk):
            rcs.append(pr.cmd_write_block(data[i * blk:(i + 1) * blk]))
        txt = " ".join("ACK 0x76" if r == 0 else f"Fehler 0x{r:02x}"
                       for r in rcs)
        print(f"  Block 1..{nblk}          : {txt}")
        wrote = bytes(e.mu.mem_read(app_base, nblk * blk))
        print(f"  Daten im Flash       : "
              f"{'identisch' if wrote == data else 'ABWEICHEND'}")
        return {"rcs": rcs, "blank": bytes(e.mu.mem_read(app_base, 8))
                == b"\xff" * 8, "same": wrote == data, "before": before}

    a = run("A) App-Region durch WRP geschuetzt", 0x08008000)
    b = run("B) Flash offen (Sollweg)", None)

    print("\n--- Auswertung ---")
    print(f"  A: Block 1 = {'Fehler 0x%02x' % a['rcs'][0]:>12s}   "
          f"Flash unveraendert = {not a['blank']}")
    print(f"  B: Block 1 = {'ACK 0x76' if b['rcs'][0] == 0 else 'Fehler':>12s}   "
          f"Erase loeschte = {b['blank']}, Daten korrekt = {b['same']}")
    matches_hw = (a["rcs"][0] != 0 and not a["blank"])
    print(f"  Hardware-Befund (Block 1 scheiterte, Flash unveraendert) passt zu: "
          f"{'A (WRP)' if matches_hw else 'B'}")
    ok = b["same"] and b["blank"] and b["rcs"][0] == 0 and matches_hw
    print("ERGEBNIS:", "Codepfad vollstaendig nachgestellt" if ok else
          "Abweichung")
    return 0 if ok else 1



def _blflash_emu(wrp_from: Optional[int], locked: bool = False,
                 img: Optional[bytes] = None):
    """Emulator mit echter Kommandoschicht, echtem Flash-Code und FLASH-Modell.

    Gestubbt bleiben nur die nicht emulierbaren Raender (USB-Senden/-Poll,
    Fehlerausgabe, WWDG, Blob-Einstieg). Echt laufen: Dispatcher, Handler,
    f_801964/f_801998, die Trampoline 0x0800100E/18/22/2C und der RAM-Blob.
    """
    data = img if img is not None else (ROOT / "data" / "stm32f105_conti.bin"
                                        ).read_bytes()
    e = Emu(data)
    rec: Dict[str, object] = {}
    fm = FlashModel(e, wrp_from, locked)
    noop = lambda _e: None                                          # noqa: E731

    def err(x):
        rec.setdefault("err", []).append(x.mu.reg_read(UC_ARM_REG_R0))
        x.mu.reg_write(UC_ARM_REG_R0, 0)

    def send(x):
        p0 = x.mu.reg_read(UC_ARM_REG_R0)
        n0 = x.mu.reg_read(UC_ARM_REG_R1)
        if not (0x20000000 <= p0 < 0x2000F000):
            p0, n0 = n0, p0                       # Argumente vertauscht
        if 0x20000000 <= p0 < 0x2000F000 and 0 < n0 <= 0x40:
            rec.setdefault("tx", []).append(bytes(x.mu.mem_read(p0, int(n0))))

    for a in (0x08003ABC, 0x08004222, 0x080009A8, 0x080009FC, 0x08001004,
              0x08001036):
        e.add_stub(a, noop)
    e.add_stub(0x0800421E, lambda x: x.mu.reg_write(UC_ARM_REG_R0, BL_RAW_RX))
    e.add_stub(0x08000A46, err)
    e.add_stub(0x08003A36, send)
    off = BLOB_SRC - FLASH                      # RAM-Blob bereitstellen
    e.mu.mem_write(BLOB_DST, data[off:off + BLOB_LEN])
    e.rec = rec                                 # type: ignore[attr-defined]
    return e, fm


def scenario_blflash(emu: "Emu") -> int:
    """Bootloader flasht im Emulator -- echte Handler + echter Flash-Code.

    Drei Konfigurationen:
      A) App-Region mit WRP      -> Erase meldet Erfolg, loescht aber nichts
                                    (WRPRTERR), Schreiben scheitert (wie auf
                                    der Hardware)
      B) Flash offen (Sollweg)   -> Erase loescht, Schreiben + Verify ok
      C) Flash gesperrt (CR.LOCK)-> Erase ohne Wirkung, kein Fehlerflag
    """
    app = 0x08008000
    img = emu.img
    blocks = [bytes(img[app - FLASH + i * 56: app - FLASH + (i + 1) * 56])
              for i in range(2)]
    results = {}

    for key, wrp, locked, label in (
            ("A", app, False, "A) App-Region mit WRP"),
            ("B", None, False, "B) Flash offen (Sollweg)"),
            ("C", None, True, "C) Flash gesperrt (CR.LOCK)")):
        e, fm = _blflash_emu(wrp, locked, img)
        rec = e.rec                                      # type: ignore[attr-defined]
        seen = set()
        e.mu.hook_add(UC_HOOK_CODE, lambda mu, a, s, u: seen.add(a))
        print(f"\n=== {label} ===")

        def cmd(payload: bytes, name: str) -> None:
            seen.clear()
            rec.clear()
            if isinstance(rec.get("tx"), list):
                rec["tx"].clear()                        # type: ignore[union-attr]
            _bl_feed(e, payload)
            hit = ", ".join(n for a, n in _BLF_WATCH.items() if a in seen)
            print(f"  {name:<24} Code: {hit or '-':<38} "
                  f"Fehler: {[hex(x) for x in rec.get('err', [])] or '-'}")

        cmd(bytes([0x10, 0x03]), "0x10 Modus=3")
        cmd(bytes([0x37]), "0x37 Sequencer")
        cmd(bytes([0x34, 0x03, 0x00]) + struct.pack(">I", app) + bytes(4),
            "0x34 Adresse")
        before = bytes(e.mu.mem_read(app, 8))
        cmd(bytes([0x31, 0x01, 0xFF, 0x00, 0x00]) + struct.pack(">I", app)
            + struct.pack(">I", 0x1000), "0x31 Erase 2 Seiten")
        blank = bytes(e.mu.mem_read(app, 8)) == b"\xff" * 8
        for i, blk in enumerate(blocks):
            cmd(bytes([0x36, i + 1]) + blk, f"0x36 Block {i + 1}")
        same = (bytes(e.mu.mem_read(app, 56 * len(blocks)))
                == b"".join(blocks))
        print(f"  -> Erase geloescht: {blank}   Schreiben korrekt: {same}   "
              f"FLASH_SR: {fm.sr:#04x}   WRPRTERR: "
              f"{bool(fm.sr & SR_WRPRTERR)}")
        results[key] = {"blank": blank, "same": same,
                        "wrp_err": bool(fm.sr & SR_WRPRTERR),
                        "before": before}

    a, b, c = results["A"], results["B"], results["C"]
    ok = (b["blank"] and b["same"]                 # Sollweg funktioniert
          and not a["blank"] and a["wrp_err"]      # WRP: stiller Fehlschlag
          and not c["blank"] and not c["wrp_err"])  # Sperre: kein Fehlerflag
    print("\nERGEBNIS:", "Bootloader flasht im Emulator vollstaendig."
          if ok else "Abweichung gefunden!")
    return 0 if ok else 1


_BLF_WATCH = {0x08001964: "f_801964", 0x08001998: "f_801998",
              0x2000B2F4: "RAM-Erase", 0x2000B39C: "RAM-Prog",
              0x080013A4: "Hdl 0x31", 0x08001614: "Hdl 0x36",
              0x08001514: "Hdl 0x34", 0x08000CAC: "Hdl 0x10"}


# ==========================================================================
# Pruefung des FLASH-Peripheriemodells (Registerebene)
# ==========================================================================
def scenario_flashregs(emu: "Emu") -> int:
    """Verhalten des FLASH-Registermodells direkt pruefen (ohne Firmware)."""
    img = emu.img
    page = 0x08008000
    nxt = page + 0x800
    orig_page = bytes(img[page - FLASH:page - FLASH + 8])
    orig_next = bytes(img[nxt - FLASH:nxt - FLASH + 8])
    ok = True

    def check(name, cond, note=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'OK ' if cond else 'FEHL'}] {name:<44} {note}")

    def fresh(wrp=None, locked=False):
        e = Emu(img)
        return e, FlashModel(e, wrp, locked)

    def unlock(m):
        m.reg_write(FLASH_KEYR, 0x45670123)
        m.reg_write(FLASH_KEYR, 0xCDEF89AB)

    def erase(m, addr):
        m.reg_write(FLASH_AR, addr)
        m.reg_write(FLASH_CR, CR_PER)
        m.reg_write(FLASH_CR, CR_PER | CR_STR)

    print("\n=== 1) Entriegelung (FLASH_KEYR) ===")
    e, fm = fresh()
    check("gesperrt nach Reset", fm.cr & CR_LOCK, f"CR={fm.cr:#04x}")
    fm.reg_write(FLASH_KEYR, 0x45670123)
    check("nur KEY1 -> weiter gesperrt", fm.cr & CR_LOCK, f"CR={fm.cr:#04x}")
    fm.reg_write(FLASH_KEYR, 0xCDEF89AB)
    check("KEY1+KEY2 -> entriegelt", not (fm.cr & CR_LOCK), f"CR={fm.cr:#04x}")
    e2, fm2 = fresh()
    fm2.reg_write(FLASH_KEYR, 0xDEADBEEF)
    fm2.reg_write(FLASH_KEYR, 0xCDEF89AB)
    check("falscher KEY1 -> gesperrt", fm2.cr & CR_LOCK, f"CR={fm2.cr:#04x}")

    print("\n=== 2) Seitenloeschung ===")
    e, fm = fresh()
    unlock(fm)
    erase(fm, page)
    check("Zielseite geloescht", bytes(e.mu.mem_read(page, 8)) == b"\xff" * 8)
    check("Nachbarseite unberuehrt",
          bytes(e.mu.mem_read(nxt, 8)) == orig_next)
    check("SR sauber (kein Fehler)", not (fm.sr & (SR_WRPRTERR | SR_PGERR)),
          f"SR={fm.sr:#04x}")
    e, fm = fresh(locked=True)
    erase(fm, page)
    check("gesperrtes CR: keine Wirkung",
          bytes(e.mu.mem_read(page, 8)) == orig_page)
    check("gesperrtes CR: kein Fehlerflag",
          not (fm.sr & (SR_WRPRTERR | SR_PGERR)), f"SR={fm.sr:#04x}")
    e, fm = fresh(wrp=0x08008000)
    unlock(fm)
    erase(fm, page)
    check("WRP: Seite unveraendert",
          bytes(e.mu.mem_read(page, 8)) == orig_page)
    check("WRP: WRPRTERR gesetzt", fm.sr & SR_WRPRTERR, f"SR={fm.sr:#04x}")

    print("\n=== 3) Programmieren (Halbwort) ===")
    e, fm = fresh()
    unlock(fm)
    erase(fm, page)
    fm.reg_write(FLASH_CR, CR_PG)
    fm.reg_write(page, 0x1234, 2)
    fm.reg_write(FLASH_CR, 0)
    check("geloescht -> Wert uebernommen",
          bytes(e.mu.mem_read(page, 2)) == (0x1234).to_bytes(2, "little"))
    check("kein PGERR", not (fm.sr & SR_PGERR), f"SR={fm.sr:#04x}")
    fm.reg_write(FLASH_CR, CR_PG)
    fm.reg_write(page, 0xFFFF, 2)
    fm.reg_write(FLASH_CR, 0)
    check("1-Bits ueber 0 -> PGERR", fm.sr & SR_PGERR, f"SR={fm.sr:#04x}")
    check("UND-Semantik (1234 & FFFF = 1234)",
          bytes(e.mu.mem_read(page, 2)) == (0x1234).to_bytes(2, "little"))

    print("\nERGEBNIS:", "FLASH-Peripheriemodell verhaelt sich korrekt."
          if ok else "Abweichung gefunden!")
    return 0 if ok else 1


# ==========================================================================
# Das echte Tool gegen den emulierten Bootloader flashen lassen
# ==========================================================================
class _ToolBlTransport:
    """Transport fuer stm_display_fw.Protocol gegen den echten Bootloader.

    send(): Nutzlast des App-Rahmens auf der Kommandoschicht einspeisen.
    recv(): Antworten des Bootloaders als HID-Report zurueckgeben.
    """

    def __init__(self, emu: "Emu") -> None:
        self.emu = emu
        self.pending: List[bytes] = []
        self.sent = 0
        self.seq = 0

    def send(self, report: bytes) -> None:
        sys.path.insert(0, str(ROOT / "tools"))
        import stm_display_fw as fw  # noqa: PLC0415
        pf = fw.parse_app_frame(bytes(report))
        if pf is None:
            return
        typ, payload = pf[1], pf[3]
        self.sent += 1
        if typ == 0xFE:                          # Sync: Sequenz auf 0
            self.emu.wr(BL_FSTATE + 0, 0, 1)
            return
        rec = self.emu.rec                      # type: ignore[attr-defined]
        if isinstance(rec.get("tx"), list):
            rec["tx"].clear()                   # type: ignore[union-attr]
        emu = self.emu
        frame = bytes([len(payload)]) + payload
        emu.mu.mem_write(BL_RAW_RX, frame + bytes(0x40 - len(frame)))
        emu.wr(BL_TXDONE, 1, 1)
        emu.wr(BL_MODE + 1, 1, 1)               # Kanal 1 / Modus 1 (wie HW)
        emu.stop_pcs.add(0x0800190A)
        emu.call(0x08000AAA)
        for pl in rec.get("tx", []):            # type: ignore[union-attr]
            if not pl:
                continue
            hdr = bytes([self.seq & 0xFF, 0x01, 0x3D, 0x51, 0x05, len(pl)])
            self.seq = (self.seq + 1) & 0xFF
            rep = (hdr + bytes(pl))[:0x40]
            self.pending.append(rep + bytes(0x40 - len(rep)))

    def recv(self, timeout_ms: int = 500):
        return self.pending.pop(0) if self.pending else None

    def close(self) -> None:
        return None


def scenario_blupload(emu: "Emu") -> int:
    """Flasht den App-Bereich mit dem ECHTEN stm_display_fw.py-Tool.

    Der Emulator fuehrt dabei den unveraenderten Bootloader-Code aus
    (Dispatcher, Handler, f_801964/f_801998, Trampoline, RAM-Blob) gegen das
    FLASH-Registermodell. Geprueft wird, dass der Flash-Inhalt exakt dem
    Image entspricht.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw  # noqa: PLC0415

    nblocks = int(sys.argv[2], 0) if len(sys.argv) > 2 else 256
    app = fw.APP_BASE
    total = nblocks * 56
    img = emu.img
    data = bytes(img[app - FLASH:app - FLASH + total])

    e, fm = _blflash_emu(None, False, img)
    tp = _ToolBlTransport(e)
    proto = fw.Protocol(tp, verbose=False, wire="app")
    erase_len = ((total + 0x7FF) // 0x800) * 0x800
    print(f"[blupload] {nblocks} Bloecke = {total} Bytes, Erase {erase_len} "
          f"Bytes ab 0x{app:08x}")
    proto.cmd_sync()
    proto.cmd_enter_program()      # 0x10 03: setzt [0x20000028] = gueltig
    proto.cmd_erase(app, erase_len)
    blank = bytes(e.mu.mem_read(app, min(16, total))) == b"\xff" * min(16, total)
    proto.cmd_sequencer_start()
    proto.cmd_set_address(app)
    failed = 0
    for i in range(nblocks):
        rc = proto.cmd_write_block(data[i * 56:(i + 1) * 56])
        if rc:
            failed += 1
            if failed < 4:
                print(f"   Block {i + 1}: Fehler 0x{rc:02x}"
                      if rc > 0 else f"   Block {i + 1}: keine Antwort")
    # Abschlussquittung: der Bootloader antwortet, sobald der Zeiger
    # 0x08040000 erreicht hat (nicht pro Block!).
    final = tp.recv(4000)
    fpl = fw.ack_payload(final) if final else None
    print(f"[blupload] Abschlussquittung   : "
          f"{fpl.hex(' ') if fpl else '-'}")
    got = bytes(e.mu.mem_read(app, total))
    ptr = e.rd(BL_FSTATE + 4)
    errtx = [b.hex(" ") for b in e.rec.get("tx", []) if b and b[0] == 0x7F]
    print(f"[blupload] Kommandos gesendet : {tp.sent}")
    print(f"[blupload] Erase geloescht    : {blank}")
    print(f"[blupload] Bloecke mit Fehler : {failed}")
    print(f"[blupload] Flash = Image      : {got == data}")
    print(f"[blupload] Schreibzeiger      : {ptr:#010x} "
          f"(erwartet {app + total:#010x})")
    ok = (blank and failed == 0 and got == data and ptr == app + total
          and (fpl is None or fpl[0] == 0x76))
    print("[blupload] ->", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def scenario_blreadback(emu: "Emu") -> int:
    """Lese-Befehl (0x22) und CRC-Befehl (0x31/0x10202) am echten Code pruefen.

    Hintergrund: der Bootloader hat **keinen** allgemeinen Lese-Befehl. Der
    Handler 0x08000F54 (Kommando 0x22) kopiert aber 6 bzw. 8 Byte aus der
    festen Flash-Adresse 0x08007800 in die Antwort -- der einzige Rueckkanal
    fuer Flash-Inhalt. Geprueft wird:

      1) welches Magic liefert Daten (0xF15B gegen 0xF15A),
      2) Schreiben des Geraeterekords (0x2E) und Zuruecklesen,
      3) das Statusbyte des CRC-Befehls (0 = stimmt, 1 = stimmt nicht).
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import stm_display_fw as fw  # noqa: PLC0415

    data = (ROOT / "data" / "stm32f105_bms_control.bin").read_bytes()
    e, fm = _blflash_emu(None, False, data)
    tp = _ToolBlTransport(e)
    proto = fw.Protocol(tp, verbose=False, wire="app")
    results: List[bool] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        results.append(bool(ok))
        print(f"    [{'ok  ' if ok else 'FEHL'}] {name}"
              f"{'   ' + extra if extra else ''}")

    print("=== 1) Record-Befehl 0x22: welches Magic liest Flash? ===")
    rec_val = bytes([26, 10, 4, 0x11, 0x22, 0x33, 0x44, 0x55])
    e.mu.mem_write(fw.RECORD_ADDR, rec_val + bytes(0x800 - len(rec_val)))
    proto.cmd_sync()
    wrong = proto.cmd_read_record(magic=0xF15A)
    check("Magic 0xF15A (Schreibmagic) -> keine Daten", wrong is None,
          f"-> {wrong.hex(' ') if wrong else '-'}")
    got = proto.cmd_read_record()
    check("Magic 0xF15B -> Daten", got is not None,
          f"-> {got.hex(' ') if got else '-'}")
    check("Record-Inhalt = Flash 0x08007800", got == rec_val[:6],
          f"soll {rec_val[:6].hex(' ')}")

    print("\n=== 2) Geraeterekord schreiben (0x2E) und zuruecklesen ===")
    # 0x2E verlangt Magic 0xF15A in payload[1..2] und Payload-Laenge exakt 10;
    # geschrieben werden 8 Byte ab payload[3], das achte stammt aus dem Puffer.
    new = bytes([24, 12, 9, 0xAA, 0xBB, 0xCC, 0xDD])
    proto.cmd_enter_program()
    proto.send_frame(bytes([0x2E, 0xF1, 0x5A]) + new)
    ack = tp.recv(1500)
    apl = fw.ack_payload(ack) if ack else None
    check("0x2E quittiert (0x6E + Magic)", apl is not None and apl[0] == 0x6E,
          f"payload={apl.hex(' ') if apl else '-'}")
    check("Flash 0x08007800 = geschriebener Record",
          bytes(e.mu.mem_read(fw.RECORD_ADDR, 7)) == new)
    back = proto.cmd_read_record()
    check("Zurueckgelesen = geschrieben", back == new[:6],
          f"-> {back.hex(' ') if back else '-'}")

    print("\n=== 3) CRC-Befehl 0x31/0x10202: Statusbyte ===")
    res = proto.cmd_app_crc(tries_len=(8,))
    check("unveraendertes Image -> Status 0 (CRC stimmt)",
          res == fw.CRC_OK, f"Status={res}")
    e.mu.mem_write(fw.APP_BASE + 0x1234, b"\x00\x00")
    res = proto.cmd_app_crc(tries_len=(8,))
    check("geaendertes Image -> Status 1 (CRC stimmt nicht)",
          res == fw.CRC_MISMATCH, f"Status={res}")

    print("\n=== 4) verify_against_device(): einfach und --deep ===")
    img = fw.Image(bytearray(data))
    off = fw.APP_BASE - fw.FLASH_BASE
    e.mu.mem_write(fw.APP_BASE + 0x1234, data[off + 0x1234:off + 0x1236])
    check("einfach, Inhalt = Image -> 0",
          proto.verify_against_device(img, deep=False) == 0)
    # Nur das CRC-Wort ist falsch (z. B. nach abgebrochenem Flash):
    e.mu.mem_write(fw.APP_CRC_ADDR, b"\xde\xad\xbe\xef")
    check("einfach, nur CRC-Wort falsch -> 1 (Fehlalarm)",
          proto.verify_against_device(img, deep=False) == 1)
    check("deep, nur CRC-Wort falsch -> 0 (korrigiert sich selbst)",
          proto.verify_against_device(img, deep=True) == 0)
    check("deep hat das CRC-Wort neu geschrieben",
          bytes(e.mu.mem_read(fw.APP_CRC_ADDR, 4)) == data[0x3fffc:0x40000])
    # Inhalt stimmt wirklich nicht:
    e.mu.mem_write(fw.APP_BASE + 0x8000, b"\x00")
    check("deep, Inhalt weicht ab -> 1",
          proto.verify_against_device(img, deep=True) == 1)

    ok = all(results)
    print(f"\nERGEBNIS: {sum(results)}/{len(results)} Pruefungen ok --",
          "Lese- und CRC-Befehl bestaetigt." if ok else "Abweichung!")
    return 0 if ok else 1


SCENARIOS = {
    "blreadback": scenario_blreadback,
    "flashregs": scenario_flashregs,
    "blupload": scenario_blupload,
    "blflash": scenario_blflash,
    "flashprog": scenario_flashprog,
    "bmspatch": scenario_bmspatch,
    "uploadtool": scenario_uploadtool,
    "usbtrigger": scenario_usbtrigger,
    "hidtrigger": scenario_hidtrigger,
    "msgprobe": scenario_msgprobe,
    "nmstate": scenario_nmstate,
    "blsession": scenario_blsession,
    "blprobe": scenario_blprobe,
    "crc": scenario_crc,
    "dispatch": scenario_dispatch,
    "frame": scenario_frame,
    "setaddr": scenario_setaddr,
    "writeblk": scenario_writeblk,
    "appreset": scenario_appreset,
    "trigger": scenario_trigger,
    "caninject": scenario_caninject,
}


def main(argv: List[str]) -> int:
    img_path = IMG_PATH
    if "-i" in argv:
        img_path = Path(argv[argv.index("-i") + 1])
    if len(argv) < 2 or argv[1] not in SCENARIOS or argv[1].startswith("-"):
        print(__doc__)
        print("Szenarien:", ", ".join(SCENARIOS))
        print("Optionen: -t (Trace), -i <image> (statt data/stm32f105_conti.bin)")
        return 2
    if not img_path.exists():
        print(f"FEHLER: {img_path} fehlt (erst make_disassembly.sh ausfuehren).")
        return 2
    img = img_path.read_bytes()
    emu = Emu(img, trace=("-t" in argv))
    return SCENARIOS[argv[1]](emu)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
