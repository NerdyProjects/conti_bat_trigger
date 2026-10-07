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
| `0x22` | `0x08000EAE` | Magic in payload[1..2] (Big-Endian), Laenge **3**: `0xF100`→`0x08000EF2` Quittung, `0xF101`→`0x08000E3C` Statusantwort, **`0xF15B`→`0x08000F54` = 6 Byte aus Flash `0x08007800`** (Kanal 2: 8 Byte). Antwort `62 <magic> <6 Datenbytes>`. **Der einzige lesende Befehl.** `0xF15A` liefert Fehler `0x13` (= Schreibmagic von `0x2E`) **[V]** |
| `0x27` | `0x080011DA` | Challenge/Response: Sub `0x11` (Laenge 2) bildet mit `0x08000968` zwei Pruefwerte in `0x2000002C/30` und liefert den ersten als 32-Bit-Wort zurueck (Kanal 1: 6 Byte ab Index 2, Kanal 2: 7 Byte); Sub `0x12` (Laenge 6, payload[1]==1) prueft die Antwort mit `0x080008FE` (`a*i + b*(i+1) == c`, i ∈ 1..5) → 2 Byte Antwort, sonst Fehler `0x35` **[V]** |
| `0x2E` | `0x08001224` | **Geraeterekord schreiben**: Magic **`0xF15A`** in payload[1..2], Laenge **exakt 10** (sonst Fehler `0x13`); Pruefung payload[3] ≤ 99, payload[4] ∈ 1..12, payload[5] ∈ 1..31 (Jahr/Monat/Tag). Loescht 2 KiB bei `0x08007800` (`0x08001964`) und schreibt 8 Byte ab payload[3] (`0x08001998`) — das achte Byte liest der Handler hinter dem Payload aus dem Puffer. ACK `6e f1 5a` **[V]** |
| `0x31` | `0x080013A4` | 24-Bit-Unterkommando aus payload[1..3] (Big-Endian), Laenge ≥ 3: **`0x10202`** = Applikations-CRC pruefen (Laenge 8 und Modus 1, sonst Laenge 4 in Modus 2; verlangt das Flag `[0x20000028]`), **`0x1FF00`** = Flash loeschen (Laenge 13, Adresse payload[5..8], Laenge payload[9..12], je Big-Endian, 2-KiB-ausgerichtet), **`0x10203`** = Quittung (Laenge 4). Alles andere → Fehler `0x12` **[V]** |
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
| `blreadback` | **Lese-Befehl und CRC-Status**: `0x22` mit `0xF15B` liefert die 6 Byte aus `0x08007800` (mit `0xF15A` keine Daten), Record via `0x2E` schreiben und zuruecklesen, CRC-Status `0`/`1` fuer intaktes/geaendertes Image, `verify` und `verify --deep` (8 bzw. 13 Pruefungen) |
| `blsession` | **Kompletter Upload**: `0x10(3)`→`0x37`→`0x34`→4096×`0x36`→`0x3E`; Zeiger endet exakt auf `0x08040000`, Flash-Inhalt identisch → **OK** |
| `appreset` | `0x08017378(1)` / `0x08016E84(2)` → `AIRCR = 0x05FA0004` |
| `trigger` | Dispatcher `0x08018D04` → Reset `0x08017378` (Nachricht-Id `0x304`) |
| `caninject` | CAN-Frame in den echten Empfangshandler `0x0801B066` injizieren |
| `uploadtool` | **Tool-Upload**: `stm_display_fw.Protocol.upload()` gegen `0x08000AAA` — 4100 Reports, 4096 Flash-Writes, Zeiger endet exakt auf `0x08040000`, Inhalt identisch |
| `hidtrigger` | **Tool-Trigger**: `build_app_report()` → App-Empfangspfad → Index 4 → `0x0304` → `0x08017378` (beide Huellen) |
| `bmspatch` | **BMS-Patch**: 19 Checks gegen Original + gepatchte Firmware (aus `.hex` geladen) — 0x555 folgt dem Displayzustand, Abschaltpfad, Zustandsmaschinen-Sackgasse, Bootloader-CRC |

Damit sind Rahmenformat, Kommandocodes, die **komplette Session** und der
Abschluss **bestätigt**.
Aufruf: `python3 tools/emu.py {crc,dispatch,frame,setaddr,writeblk,appreset,trigger,caninject,blprobe,blsession,bmspatch} [-t] [-i <image>]`
`blsession` akzeptiert optional eine Bytezahl (Standard `0x1000`):
`python3 tools/emu.py blsession 0x38000`.

## 7. Flash auslesen (Dump) -- was geht und was nicht

**Ein allgemeiner Dump ist nicht moeglich.** Der Dispatcher `0x08000B5A`
kennt genau zehn Kommandos (Tabelle oben); keines liefert Flash-Inhalt, und
kein Antwortpfad enthaelt Nutzdaten aus dem Speicher -- Antworten tragen
ausser dem Kommandokopf nur Status (der Antwortbau `0x0800130C` leitet nur
Werte aus den Argumenten weiter). Auch die 0x50-Byte-Schreibfunktion
`0x0800174E` und der Puffer `0x2000025C` gehen **ins** Flash, nicht heraus.
Die einzigen Ausnahmen sind:

1. **Geraeterekord (6 Byte)**: Kommando `0x22` mit Magic `0xF15B` liest die
   feste Adresse `0x08007800` (`0x08000F54`, Literal `*(0x08000FE0)`). Die
   Adresse steht als Konstante im Code und ist nicht beeinflussbar; die Seite
   ist im Original leer (`0xFF`) und wird von `0x2E` beschrieben.
2. **CRC-Status (1 Bit)**: Kommando `0x31/0x10202` vergleicht
   CRC32(`0x08008000 + 0x00037FFC`) mit dem Wort bei `0x0803FFFC` und meldet
   **`0 = gleich`, `1 = ungleich`** (Werte des Antwortbaus `0x0800130C`, im
   Emulator bestaetigt -- die fruehere Annahme `1 = ok` war falsch).
   Reichweite und Adresse sind fest verdrahtet; ein Teilbereich ist nicht
   abfragbar.

**Inhalt pruefen statt lesen** (`tools/stm_display_fw.py`):
* `verify <image>` -- **nur lesend** (CRC-Abfrage). Status 0 belegt die
  *Selbstkonsistenz* des Applikationsbereichs (CRC32 passt zum gespeicherten
  CRC-Wort); ein anderes, in sich stimmiges Image liefert ebenfalls 0.
* `verify <image> --deep -y` -- **schreibt** und beweist damit exakt: die letzte
  Seite `0x0803F800..0x0803FFFF` wird geloescht und aus dem Image neu
  geschrieben (setzt das CRC-Wort des Images), danach wird die CRC abgefragt.
  Status 0 ⇔ der **gesamte** Bereich entspricht diesem Image; der alte Inhalt
  der Seite ist nicht lesbar und damit nicht sicherbar. Ohne `-y` bricht das
  Tool mit einer Erklaerung ab, ohne etwas zu senden.
* `dump rec` -- liest den Geraeterekord (`0x22`, 6 Byte). `dump app` erklaert
  die Grenze.

Eine Record-Runde im Emulator (`python3 tools/emu.py blreadback`) zeigt, dass
`0x2E`/`0x22` tatsaechlich schreiben und lesen -- der Rueckkanal ist also ein
**echter** Flash-Zugriff und keine Konstante.

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
  Wire-Bytes `[0x22, 0xF1, 0x00]` = Magic `0xF100` -> Quittung (`0x08000EF2`),
  `[0x22, 0xF1, 0x01]` = Magic `0xF101` -> Statusantwort (`0x08000E3C`),
  `[0x22, 0xF1, 0x5B]` = Magic `0xF15B` -> **6 Byte aus Flash `0x08007800`**
  (`0x08000F54`). Das Schreibmagic `0xF15A` (`0x2E`) liefert hier Fehler `0x13`.
  Achtung: das ist eine andere Baustelle als die Session-Magics der App.
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
64-Byte HID-OUT-Report             -> Empfangsparser 0x0801D634 (Typ 1)
  00 01 3D 50 05 02 11 03 ...        0x0801D75C: Sequenz/Marke/Laenge ok
                                     -> 0x0801D5C2  -> FIFO A
                                     -> 0x0801D900: Nachrichtenobjekt
                                        obj[+16] = Block+3  (Nutzdaten)
                                        obj[+12] = 2        (Laenge)
                                     -> Modul 0x0801819A/0x080181CA
                                        Matcher 0x08018652
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
python3 tools/stm_display_fw.py reset                 # Steuerrahmen + Trigger
python3 tools/stm_display_fw.py reset --channel 0x300  # andere Kanal-ID
python3 tools/stm_display_fw.py reset --msg "11 07"    # andere Nutzlast
python3 tools/stm_display_fw.py raw "11 03" --app-frame --sync --listen 1500
```
`reset` oeffnet die App-HID-Schnittstelle, sendet zuerst den Steuerrahmen
Typ `0xFE` (setzt den Zaehler der App auf 0), danach den Trigger-Rahmen mit
Sequenz 0, und wartet, bis `"CEBS Bootloader Mode"` erscheint (Exit-Code 0).
`--legacy-wire len|raw` sendet die frueher vermutete Huelle (nur zum Testen).

### Emulator-Nachweis
`python3 tools/emu.py usbtrigger` faehrt den **echten** USB-Empfangsweg der App
und benutzt dabei ausschliesslich Firmware-Code:

```
1) Typ-0xFE-Rahmen           -> Zaehler 0x20000BAC = 0          OK
2) Trigger-Rahmen            -> Parser 0x0801D634 -> FIFO A +1  OK
3) Port-Task 0x0801D81E      -> obj[+12]=2, obj[+16] = "11 03"  OK
4) Matcher                   -> Index 4, id 0x0304,
                                fn = 0x08017378                 OK
   Dispatcher 0x08018D04     -> Reset-Handler erreicht          OK
```

`python3 tools/emu.py hidtrigger` prueft zusaetzlich die alten, verworfenen
Hüllen-Varianten `len`/`raw` (bleibt als Regressionsschutz erhalten).

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

### 8.1 Das App-Transportprotokoll (vollstaendig)

Der App-HID-Report ist im **Normalbetrieb** kein Rohbefehl, sondern ein
gerahmtes Transportformat. Es wurde aus Sender (`0x0801D1DA`, baut den
Rahmen) und Empfaenger (`0x0801D634` Typverteilung, `0x0801D75C` Pruefung)
rueckgewonnen - beide Seiten benutzen **dasselbe** Layout:

```
Byte  Bedeutung
 0    Sequenzzaehler   muss  *(0x20000BAC)  entsprechen, sonst verwirft
                       die App den Rahmen und antwortet mit Typ 0xFF
 1    Rahmentyp
 2    0x3D             feste Marke (nur Typ 1)
 3-4  Kanal-ID         u16 little-endian, nur freigegebene IDs
 5    Nutzlaenge       <= 58
 6..  Nutzdaten        auf 64 Byte mit 0x00 aufgefuellt
```

| Typ | Richtung | Bedeutung | Handler |
|-----|----------|-----------|---------|
| `0x00` | rein | bis zu 5 CAN-Frames a 11 Byte | `0x0801D65C` |
| `0x01` | rein | ein Frame: Kanal-ID + Daten | `0x0801D75C` -> `0x0801D5C2` |
| `0x02` | rein | Variante | `0x0801D6E8` |
| `0x03` | rein | Datenkanal (Laenge <= 61) | `0x0801D7A6` -> `0x0801D494` |
| `0xFD` | rein | Steuerung | `0x0801D7F4` |
| `0xFE` | rein | **Zaehler auf 0 setzen** | `0x0801D744` |
| `0xFF` | rein | Steuerung (Antwort auf Fehler) | `0x0801D814` |

**Kanal-IDs** (Flash-Tabelle `0x08037B3A`, geprueft von `0x0801D4E2`;
`0x0801D522` prueft die Teilmenge in `0x08037B3C`):

```
0x0550  0x0552  0x0422  0x0425  0x0101  0x0331  0x0668  0x0300
```

**Weg vom Report zur Nachricht** (alles Firmware-Code):

```
0x0801D634  Typverteilung
0x0801D75C  Prueft Sequenz, Marke 0x3D, Laenge <= 58
0x0801D5C2  baut Block  [id_lo][id_hi][len][daten]
0x0801D4E2  ID-Freigabe -> Push in FIFO A  (*(0x20000BA0), Puffer 64 B)
0x0801D81E  Port-Task; 0x0801D900 holt Block aus FIFO A
            Nachrichtenobjekt 0x2000B814 (*(0x2000092C)):
              obj[+12] = len          (u16)
              obj[+16] = Block+3      -> Nutzdaten
              obj[+2]  = 2
0x0801819A  Objekt registrieren (Queue-Zustand 0x2000092C/0x20000930)
0x080181CA  -> 0x08018160  Nachrichtenauswertung
```

**Der Reset-Trigger** ist damit: Rahmen Typ 1, Sequenz 0, beliebige
freigegebene Kanal-ID, Nutzdaten `11 03`.

Warum das sicher ist: die Nutzdaten, die `0x0801D900` ins Objekt schreibt,
sind **identisch** mit den Bytes, mit denen der Emulator im Szenario
`msgprobe`/`trigger` den Reset-Handler findet (Index 4 -> `fnTable[3]` =
`0x08017378`). Der frueher vermutete Rahmen `[len] 11 03` bzw. `11 03 ...`
konnte nicht funktionieren, weil die Bytes dann an der falschen Stelle liegen
und die Marke `0x3D` fehlt.

### 8.2 Offene Punkte

* Auf echter Hardware ist `reset` mit diesem Rahmen noch **nicht** bestaetigt
  (Stand: Emulator verifiziert, Hardware-Test offen).
* Unbekannt ist, ob die App den Kanal `0x0550` akzeptiert, ohne dass ein
  bestimmter Zustand (NM/Session) vorher gesetzt wurde; `--channel` erlaubt
  das Durchprobieren der 8 freigegebenen IDs.
* Der Sender des 11-Byte-Datensatzes fuer Typ 0 (`0x0801D494`, OTA-Kanal) ist
  nicht weiter untersucht.

## 9. Flash-Ablauf auf echter Hardware (Stand 2026-10-04)

### 9.1 Loeschen (`0x31/0x1FF00`)

* Das Loeschen von 224 KiB (112 Seiten a 2 KiB) dauert **~2,5 s**; der
  Bootloader quittiert erst nach der letzten Seite. Ein 4-s-Wartefenster war
  zu kurz -- das Tool meldete "keine Antwort", obwohl der Bereich danach leer
  war. Deshalb: `ERASE_TIMEOUT_MS = 60000` (Option `--erase-timeout S`) mit
  Fortschrittsausgabe.
* Antwort `71 01 ff 00 <status>`: **`status = 0` = ok, `1` = Fehler**
  (Handler `0x080014D2` ueber `0x0800130C`; im Modus-1-Zweig `0x0800134C`
  wird `r5 == 1` auf `0` und `r5 == 2` auf `1` abgebildet; auf Hardware und im
  Emulator `blupload` bestaetigt).

### 9.2 Schreiben (`0x36`) -- Sequenz und Quittungen

* Nach `0x37` (Sequencer) **und** `0x34` (Adresse) erwartet das Geraet
  **Sequenz 1** (am Geraet gemessen: seq 1 -> Programmfehler `0x72`,
  seq 2/3 -> Sequenzfehler `0x73`).
* `0x37` allein genuegt nicht: `0x36` antwortet dann mit **`0x24`**
  ("Datenmodus" fehlt) -- erst `0x34` setzt das Flag.
* Erfolgreiche `0x36`-Bloecke werden **nicht** einzeln quittiert; nur Fehler
  kommen sofort (`7f 36 73` Sequenz, `7f 36 72` Programmieren), und einmalig
  `0x76`, wenn der Zeiger `0x08040000` erreicht.
* **Ein abgelehnter Block erhoeht die erwartete Sequenz nicht** -- danach
  scheitert jeder weitere Block mit `0x73`. Fehler also immer mit
  Resync (0x37 + 0x34) und erneutem Senden beantworten.

### 9.2.1 DATENSTROM-MODUS -- die eigentliche Ursache aller 0x72-Fehler

Der Bootloader kennt **zwei** Betriebsarten, und der Rahmenaufbau wechselt:

| | Kommando-Modus | Datenstrom-Modus |
|---|---|---|
| Nutzlast | `[cmd][…]` | **nur Daten** (kein Kommando-/Sequenzbyte) |
| Laenge | beliebig (cmd-abhaengig) | = Anzahl zu programmierender Bytes |
| Zieladresse | aus dem Kommando (`0x34`) | Schreibzeiger, laeuft mit |
| Antwort | je Kommando | nur Fehler bzw. `76` am Ende |

Umschaltung: Das **erste erfolgreiche `0x36`** setzt `[0x2000001C] = 1`
(Handler `0x08001614`, `strb r1,[r5]` bei `0x08001682`). Ab dann geht
**jede** Nutzlast an `0x0800168A` statt an den Dispatcher
(`0x08000B32/34`: `cbz r0, Dispatcher`) und wird mit
`f_801998(Zeiger, Laenge, payload)` wortwoertlich programmiert.

Zurueck in den Kommandomodus kommt das Geraet nur, wenn

* der Zeiger **exakt `0x08040000`** erreicht (`0x080016C4` -> Antwort `76 <seq>`), oder
* ein Programmierfehler auftritt (`0x080016AE`: Fehlerantwort `0x72`, Flag = 0).

Das Flag bleibt deshalb so hartnaeckig: geloescht wird es beim Empfang nur,
wenn der **vorherige** Rahmen eine Antwort erzeugt hat -- Merker ist das
TX-Fertig-Flag `[0x20000171]`, das `0x08003AD8` nach `[0x20000247]` spiegelt
und `0x08000ACC` auswertet. Ein `0x36`-Block **antwortet nicht**, also fehlt
der Merker und der Strom laeuft weiter.

**Damit erklaert sich der Hardwarefehler bei exakt `0x080081C0`** (= 8 x 56):
Block 1 lief als Kommando (56 Byte), die Bloecke 2..8 wurden als Datenstrom
mit je **58** Byte Nutzlast (einschliesslich `36 xx`!) programmiert. Der
Geraetezeiger stand dadurch 14 Byte weiter als die Hostrechnung; das Resync
`0x37`+`0x34` setzte ihn auf `0x080081C0` **zurueck** -- also mitten in
beschriebenen Flash. Programmieren kann nur 1 -> 0, die abschliessende
Verifikation der Blob-Routine faellt durch (Status 4) -> Wrapper 3 ->
Fehler `0x72`. **Kein WRP, kein 0xFF-Problem.**

**Richtiges Verfahren** (`Protocol.upload_stream()`, ueber `upload … --region app`
jetzt der Standard):

1. Sitzung (`0x10`) + `0x31/0x10203` (Unlock), Bereich seitenweise loeschen.
2. `0x37` + `0x34` auf den Start (`0x08008000`) -> Erwartung Sequenz 1.
3. **Ein** Kommandorahmen `36 01 <56 Byte>`.
4. Danach 4095 **reine Datenrahmen** a 56 Byte. App-Bereich = `0x38000` Byte =
   exakt `4096 x 56`, der letzte Rahmen endet also genau auf `0x08040000`
   -> Antwort `76` und Kommandomodus ist wieder aktiv.
5. App-CRC (`0x31/0x10202`) pruefen, dann `0x3E 0x80` (Reset).

Wichtig: die 0xFF-Bloecke duerfen **nicht** uebersprungen werden (der Zeiger
muss lueckenlos bis `0x08040000` laufen); 0xFF auf geloeschtem Flash zu
programmieren ist erlaubt (nur 1 -> 0 wird geschrieben). Der Blob lehnt
Adressen `< 0x08007800` ab (`0x080019A6`), der Bootloader-Bereich ist also
grundsaetzlich nicht ueber `0x36` beschreibbar.

**Falle: die Laenge muss GERADE sein.** Die Programm-Routine rechnet die
Laenge in Halbwoertern (`0x0800526E: lsrs r0,r0,#1`) und verifiziert genau
diese Anzahl. Ein einzelnes Byte am Ende wird also **stillschweigend nicht
programmiert** -- und weil die Verifikation dasselbe verkuerzte Fenster
prueft, meldet das Geraet trotzdem Erfolg. Am 2026-10-04 auf Hardware
beobachtet: `36 01 A0` (1 Byte) -> keine Fehlermeldung, aber die
Datensatzseite blieb `ff ff ff ff ff ff`; der Datenstrom wurde trotzdem
aktiviert. `Protocol.cmd_write_block()`/`cmd_write_stream()` lehnen ungerade
Laengen daher mit `ValueError` ab, und `writetest` benutzt einen 2-Byte-Block.

Emulatorbeweis: `python3 tools/emu.py uploadtool` faehrt **beide** Wege -- das
alte Verfahren muss scheitern ("Geraet kommt nicht zurueck", `[0x2000001C]=1`),
das Stromverfahren liefert `Flash == Image` (0 abweichende Bytes), Zeiger
`0x08040000`, App-CRC Status 0 und sauberes `FLASH_SR`.

### 9.3 Upload-Verfahren im Tool -- historisch (`--legacy-blocks`)

> **Ueberholt.** Dieses blockweise Verfahren ist genau das, was am
> Datenstrom-Modus scheitert (siehe 9.2.1). Es bleibt nur fuer
> Vergleichsmessungen im Emulator erhalten (`--legacy-blocks`).

`Protocol.upload()` arbeitet in Blockbuendeln (Standard 8 Bloecke a 56 Byte):

1. `0x37` + `0x34` -- Sequencer und Adresse absolut setzen,
2. Buendel senden,
3. Fehlerrahmen einsammeln (`--settle-ms`, Standard 40 ms),
4. `0x24` (Datenmodus fehlt) -> Sitzung neu aufbauen und weiter,
5. `0x73` (Sequenzfehler) -> **ab dem ersten abgelehnten Block weiter**
   (siehe 9.5), niemals Bloecke wiederholen, die schon geschrieben sind,
6. `0x72` (Programmierfehler) -> **sofortiger Abbruch** mit Hinweis auf
   Power-Cycle (siehe 9.4),
7. bei USB-Abriss (`HIDException`) neu verbinden (ueber
   `Transport.reopen()`), Sitzung neu aufbauen und weiter,
8. `--pace-ms` (Standard 20 ms) Pause je Buendel,
9. Endkontrolle: `0x31/0x10202` -- **erst bei Status 0** wird `0x3E` (Reset)
   ausgeloest; bei Status 1 bleibt das Geraet im Bootloader (erneut flashen,
   dann wird vorher geloescht).

Nach dem Loeschen (und gelegentlich mitten im Schreiben) meldet hidapi einen
Transportfehler (``OSError``/``HIDException``). Das ist **kein Abstecken**: das
Geraet bleibt am Bus, ``dmesg`` bleibt leer. Ursache ist die Unlock-Routine des
Blobs (`+0x2C`), die fuer Flash-Befehle den Systemtakt umschaltet:

* ``RCC_CR &= 0xFEF2FFFF`` -> HSEON, HSEBYP, CSSON und **PLLON** aus,
* ``RCC_CR |= 1`` (HSI an) und ``RCC_CFGR = 0x9F0000`` (SW = HSI),
* danach ``FLASH_KEYR = KEY1/KEY2``.

Der USB-Takt kommt vom PLL (USBPRE) -- also ist das Geraet waehrend und kurz
nach einem Flash-Befehl **stumm**, ohne sich abzumelden. Deshalb:

* ``Transport.recv()`` wiederholt einen fehlgeschlagenen Lesevorgang einmal
  nach 20 ms,
* ``Transport.reopen()`` unterscheidet: Knoten noch da -> nur neu oeffnen
  (Stall), Knoten weg -> auf Neuanmeldung warten,
* ``--settle-ms`` (120 ms) und ``--pace-ms`` (50 ms) halten die Pausen
  gross genug, damit die stumme Phase nicht mit dem naechsten Kommando
  zusammenfaellt,
* jede Sitzung entsperrt den Flash explizit mit ``0x31/0x10203``.

### 9.3.1 Fallstrick: Lese-Timeout 0 blockiert (hidapi-Bindungen)

Die beiden verbreiteten ``hid``-Bindungen behandeln ``read(len, 0)``
**unterschiedlich**:

* neue Bindung (``hid.Device.read(size, timeout)``, ctypes-Variante): ``0``
  geht als ``hid_read_timeout(..., 0)`` raus -> nicht blockierend,
* klassische Bindung (``hid.device.read(max_length, timeout_ms=0)``, z. B. das
  PyPI-Wheel ``hidapi 0.15.0``): ``timeout_ms <= 0`` geht auf **``hid_read()``**
  -> das ist der *blockierende* Aufruf und wartet **unbegrenzt** auf einen
  Report.

Im Datenstrom antwortet das Geraet auf erfolgreiche Rahmen nicht. Ein
``recv(0)``/``poll(0)`` blieb deshalb in der klassischen Bindung genau am
Stromanfang stehen -- direkt nach dem ersten ``-> … | 36 01 …`` kam keine
Ausgabe mehr, obwohl das Geraet weiterlief. Deshalb:

* ``Transport.recv()``/``Transport.poll()`` runden das Timeout ueber
  ``_hid_timeout()`` auf **mindestens 1 ms** auf (und auf ``int``),
* der Drain des ersten ``0x36``-Rahmens (``Protocol.cmd_write_block()``) nutzt
  ``poll()`` statt ``recv()``: ein Lesefehler ist dort normal und darf den
  Handle **nicht** mitten im Strom neu aufbauen.

### 9.4 Latchender Flash-Fehlerzustand -- Mechanismus bewiesen

Wer auf **nicht geloeschten** Flash programmiert (Loeschen unvollstaendig
oder Block doppelt geschrieben), setzt beim STM ein Fehlerbit im `FLASH_SR`.
Danach nimmt der Controller **weder Loeschen noch Programmieren** an, auch
nicht auf leerem, ungeschuetztem Bereich:

* Loeschen meldet **Status 1** (und loescht nichts),
* jeder `0x36`-Block meldet **`7f 36 72`**.

Ursache im Code: der RAM-Blob prueft beim Eintritt `SR` bit 4/bit 2
(`0x080051E0` Erase, `0x0800527A` Program) und bricht dann ab. Die Routine,
die `SR` per W1C (`0x34` = bits 2/4/5) wieder loeschen wuerde, steht erst
**hinter** dieser Pruefung (`0x08005202`) -- sie wird nie erreicht.

**Kein Kommando entfernt das Bit** (getestet: `0x10`, `0x37`, `0x34`,
`0x31/0x10202`, `0x2E`), und ein Software-Reset (`0x3E 0x80`) half auf der
Hardware ebenso wenig wie 90 s Stille. **Nur ein Power-Cycle** (Akku/Display
kurz trennen) setzt `FLASH_SR` zurueck.

Nachgestellt im Emulator: `python3 tools/emu.py blsticky` laeuft mit dem
*echten* Bootloader-Code und dem FLASH-Modell und prueft alle sechs Schritte
(Sollweg, PGERR, Erase -> Status 1, Schreiben -> 0x72, kein Kommando loescht
das Bit, Power-Cycle hilft) -- 13/13 Checks gruen.

### 9.6 Flash-Sonde vor dem Schreiben (Datensatzseite 0x08007800)

Der Erase-Befehl meldet auch dann Erfolg, wenn das Flash-Interface gesperrt
ist (er prueft nur BSY). Der erste Programmierversuch trifft dann **belegten**
Flash, setzt ein Fehlerbit und laesst danach gar nichts mehr zu (9.4).

Deshalb prueft ``Protocol.flash_canary()`` den kompletten Pfad
Unlock/Erase/Programm/Verify **vor** dem App-Schreiben -- auf der einzigen
Seite, die beschreibbar *und* wieder lesbar ist: dem Geraeterekord bei
``0x08007800`` (ausserhalb der App-CRC).

* schreiben: ``0x2E`` mit Magic ``0xF15A``, Payload-Laenge exakt 10
  (Jahr/Monat/Tag + 4 Datenbytes), ACK ``6e f1 5a``,
* zuruecklesen: ``0x22``/``0xF15B`` -> 6 Byte, Vergleich mit dem Geschriebenen.

Die Sonde laeuft dreimal: nach dem Sitzungsaufbau, nach dem Loeschen und nach
jedem Sitzungs-Neuaufbau (z. B. nach einem Takt-Stall). Schlaegt sie fehl,
wird **nichts** im Applikationsbereich geschrieben -- es genuegt ein
Power-Cycle, das Geraet bleibt unversehrt. Im Emulator geprueft durch
``python3 tools/emu.py uploadtool`` (dort laeuft die Sonde ueber den echten
Code mit).

### 9.5 Regel: jeden Halbwort nur EINMAL programmieren

Weil ein zweiter Schreibzugriff auf dieselbe Stelle das Fehlerbit setzt, darf
ein Wiederholungsversuch **niemals** schon geschriebene Bloecke erneut senden.
Nutze die Fehlerrahmen als Zaehler: bei einem Sequenzfehler (`0x73`) wurde der
erste abgelehnte Block und alles danach **nicht** geschrieben, alles davor
schon. Also gilt `geschrieben = gesendet - Fehlerrahmen`, und der
Wiederaufsetzpunkt ist die Adresse des ersten abgelehnten Blocks. Deshalb
wurde die fruehere Variante "Buendel komplett wiederholen" und auch ein
zweiter Durchgang ohne Loeschen (`--passes`) wieder entfernt.

End-to-End im Emulator: `python3 tools/emu.py uploadtool` fahrt den echten
`Protocol.upload()`-Pfad (Loeschen, Buendel mit Resync, CRC-Endkontrolle,
Reset) gegen den echten Bootloader-Code -- Flash == Image, Zeiger korrekt,
CRC-Status 0, keine Fehler, `FLASH_SR` sauber.

Empfohlener Ablauf bei diesem Bild:

1. Stromversorgung trennen, kurz warten, wieder anschliessen,
2. `python3 tools/stm_display_fw.py info` (Bootloader muss erscheinen),
3. `python3 tools/stm_display_fw.py flash data/stm32f105_bms_control.bin --region app`
   -- Erase, Buendel mit Wiederholungen, CRC-Endkontrolle, Reset.


## 10. CAN-Bruecke der App (HID <-> CAN) [V]

Die App ist nicht nur Nachrichtenempfaenger, sondern **Bruecke zum CAN-Bus**:
Rahmen aus dem HID-Transport werden gesendet, empfangene CAN-Frames zum Host
gemeldet. Verifiziert end-to-end im Emulator mit dem ausgelieferten Tool:
`python3 tools/emu.py canbridge`.

### 10.1 Senden (Host -> CAN)

Derselbe Verpacker wie beim App-Kanal (`0x0801D5C2`) schickt den Block
`[id_lo][id_hi][len][daten]` zusaetzlich in eine **eigene Sende-FIFO B**
(Zeiger `0x20000BA4`); die eigentliche Nachricht geht an FIFO A
(`0x20000BA0`). Der Port-Task `0x0801D900` leert FIFO B:

```
0x0801D75C  Typ-1-Rahmen  [seq][01][3D][id_lo][id_hi][len][daten]  (len<=58)
0x0801D65C  Typ-0-Rahmen  [seq][00][n*11][ (id_lo id_hi dlc daten[8]) * n ]  (n<=5)
0x0801D5C2  baut [id_lo][id_hi][len][daten] und pusht:
    - FIFO A, wenn 0x0801D4E2(id) = 1     (nur 0x550/0x552)
    - FIFO B, wenn 0x0801D522(id) = 1 und len<=8
0x0801D900  Port-Task; bei freier Mailbox 0x0801D158(r0) rufen
0x0801D158  Block aus FIFO B -> Slot 27: 0x0801BA24(id), 0x0801BA3A(dlc),
            0x0801BA46(daten), 0x0801B7A0(27)
0x0801B7A0  prueft CAN-bereit (0x20000AD4 Bit0) und Mailbox frei
            (0x20000ABC == 0xFFFF) -> 0x0801B07E(slot)
0x0801B07E  schreibt CAN1-Mailbox 0: TI0R = id<<21, TDT0R = DLC,
            TDL0R/TDH0R = 8 Datenbytes, dann TI0R |= 1 (TXRQ)
```

**ID-Filter (wichtig):** `0x0801D522` prueft mit der Tabelle `0x08037B3C`
gegen die ID. Die Routine ist **fehlerhaft** -- sie liefert 1 fuer jede ID
**ausser 0x0550** (`0x0801D5B8..0x0801D5BC`: nur wenn die ID in Tabelle A
*und* B steht, wird 0 zurueckgegeben). Praktische Folge: eine beliebige
11-Bit-ID `!= 0x0550` wird auf den CAN-Bus gesendet. `0x0550` ist der reine
App-Nachrichtenkanal und landet **nicht** auf dem Bus. (0x0552 geht in beide
Wege.)

Die CAN-Frame-ID ist die **Kanal-ID** des HID-Rahmens, `len` (max. 8) wird
zur DLC. Das Tool baut die Rahmen selbst (`tools/can_hid.py`).

### 10.2 Empfangen (CAN -> Host)

Der CAN-RX-Handler gibt passende Frames an den HID-Sender weiter:

```
IRQ20 (CAN1_RX0) -> 0x0801B066 (liest RF0R) -> 0x0801AFC6
  -> 0x0801D35A(r0 = CAN-State)
       r6 = STDID (RIR>>21), r7 = DLC, 8 Datenbytes
       0x0801D338(id)  Filter, Tabelle 0x08037B3E, Limit 6
       0x0800B59C()==4 (USB-Port bereit) und id != 0x0300
  -> 0x0801D1DA(typ=1, id, dlc, daten) -> 0x0801D1C0 (HID-IN-Report)
```

Der Host erhaelt also einen **Typ-1-Rahmen**: Kanal = CAN-ID, Nutzlast =
CAN-Daten. Zurueckgemeldet wird nur die feste ID-Liste

```
0x0422  0x0425  0x0101  0x0331  0x0668        (0x0300 wird uebersprungen)
```

andere IDs (z. B. 0x404/0x405 der BMS) bleiben ungemeldet — **generisch wird
der Empfang erst mit dem optionalen Patch C1** (§10.6). Weitergeleitete
Frames werden **nicht** mehr an die App-Handler im selben Pfad verteilt
(`0x0801D35A` liefert dann 0); C1 hebt genau das auf.

### 10.3 Sequenz und Quittungen

* Ein Steuerrahmen **Typ 0xFE** setzt den Zaehler `0x20000BAC` auf 0
  (`0x0801D744`). Danach muss die Sequenz im Byte 0 mit 0, 1, 2, ... laufen.
* Ein Rahmen mit falscher Sequenz wird verworfen und mit einem **Typ-0xFF**
  Rahmen beantwortet (`0x0801D1DA(0xFF,...)`).
* Erfolgreiche Sende-Rahmen werden **nicht** quittiert -- nur Fehler kommen
  zurueck. Das Tool synchronisiert bei einem 0xFF-Rahmen neu.

### 10.4 Werkzeug

```
python3 tools/can_hid.py info
python3 tools/can_hid.py send 0x201 01 02 03 04        # ein CAN-Frame
python3 tools/can_hid.py send 0x201 01 02 --repeat 10
python3 tools/can_hid.py batch --frame "0x201 01 02" --frame "0x202 03 04"
python3 tools/can_hid.py listen 5                      # CAN-Frames mitlesen
python3 tools/can_hid.py selftest                      # Rahmencodes
python3 tools/emu.py canbridge                         # Ende-zu-Ende ohne HW
```

Emulator-Nachweis (`canbridge`): Tool-Rahmen Typ 1 -> FIFO B, Typ-0-Batch ->
2 Bloecke; Sende-FIFO -> `TI0R=0x40200001` (= `0x201<<21 | TXRQ`), `TDT0R=4`,
`TDL0R=0xDDCCBBAA`; CAN-ID 0x0425 -> HID-Rahmen mit Kanal 0x0425 und den
Daten, 0x0404 -> keine Weiterleitung.

### 10.5 Stand / offene Punkte

* **[V]** Rahmenformate, ID-Filter, FIFO B, die Sende-Registerfolge
  (`0x0801D158` -> `0x0801B7A0` -> `0x0801B07E`) und die HID-Weiterleitung
  (`0x0801B066` -> `0x0801D35A` -> `0x0801D1DA`) sind im Emulator mit dem
  ausgelieferten Tool-Code belegt (`emu.py canbridge`).
* **[R]** Auf echter Hardware steht der Nachweis noch aus (wie beim
  Reset-Trigger, siehe 8.2). Der Port-Task leert FIFO B nur, wenn die
  Port-Abfrage `0x800A728(*(0x20000BB0))` 1 liefert (0x0801D948); das ist
  normaler Laufzeit-Zustand der App und wird hier nicht mitsimuliert.
* Der TX-done-ISR `0x0801B16A` zieht ueber `0x0801D196`/`0x0801D158`
  ebenfalls aus FIFO B, unterliegt aber der Scheduler-Zustandsmaschine
  (`0x20000ABC` = "in flight", Pending-Maske `0x20000AE8`).

## 11. Generischer CAN-Empfang (optionaler Patch C1) [V]

Der Empfangspfad aus §10.2 hat eine feste ID-Whitelist. Soll **jedes**
empfangene CAN-Frame am Host ankommen, ist ein Firmware-Patch noetig; das
Patch-Werkzeug `tools/patch_bms.py` liefert ihn als **optionale** Ergaenzung
(Standard-Auswahl bleibt unveraendert):

```
python3 tools/patch_bms.py --can-sniffer          # P1..P4/P6/P7 + C1
python3 tools/stm_display_fw.py flash data/stm32f105_bms_control.bin
```

### 11.1 Die drei Aenderungen

| Adresse | Original | Neu | Wirkung |
|---|---|---|---|
| `0x0801D338` | `02 46 00 20 00 21 08 e0` | `01 20 70 47 00 bf 00 bf` | Filter `0x0801D338` wird zu `movs r0,#1; bx lr` -> ID-Pruefung entfaellt |
| `0x0801D3C4` | `4f f0 00 08` (`mov r8,#0`) | `00 bf 00 bf` | `0x0801D35A` liefert kein 0 mehr -> **App-Handler-Dispatch laeuft weiter** |
| `0x0801D3AE` | `09 d0` (`beq 0x801D3C4`) | `00 bf` | auch `0x300` wird gemeldet |

Ohne die zweite Aenderung wuerde `0x0801D35A` fuer jedes Frame 0 liefern
("verbraucht") und der CAN-Handler `0x0801AFC6` wuerde die Frames **nicht**
mehr an die App ausliefern -- das Display saehe dann z. B. `0x201`/`0x555`
nicht mehr. C1 haelt den Dispatch deshalb erhalten.

### 11.2 Nachweis

`python3 tools/emu.py canbridge` prueft in einem Durchlauf:

* **Original**: `0x425` -> HID, `0x404` -> nicht gemeldet (Handler laeuft);
* **C1**: `0x404`, `0x201`, `0x300` -> HID **und** App-Handler `0x080194D6`
  bzw. `0x08019512`/`0x080167E4` laufen weiter.

Ferner im Image geprueft: `patch_bms.py --can-sniffer` erzeugt
`0xb5890bcf` als neue App-CRC, `emu.py` auf diesem Image meldet alle IDs
(0x404/0x201/0x300/0x555/0x425) und fuehrt `0x404` weiter an den Handler.

### 11.3 Grenzen

* Jedes CAN-Frame erzeugt einen 64-Byte-HID-Report. Bei hoher Buslast (viele
  hundert Frames/s) ist die Full-Speed-USB-Strecke der Engpass -- Frames
  gehen dann verloren (die FIFO-Pushs schlagen fehl, ohne die App zu stoeren).
* Der Patch ist **nicht** Teil der Standard-BMS-Firmware; ohne
  `--can-sniffer` bleibt das Image wie bisher.
