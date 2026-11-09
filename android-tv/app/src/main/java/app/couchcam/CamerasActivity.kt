package app.couchcam

import android.app.Activity
import android.os.Bundle
import android.view.Gravity
import android.view.KeyEvent
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.util.concurrent.Executors

/**
 * Pick which cameras are on screen and their order, with the remote:
 *   OK            show / hide the camera
 *   hold OK       pick it up, then ▲ ▼ to move it, OK (or BACK) to drop
 * Every change is saved right away; BACK returns to the grid.
 */
class CamerasActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var list: LinearLayout
    private lateinit var count: TextView
    private val io = Executors.newSingleThreadExecutor()
    private var cams = mutableListOf<Cam>()
    private var hidden = mutableSetOf<String>()
    private var moving = -1
    private var focusIndex = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        hidden = prefs.hidden.toMutableSet()
        val pad = Ui.dp(this, 32)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, Ui.dp(this@CamerasActivity, 24), pad, Ui.dp(this@CamerasActivity, 16))
        }
        val head = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        head.addView(Ui.label(this, "Cameras on screen", 24f, bold = true))
        count = Ui.label(this, "", 15f, Ui.DIM).apply { setPadding(Ui.dp(this@CamerasActivity, 16), 0, 0, 0) }
        head.addView(count, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        val gap = Ui.dp(this, 10)
        fun pill(t: String, f: () -> Unit) = head.addView(Ui.pill(this, t, f),
            LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { leftMargin = gap })
        pill("Show all") { hidden.clear(); save(); rebuild() }
        pill("Hide all") { hidden = cams.map { it.name }.toMutableSet(); save(); rebuild() }
        pill("A–Z") { cams.sortBy { it.label.lowercase() }; save(); rebuild() }
        pill("Done") { finish() }
        col.addView(head)
        col.addView(Ui.label(this, "OK: show / hide     Hold OK: move it with ▲ ▼, then OK     ▶ ☁: stream via Wyze's cloud", 14f, Ui.DIM).apply {
            setPadding(0, Ui.dp(this@CamerasActivity, 8), 0, Ui.dp(this@CamerasActivity, 12))
        })
        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(ScrollView(this).apply { addView(list); isVerticalScrollBarEnabled = false },
            LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(col)

        list.addView(Ui.label(this, "Loading cameras…", 16f, Ui.DIM))
        io.execute {
            val r = runCatching { prefs.sorted(prefs.bridge().cameras()) }
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                r.onSuccess { cams = it.toMutableList(); rebuild() }
                r.onFailure { list.removeAllViews(); list.addView(Ui.label(this, "Can't load cameras: ${it.message}", 16f)) }
            }
        }
    }

    override fun onDestroy() { super.onDestroy(); io.shutdownNow() }

    private fun save() {
        prefs.order = cams.map { it.name }
        prefs.hidden = hidden
    }

    private fun rebuild() {
        list.removeAllViews()
        cams.forEachIndexed { i, cam -> list.addView(row(i, cam)) }
        count.text = "${cams.count { it.name !in hidden }} of ${cams.size} shown"
        list.getChildAt(focusIndex.coerceIn(0, (cams.size - 1).coerceAtLeast(0)))?.requestFocus()
    }

    private fun row(i: Int, cam: Cam): LinearLayout {
        val shown = cam.name !in hidden
        val r = LinearLayout(this).apply {
            gravity = Gravity.CENTER_VERTICAL
            val p = Ui.dp(this@CamerasActivity, 14)
            setPadding(p, Ui.dp(this@CamerasActivity, 10), p, Ui.dp(this@CamerasActivity, 10))
            isFocusable = true
            isClickable = true
            background = if (i == moving)
                Ui.rounded(Ui.CARD_FOCUS, Ui.dp(this@CamerasActivity, 10).toFloat(), Ui.dp(this@CamerasActivity, 3), Ui.WARN)
            else Ui.focusCard(this@CamerasActivity)
            alpha = if (shown) 1f else 0.55f
        }
        r.addView(Ui.label(this, if (shown) "☑" else "☐", 24f, if (shown) Ui.ACCENT else Ui.DIM),
            LinearLayout.LayoutParams(Ui.dp(this, 40), WRAP_CONTENT))
        val text = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        text.addView(Ui.label(this, cam.label, 18f, bold = true))
        val src = if (cam.isRing) "Ring" else "Wyze"
        text.addView(Ui.label(this, "$src · ${cam.model}" + (if (cam.battery) "  · 🔋" else ""), 13f, Ui.DIM))
        r.addView(text, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        if (cam.cloud != null) {
            // ▶ from the row reaches this: stream via Wyze's cloud like the phone app (weak-Wi-Fi cameras).
            val c = Ui.pill(this, if (cam.cloud) "☁ Cloud (beta): ON" else "☁ Cloud (beta): off") { toggleCloud(i) }
            r.addView(c, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { rightMargin = Ui.dp(this@CamerasActivity, 12) })
            // The pill sits inside the row, so the stock focus search won't step between them.
            r.setOnKeyListener { _, code, ev ->
                if (code == KeyEvent.KEYCODE_DPAD_RIGHT && ev.action == KeyEvent.ACTION_DOWN && moving < 0) { c.requestFocus(); true } else false
            }
            c.setOnKeyListener { _, code, ev ->
                if (code == KeyEvent.KEYCODE_DPAD_LEFT && ev.action == KeyEvent.ACTION_DOWN) { r.requestFocus(); true } else false
            }
        }
        r.addView(Ui.label(this, if (i == moving) "▲▼ move · OK drop" else "${i + 1}", 14f, Ui.DIM))
        r.setOnClickListener {
            if (moving >= 0) return@setOnClickListener
            focusIndex = i
            if (!hidden.add(cam.name)) hidden.remove(cam.name)
            save(); rebuild()
        }
        r.setOnLongClickListener { moving = i; focusIndex = i; rebuild(); true }
        r.setOnFocusChangeListener { _, f -> if (f) focusIndex = i }
        val gap = Ui.dp(this, 6)
        r.layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { bottomMargin = gap }
        return r
    }

    private fun toggleCloud(i: Int) {
        val cam = cams[i]
        val on = cam.cloud != true
        focusIndex = i
        cams[i] = cam.copy(cloud = on)
        rebuild()
        count.text = if (on) "Switching ${cam.label} to Wyze cloud…" else "Switching ${cam.label} back to local…"
        io.execute {
            val ok = prefs.bridge().setCloud(cam.name, on)
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                if (!ok) { cams[i] = cam; rebuild(); count.text = "Couldn't switch: update the camera server" }
                else count.text = "${cams.count { it.name !in hidden }} of ${cams.size} shown"
            }
        }
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (moving < 0) return super.dispatchKeyEvent(event)
        // Swallow the rest of the long-press that started the move, and key-ups.
        if (event.action != KeyEvent.ACTION_DOWN || event.repeatCount > 0) return true
        val target = when (event.keyCode) {
            KeyEvent.KEYCODE_DPAD_UP -> moving - 1
            KeyEvent.KEYCODE_DPAD_DOWN -> moving + 1
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_BACK -> {
                focusIndex = moving; moving = -1; save(); rebuild(); return true
            }
            else -> return true
        }
        if (target in cams.indices) {
            cams.add(target, cams.removeAt(moving))
            moving = target; focusIndex = target
            rebuild()
        }
        return true
    }
}
