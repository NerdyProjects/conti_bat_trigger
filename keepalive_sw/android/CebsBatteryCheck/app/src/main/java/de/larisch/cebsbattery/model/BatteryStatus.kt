package de.larisch.cebsbattery.model

/**
 * Inhalt der BLE-Charakteristik `00000a01-…` (20 Byte, Little-Endian).
 *
 * Die 20 Byte sind die Verkettung der BMS-CAN-Rahmen 0x404 + 0x405 + 0x406,
 * siehe `keepalive_sw/tools/README.md` Abschnitt 6.
 */
data class BatteryStatus(
    /** AverageCurrent [mA], negativ = Entladen. */
    val currentMa: Int,
    /** Spannung [mV]. */
    val voltageMv: Int,
    /** Ladezustand [%], `null` wenn das BMS 0xFF als "ungueltig" meldet. */
    val socPercent: Int?,
    /** Gesundheitszustand [%]. */
    val sohPercent: Int?,
    /** Restkapazitaet [mAh]. */
    val remainingMah: Int?,
    /** Vollkapazitaet [mAh]. */
    val fullMah: Int?,
) {
    val voltageV: Double get() = voltageMv / 1000.0

    val currentA: Double get() = currentMa / 1000.0

    val powerW: Double get() = voltageV * currentA

    val direction: CurrentDirection
        get() = when {
            currentMa <= -IDLE_THRESHOLD_MA -> CurrentDirection.DISCHARGING
            currentMa >= IDLE_THRESHOLD_MA -> CurrentDirection.CHARGING
            else -> CurrentDirection.IDLE
        }

    enum class CurrentDirection { CHARGING, DISCHARGING, IDLE }

    companion object {
        /** Unterhalb dieses Betrags gilt der Strom als "Ruhe". */
        private const val IDLE_THRESHOLD_MA = 30

        /** Vom BMS als "Wert ungueltig" markierter SOC. */
        private const val SOC_INVALID = 0xFF

        /** Kleinste Laenge, aus der sich Strom und Spannung lesen lassen. */
        private const val MIN_LENGTH = 4

        /**
         * Zerlegt die Rohdaten; liefert `null`, wenn zu wenige Bytes ankommen.
         * Felder, deren Bytes fehlen, bleiben `null`.
         */
        fun from(data: ByteArray): BatteryStatus? {
            if (data.size < MIN_LENGTH) return null

            val soc = if (data.size >= 6) data[4].toInt() and 0xFF else null

            return BatteryStatus(
                currentMa = int16(data, 0),
                voltageMv = u16(data, 2),
                socPercent = soc?.takeUnless { it == SOC_INVALID },
                sohPercent = if (data.size >= 6) data[5].toInt() and 0xFF else null,
                remainingMah = if (data.size >= 10) u16(data, 6) else null,
                fullMah = if (data.size >= 10) u16(data, 8) else null,
            )
        }

        private fun u16(data: ByteArray, offset: Int): Int =
            (data[offset].toInt() and 0xFF) or ((data[offset + 1].toInt() and 0xFF) shl 8)

        private fun int16(data: ByteArray, offset: Int): Int = u16(data, offset).toShort().toInt()
    }
}
