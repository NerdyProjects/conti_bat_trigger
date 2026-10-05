# STM "Display"-Firmware (STM32F105) – Firmware-Update-Mechanismus

Quelle: `stm32f105_conti.hex` (Cortex-M3, 256 KB Flash @ `0x08000000`).
Disassembly: `stm32f105_conti.dis` (siehe `make_disassembly.sh`).

## Ergebnis

**Ja, es gibt einen Firmware-Update-Mechanismus.** Der Dump enthält **zwei
unabhängig gelinkte Programme** (je eigene Vektor-Tabelle + C-Runtime):

| Region | Adresse | Rolle | USB-Identität |
|---|---|---|---|
| Bootloader | `0x08000000`–`0x08007FFF` (32 KB) | IAP-Updater, USB-HID | `CEBS Bootloader Mode` |
| Applikation | `0x08008000`–`0x0803FFFF` | eBike-Display | `Continental eBike System` |

CRC32-Absicherung: `CRC32(0x08008000..0x0803FFFB) = 0x561FEB04`,
gespeichert **Big-Endian** am Flash-Ende `0x0803FFFC`.
Nachgerechnet mit dem STM32-Hardware-CRC (Poly `0x4C11DB7`, Init `0xFFFFFFFF`)
→ **stimmt exakt überein**.

## Boot-Ablauf (Bootloader)

Vektor-Tabelle `0x08000000`: SP=`0x2000B248`, Reset=`0x08000269` → `0x08000150` (`__main`/Scatterload).

Entscheidung in `0x08001A92`:

```
r4 = (RCC_CSR.Reset-Quelle == SFTRST) ? 0 : 1     ; via 0x080019F0 -> 0x08001B7A
r0 = (CRC32(App) != Wert@0x0803FFFC) ? 1 : 0      ; via 0x08001A02 -> HW-CRC 0x08000924
if (r4 == 1 && r0 == 0)  -> 0x08001A3C   ; App starten
else                     -> 0x08001AAA   ; im USB-HID-Bootloader bleiben
```

* `0x08001A3C` = **Sprung in die App**: MSP ← `[0x08008000]`, Sprung nach
  `[0x08008004]` (= `0x080082FD`). Die App setzt danach selbst
  `SCB->VTOR = 0x08008000` (`0x08008334`, Literal `0x080083C8`).
* `0x08001B7A` liest `RCC_CSR` (`0x40021000+0x24`), Maske `0x50000000`:
  * nur **SFTRST** (Software-Reset) → Rückgabe 1 → **Bootloader-Modus erzwingen**
  * sonst (POR/PIN/WWDG) → Rückgabe 0/2 → App starten (wenn CRC ok);
    dabei werden die Reset-Flags via RMVF (`0x01000000`) gelöscht.
* `0x08001B66` = `NVIC_SystemReset` im Bootloader (`AIRCR 0xE000ED0C = 0x05FA0004`).

### Trigger für ein Update
Da nur der Bootloader den Flash beschreiben kann (die Unlock-Keys
`0x45670123`/`0xCDEF89AB` existieren **nur** in Region 1), wird ein Update so
eingeleitet:

1. Die App führt einen **Software-Reset** aus (`NVIC_SystemReset`,
   App-Funktion `0x08017FA2`). Aufrufer u. a. `0x08016E8E` (wenn Arg==2) und
   `0x08017380` (wenn Arg==1) – also ein Kommando-/Callback-Pfad.
2. Der Bootloader erkennt `SFTRST` und bleibt im USB-HID-Update-Modus.
3. Alternativ: CRC der App ist ungültig (unvollständiger Flash) → Bootloader bleibt.

## Bootloader-Update-Pfad (USB HID)

* USB-String-Deskriptoren des Bootloaders via `adr`:
  * `0x08001DF0` "CEBS Bootloader Mode"
  * `0x08001E08` "Continental", `0x08001E14` "000000000001"
  * `0x08001E24` "HID Config", `0x08001E30` "HID Interface"
  * Deskriptor-Builder-Funktionen: `0x08001C50`, `0x08001C62`, `0x08001C72`,
    `0x08001C82`, `0x08001C96`, `0x08001CA6`
* Haupt-Loop `0x08001ACC`: setzt Flag `[0x20000044]==1` voraus, verarbeitet
  empfangene HID-Reports, sammelt Bytes in Puffer `0x2000025C` (Literal
  `0x08001BA4`), und flusht alle **80 Bytes** über `0x0800174E`.
* `0x0800174E` → `0x08001998` (Bereichsprüfung, `0x08008000`..`0x0803FFFC`)
  → Aufruf des Flash-Treibers über **RAM-Stubs**:
  * `0x08001004` → `0x2000B263`, `0x0800100E` → `0x2000B275`,
    `0x08001022` → `0x2000B2A7`, `0x0800102C` → `0x2000B39D`,
    `0x08001036` → `0x2000B249`
  * D. h. der Flash-Treiber wird zur Laufzeit **nach SRAM kopiert und dort
    ausgeführt** (auf STM32F1 zwingend, da Flash während des Schreibens nicht
    lesbar ist).

### Flash-Treiber (Region 1)
* `0x08005134` – `FLASH->KEYR` unlock (`0x45670123`, `0xCDEF89AB`), RCC-Setup
* `0x08005166` – `FLASH->CR |= 0x80` → **LOCK** (re-lock)
* `0x08005180` – **FLASH_WaitForLastOperation** (BSY-Poll, Timeout `0xB0000`)
* `0x080051B4` – **Page-Erase** (`FLASH_CR`: `PER` + `STRT`, `FLASH_AR`)
* `0x0800525C` – **Halfword-Programmierung** (`FLASH_CR`: `PG`, `strh`)
* Literale: `0x0800530C`=RCC `0x40021000`, `0x08005318`=FLASH `0x40022000`

## Applikationsseite (Region 2, "Continental eBike System")

* Eigene USB-Deskriptoren (zur Laufzeit in RAM aufgebaut,
  Ziele `0x200001C1`/`0x200001DD`/`0x20005E00`), Strings:
  * `0x0800B4E8` "Continental eBike System"
  * `0x0800B504` "Continental", `0x0800B510` "000000000001"
  * `0x0800B520` "HID Config", `0x0800B52C` "HID Interface"
  * Builder: `0x0800B244`, `0x0800B264`, `0x0800B28E`, `0x0800B2AA`,
    `0x0800B2CA`, `0x0800B2F4`
* `NVIC_SystemReset` = `0x08017FA2` (Aufrufer `0x08016E8E`, `0x08017380`)
* Keine Flash-Unlock-Keys in Region 2 → App kann sich **nicht** selbst flashen.

## Fazit
Das Display wird über einen **USB-HID-Bootloader** aktualisiert
("CEBS Bootloader Mode"), der über einen Software-Reset der App (oder eine
ungültige App-CRC) gestartet wird. Ein Update über CAN ist im Dump **nicht**
erkennbar.
