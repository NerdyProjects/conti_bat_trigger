/* ---------------------------------------------------------------------------
 * o2_cave.ld -- Platzierung der O2-Code-Cave in der Display-Firmware
 *
 * Die Adressen sind die Einhaengepunkte in data/stm32f105_conti.bin
 * (Applikationsbereich 0x08008000..0x0803FFFF):
 *
 *   0x080093D8   `bl 0x08016822` im 100-ms-Task 0x080093D2  -> Trampolin
 *   0x0803FF00   freie Flaeche in der letzten App-Seite      -> Cave
 *
 * Cave-Platz: die letzte 2-KiB-Seite 0x0803F800..0x0803FFFF enthaelt am Ende
 * nur das App-CRC-Wort (0x0803FFFC) und ist sonst geloescht. Sie ist damit die
 * einzige Stelle im geloeschten Block 0x08038184..0x0803FFFB, die **kein**
 * Datensammler belegen darf (er wuerde die CRC zerstoeren). Genau das prueft
 * tools/patch_bms.py: Cave-Bereich 0xFF **und** kein 4-Byte-Wort im ganzen
 * Image, das in die Seite zeigt -- 0xFF allein genuegt nicht!
 *
 * Beide Adressen sind auch in tools/patch_bms.py als O2_TRAMPOLINE / O2_CAVE
 * hinterlegt; das Werkzeug prueft vor dem Patchen, dass an der
 * Trampolin-Stelle wirklich `bl 0x08016822` steht und dass beide Werte zu
 * dieser Datei passen.
 * ------------------------------------------------------------------------- */
SECTIONS
{
    .tramp 0x080093D8 : { *(.tramp) }
    .cave  0x0803FF00 : { *(.cave) }
}
