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
        emu = self.emu
        # Das Tool baut bereits [len][payload] -> passt genau in BL_RAW_RX
        emu.mu.mem_write(BL_RAW_RX, rep)
        emu.wr(BL_TXDONE, 1, 1)
        emu.wr(BL_MODE + 1, 0, 1)              # Kanal 1 (USB)
        emu.stop_pcs.add(0x0800190A)
        emu.call(0x08000AAA)

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


SCENARIOS = {
    "uploadtool": scenario_uploadtool,
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
    if len(argv) < 2 or argv[1] not in SCENARIOS:
        print(__doc__)
        print("Szenarien:", ", ".join(SCENARIOS))
        return 2
    if not IMG_PATH.exists():
        print(f"FEHLER: {IMG_PATH} fehlt (erst make_disassembly.sh ausfuehren).")
        return 2
    img = IMG_PATH.read_bytes()
    emu = Emu(img, trace=("-t" in argv))
    return SCENARIOS[argv[1]](emu)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
