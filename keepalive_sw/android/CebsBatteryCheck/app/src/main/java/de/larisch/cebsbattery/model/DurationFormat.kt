package de.larisch.cebsbattery.model

/**
 * Dauer als kurzer Text fuer die Restdauer-Anzeige:
 * unter einer Minute `< 1 min`, unter einer Stunde nur Minuten,
 * bis 100 Stunden `X h Y min`, darueber in Tagen.
 */
internal fun formatDuration(seconds: Long): String {
    val hours = seconds / 3600
    val minutes = (seconds % 3600) / 60
    return when {
        // Ab vier Tagen sind Stundenangaben unhandlich.
        hours >= 100 -> {
            val days = hours / 24
            val rest = hours % 24
            if (rest > 0) "$days Tage $rest h" else "$days Tage"
        }

        hours >= 1 -> "$hours h $minutes min"
        minutes >= 1 -> "$minutes min"
        else -> "< 1 min"
    }
}
