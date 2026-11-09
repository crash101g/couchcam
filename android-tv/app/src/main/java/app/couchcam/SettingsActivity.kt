package app.couchcam

import android.app.Activity
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.util.concurrent.Executors

class SettingsActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var result: TextView
    private val io = Executors.newSingleThreadExecutor()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        val pad = Ui.dp(this, 32)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, Ui.dp(this@SettingsActivity, 24), pad, pad)
        }
        col.addView(Ui.label(this, "Settings", 24f, bold = true))

        val url = field(col, "Bridge address", prefs.bridgeUrl, "e.g. 192.168.1.50  (port 5090, the hub, is assumed)")
        val token = field(col, "API token (only if you set BRIDGE_API_TOKEN)", prefs.apiToken, "", secret = true)
        val user = field(col, "Stream username (only if you set STREAM_AUTH)", prefs.streamUser, "")
        val pass = field(col, "Stream password", prefs.streamPass, "", secret = true)
        val snap = field(col, "Grid snapshot refresh, seconds (2-120)", prefs.snapSeconds.toString(), "", number = true)
        val tour = field(col, "Tour: seconds per camera (5-300)", prefs.tourSeconds.toString(), "", number = true)

        var live = prefs.liveGrid
        lateinit var liveBtn: TextView
        liveBtn = Ui.pill(this, "") { live = !live; liveBtn.text = liveLabel(live) }.apply { text = liveLabel(live) }
        col.addView(liveBtn, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { topMargin = Ui.dp(this@SettingsActivity, 16) })
        val modes = listOf("auto", "sd", "hd")
        val modeNames = mapOf("auto" to "Quality: Auto (SD grid, HD full screen)", "sd" to "Quality: always SD",
            "hd" to "Quality: always HD")
        var mode = prefs.qualityMode
        lateinit var qBtn: TextView
        qBtn = Ui.pill(this, "") {
            mode = modes[(modes.indexOf(mode) + 1) % modes.size]
            prefs.qualityMode = mode
            qBtn.text = modeNames[mode]
        }.apply { text = modeNames[mode] }
        col.addView(qBtn, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { topMargin = Ui.dp(this@SettingsActivity, 10) })
        col.addView(Ui.label(this, "Live grid plays up to 4 cameras at once. Smooth on a box like the Onn 4K Pro; " +
            "on a 1 GB TV, snapshots are the safer choice.", 13f, Ui.DIM).apply { setPadding(0, Ui.dp(this@SettingsActivity, 6), 0, 0) })

        val row = LinearLayout(this).apply { gravity = Gravity.START; setPadding(0, Ui.dp(this@SettingsActivity, 24), 0, 0) }
        val gap = Ui.dp(this, 12)
        fun btn(t: String, f: () -> Unit) = row.addView(Ui.pill(this, t, f),
            LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { rightMargin = gap })

        btn("Find bridge") {
            result.text = "Scanning the network…"
            io.execute {
                val found = Bridge.discover(this)
                runOnUiThread {
                    if (found != null) { url.setText(found); result.text = "Found $found" }
                    else result.text = "Nothing answered on port 5090 or 5080."
                }
            }
        }
        btn("Test") {
            val b = Bridge(Bridge.normalize(url.text.toString()), token.text.toString().trim(),
                user.text.toString().trim(), pass.text.toString())
            result.text = "Testing ${b.base}…"
            io.execute {
                val msg = try {
                    val cams = b.cameras()
                    "OK: ${cams.size} camera(s): " + cams.joinToString { it.label }
                } catch (e: Exception) { "Failed: ${e.message ?: e.javaClass.simpleName}" }
                runOnUiThread { result.text = msg }
            }
        }
        btn("Save") {
            prefs.bridgeUrl = url.text.toString()
            prefs.apiToken = token.text.toString()
            prefs.streamUser = user.text.toString()
            prefs.streamPass = pass.text.toString()
            snap.text.toString().toIntOrNull()?.let { prefs.snapSeconds = it }
            tour.text.toString().toIntOrNull()?.let { prefs.tourSeconds = it }
            prefs.liveGrid = live
            finish()
        }
        btn("Cameras on screen…") { startActivity(android.content.Intent(this, CamerasActivity::class.java)) }
        col.addView(row)

        result = Ui.label(this, "", 15f, Ui.DIM).apply { setPadding(0, Ui.dp(this@SettingsActivity, 16), 0, 0) }
        col.addView(result)

        setContentView(ScrollView(this).apply { addView(col) })
        url.requestFocus()
    }

    override fun onDestroy() { super.onDestroy(); io.shutdownNow() }

    private fun liveLabel(on: Boolean) = if (on) "Live grid: ON" else "Live grid: OFF (snapshots)"

    private fun field(parent: LinearLayout, label: String, value: String, hint: String,
                      secret: Boolean = false, number: Boolean = false): EditText {
        parent.addView(Ui.label(this, label, 14f, Ui.DIM).apply { setPadding(0, Ui.dp(this@SettingsActivity, 14), 0, Ui.dp(this@SettingsActivity, 4)) })
        val e = EditText(this).apply {
            setText(value)
            this.hint = hint
            setTextColor(Ui.TEXT)
            setHintTextColor(Ui.DIM)
            setSingleLine(true)
            inputType = when {
                number -> InputType.TYPE_CLASS_NUMBER
                secret -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                else -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            }
            background = Ui.focusCard(this@SettingsActivity)
            val p = Ui.dp(this@SettingsActivity, 12)
            setPadding(p, p, p, p)
        }
        parent.addView(e, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        return e
    }
}
