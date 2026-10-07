package de.larisch.cebsbattery.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BatteryStatusTest {

    /** Beispiel aus tools/README.md: I = -90 mA, U = 40.710 V, SOC 89 %, SOH 100 %. */
    private val sample = hex("a6 ff 06 9f 59 64 ba 36 c2 3d 00 00 00 00 a6 ff 00 00 00 00")

    @Test
    fun `decodes documented sample`() {
        val status = requireNotNull(BatteryStatus.from(sample))

        assertEquals(-90, status.currentMa)
        assertEquals(40.710, status.voltageV, 1e-9)
        assertEquals(89, status.socPercent)
        assertEquals(100, status.sohPercent)
        assertEquals(14010, status.remainingMah)
        assertEquals(15810, status.fullMah)
        assertEquals(-3.6639, status.powerW, 1e-4)
        assertEquals(BatteryStatus.CurrentDirection.DISCHARGING, status.direction)
    }

    @Test
    fun `positive current means charging`() {
        val status = requireNotNull(BatteryStatus.from(hex("2c 01 06 9f 59 64 00 00 00 00")))

        assertEquals(300, status.currentMa)
        assertEquals(BatteryStatus.CurrentDirection.CHARGING, status.direction)
    }

    @Test
    fun `small current is idle`() {
        val status = requireNotNull(BatteryStatus.from(hex("0a 00 06 9f 59 64 00 00 00 00")))

        assertEquals(BatteryStatus.CurrentDirection.IDLE, status.direction)
    }

    @Test
    fun `soc 0xff is reported as unknown`() {
        val status = requireNotNull(BatteryStatus.from(hex("a6 ff 06 9f ff 64")))

        assertNull(status.socPercent)
        assertNull(status.remainingMah)
    }

    @Test
    fun `short answer keeps what is there`() {
        val status = requireNotNull(BatteryStatus.from(hex("a6 ff 06 9f")))

        assertEquals(-90, status.currentMa)
        assertEquals(40710, status.voltageMv)
        assertNull(status.socPercent)
        assertNull(status.sohPercent)
    }

    @Test
    fun `too short answer is ignored`() {
        assertNull(BatteryStatus.from(hex("a6 ff 06")))
        assertNull(BatteryStatus.from(ByteArray(0)))
    }

    @Test
    fun `avg current is decoded from bytes 15-16`() {
        val status = requireNotNull(
            BatteryStatus.from(hex("e2 ff a2 9e 58 64 1a 36 12 3e 00 00 00 00 6a ff 00 00 00 00")),
        )

        assertEquals(-30, status.currentMa)
        assertEquals(-150, status.currentAvgMa)
        assertEquals(-150, status.referenceCurrentMa)
    }

    @Test
    fun `remaining time uses the average current`() {
        // 13850 mAh bei 150 mA Entladestrom -> 92 h 20 min
        val status = requireNotNull(
            BatteryStatus.from(hex("e2 ff a2 9e 58 64 1a 36 12 3e 00 00 00 00 6a ff 00 00 00 00")),
        )

        assertEquals(332_400L, status.estimatedRemainingSeconds())
    }

    @Test
    fun `remaining time falls back to the instant current`() {
        // 14 Byte: der Durchschnittsstrom fehlt noch
        val status = requireNotNull(BatteryStatus.from(hex("e2 ff a2 9e 58 64 1a 36 12 3e 00 00 00 00")))

        assertNull(status.currentAvgMa)
        assertEquals(-30, status.referenceCurrentMa)
        assertEquals(1_662_000L, status.estimatedRemainingSeconds())
    }

    @Test
    fun `no remaining time while charging or idle`() {
        val charging = requireNotNull(BatteryStatus.from(hex("2c 01 06 9f 59 64 00 00 00 00")))
        val idle = requireNotNull(BatteryStatus.from(hex("0a 00 06 9f 59 64 00 00 00 00")))

        assertNull(charging.estimatedRemainingSeconds())
        assertNull(idle.estimatedRemainingSeconds())
    }

    private fun hex(text: String): ByteArray =
        text.split(" ").map { it.toInt(16).toByte() }.toByteArray()
}
