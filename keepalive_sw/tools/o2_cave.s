@ ---------------------------------------------------------------------------
@ o2_cave.s -- O2 "Selbstversorgung": Code-Cave + Trampolin
@              fuer die STM32F105-Display-Firmware (data/stm32f105_conti.hex)
@
@ Zweck
@   Die Firmware haelt den Akku (CAN 0x555 = "Spannung an") nur dann dauerhaft
@   an, wenn sie regelmaessig CAN 0x201 mit dem Fahrt-Byte empfaengt: der
@   5-Minuten-Timer laedt nur nach, solange f_0F390() = u16(RAM 0x20000A0C)/10
@   groesser als 10 ist. Ohne 0x201 ist dieses Wort 0 -> Countdown 3000 x 100 ms
@   -> Abschaltflag 0x200008FF -> Zustand 3 (Dauer-Aus) -> 0x555 = 0.
@
@   Dieser Code stellt den Zustand her, statt die Schutzlogik abzuschalten:
@   er setzt im 100-ms-Task das 0x201-Latch (0x2000087E = 1, macht f_167E4)
@   und schreibt die 0x201-Nutzlast 0x20000A0C = 0x0100 (Wire-Bytes {00 01}
@   = "Fahrt", genau wie das Motor-Keepalive). Danach holt er die ersetzte
@   Zustandsmaschine nach. Ergebnis: f_0F390() = 256/10 = 25 > 10, der
@   Original-Code setzt 0x555 = 1 und der Timer laedt von allein nach.
@
@ Eingehaengt wird nicht durch Code-Einschub, sondern durch Umbiegen eines
@ vorhandenen Aufrufs (Trampolin). Im 100-ms-Task 0x080093D2 steht
@
@     0x080093D8:  bl 0x08016822        (Zustandsmaschine)
@
@ Diese 4 Byte werden durch `bl o2_cave` ersetzt; die Cave holt den
@ Originalaufruf nach. Der Cave-Code liegt in der letzten App-Seite
@ 0x0803FF00 (Seite 0x0803F800..0x0803FFFF) -- die enthaelt nur das App-CRC-Wort
@ bei 0x0803FFFC und ist sonst geloescht, und sie ist die einzige Stelle im
@ geloeschten Block 0x08038184..0x0803FFFB, die kein Datensammler belegen darf
@ (er wuerde die CRC zerstoeren). tools/patch_bms.py prueft das vor dem Patchen:
@ Cave-Bereich muss 0xFF sein **und** kein 4-Byte-Wort im Image darf in die
@ 2-KiB-Seite zeigen -- 0xFF allein genuegt nicht (siehe Abschnitt 10 der Doku).
@
@ Symbole (Adressen kommen von aussen, siehe tools/patch_bms.py)
@   F_167E4       0x080167E4  Post-Call von CAN 0x201: setzt [0x2000087E] = 1
@   F_16822       0x08016822  Zustandsmaschine (der ersetzte Aufruf)
@   O2_X201_SLOT  0x20000A0C  RAM-Slot der 0x201-Nutzlast (Signal 14, DLC 4)
@   o2_cave       wird vom Linker aus tools/o2_cave.ld platziert (0x08038184)
@
@ Bauen
@   arm-none-eabi-as -mthumb -o o2_cave.o tools/o2_cave.s \
@       --defsym F_167E4=0x080167E4 --defsym F_16822=0x08016822 \
@       --defsym O2_X201_SLOT=0x20000A0C
@   arm-none-eabi-ld -T tools/o2_cave.ld -o o2_cave.elf o2_cave.o
@   arm-none-eabi-objcopy -O binary -j .cave  o2_cave.elf o2_cave.bin     (28 B)
@   arm-none-eabi-objcopy -O binary -j .tramp o2_cave.elf o2_tramp.bin    ( 4 B)
@
@   Fertig eingebaut wird das Ganze mit `python3 tools/patch_bms.py` (P7);
@   das Werkzeug assembliert diese Datei selbst und prueft die Originalbytes.
@ ---------------------------------------------------------------------------

    .syntax unified
    .thumb

@ --- Nutzlast des vorgetaeuschten 0x201 (Wire-Bytes 0 und 1) ---------------
@     {00 01} = Referenzcode `sendCAN(0x201, 4, 0, 1, 0, 0)`.
@     Byte 1 ist das "Fahrt"-Byte, auf das die Aktivitaetsformel reagiert.
    .set O2_X201_B0, 0
    .set O2_X201_B1, 1

@ ===========================================================================
@ Trampolin -- wird bei 0x080093D8 eingehaengt (ersetzt `bl 0x08016822`)
@ ===========================================================================
    .section .tramp, "ax"
    .global o2_tramp
o2_tramp:
    bl      o2_cave                 @ in die Code-Cave, die f_16822 nachholt

@ ===========================================================================
@ Code-Cave -- Platzierung 0x08038184 (siehe tools/o2_cave.ld)
@ ===========================================================================
    .section .cave, "ax"
    .global o2_cave
o2_cave:
    push    {r4, lr}                @ r4 erhalten, lr fuer den Nachaufruf
    movs    r0, #1
    bl      F_167E4                 @ Latch [0x2000087E] = 1 ("0x201 war da")
                                    @ ACHTUNG: f_167E4 clobbert r0..r3 --
                                    @ r1 (Zielzeiger) erst danach laden!
    ldr     r1, =O2_X201_SLOT       @ r1 -> 0x20000A0C (0x201-Nutzlast)
    movs    r0, #O2_X201_B0
    strb    r0, [r1, #0]
    movs    r0, #O2_X201_B1         @ 0x01 = "Fahrt"
    strb    r0, [r1, #1]
    bl      F_16822                 @ der ersetzte Originalaufruf
    pop     {r4, pc}

    .ltorg                          @ Literalpool: 0x20000A0C
