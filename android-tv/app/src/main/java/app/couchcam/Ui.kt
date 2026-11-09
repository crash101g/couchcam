package app.couchcam

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.StateListDrawable
import android.view.Gravity
import android.view.View
import android.widget.TextView

/** Small view helpers so the layouts can live in code (no AppCompat/Leanback needed). */
object Ui {
    const val ACCENT = 0xFF4DA3FF.toInt()
    const val CARD = 0xFF1A1F27.toInt()
    const val CARD_FOCUS = 0xFF253042.toInt()
    const val TEXT = 0xFFE8ECF2.toInt()
    const val DIM = 0xFF8A94A6.toInt()
    const val OK = 0xFF3DDC84.toInt()
    const val WARN = 0xFFFFB020.toInt()
    const val BAD = 0xFFFF5A5F.toInt()

    fun dp(ctx: Context, v: Int) = (v * ctx.resources.displayMetrics.density).toInt()

    fun rounded(color: Int, radius: Float, stroke: Int = 0, strokeColor: Int = 0) =
        GradientDrawable().apply {
            setColor(color); cornerRadius = radius
            if (stroke > 0) setStroke(stroke, strokeColor)
        }

    /** Card background that grows an accent outline when focused. */
    fun focusCard(ctx: Context, base: Int = CARD): StateListDrawable {
        val r = dp(ctx, 10).toFloat()
        return StateListDrawable().apply {
            addState(intArrayOf(android.R.attr.state_focused), rounded(CARD_FOCUS, r, dp(ctx, 3), ACCENT))
            addState(intArrayOf(), rounded(base, r))
        }
    }

    /** Focusable pill button for the D-pad. */
    fun pill(ctx: Context, text: String, onClick: () -> Unit) = TextView(ctx).apply {
        this.text = text
        setTextColor(TEXT)
        textSize = 16f
        typeface = Typeface.DEFAULT_BOLD
        gravity = Gravity.CENTER
        val h = dp(ctx, 18); val v = dp(ctx, 9)
        setPadding(h, v, h, v)
        isFocusable = true
        isClickable = true
        background = StateListDrawable().apply {
            val r = dp(ctx, 22).toFloat()
            addState(intArrayOf(android.R.attr.state_focused), rounded(ACCENT, r))
            addState(intArrayOf(), rounded(0x33FFFFFF, r))
        }
        setOnClickListener { onClick() }
    }

    fun label(ctx: Context, text: String, size: Float = 16f, color: Int = TEXT, bold: Boolean = false) =
        TextView(ctx).apply {
            this.text = text; textSize = size; setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    fun stateColor(c: Cam) = when {
        c.healthy -> OK
        c.state == "connecting" || c.state == "discovering" -> WARN
        else -> BAD
    }

    fun dot(ctx: Context, color: Int) = View(ctx).apply {
        background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(color) }
    }

    val SCRIM = Color.argb(170, 0, 0, 0)
}
