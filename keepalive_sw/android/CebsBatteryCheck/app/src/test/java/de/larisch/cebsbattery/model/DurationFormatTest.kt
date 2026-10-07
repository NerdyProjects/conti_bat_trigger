package de.larisch.cebsbattery.model

import org.junit.Assert.assertEquals
import org.junit.Test

class DurationFormatTest {

    @Test
    fun `less than a minute`() {
        assertEquals("< 1 min", formatDuration(0))
        assertEquals("< 1 min", formatDuration(59))
    }

    @Test
    fun `minutes only below one hour`() {
        assertEquals("1 min", formatDuration(60))
        assertEquals("42 min", formatDuration(42 * 60))
        assertEquals("59 min", formatDuration(59 * 60 + 59))
    }

    @Test
    fun `hours and minutes`() {
        assertEquals("1 h 0 min", formatDuration(3600))
        assertEquals("3 h 42 min", formatDuration(3 * 3600 + 42 * 60 + 5))
        assertEquals("99 h 59 min", formatDuration(99 * 3600 + 59 * 60))
    }

    @Test
    fun `from 100 hours in days`() {
        assertEquals("4 Tage 4 h", formatDuration(100 * 3600))
        assertEquals("5 Tage 18 h", formatDuration(138 * 3600))
        assertEquals("6 Tage", formatDuration(6 * 24 * 3600))
    }
}
