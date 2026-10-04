# STM "Display" (STM32F105) – Bootloader / Upload-Protokoll

Alle Adressen beziehen sich auf `data/stm32f105_conti.bin`
(Basis `0x08000000`) bzw. `data/stm32f105_conti.dis`.
Legende: **[V]** = aus dem Disassembly verifiziert, **[R]** = rekonstruiert
(plausibel, aber noch nicht am Gerät bestätigt).

## 1. Flash-Layout
| Bereich | Adresse | Inhalt |
|---|---|---|
| Bootloader | `0x08000000`–`0x08007FFF` | USB-HID-Updater, Vektor-Tabelle, SP=`0x2000B248` |
| Applikation | `0x08008000`–`0x0803FFFB` | Display-App, Vektor-Tabelle `0x08008000`, SP=`0x2000C9E0` |
| CRC32(App) | `0x0803FFFC` | Big-Endian, STM32-HW-CRC (Poly `0x4C11DB7`) |

Verifiziert: `CRC32(0x08008000..0x0803FFFB) = 0x561FEB04`.

## 2. Boot-Entscheidung und Trigger [V]
`0x08001A92`:
```
r4 = (RCC_CSR.Resetquelle == SFTRST) ? 0 : 1      ; 0x080019F0 -> 0x08001B7A
r0 = (CRC32(App) != CRC@0x0803FFFC) ? 1 : 0       ; 0x08001A02 -> 0x08000924 (HW-CRC)
if (r4 == 1 && r0 == 0)  0x08001A3C   # App starten
else                     0x08001AAA   # USB-HID-Bootloader bleiben
```
* `0x08001B7A` liest `RCC_CSR` (`0x40021000+0x24`), Maske `0x50000000`:
  * **SFTRST** gesetzt → Bootloader-Modus (Rueckgabe 1)
  * sonst (POR/PIN/WWDG) → App starten, wenn CRC ok
* Sprung in die App `0x08001A3C`: `MSP = [0x08008000]`, `bx [0x08008004]`.
* `0x08001B66` = `NVIC_SystemReset` (`AIRCR=0x05FA0004`).

### Update-Trigger
1. **Software-Reset** (`NVIC_SystemReset`) → SFTRST → Bootloader-Modus.
   * aus der App heraus: App-Funktion `0x08017FA2`, Aufrufer `0x08016E8E`
     (Arg==2) und `0x08017380` (Arg==1).
   * **ohne App-Mitarbeit:** per SWD `mww 0xE000ED0C 0x05FA0004`
     (SYSRESETREQ) → ebenfalls SFTRST → Bootloader.
2. **App-CRC ungueltig** (unvollstaendiger Flash) → Bootloader bleibt.
3. POR/PIN/WWDG-Reset → App (wenn CRC ok).

## 3. USB
* Produktstrings: Bootloader = **"CEBS Bootloader Mode"**, App =
  **"Continental eBike System"**; Hersteller "Continental",
  Serial "000000000001", Interface-Strings "HID Config"/"HID Interface". **[V]**
* HID: **64-Byte Reports** (IN und OUT). **[V]** (`REPORT_SIZE = 0x40`)
* VID/PID liegen nicht als Konstante im Flash (Deskriptoren werden zur
  Laufzeit in RAM gebaut) → Geraet am besten ueber den **Produktstring**
  finden (macht `stm_display_fw.py`).

## 4. Rahmen (Framing)

Der Bootloader verarbeitet **zwei Kanaele** (`0x2000001C` Byte 1):

**Kanal 1 = USB-HID (verifiziert).** Der 64-Byte-HID-Block landet direkt bei
`0x20000206`; dessen **Byte 0 ist die Laenge**, der Payload folgt ab `+1`.
Ein getrenntes Flag bei `0x20000247` meldet "Report abgeholt" (loescht den
Datenmodus). Buffer-Init/Zeroing: `0x080009D4`, gelesen in `0x08000AC0`.
```
Byte 0   : len                     (Payload-Laenge, 0 = Leer-Report)
Byte 1.. : payload[0..len-1]       payload[0] = Kommando-Code
Rest     : 0x00 bis 64 Byte aufgefuellt
```
**Kanal 2 = gerahmt** (`0x08000AFC`..`0x08000B58`), mit Kopf und fester
20-Byte-Datenphase – vermutlich CAN:
```
Byte 0 : 0x02
Byte 1 : 0x21
Byte 2 : len                       (Payload-Laenge)
Byte 3..: payload[0..len-1]        payload[0] = Kommando-Code
```
**Geraet → Host:** Antwortcode ist immer **`Kommando | 0x40`**, gefolgt von
Laenge/Daten. Beispiel `0x10` → `50 03 01 f4 03 e8`, `0x34` → `74 03 03 80 00`,
`0x37` → `77`, `0x3E|0x00` → `7e 00`. Auf Kanal 2 wird die Antwort in eine
20-Byte-Struktur verpackt und mit Kopf `57 01 20` gesendet (`0x080042AE`).

## 5. Kommandos (Dispatcher `0x08000B5A`, payload[0])
| Code | Handler | Bedeutung |
|---|---|---|
| `0x10` | `0x08000CAC` | **Modus setzen**, Laenge **2**, Arg ∈ {1,2,3,0x81,0x82,0x83} → `[0x20000024]`=Arg; Arg **2/3/0x82/0x83** setzt zusaetzlich `[0x20000028]`=1; ACK `50 <arg> 01 f4 03 e8` **[V]** |
| `0x11` | `0x08000DA2` | Laenge **2**, Arg ∈ {1,3,0x81,0x83} → `[0x20000025]` bzw. `[0x20000026]`=1 **[R]** |
| `0x22` | `0x08000EAE` | Magic `0xF100` / `0xF15A` (wire: `00 F1` / `5A F1`) **[V]** |
| `0x27` | `0x080011DA` | Sub 0x11 / 0x12 (Erase/Info?) **[R]** |
| `0x2E` | `0x08001224` | 2048-Byte-Block (`*(0x08001300)`), Versionfelder **[R]** |
| `0x31` | `0x080013A4` | Magic `0x10002/0x10003/0x10202/0x10302` **[V]** |
| `0x34` | `0x08001514` | **Adresse setzen**: payload[1] Nibbles, payload[3..6] = 32-Bit **Big-Endian**-Adresse → `0x080017BE` **[V]** |
| `0x36` | `0x08001614` | **Block schreiben**: Laenge **2..58**, payload[1]=Sequenz (muss `[0x20000038+0]` entsprechen, sonst Fehler `0x73`), payload[2..]=Daten; `len-2` Bytes werden an `0x20000038+4` programmiert, Zeiger += `len-2`, Sequenz +1 mit **Wrap 255 → 1**; setzt `[0x2000001C]`=1 (Datenmodus) **[V]** |
| `0x37` | `0x080017EC` | **Sequencer-Reset**, Laenge **1** → Seq=1, Zeiger=0; ACK `77` **[V]** |
| `0x3E` | `0x08001884` | **Finish**, Laenge **2**, Arg 0x00 oder 0x80. Arg 0x80 → keine Antwort; danach laeuft der BL in einen 5000-Tick-Timeout und macht **`SCB->AIRCR = 0x05FA0004`** (`0x08001B66`), wodurch die App startet **[V]** |
| sonst | `0x08000A46` | Fehler/NAK mit Fehlercode |

Weitere Bausteine **[V]**:
* Schreibzeiger liegt bei `0x20000038+4` (`0x080017BE`).
* Flash-Treiber (nur Bootloader): Unlock `0x45670123`/`0xCDEF89AB` @
  `0x08005314`/`0x0800531C`; WaitForLastOp `0x08005180`; Page-Erase
  `0x080051B4`; Halfword-Programm `0x0800525C`; Wrapper `0x08001964`
  (Erase, Adresse muss 2-KiB-aligned sein) / `0x08001998` (Write).
* Treiber laeuft aus **SRAM** (RAM-Stubs `0x2000B249/63/75/A7/9D/F5` via
  `0x08001004`..`0x08001036`) — noetig, weil STM32F1 nicht aus dem Flash
  lesen kann, waehrend es schreibt.
* Zusaetzlicher Datenstrom-Pfad in der Haupt-Loop `0x08001ACC`:
  Puffer `0x2000025C`, Flush alle **80 Bytes** via `0x0800174E` (fuer
  zusammenhaengende Downloads).

## 6. Per Emulation verifiziert (`tools/emu.py`)
Mit Unicorn (ARM Cortex-M3) werden einzelne Funktionen aus dem Dump mit
präpariertem SRAM/Peripherie aufgerufen:

| Szenario | Ergebnis |
|---|---|
| `crc` | `0x08000924` liefert exakt den Python-CRC → Harness + CRC-Modell OK |
| `dispatch` | Jeder Kommando-Code trifft den erwarteten Handler (alle 10) |
| `frame` | `02 21 <len> <payload>` wird akzeptiert, `len` + Payload korrekt kopiert |
| `setaddr` | `0x34`: Schreibzeiger = BE-Adresse aus payload[3..6] (Payload-Länge **11**) |
| `writeblk` | `0x36`: `len-2` Datenbytes an Zeiger geschrieben, Zeiger += `len-2`, Sequenz +1 |
| `blprobe` | Alle Kommandos mit definierten Laengen → Fehlercodes `0x12/0x13/0x24/0x33/0x73`, ACK `77` bei `0x37` |
| `blsession` | **Kompletter Upload**: `0x10(3)`→`0x37`→`0x34`→4096×`0x36`→`0x3E`; Zeiger endet exakt auf `0x08040000`, Flash-Inhalt identisch → **OK** |
| `appreset` | `0x08017378(1)` / `0x08016E84(2)` → `AIRCR = 0x05FA0004` |
| `trigger` | Dispatcher `0x08018D04` → Reset `0x08017378` (Nachricht-Id `0x304`) |
| `caninject` | CAN-Frame in den echten Empfangshandler `0x0801B066` injizieren |
| `uploadtool` | **Tool-Upload**: `stm_display_fw.Protocol.upload()` gegen `0x08000AAA` — 4100 Reports, 4096 Flash-Writes, Zeiger endet exakt auf `0x08040000`, Inhalt identisch |
| `hidtrigger` | **Tool-Trigger**: `build_app_report()` → App-Empfangspfad → Index 4 → `0x0304` → `0x08017378` (beide Huellen) |

Damit sind Rahmenformat, Kommandocodes, die **komplette Session** und der
Abschluss **bestätigt**.
Aufruf: `python3 tools/emu.py {crc,dispatch,frame,setaddr,writeblk,appreset,trigger,caninject,blprobe,blsession} [-t]`
`blsession` akzeptiert optional eine Bytezahl (Standard `0x1000`):
`python3 tools/emu.py blsession 0x38000`.

Wichtige RAM-Adressen (aus den Literal-Pools):
* `0x200001CC` = Payload-Puffer, `0x2000001E` = Payload-Länge (halfword)
* `0x20000038` = Flash-State: `+0` Sequenz, `+1/+2` Nibbles, `+4` Schreibzeiger
* `0x20000040` = Erase-State: `+4` Adresse, `+8` Länge (Wrapper `0x08001964`)
* `0x08007800` = Ziel des 2048-Byte-Blocks (Kommando `0x2E`)

### USB-Identitäten (am Gerät verifiziert, Linux-dmesg)
```
idVendor=0x2A8A  idProduct=0x0010  bcdDevice=1.00
Manufacturer: Continental   Product: "Continental eBike System "   Serial: 000000000001
USB HID v1.11 Device   ->   hidraw4
```
Das ist die **App**. Der Bootloader hat denselben Vendor (VID `0x2A8A`) und den
Produktstring **"CEBS Bootloader Mode"** (eigene PID). Das Tool sucht daher
zuverlässig über den Produktstring.

### Trigger aus der App (per Emulation verifiziert)
Der Bootloader betritt den Update-Modus nur bei **SFTRST** (Software-Reset) oder
ungültiger App-CRC. Die App kann also nur per `NVIC_SystemReset` (`0x08017FA2`)
hineinspringen. Verifizierte Pfade (`tools/emu.py`):

```
python3 tools/emu.py appreset   # 0x08017378(1) & 0x08016E84(2) -> AIRCR=0x05FA0004  OK
python3 tools/emu.py trigger    # Dispatcher 0x08018D04 -> Reset 0x08017378         OK
```

* `0x08017378` (Reset wenn Arg==1) und `0x08016E84` (Reset wenn Arg==2)
  schreiben `AIRCR = 0x05FA0004` (SYSRESETREQ). Mit Arg≠Wert passiert nichts.
* Der Event-Dispatcher **`0x08018D04`** erreicht mit gesetztem Nachrichten-State
  den Handler `0x08017378`:
  * `0x20000930` bit6 gesetzt (Event anstehend)
  * Halfword `0x20000928` bit11 gesetzt
  * Byte `0x20000941` = **4** → Tabelle `0x08036F40`, Struktur-Id **`0x304`**
    → `fnarray[3]` (`0x08036F2C`) = `0x08017378` → Reset
  * `0x20000928` Byte0 == 0 und `0x20000942` == 0 (sonst Arg≠1)

Der Trigger ist also eine **proprietäre Nachricht mit Id `0x304`**, kein
standardisierter USB-Request. Nächster Schritt: Producer
von `0x20000928`/`0x20000941` bis zum CAN- oder USB-Eingang zurückverfolgen.

#### Kanal-Analyse (App)
* **CAN**: `IRQ20 (CAN1_RX0)` → `0x0801B066` → `0x0801AFC6` (liest `CAN1_RIR`,
  extrahiert die 11-Bit-ID `(RIR>>21)<<21`, sucht sie in einer Tabelle
  `*(0x0801B30C)` mit bis zu 22 Einträgen, dann Dispatch). CAN-Register
  `0x400064xx` (u. a. `0x4000640C`=RF0R, `0x40006408`=TSR).
* **USB**: `IRQ67 (OTG_FS)` → `0x0800D5F4` (USB-Stack-State `0x20005E40`);
  `IRQ42` → `0x0800D5D3`. (Der Bootloader nutzt `0x50000000` direkt.)
* **Event-/Nachrichtenmodul** `0x08018xxx`: Dispatcher `0x08018D04`, aufgerufen
  von `0x08018E26` (läuft, wenn Byte `0x20000930` != 0, aus `0x08018E38`).
  Tabelle `0x08036F40` (`{min,max,x,id,fn}`), `fnarray` `0x08036F2C`,
  Id `0x304` → `fnarray[3]` = `0x08017378` (Reset).
* **Session-Magics** im Bootloader (Kommando `0x22`, Payload-Länge 3):
  Wire-Bytes `[0x22, 0xF1, 0x00]` = Magic `0xF100`,
  `[0x22, 0xF1, 0x5A]` = Magic `0xF15A` (Handler `0x08000EF2` / `0x08000E3C`
  / `0x08000F54`).

#### Bootloader-Session (per Emulation geloest)
Die frühere Lücke ist geschlossen. Der Schreibpfad wird über `cmd 0x10`
freigeschaltet:
* `0x08001040(r0)` setzt das **"Gueltig"-Flag `[0x20000028]`**; `0x0800120E`
  liest es nur. `cmd 0x34` bricht mit Fehler `0x33` ab, wenn das Flag 0 ist.
* `cmd 0x10` mit Arg 1/0x81 → Flag 0; Arg **2/3/0x82/0x83** → Flag 1.
* `cmd 0x34` setzt die **Schreibfreigabe `[0x20000034]`=1** (`0x08001568`),
  `cmd 0x36` prueft sie (sonst Fehler `0x24`).
* `0x08001998` = `flash_write(ptr, len, buf)`: verweigert `ptr < 0x08007800`
  (Bootloader geschuetzt) und `ptr+len > 0x08040000`.

**Verifizierte Upload-Sequenz** (`tools/emu.py blsession`):
```
0x10 03                    -> 50 03 01 f4 03 e8
0x37                       -> 77
0x34 03 00 <addr BE32> 00… -> 74 03 03 80 00        (Laenge 11)
0x36 <seq> <56 Datenbytes> (x N, seq 1..255 dann wieder 1)
0x3E 80                    (keine Antwort) -> SW-Reset -> App
```
Mit 4096 × 56 Byte (= 0x38000) endet der Zeiger exakt auf `0x08040000`.

Flashen ist ausschließlich über den **USB-HID-Bootloader** möglich (kein
CAN-Update im Dump); CAN/USB sind nur fuer den *Einstieg* in den Bootloader
relevant.

## 7. Offene Punkte (vor dem ersten echten Upload klaeren!)
1. **HID-Report-Wrapper**: Der Emulator verifiziert die Protokollschicht ab
   `0x20000206` (Byte 0 = Laenge). Das Tool sendet daher standardmaessig
   `[len][payload]` (`--wire raw`); mit `--wire hdr` steht die gerahmte
   Variante `02 21 <len> <payload>` bereit. Welche Variante der USB-Stack
   tatsaechlich auf das HID-Out-Report legt, ist ohne Hardware offen.
2. **Trigger-Kanal** (CAN vs USB) der App-Nachricht Id `0x304` sowie deren
   Mehrfachrahmen-Aufbau (0x550/0x552 erreichen als Einzelframe nur die
   BMS-Handler `0x0801C702`/`0x0801CFBC`).
3. **Erase vor dem Schreiben**: `0x08001964` ist ein Erase-Wrapper; im
   Emulator wurde der Flash-Inhalt direkt ueberschrieben. Vor dem echten
   Flashen pruefen, ob `cmd 0x10`/`0x37` das Loeschen ausloest oder ob das
   Tool vorher 0xFF schreiben muss.

Empfehlung fuer die Hardware: einmal mit dem Original-Tool flashen und den
USB-Verkehr mitschneiden (Linux `usbmon`/`tshark`, Windows USBPcap+Wireshark).
Damit ist Punkt 1 sofort geklaert; die Sequenz aus Abschnitt 6 laesst sich
unveraendert uebernehmen (nur Datenbytes + CRC ersetzen).

## 8. Reset in den Bootloader (App -> Bootloader)

Fuer einen reinen PC-Workflow muss die App erst in den Bootloader gebracht
werden. Kanal und Inhalt sind jetzt bestimmt:

### Ablauf
```
PC                                  App (0x08018xxx)
--                                  ----------------
HID-Geraet VID 0x2A8A/PID 0x0010 oeffnen
  -> Enumeration -> USB-Port-Zustand obj[0xa2] = 3
64-Byte HID-OUT-Report: 11 03 ...   -> Matcher 0x08018652
                                       selector[0x11] = Definition 1 (Basis 3)
                                       payload[1] = 0x03 -> Index 4
                                     -> Tabelle 0x08036F40[4] id = 0x0304
                                     -> fnTable[3] = 0x08017378
Gerät startet neu  <--------------  SCB->AIRCR = 0x05FA0004
Gerät als "CEBS Bootloader Mode"    -> Upload nach Abschnitt 6
```

### Der App-HID-Report
* Device-Descriptor `0x08037FC0`: bcdUSB 0x0200, idVendor `0x2A8A`,
  idProduct `0x0010`, bcdDevice `0x4032`.
* Interface `0x08037FA4`: `bInterfaceClass = 0x03` (HID), 2 Endpoints.
* Report-Descriptor: Usage Page `0xFF00`, Usage Min 1 / Max 0x40,
  Logical 0..255, **Report Size 8 x Count 0x40 = 64 Byte**, Input + Output,
  **keine Report-ID**.

### Tool-Aufruf
```
python3 tools/stm_display_fw.py reset              # Huelle mit Laengenbyte
python3 tools/stm_display_fw.py reset --wire raw   # Report beginnt mit 11 03
python3 tools/stm_display_fw.py reset --msg "11 07"
```
`reset` oeffnet die App-HID-Schnittstelle, sendet den Trigger-Report und
wartet, bis `"CEBS Bootloader Mode"` erscheint (Exit-Code 0).

### Emulator-Nachweis
`python3 tools/emu.py hidtrigger` erzeugt den Report mit genau dem Code des
Tools (`stm_display_fw.build_app_report`), spielt ihn durch den echten
Empfangspfad und prueft die Kette Index 4 -> id `0x0304` -> `0x08017378`
inklusive Dispatcher-Aufruf — fuer **beide** Hüllen-Varianten `len` und `raw`.

`python3 tools/emu.py uploadtool` faehrt den **echten Upload-Pfad** des Tools
(`stm_display_fw.Protocol.upload`) gegen den emulierten Bootloader:

```
Reports gesendet  = 4100        (1x 0x10, 1x 0x37, 1x 0x34, 4096x 0x36, 1x 0x3E)
Flash-Writes      = 4096
Endzeiger         = 0x08040000  (erwartet 0x08040000)
Inhalt identisch  = True
```

Damit sind **beide** Tool-Pfade (Trigger und Flashen) gegen den Emulator
verifiziert — nicht nur das Protokoll, sondern der ausgelieferte Code.

### Noch offen
Welche der beiden Hüllen die App wirklich erwartet, ist ohne Hardware nicht
entscheidbar. Die Laengenvariante ist wahrscheinlicher, weil der Bootloader
dasselbe Report-Format mit `[len]`-Praefix benutzt.
