# BMS-Kontroll-Patch – STM32F105 "Display" (Continental / CEBS)

Ziel: **Das BMS (CAN `0x555` = „Spannung an") folgt deterministisch dem Display.**

| Display-Zustand | STM-Zustandsmaschine | `0x555` | BMS |
|---|---|---|---|
| läuft (2 s Button → ein) | Zustand 2 | `01` | **ein** |
| aus (langer Button → aus) | Zustand 1 / 3 | `00` | **aus** |

Erzeugt von `tools/patch_bms.py` aus `data/stm32f105_conti.hex`.

---

## 1. Warum das BMS bisher nicht zuverlässig einschaltete

`0x555` wird **nicht** ereignisgesteuert gesendet, sondern steht als periodische
TX-Botschaft (ca. alle 100 ms, DLC 1) in der Descriptor-Tabelle; **Bit 0 des
32-Bit-Worts bei `0x2000096C`** ist der Wert. Im Mitschnitt `can_log-3.csv` sieht
man das deutlich: alle ~100 ms ein `0x555`-Frame, anfangs `00`, ab t ≈ 1,9 s `01`.

Es genügt also, den Pufferinhalt zu setzen. Der Sender `0x0801673C` tat das aber
nur, wenn **drei** Bedingungen gleichzeitig erfüllt waren:

```c
void f_1673C(void) {                     // wird zyklisch in Zustand 2 gerufen
    if ([0x2000087E] != 1) {             // (1) Flag nur bei Empfang von CAN 0x201
        set_0x555(0);                    //     -> setzt 0x555 aktiv auf 0!
        return;
    }
    if (ramp == 15) set_0x555(1);        // (2) Rampenzähler muss exakt 15,
    ...                                  //     20, 30 oder 60 treffen
}
```

Dazu kam: `0x08016822` ist die Zustandsmaschine (läuft, wenn `f_1FD8E() != 0`):

| Zustand | Ablauf |
|---|---|
| 0 (Boot) | `f_b59c() == 2`? → 2, sonst → 1 |
| 1 | `f_166b0()` (ADC-Bypass + 500-ms-Zähler); bei Modus 2 → Zustand 2 |
| 2 | **`f_1673C()` (0x555-Logik)**; bei Modus 4 → Zustand 1 + `f_167B6()` |
| 2 | **`f_f1a0()==1` oder `[0x200008FF]==1` → Zustand 3 + `f_167B6()` + Standby** |
| 3 | nur `f_167da()` – **wird nie wieder verlassen** (Endzustand bis Power-Cycle) |

Die Abschaltpfade führen also in einen **Endzustand**, der BMS *und* Display
kostet – ohne Reset kommt man nicht heraus.

**Fazit:** `0x555` hing an CAN `0x201`, an einem Rampenzähler und an einem
Zustandsautomaten mit einer Sackgasse. Deshalb war das Einschalten unzuverlässig.

---

## 2. Bewertung des vorhandenen Patches

`data/edited_stm32f105_always_on_display.hex` enthält genau **3 geänderte Bytes**
gegenüber dem Original:

| Adresse | Original | Geändert | Wirkung |
|---|---|---|---|
| `0x08001A34` | `01` | `00` | Bootloader: App-CRC-Prüfung ausgehebelt |
| `0x080167AE` | `00` | `01` | `f_1673C`: Zweig „`[0x87E] != 1`" setzt `0x555 = **1**` statt `0` |
| `0x080167BE` | `00` | `01` | `f_167B6`: **Abschaltpfad setzt `0x555 = 1` statt `0`** |

Bewertung:

* Der Ansatz greift an der richtigen Stelle an (`0x555`-Puffer), ist aber
  **unvollständig**: er hebt nur die Abfrage `[0x2000087E] != 1` aus. Die
  Rampenbedingung (Punkt 2 oben) bleibt bestehen — `0x555` wird erst gesetzt,
  wenn der Rampenzähler 15 erreicht. Und die Zustandsmaschine kann weiterhin in
  den Endzustand 3 laufen, was `0x555` wieder auf 0 zieht.
* `0x080167BE` (Abschaltpfad) wurde **mitgepatcht** – dadurch geht das BMS bei
  ausgeschaltetem Display **nie mehr aus**. Das ist genau das Gegenteil des
  gewünschten Verhaltens.
* Die App-CRC in `0x0803FFFC` wurde **nicht** neu berechnet
  (`edited…hex`: CRC `0xca61f91b`, gespeichert `0x561feb04`). Das fällt nur
  nicht auf, weil die Prüfung in `0x08001A34` zugleich abgeschaltet wurde.

Kurz: als Notlösung brauchbar, aber er hängt weiterhin an den Zufälligkeiten der
Zustandsmaschine und kann das BMS nicht mehr abschalten.

---

## 3. Der Patch

`data/stm32f105_bms_control.bin` / `.hex` – erzeugt mit
`python3 tools/patch_bms.py`. Nur **17 Bytes** weichen vom Original ab
(8 + 2 + 2 + 1 Codebytes + 4 CRC).

### P1 – `0x0801673E` (8 Byte): `0x555 = 1`, sobald Zustand 2 läuft

```asm
;                  vorher                              nachher
0x0801673C: b510  push {r4, lr}                  b510  push {r4, lr}
0x0801673E: 4b76  ldr  r0, [pc, #180]  \         2001  movs r0, #1
0x08016740: 7800  ldrb r0, [r0, #0]     | 0x87E  f7fc fb6e  bl 0x8012E20   ; set_0x555(1)
0x08016742: 2801  cmp  r0, #1           |        bf00  nop
0x08016744: d133  bne.n 0x80167AE      /          ...   (Rampe läuft weiter)
```

`f_1673C` wird **nur** in Zustand 2 aufgerufen → `0x555 = 1` ist damit direkt an
„Display läuft" gekoppelt. Die 0x201-Abhängigkeit *und* die Rampenverzögerung
entfallen. Der folgende Rampen-Code bleibt unverändert erhalten (er setzt
denselben Wert und schreibt weiterhin `[0x20000727]`), damit sich sonst nichts
ändert.

Kosten: `f_1A616` wird jetzt zyklisch mit „1" gerufen statt mit „0" — exakt
dieselbe Aufruffrequenz wie im Original-Zweig `set_0x555(0)`.

### P2 – `0x080167BE` (1 Byte): Abschaltpfad schaltet das BMS wieder aus

`f_167B6` (Zustand 2 → 1 bzw. 2 → 3) schreibt `0x555 = 0`. Originalwert
wiederhergestellt; damit gilt: **Display aus → BMS aus.**

### P3 – `0x08001A34` (1 Byte): CRC-Prüfung des Bootloaders bleibt aktiv

`movs r5, #1` im CRC-Vergleich (`0x08001A02`). Das Werkzeug rechnet die CRC
korrekt nach (verifiziert gegen das Original: `0x561FEB04`), die Prüfung kann
also bleiben. Vorteil: Ein unvollständig geflashter Block landet im
USB-HID-Bootloader statt in einer halben Applikation.

### P4 – `0x080168A8` (2 Byte): keine Sackgasse mehr

```asm
0x080168A8:  03 d0  beq.n 0x80168B2   →   0a e0  b.n 0x80168C0
```

Überspringt `state = 3` + `0x555 = 0` + Standby. Zustand 2 wird damit **nur
noch** über den Display-Aus-Modus 4 verlassen (Richtung Zustand 1, aus dem es
zurückgeht) — kein Dauer-Aus mehr durch weggelaufene Watchdog-Flags
(`0x200008FF`, `0x20000994` Bit 0).

> Ohne P4 bleibt das ursprüngliche Verhalten erhalten; aufrufen mit
> `python3 tools/patch_bms.py --without P4`.

### P5 – `0x08009492` (2 Byte): Abschalt-Timer ausgebaut

Der zyklische Task `0x080093D2` (Periode 100 Ticks, ~100 ms) enthält einen
Countdown, der den STM selbst abschaltet:

```c
// Abschnitt 0x08009484..0x080094B8
if (f_0F390() <= 10 && mode != 4 && f_0EACA() != 1 && f_20144() != 1) {
    // keine Aktivität erkannt
    if ([0x20000100] != 0) {
        if (--[0x20000100] == 0)
            [0x200008FF] = 1;      // -> Zustand 3 = Dauer-Aus (Standby)
    }
} else {
    [0x20000100] = 3000;           // Nachladen (0xBB8)
}
```

Das ist der einzige firmware-seitige Auto-Abschalt-Countdown, der in
`[0x200008FF]` mündet — dieselbe Flag, die auch die Zustandsmaschine in den
Endzustand 3 treibt. Entfernt wird er durch Umbiegen der Verzweigung:

```asm
0x08009492:  04 d1  bne.n 0x800949E   →   ff e7  b.n 0x8009494
```

Ab jetzt wird der Zähler bei jeder Iteration auf 3000 nachgeladen und kann nie
mehr 0 werden; der Pfad `0x080094B4..0x080094B6` ist damit unerreichbar.
Der Rest des Tasks (Display-Zustandsmaschine, CAN-Status, ADC, LED) bleibt
unverändert.

> **Zeitangabe (nachgerechnet, nicht geschätzt):** Der RTOS-Tick ist
> **1 ms** — `0x0800AB36` schreibt `SysTick_LOAD = 71999` (= 72000 Takte bei
> 72 MHz) und `SysTick_CTRL = 7`. Gegenprobe aus dem CAN-Mitschnitt: die
> 0x1B5-Heartbeats kommen exakt alle **10 ms** (`can_log-3.csv`: 234, 244, 254,
> 264 …), also eine Task mit 10 Ticks.
>
> Damit gilt: Task `0x080093D2` = 100 Ticks = **100 ms Takt**, und
> 3000 × 100 ms = **300 s ≈ 5 Minuten Inaktivität** — **nicht 8 Stunden**.
> Der Countdown ist außerdem **aktivitätsabhängig**: er läuft nur, wenn
> `f_0F390() <= 10 && mode != 4 && f_0EACA() != 1 && f_20144() != 1`.
> **P5 ist also _nicht_ der 8-Stunden-Timer** (siehe Abschnitt 4).
>
> Ohne P5: `python3 tools/patch_bms.py --without P5`.

### P6 – `0x0801B944` (1 Byte): CAN-Bus-Off-Erholung einschalten

```asm
0x0801B942:  orr.w r1, r1, #1    →    orr.w r1, r1, #0x41    ; INRQ | ABOM
```

Siehe Abschnitt 4. Ohne P6: `python3 tools/patch_bms.py --without P6`.

### P7 – `0x080093D8` + Cave `0x0803FF00`: O2-Selbstversorgung

Statt die Schutzlogik abzuschalten, stellt P7 den Zustand her, den die Firmware
bei regelmäßigem Motor-Keepalive hätte: ein Trampolin im 100-ms-Task springt in
eine Code-Cave (28 Byte, im freien Block ab `0x08038184`), die das `0x201`-Latch
`[0x2000087E] = 1` setzt und die Nutzlast `[0x20000A0C] = 0x0100` schreibt
(Wire-Bytes `{00 01}` = „Fahrt"). Danach ruft sie die ersetzte
Zustandsmaschine auf.

```asm
0x080093D8:  0d f0 23 fa   (bl 0x8016822)  →  36 f0 92 fd  (bl 0x0803FF00)
```

Damit meldet die **echte** Formel `f_0F390() = 0x0100/10 = 25 > 10`:

* Der 5-Minuten-Timer lädt nach → **P5 wird überflüssig und automatisch
  übersprungen** (`--without P7` schaltet P5 wieder ein).
* Die Rampe in `f_1673C` findet das Latch gesetzt → `0x555 = 1` (P1 bleibt als
  Absicherung, ist aber nicht mehr nötig).

Quellcode: **`tools/o2_cave.s`** (Platzierung `tools/o2_cave.ld`), Bauen und
Anzeigen mit `python3 tools/patch_bms.py --print-o2`; Details, Bytes und die
Adress-Symbole in Abschnitt 10.

### Was *nicht* gepatcht wurde

Beabsichtigt unverändert: die Rampenlogik, `f_166B0` (ADC-Bypass),
`f_16678` (Init), alle CAN-Filter, der Watchdog selbst und die
BMS-Dialogfunktionen (`0x551`/`0x550`/`0x552`).

---

## 4. Zweite Ursache: der CAN-Controller bleibt stumm (STM läuft weiter)

**Symptom:** keine CAN-Nachrichten mehr (auch keine 0x1B5-Heartbeats), aber die
Batteriestands-LEDs leuchten weiter — der STM ist also nicht abgestürzt.

Das ist in der Firmware klar sichtbar. Der bxCAN des STM32F105 hat einen
Fehlerzähler; überschreitet TEC 255, geht er in den **Bus-Off** und nimmt nicht
mehr am Bus teil. Dort kommt er nur heraus, wenn **`CAN_MCR.ABOM`** gesetzt ist
(dann erholt er sich nach 128×11 rezessiven Bits selbst) *oder* wenn Software
`MCR.INRQ` erst setzt und dann wieder löscht.

Was die Firmware tatsächlich tut:

| Funktion | Verhalten |
|---|---|
| `f_1B8C8` (0x0801B8C8) | liest `CAN_ESR` (über `CAN_MSR+20`) und unterscheidet **Bus-Off** (`\|0x40`), Error-Passive (`\|0x20`), Error-Warning (`\|0x10`), Init (`\|0x02`) |
| Task 0x08009404 | `if (status & 0x72)` → `f_135DE(1)` — der Fehler wird also **nur gemeldet** (0x1B2-Fehlernahmen) |
| `f_1B3D8` (0x0801B3D8) | CAN-Modus setzen: SLEEP löschen, Mailboxen abbrechen, `MCR \|= 1` (INRQ), auf INAK warten |
| `f_1B93C` (0x0801B93C) | dieselbe Sequenz — **der einzige Ort, an dem `MCR` beschrieben wird** |
| `f_1C398` (0x0801C398) | CAN-Zustandsmaschine: Zustand 2 = INIT, Zustand 3 = RUNNING; Re-Init nur, wenn `[0x20000AF9] == 2` |

Damit gilt:

* `CAN_MCR` wird **ausschließlich** mit `\|=1` (INRQ) und `&=~2` (SLEEP)
  beschrieben → **`ABOM` (Bit 6) ist nie gesetzt**.
* `CAN_ESR` wird gelesen, der Zustand aber nur als 0x1B2 gemeldet — es gibt
  **keine automatische Reaktion** auf Bus-Off im Normalbetrieb.
* Die Re-Init-Kette (`[0x20000AF9] = 2` → `f_1C320`) wird nur aus dem
  Init-Pfad heraus aufgerufen, nicht aus dem Fehlerpfad `f_1C362`
  (der setzt nur `[0x20000AFA] = 4`).

**Wie es ohne eigene 0x555-Frames dazu kommt — ACK-Fehler:**

Ein Bus-Off braucht **keine** Kollision. Es genügt, dass der STM zeitweise der
**einzige aktive Knoten** am Bus ist — also z. B.:

* der ESP ist aus / im Reset, oder
* der ESP hat seinen Transceiver über den **STANDBY-Pin** (GPIO21) stillgelegt,
  oder
* der Bus ist nicht (korrekt) abgeschlossen bzw. eine Leitung unterbrochen.

Dann wird das **ACK-Bit** im ACK-Slot von niemandem dominant gezogen. Der bxCAN
wertet das als Fehler, sendet ein Error-Frame und erhöht **TEC um 8** pro
Vorgang. `MCR.NART` ist **nicht** gesetzt, es wird also automatisch wiederholt —
nach spätestens 32 Fehlversuchen steht TEC über 255 und der Controller geht in
**Bus-Off**. Das passiert bei reinem Senden, ganz ohne Fremdverkehr.

**Ergebnis:** Ein einziger Bus-Off lässt die Firmware dauerhaft stumm, während
Display, LEDs und Aufgaben weiterlaufen — genau das beobachtete Bild.
**P6 setzt `MCR.ABOM`**; der Controller holt sich dann nach 128×11 rezessiven
Bits selbst zurück, sobald wieder ein zweiter Knoten am Bus ist.

### Weitere Abschaltquellen (geprüft)

| Quelle | Ort | Wirkung |
|---|---|---|
| Countdown `[0x20000100]` | 0x080094B4 | `[0x200008FF] = 1` → Zustand 3 — **mit P5 entfernt** |
| 0x201-Watchdog-Timeout | 0x080195D8 | `[0x200008FF] = 1`, wenn Zustand 4 und Flag gesetzt — **mit P4 entschärft** |
| CAN-ID 0x300 | 0x0801D11E | `[0x200008FF] = 1` — **nicht erreichbar**, 0x300 fehlt in der RX-Filterliste 0x08036CD8 |
| `[0x20000994]` Bit 0 | 0x0800F1A0 | Zustand 2 → 3 — **mit P4 entschärft** |

Der RTOS-Tick ist **1 ms** (`SysTick_LOAD = 71999` in `0x0800AB36`, bestätigt
durch 10-ms-Heartbeats im Mitschnitt). Ein 8-Stunden-Wert ist im gesamten Flash
**nicht vorhanden**. Geprüft wurden — jeweils als Literalpool-Eintrag *mit*
Code-Referenz, als `movw`-Immediate und als Rohbytefolge in allen vier
Ausrichtungen:

`28800`, `288000`, `2880000`, `28800000`, `480`, `2880`, `14400`, `21600`,
`43200`, `86400`, `172800`, `57600` — **null Treffer**.

Die einzigen großen Zeitkonstanten im Image sind `36000000` (0x080132A4, wird
per `udiv` als Teiler benutzt — Zeitbasis-/Frequenzrechnung, kein Timer) und
`71999` (SysTick-Reload). Die 8 Stunden kommen daher **nicht** aus einem
Konstanten-Vergleich dieser Firmware.

Was in Frage kommt:

* ein **Akkumulator** aus vielen Reloads (dann müsste ein Zähler saturieren) —
  Kandidat: `[0x20000904]` in `0x080169BA`, saturiert bei `0x071C71C7`
  (= 2³²/36 ≈ 119,3 Mio.). Das ist aber eine **Statistik** (Trip-/Maximalwert,
  geht per `f_0EFC2(8, …)` nach außen) und **setzt kein Abschaltflag**;
* eine Abschaltung von außen (BMS, Motorsteuergerät, LED-Board) über CAN —
  beachte: `[0x200008FF] = 1` wird auch von **CAN-ID 0x300** (`f_1D114`)
  ausgelöst (in der Original-Filterliste nicht enthalten, aber ein anderes
  Gerät könnte 0x555/0x554-seitig abschalten);
* ein Hardware-Timer außerhalb des STM.

---

## 5. Sicherheit der Patches

* **Keine Stack-Änderung.** P1 nutzt nur `r0` innerhalb der vorhandenen
  `push {r4, lr}` / `pop {r4, pc}`-Klammer. P4 ist ein reiner Sprung.
* **Keine Literal-Pools überschrieben.** Der Pool bei `0x080167F4` wird von
  P1 nicht berührt; P1 endet vor `0x08016746`.
* **Keine neuen Sprungziele.** P1 nutzt den vorhandenen Aufruf `0x08012E20`,
  P4 springt auf das vorhandene `0x080168C0`.
* **Rücksprung bleibt erhalten.** Nach P1 läuft der Rest von `f_1673C`
  unverändert bis `pop {r4, pc}`.
* **Idempotent & selbstprüfend.** `patch_bms.py` prüft an jeder Stelle die
  erwarteten Ausgangsbytes und bricht bei fremder Firmware ab; ein zweiter Lauf
  liefert bitidentisch dasselbe Image.
* **CRC-Nachrechnung** mit der verifizierten STM32-Hardware-CRC
  (`crc32_stm32`), Ergebnis `0x5ED6678E` – geprüft mit
  `tools/stm_display_fw.py verify`.

---

## 6. Verifikation

### Statisch

* **Byte-Diff:** genau **46 Bytes** geändert – P1 (8) + P4 (2) + P6 (1) +
  P7 (Trampolin 3 + Code-Cave 28) + CRC (4). P5 entfällt (durch P7 abgelöst).
* **Disassembly** aller Stellen mit `arm-none-eabi-objdump` geprüft:
  Sprungziele lösen exakt auf (`bl 0x8012e20`, `b.n 0x80168c0`,
  `orr.w r1, r1, #0x41`, `bl 0x803ff00`) — letzteres kommt direkt aus dem
  Assembler-Build von `tools/o2_cave.s`.
* **CRC:** `0x81517823`, geprüft mit `tools/stm_display_fw.py verify`.
* **HEX/BIN-Roundtrip** bitidentisch; Skript bei zweitem Lauf idempotent.

### In der Emulation (`tools/emu.py bmspatch`)

37 Checks gegen **Original und gepatchte Firmware gleichzeitig**; die gepatchte
wird absichtlich über den `.hex`-Pfad geladen. Es läuft jeweils echter
Firmware-Code (Unicorn/Cortex-M3), keine Nachbildung:

| # | Prüfung | Original | Patch |
|---|---|---|---|
| A | `0x201`-Handler `0x080167E4` setzt `[0x2000087E]` | 1 | 1 |
| B | `f_1673C` ohne `0x201` (Rampe 0 / 15) | 0 / 0 | – |
| C | `f_1673C` mit `0x201` (Rampe 0 / 15) | 0 / **1** | – |
| D | `f_1673C` ohne `0x201` (Rampe 0) | – | **1** |
| E | Abschaltpfad `f_167B6` | – | **0** |
| F | Zustandsmaschine, Modus 2 → Zustand / `0x555` | 2 / 1 | 2 / 1 |
| G | Zustand 2 + `[0x8FF]=1` + `f_f1a0()=1` | **3 / 0** | **2 / 1** |
| I | Modus 4 (Display aus) → Zustand / `0x555` | – | 1 / 0 |
| J | Bootloader-CRC `0x08000924` == Wert bei `0x0803FFFC` | OK | OK |
| K | Task mit leerer Nutzlast **ohne** Cave-Lauf (Harness) | Flag **1** | Flag **1** |
| L | `f_1B93C` → `CAN_MCR` | `0x01` | `0x41` (ABOM) |
| M | echte Formel `f_0F390()` bei Nutzlast `0x0000` / `0x0100` | 0 / 25 | 0 / 25 |
| M | Abschaltflag dazu | **1** / 0 | **1** / 0 |
| N | **P7-Cave im Image ohne P1/P5**: Latch, Nutzlast, Zustand, `f_0F390()`, Timer, Flag | – | 1 / `0x0100` / 2 / 25 / **3000** / **0** |
| N | Gegenprobe gleiches Image **ohne** Cave | – | Timer 0, Flag **1** |

Aussage der Schlüsselpaare:

* **B vs. C** — die Emulation ist originalgetreu: das Original *kann* `0x555`
  auf 1 setzen, aber **nur** mit empfangenem `0x201` *und* Rampenzähler 15.
  Das ist genau die Ursache des unzuverlässigen Einschaltens.
* **D** — der Patch hebt beide Bedingungen auf.
* **G vs. H** — beweist P4: im Original führt `[0x200008FF]` in die Sackgasse
  Zustand 3 (dauerhaft aus, auch für das Display), mit Patch bleibt Zustand 2.
* **J** — die emulierte CRC-Funktion rechnet über den echten Applikationsbereich
  und trifft den gespeicherten Wert → der Bootloader startet das gepatchte Image
  (P3 ist also unschädlich).
* **K** — der Task `0x080093D2` läuft mit realem Code eine Iteration durch; im
  Original setzt der abgelaufene Countdown `[0x200008FF] = 1`, mit P5 bleibt das
  Flag 0 und der Zähler steht auf 3000.
* **L** — `f_1B93C` schreibt im Original nur `MCR = 0x01`, mit P6 `0x41`
  (INRQ | ABOM).

Regression: alle 15 Emulator-Szenarien laufen unverändert durch
(`crc`, `dispatch`, `frame`, `setaddr`, `writeblk`, `appreset`, `trigger`,
`blprobe`, `blsession`, `nmstate`, `msgprobe`, `hidtrigger`, `caninject`,
`uploadtool`, `bmspatch`).

---

## 7. Flashen

```sh
python3 tools/patch_bms.py                              # erzeugt bin + hex
python3 tools/stm_display_fw.py verify data/stm32f105_bms_control.hex
python3 tools/stm_display_fw.py upload data/stm32f105_bms_control.bin --region app
```

`--region app` schreibt `0x08008000..0x0803FFFF`. Der Bootloader-Bereich
(`0x08000000..0x08007FFF`, enthält P3) bleibt davon unberührt und muss getrennt
geschrieben werden (`--region all` bzw. separater `setaddr`-Lauf), falls die
CRC-Prüfung im Bootloader wieder aktiviert werden soll.

## 8. Erwartetes Verhalten danach

| Ereignis | Erwartung |
|---|---|
| 2 s Button → Display ein | Zustand 0 → 2, `0x555 = 01` innerhalb ~100 ms, BMS ein |
| langer Button → Display aus | Modus 4 → Zustand 1, `0x555 = 00`, BMS aus |
| Button → wieder ein | Modus 2 → Zustand 2, `0x555 = 01`, BMS ein |
| ESP-Keepalive `0x201` fällt aus | `0x555` **bleibt 1** (P1); kein Dauer-Aus mehr (P4) |
| Bus-Off am ESP | Display und BMS bleiben an |
| Bus-Off am **STM** | P6: der bxCAN erholt sich nach 128×11 rezessiven Bits selbst — Heartbeats laufen wieder an |
| Inaktivität (Display an, kein Verkehr) | **P7**: Aktivität ist dauerhaft vorhanden (`f_0F390() = 25`), der Zähler lädt nach — kein Abschalten nach 5 min |
| Gar kein CAN am Bus (kein ESP, kein Motor) | **P7**: `0x555 = 01` trotzdem, Display bleibt an |

Kontrolle im Web-UI (`/canlog`): Spalte `0x555` muss dem Displayzustand folgen,
und `0x1B5` Byte 0 sollte `0x19` statt `0x00` zeigen.

---

## 9. Ganz ohne externe CAN-Nachrichten: wo `0x201` wirklich gebraucht wird

Frage: Reicht der Patchsatz, damit die Firmware **ohne jede** externe Botschaft
(kein `0x201`, kein Motor-Keepalive) eigenständig aktiv bleibt — oder braucht sie
den Zustand „`0x201` mit Fahrt" weiterhin?

### 9.1 Was `0x201` im Datenmodell ist (verifiziert)

| Fakt | Wert |
|---|---|
| ID → Signal | `0x201` = Signal **14** (Tabelle `0x08036BE4`) |
| Signal → RAM-Slot | **`0x20000A0C`**, erwartete Länge **4** (`0x08036C80[14]`, `0x08036C68[14]`) |
| Decode-Handler | `0x08019512`, Post-Call `0x080167E4` |
| Post-Call | setzt **Latch `0x2000087E = 1`** |
| Latch löschen | nur `f_16678`, aufgerufen **einmalig** über `0x080093A6` (Task-Init) → „`0x201` war irgendwann da" |
| zusätzlich | Handler setzt Bit 7 in `0x20000AF7` (Leser: `0x0801C144`) |
| Nutzlast | bleibt als 4 Byte in `0x20000A0C` stehen |

Die **Nutzlast ist das Aktivitätssignal**: `f_19926()` liest die ersten beiden
Bytes von `0x20000A0C` als 16-Bit-Wort (Little-Endian).

### 9.2 Die drei Verbraucher

1. **`0x555`-Freigabe** in `f_1673C`: Gate auf das **Latch** → durch **P1**
   entfernt.
2. **5-Minuten-Timer** (`0x08009480`, siehe P5): lädt nur nach, wenn
   `f_0F390() > 10` mit `f_0F390() = Wort(0x20000A0C) / 10`. Ohne `0x201` ist das
   Wort 0 → `0/10 = 0` → Countdown `3000 × 100 ms = 300 s` → `0x200008FF = 1`
   → Zustand 3 (Dauer-Aus) → `0x555 = 0`.
   Mit dem ESP-Keepalive `{00 01 00 00}` ist das Wort **`0x0100` = 256** →
   `256/10 = 25 > 10` → Aktivität erkannt. **Das ist der „Fahrt"-Zustand, den
   die Firmware ohne CAN nicht sieht.**
   Im Emulator beidseitig nachgewiesen (Checks `M` in `tool/emu.py bmspatch`):
   `P_X201=0x0000 → f_0F390()=0 → Flag=1` und
   `P_X201=0x0100 → f_0F390()=25 → Zähler=3000, Flag=0`.
3. **Abschaltaktion** `f_195BE` (Aktionstabelle `0x08037908` Index 7, nur
   indirekt aufgerufen): wenn **Fahrzustand `0x20000C5C == 4`** und Latch `== 1`
   → löscht `0x20000A0C/0D` **und** setzt `0x200008FF = 1`. Die Wirkung ist
   durch **P4** entschärft, der Schreibzugriff auf die Nutzlast bleibt.

Weitere Aktivitätsquellen im selben Task (für den CAN-losen Fall irrelevant):
`f_B59C() == 4` (USB-/NM-Zustand), `f_EACA() == 1`, `f_20144() == 1`.

### 9.3 Was der heutige Patchsatz leistet — und was er kostet

* Mit **P1 + P4 + P5** ist emulativ kein Pfad mehr offen, der ohne CAN in
  Zustand 3 oder auf `0x555 = 0` führt (Checks `F`, `G`, `K`).
* Es ist aber **Symptombekämpfung**: die Firmware „glaubt" weiterhin, es gebe
  keinen Motor und keine Fahrt. Alles, was an `f_1FD8E()` (Fahrzustand
  `0x20000C5C`, **24 Aufrufer**) oder an `0x20000AF7` hängt, bleibt im
  „Nicht-Fahrt"-Zweig.
* **P5** hebelt die Schutzlogik aus (immer nachladen), statt den Zustand
  herzustellen.

### 9.4 Optionen

| # | Ansatz | Wirkung | Bewertung |
|---|---|---|---|
| **O1** | **Nutzlast vorbelegen**: in `f_16678` (läuft einmal beim Task-Start) `[0x20000A0C] = 0x0100` schreiben, optional Latch `[0x2000087E] = 1` | Die Firmware sieht exakt den Zustand „`0x201` mit Fahrt-Byte liegt an"; der 5-Minuten-Schutz bleibt **echt** und scharf | **bevorzugt**: wenige Bytes, P4/P5 werden überflüssig. Hürde: in `f_16678` ist kein freier Literalpool-Platz — die Adresse `0x20000A0C` muss aus einem vorhandenen Pool erreicht werden (z. B. `0x0801984C` in `f_195BE`) oder der Schreibbefehl zieht in eine andere Init-Funktion |
| **O2** | **Signalquelle nachbilden**: `0x20000A0C` zyklisch (z. B. im 100-ms-Task) neu schreiben | wie O1, aber „lebendig" (auch nachdem `f_195BE` den Slot gelöscht hat) | mittel: ~20 B Code, braucht freien Platz |
| **O3** | `f_0F390` (oder das nur von ihm genutzte `f_19926`) auf konstant > 10 patchen (`movs r0,#11; bx lr`) | Timer lädt immer nach | 2–4 Byte, aber qualitativ dasselbe „Aushebeln" wie P5 |
| **O4** | Statusbit `0x20000AF7` mitbedienen | Firmware meldet BMS/Diagnose „`0x201` gesehen" | offen: Leser `0x0801C144` noch nicht analysiert |
| **O5** | Fahrzustand `0x20000C5C = 4` setzen | Verhalten wie „Fahrt" in allen 24 `f_1FD8E()`-Nutzern | **riskant**: Nebenwirkungen unbekannt; der Writer ist noch nicht lokalisiert (kein Literalpool-Eintrag, vermutlich über Basiszeiger/Struktur) |
| **O6** | nicht patchen, `0x201` weiter vom ESP senden (70–100 ms) | minimal-invasiv | Abhängigkeit bleibt: ESP-Ausfall oder Bus-Off ⇒ Abschaltung nach 5 min |

### 9.5 Empfehlung

1. **O1** umsetzen und danach **P4/P5 entfernen** — das ist die semantische
   Lösung: der Zustand wird hergestellt, statt die Schutzlogik zu lähmen.
   Prüfbar mit den `M`-Checks (müssen unverändert 25/0 liefern, jetzt ohne
   Eingriff am Timer).
2. `f_16678` ist der richtige Ort (läuft genau einmal, schreibt schon
   RAM-Flags); alternativ das Task-Init `0x080093A6`.
3. **O5** erst angehen, wenn der Writer bekannt ist — sonst blind.
4. Offen bleibt die **Batterie-Seite**: die beobachteten „5 Minuten" passen
   exakt zu `3000 × 100 ms` im Display, also stammt der Standby-Schutz aus der
   Display-Firmware. Ob die BMS zusätzlich eine eigene Standby-Regel hat, lässt
   sich nur über einen Mitschnitt (`can_log`) mit `0x555 = 01` über > 5 min
   klären. Solange das nicht belegt ist, ist „BMS-seitiger Timer" Spekulation.

---

## 10. Platz für O2: freier Block und Einhängepunkt

Im Applikationsbereich gibt es einen **zusammenhängenden gelöschten Bereich von
32 376 Byte (31,6 KiB)**:

```
0x08038184 .. 0x0803FFFB   0xFF  (endet direkt vor dem App-CRC-Wort 0x0803FFFC)
```

> **Lehre aus dem ersten Versuch: 0xFF heißt *nicht* „unbenutzt“.** Der Block
> wird an seinem **ersten Byte referenziert** — der Datenbereich enthält einen
> Deskriptor `0x08037F54 -> 0x08038184`. Die Stelle wird also möglicherweise als
> *Wert* gelesen (z. B. als „belegt/frei“-Feld); ein Cave-Start genau dort ist
> riskant. Gegenprobe, dass 0xFF Daten sein können: der 148-Byte-0xFF-Lauf bei
> `0x0802F901` ist in Wahrheit eine **36-Byte-Record-Tabelle**
> (`0x08035AB8..` zeigt auf `0x0802F707`, `+0x24`, `+0x48` …) — dort liegen
> `0xFF`-Bytes als Nutzdaten.
>
> Deshalb prüft `tools/patch_bms.py` jetzt **vor** dem Patchen (und bricht sonst
> ab): Cave-Bereich muss `0xFF` sein **und** kein 4-Byte-Wort im ganzen Image darf
> in die 2-KiB-Seite der Cave zeigen (Pointer, Deskriptoren, Literal-Pools sind
> alle 4-Byte-ausgerichtet). Ausnahme: Zeiger auf das App-CRC-Wort.

**Gewählter Platz: `0x0803FF00`** — in der letzten App-Seite
`0x0803F800..0x0803FFFF`. Sie enthält nur das CRC-Wort bei `0x0803FFFC` und ist
sonst leer; sie ist die einzige Stelle im gelöschten Block, die **kein**
Datensammler belegen darf (er würde die CRC zerstören). Die Seite ist damit
nicht nur „unreferenziert“, sondern strukturell reserviert. Kleinere
0xFF-Lücken (1 KiB bei `0x08023F0D` usw.) sind dagegen **nicht** verwendbar —
sie gehören zu Tabellen (siehe oben).

**Einhängepunkt (Trampolin, ohne ein einziges freies Byte an Ort und Stelle):**
umgesetzt als **P7** in `tools/patch_bms.py`. Statt Code einzuklemmen, wird ein
vorhandener `bl` im 100-ms-Task **umgebogen** und der ursprüngliche Aufruf aus
dem Block heraus nachgeholt:

```asm
; 0x080093D8 im 100-ms-Task 0x080093D2
- bl 0x8016822        ; Zustandsmaschine      (0d f0 23 fa)
+ bl 0x0803FF00      ; -> Code-Cave          (36 f0 92 fd)
```

**Der Code selbst ist Assembler-Quelltext im Repo** — `tools/o2_cave.s`
(Platzierung `tools/o2_cave.ld`, Bauen/Anzeigen mit
`python3 tools/patch_bms.py --print-o2`). `patch_bms.py` assembliert ihn beim
Patchen selbst und prüft die Adressen als Symbole (`--defsym`):

```asm
o2_tramp:                            @ .tramp, platziert auf 0x080093D8
    bl      o2_cave                  @ = 0x0803FF00

o2_cave:                             @ .cave, platziert auf 0x0803FF00
    push    {r4, lr}
    movs    r0, #1
    bl      F_167E4                  @ Latch [0x2000087E] = 1 ("0x201 war da")
    ldr     r1, =O2_X201_SLOT        @ r1 -> 0x20000A0C
    movs    r0, #O2_X201_B0
    strb    r0, [r1, #0]
    movs    r0, #O2_X201_B1          @ 0x01 = "Fahrt"
    strb    r0, [r1, #1]
    bl      F_16822                  @ der ersetzte Originalaufruf
    pop     {r4, pc}
    .ltorg                           @ Literalpool: 0x20000A0C
```

Fertige Bytes (28 + 4, aus dem Assembler):

```
Cave      0x0803FF00: 10 b5 01 20 d6 f7 6e fc 03 49 00 20 08 70 01 20 48 70
                       d6 f7 86 fc 10 bd 0c 0a 00 20
Trampolin 0x080093D8: 36 f0 92 fd        (ersetzt 0d f0 23 fa)
```

> **Gelernt beim Bau (vom Emulator gefunden):** `f_167E4` ist eine normale
> C-Funktion und clobbert nach AAPCS `r0..r3`. Wird der Zielzeiger `r1` **vor**
> dem Aufruf geladen, zeigt er danach auf `0x1` → Schreibzugriff ins Nichts
> (`UC_ERR_MAP` im Emulator). Deshalb wird `r1` erst **nach** dem Latch-Aufruf
> geladen. Die Reihenfolge im `.s` ist damit nicht beliebig.

Wirkung: die Firmware sieht dauerhaft genau den Zustand, den sie bei
regelmäßigem `0x201` mit Fahrt-Byte hätte (`f_0F390() = 25 > 10` → 5-Minuten-Timer
lädt nach). Damit wird **P5 überflüssig** — `patch_bms.py` überspringt P5
automatisch, solange P7 dabei ist (`--without P7` schaltet P5 wieder ein) — und
der Originalcode funktioniert von selbst:

* `f_1673C` (Rampe) findet das Latch gesetzt und setzt `0x555 = 1` beim ersten
  Rampenpunkt (15, ~1,5 s nach Zustandswechsel) — der Wert bleibt danach stehen.
* Der Countdown lädt nach, weil Aktivität erkannt wird.

**P1 bleibt trotzdem drin** (Absicherung, kostet nichts) — P7 allein genügt aber
ebenfalls, das prüft der Emulator mit einem Image ohne P1/P5 (Checks `N`).

**Achtung, dann wird P4 wichtig:** mit dauerhaft gesetztem Latch löst die
Abschaltaktion `f_195BE` (`Fahrzustand 0x20000C5C == 4` **und** Latch) das
Abschaltflag `0x200008FF` aus. Ohne P4 führt das in den Endzustand 3. P4 deckt
genau diesen Pfad ab.

## 11. Verhalten, wenn `0x201` sofort mit „Fahrt" beginnt

Geprüft gegen `f_1673C` (siehe §1) — es gibt **keine** „der Motor muss im Stand
starten"-Sperre:

| Bedingung | Wirkung |
|---|---|
| Latch `0x2000087E == 1` | Voraussetzung für alles Weitere (P1 entfernt sie) |
| Rampenzähler `== 15` | `0x555 = 1` — **ohne** weitere Bedingung |
| Rampenzähler `== 20 / 30 / 60` | `0x555 = 1` nur wenn `f_0F1B2() == 0` |

`f_0F1B2()` ist **Bit 1 des `0x202`-Signals** (Slot `0x20000A04`, DLC 8). Das ist
die einzige Stelle, an der ein Motor-/Fahrzeugzustand die Freigabe beeinflusst —
und sie wirkt nur auf die **Wiederholungspunkte** 20/30/60, nicht auf den ersten
(15). Praktisch heißt das:

* Ja, es gibt ein Gate, und es liest den Motorzustand (`0x202` Byte 0 Bit 1).
  Ob das „Motor läuft" oder „Stillstand erkannt" bedeutet, ist **nicht belegt**
  (kein Wakeup-/Timeout-Nachbar, kein Default-Wert im Image).
* Nein, es verhindert den Start nicht: der erste Freigabepunkt nach dem
  Zustandswechsel ist immer möglich.
* Wichtig für die Fahrt-Erkennung: das „Fahrt"-Byte ist **Byte 1** der
  `0x201`-Nutzlast. `{00 01 ...}` ⇒ Wort `0x0100` ⇒ `f_0F390() = 25 > 10`.
  `{00 00 ...}` (**nur Latch, kein Fahrt-Byte**) ⇒ Wort 0 ⇒ **keine** Aktivität ⇒
  der 5-Minuten-Timer läuft trotz `0x201` ab. Genau deshalb sendet der
  Referenzcode `sendCAN(0x201, 4, 0, 1, 0, 0)`.

## 12. Abschaltung bei zu hohem Strom — STM oder BMS?

Beobachtung: die Batterie schaltet ab, wenn innerhalb von ~2 s nach `0x555 = 1`
ein zu hoher Strom (≈ > 2 A) fließt. Befund aus der STM-Firmware: **das ist
nicht dort implementiert.**

Belege:

1. `set_0x555` (`f_12E20` → `f_1A616`, schreibt **Bit 0** des Worts `0x2000096C`)
   hat genau **6 Aufrufer**, alle in `f_1673C`/`f_167B6`:
   `0x08016750/64/78/8C` (Rampe, `= 1`) und `0x080167B0/0x080167C0` (`= 0`).
   Damit sind die **einzigen** Abschaltgründe: fehlendes Latch oder das
   Verlassen von Zustand 2 (Modus 4, `f_F1A0()`, Abschaltflag). **Kein
   stromabhängiger Pfad.**
2. Die Batteriedaten (`0x404` → Slot `0x200009D0`) haben drei Code-Nutzer:
   `f_0F164` (Byte 4 = SOC, auf 100 begrenzt), `f_0F170` (Byte 5),
   `0x08017B48/0x08017B66` (Byte 4/5 in einen TX-Rahmen) und `f_19864`
   (**Byte 2–3 = Spannung**). **Byte 0/1 (Strom) wird nirgends gelesen.**
   Einziger Schreiber: `f_195A4` setzt Byte 4 auf `0xFF` (Daten ungültig).
3. Der STM misst den Hauptstrom nicht selbst (nur ADC-Kanal 9 für den Taster/
   die Boardspannung, `0x0800E4C2`).
4. Muster: „kleines Zeitfenster nach dem Einschalten + niedrige Stromgrenze" ist
   eine klassische **Vorlade-/Softstart-Prüfung**. Sie sitzt dort, wo der
   Leistungsschalter und der Stromsensor sitzen — in der **BMS**.

**Gegenprobe am Motorrad (entscheidend, ohne Firmware-Eingriff):** während des
Abschaltens den `can_log` mitlaufen lassen.

* Läuft `0x555 = 01` **weiter** (und `0x1B5`-Heartbeats) bis der Bus stirbt →
  die BMS hat abgeschaltet, der STM hat es nicht befohlen.
* Geht `0x555` auf `00` oder brechen die Frames ab, obwohl der STM noch
  Spannung hat → der STM hat abgeschaltet; dann greift einer der drei Pfade aus
  (1). Zusätzlich `0x420` beobachten: **Bit 0** ist der einzige externe
  „Fehler → Endzustand 3"-Eingang (`f_F1A0()`, Slot `0x20000994`). Der ESP32
  sendet `0x420` nicht — solange das so bleibt, kann dieser Pfad nicht feuern.

Was daraus folgt: gegen eine BMS-Vorlade-/Überstromregel hilft kein
STM-Patch. Ansatzpunkte sind dann die Last-Seite (Vorladewiderstand/Relais wie
im Original-Controller, Last erst nach der Freigabe zuschalten) oder ein
späteres `0x555 = 1` (der ESP32 hat dafür bereits den `0x555`-Test-Timer).
Der einzige *firmware*-seitige Hebel wäre, `0x555` erst nach dem Anlaufen der
Last zu setzen — dafür gibt es aber keinen Sensor im STM.
