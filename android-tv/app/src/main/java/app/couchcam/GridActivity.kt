package app.couchcam

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.FrameLayout
import android.widget.GridLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.rtsp.RtspMediaSource
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import kotlin.math.ceil

/**
 * Home screen: every camera as a tile. Tiles show live snapshots refreshed every few seconds
 * (cheap on a 1 GB TV); "Live grid" in Settings swaps the first four for real RTSP players.
 * OK opens a camera full screen. Long-press OK on a tile to move it, OK again to drop.
 */
@OptIn(markerClass = [UnstableApi::class])
class GridActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var status: TextView
    private lateinit var body: FrameLayout
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newFixedThreadPool(2)

    private var cams: List<Cam> = emptyList()
    private val tiles = mutableListOf<Tile>()
    private var generation = 0          // bumps to cancel stale refresh loops
    private var active = false
    private var focusIndex = 0
    private var moving = -1             // index of the tile being moved, or -1
    private val clock = SimpleDateFormat("h:mm:ss a", Locale.US)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        val pad = Ui.dp(this, 24)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, Ui.dp(this@GridActivity, 16), pad, 0)
        }
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        header.addView(Ui.label(this, "CouchCam", 24f, Ui.TEXT, bold = true))
        status = Ui.label(this, "", 14f, Ui.DIM).apply { setPadding(Ui.dp(this@GridActivity, 16), 0, 0, 0) }
        header.addView(status, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        val gap = Ui.dp(this, 10)
        fun addPill(t: String, f: () -> Unit) = header.addView(Ui.pill(this, t, f),
            LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { leftMargin = gap })
        addPill("▶ Tour") { if (cams.isNotEmpty()) openPlayer(0, tour = true) }
        addPill("☰ Cameras") { startActivity(Intent(this, CamerasActivity::class.java)) }
        addPill("⟳ Refresh") { load() }
        addPill("⚙ Settings") { startActivity(Intent(this, SettingsActivity::class.java)) }
        root.addView(header, LinearLayout.LayoutParams(MATCH_PARENT, Ui.dp(this, 56)))

        body = FrameLayout(this)
        root.addView(body, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        active = true
        if (prefs.bridgeUrl.isEmpty()) findBridge() else load()
    }

    override fun onPause() {
        super.onPause()
        active = false
        generation++
        tiles.forEach { it.stopLive() }
    }

    override fun onDestroy() {
        super.onDestroy()
        io.shutdownNow()
    }

    // ---- loading -------------------------------------------------------------------------

    private fun findBridge() {
        showMessage("Looking for your Wyze bridge on the network…")
        io.execute {
            val url = Bridge.discover(this)
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                if (url != null) { prefs.bridgeUrl = url; load() }
                else showMessage("No CouchCam hub (:5090) or bridge (:5080) found. Is the server running (server/install.sh --status)?",
                    "Search again" to { findBridge() },
                    "Settings" to { startActivity(Intent(this, SettingsActivity::class.java)) })
            }
        }
    }

    private fun load() {
        val gen = ++generation
        status.text = "Connecting to ${prefs.bridge().host}…"
        io.execute {
            try {
                val bridge = prefs.bridge()
                val all = prefs.sorted(bridge.cameras())
                val hidden = prefs.hidden
                val list = all.filter { it.name !in hidden }
                // Grid quality: SD unless "always HD". Wyze cams on Wi-Fi cope far better with several SD streams.
                val want = if (prefs.qualityMode == "hd") "hd" else "sd"
                list.filter { !it.isRing && it.cloud != true && it.quality.isNotEmpty() && it.quality != want }
                    .forEach { runCatching { bridge.setQuality(it.name, want) } }
                runOnUiThread {
                    if (gen != generation || isDestroyed) return@runOnUiThread
                    cams = list
                    if (all.isEmpty()) showMessage("The bridge is up but has no cameras yet. Give discovery a minute.",
                        "Retry" to { load() })
                    else if (list.isEmpty()) showMessage("All ${all.size} cameras are hidden.",
                        "Choose cameras" to { startActivity(Intent(this, CamerasActivity::class.java)) })
                    else { buildGrid(); startRefresh(gen) }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    if (gen != generation || isDestroyed) return@runOnUiThread
                    showMessage("Can't reach the bridge at ${prefs.bridgeUrl}\n${e.message ?: e.javaClass.simpleName}",
                        "Retry" to { load() },
                        "Find bridge" to { findBridge() },
                        "Settings" to { startActivity(Intent(this, SettingsActivity::class.java)) })
                }
            }
        }
    }

    private fun showMessage(msg: String, vararg actions: Pair<String, () -> Unit>) {
        tiles.forEach { it.stopLive() }
        tiles.clear()
        body.removeAllViews()
        status.text = ""
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
        }
        box.addView(Ui.label(this, msg, 18f).apply { gravity = Gravity.CENTER })
        val row = LinearLayout(this).apply { gravity = Gravity.CENTER; setPadding(0, Ui.dp(this@GridActivity, 20), 0, 0) }
        actions.forEach { (t, f) ->
            row.addView(Ui.pill(this, t, f), LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply {
                leftMargin = Ui.dp(this@GridActivity, 8); rightMargin = Ui.dp(this@GridActivity, 8)
            })
        }
        box.addView(row)
        body.addView(box, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        row.getChildAt(0)?.requestFocus()
    }

    // ---- grid ----------------------------------------------------------------------------

    private fun buildGrid() {
        tiles.forEach { it.stopLive() }
        tiles.clear()
        body.removeAllViews()

        val n = cams.size
        val cols = when { n <= 1 -> 1; n <= 4 -> 2; n <= 9 -> 3; else -> 4 }
        val rows = ceil(n / cols.toDouble()).toInt()
        val gap = Ui.dp(this, 12)
        val availW = resources.displayMetrics.widthPixels - Ui.dp(this, 48)
        val availH = resources.displayMetrics.heightPixels - Ui.dp(this, 16 + 56 + 16)
        var w = (availW - (cols - 1) * gap) / cols
        var h = w * 9 / 16
        if (rows <= 3 && rows * h + (rows - 1) * gap > availH) {
            h = (availH - (rows - 1) * gap) / rows
            w = h * 16 / 9
        }

        val grid = GridLayout(this).apply { columnCount = cols }
        cams.forEachIndexed { i, cam ->
            val t = Tile(i, cam, w, h)
            tiles += t
            grid.addView(t.view, GridLayout.LayoutParams().apply {
                width = w; height = h
                setMargins(if (i % cols == 0) 0 else gap / 2, gap / 2, if (i % cols == cols - 1) 0 else gap / 2, gap / 2)
            })
        }
        val scroll = ScrollView(this).apply { isFillViewport = true; isVerticalScrollBarEnabled = false }
        val center = FrameLayout(this)
        center.addView(grid, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.CENTER_HORIZONTAL))
        scroll.addView(center)
        body.addView(scroll, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))

        updateStatus()
        tiles.getOrNull(focusIndex.coerceIn(0, n - 1))?.view?.requestFocus()
        if (prefs.liveGrid) tiles.filter { it.cam.liveOk }.take(4).forEach { it.startLive() }
    }

    private fun updateStatus() {
        val up = cams.count { it.healthy }
        status.text = "${prefs.bridge().host} · $up/${cams.size} online · ${clock.format(Date())}" +
            if (moving >= 0) "   MOVING: ◀ ▲ ▼ ▶ then OK" else ""
    }

    /** Snapshot + state loop. One snapshot at a time, so a slow camera never piles up requests. */
    private fun startRefresh(gen: Int) {
        val bridge = prefs.bridge()
        val snapshotTiles = tiles.filter { !it.live }
        io.execute {
            for (t in snapshotTiles) {
                if (gen != generation || !active) return@execute
                val bmp = runCatching { bridge.snapshot(t.cam.name, t.w) }.getOrNull()
                runOnUiThread { if (gen == generation) t.show(bmp) }
            }
            val fresh = runCatching { bridge.cameras() }.getOrNull()
            runOnUiThread {
                if (gen != generation || !active) return@runOnUiThread
                // Cameras that appeared (Ring cams arrive once ring-mqtt has logged in) or went away.
                if (fresh != null && moving < 0) {
                    val hidden = prefs.hidden
                    val vis = prefs.sorted(fresh).filter { it.name !in hidden }
                    if (vis.isNotEmpty() && vis.map { it.name }.toSet() != tiles.map { it.cam.name }.toSet()) {
                        cams = vis
                        buildGrid()
                        startRefresh(++generation)
                        return@runOnUiThread
                    }
                }
                fresh?.forEach { c -> tiles.firstOrNull { it.cam.name == c.name }?.update(c) }
                if (fresh != null) cams = tiles.map { it.cam }
                updateStatus()
                main.postDelayed({ if (gen == generation && active) startRefresh(gen) }, prefs.snapSeconds * 1000L)
            }
        }
    }

    private fun openPlayer(index: Int, tour: Boolean = false) {
        focusIndex = index
        startActivity(Intent(this, PlayerActivity::class.java).apply {
            putStringArrayListExtra("names", ArrayList(cams.map { it.name }))
            putStringArrayListExtra("labels", ArrayList(cams.map { it.label }))
            putStringArrayListExtra("urls", ArrayList(cams.map { prefs.bridge().rtspUrl(it) }))
            putExtra("ring", cams.map { it.isRing }.toBooleanArray())
            putExtra("cloud", cams.map { it.cloud == true }.toBooleanArray())
            putExtra("index", index)
            putExtra("tour", tour)
        })
    }

    // ---- reordering ----------------------------------------------------------------------

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.action == KeyEvent.ACTION_DOWN && event.keyCode == KeyEvent.KEYCODE_MENU) {
            startActivity(Intent(this, SettingsActivity::class.java)); return true
        }
        if (moving < 0) return super.dispatchKeyEvent(event)
        // Swallow the rest of the long-press that started move mode, and all key-ups.
        if (event.action != KeyEvent.ACTION_DOWN || event.repeatCount > 0) return true
        val cols = (tiles.firstOrNull()?.view?.parent as? GridLayout)?.columnCount ?: 1
        val target = when (event.keyCode) {
            KeyEvent.KEYCODE_DPAD_LEFT -> moving - 1
            KeyEvent.KEYCODE_DPAD_RIGHT -> moving + 1
            KeyEvent.KEYCODE_DPAD_UP -> moving - cols
            KeyEvent.KEYCODE_DPAD_DOWN -> moving + cols
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_BACK -> {
                moving = -1; prefs.saveShownOrder(cams.map { it.name }); buildGrid(); startRefresh(++generation); return true
            }
            else -> return true
        }
        if (target in cams.indices) {
            val list = cams.toMutableList()
            val c = list.removeAt(moving); list.add(target, c)
            cams = list; moving = target; focusIndex = target
            buildGrid(); tiles[target].markMoving()
        }
        return true
    }

    // ---- one tile ------------------------------------------------------------------------

    private inner class Tile(val index: Int, var cam: Cam, val w: Int, val h: Int) {
        val view = FrameLayout(this@GridActivity)
        private val image = ImageView(this@GridActivity).apply { scaleType = ImageView.ScaleType.CENTER_CROP }
        private val name = Ui.label(this@GridActivity, cam.tileLabel, 16f, bold = true)
        private val dot = Ui.dot(this@GridActivity, Ui.stateColor(cam))
        private val note = Ui.label(this@GridActivity, "loading…", 13f, Ui.DIM).apply { gravity = Gravity.CENTER }
        private var player: ExoPlayer? = null
        private var playerView: PlayerView? = null
        val live get() = player != null

        init {
            val ctx = this@GridActivity
            val r = Ui.dp(ctx, 10).toFloat()
            view.isFocusable = true
            view.isClickable = true
            view.background = Ui.focusCard(ctx)
            val inset = Ui.dp(ctx, 3)
            view.setPadding(inset, inset, inset, inset)
            view.clipToOutline = true
            image.clipToOutline = true
            image.background = Ui.rounded(Ui.CARD, r)
            view.addView(image, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            view.addView(note, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))

            val bar = LinearLayout(ctx).apply {
                gravity = Gravity.CENTER_VERTICAL
                val p = Ui.dp(ctx, 10)
                setPadding(p, p, p, p)
                background = GradientDrawable(GradientDrawable.Orientation.BOTTOM_TOP, intArrayOf(0xCC000000.toInt(), 0x00000000))
            }
            val ds = Ui.dp(ctx, 10)
            bar.addView(dot, LinearLayout.LayoutParams(ds, ds).apply { rightMargin = Ui.dp(ctx, 8) })
            bar.addView(name)
            view.addView(bar, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.BOTTOM))

            view.setOnClickListener { if (moving < 0) openPlayer(index) }
            view.setOnLongClickListener { moving = index; markMoving(); updateStatus(); true }
            view.setOnFocusChangeListener { v, f ->
                if (f) focusIndex = index
                v.animate().scaleX(if (f) 1.04f else 1f).scaleY(if (f) 1.04f else 1f).setDuration(120).start()
            }
        }

        fun markMoving() {
            view.background = Ui.rounded(Ui.CARD_FOCUS, Ui.dp(this@GridActivity, 10).toFloat(), Ui.dp(this@GridActivity, 4), Ui.WARN)
            view.requestFocus()
        }

        fun show(bmp: Bitmap?) {
            if (bmp != null) { image.setImageBitmap(bmp); note.text = "" }
            else if (image.drawable == null) note.text = if (cam.healthy) "no picture yet" else cam.state
        }

        fun update(c: Cam) {
            cam = c
            name.text = c.tileLabel
            (dot.background as GradientDrawable).setColor(Ui.stateColor(c))
        }

        fun startLive() {
            if (player != null) return
            val ctx = this@GridActivity
            val p = ExoPlayer.Builder(ctx).setLoadControl(Players.lowLatency()).build()
            p.volume = 0f
            val pv = PlayerView(ctx).apply {
                useController = false
                resizeMode = AspectRatioFrameLayout.RESIZE_MODE_ZOOM
                isFocusable = false
                this.player = p
            }
            view.addView(pv, 1, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
            p.setMediaSource(Players.rtsp(prefs.bridge().rtspUrl(cam)))
            p.addListener(Players.retrying(p, main, url = { prefs.bridge().rtspUrl(cam) }))
            p.prepare(); p.playWhenReady = true
            player = p; playerView = pv
            note.text = ""
        }

        fun stopLive() {
            player?.release(); player = null
            playerView?.let { view.removeView(it) }; playerView = null
        }
    }
}

/** Shared Media3 setup: TCP RTSP (survives Docker/NAT), short buffers for low delay, auto-retry. */
@OptIn(markerClass = [UnstableApi::class])
object Players {
    fun lowLatency() = androidx.media3.exoplayer.DefaultLoadControl.Builder()
        .setBufferDurationsMs(500, 2000, 250, 250)
        .setPrioritizeTimeOverSizeThresholds(true)
        .build()

    fun rtsp(url: String) = RtspMediaSource.Factory()
        .setForceUseRtpTcp(true)
        .setTimeoutMs(10_000)
        .createMediaSource(MediaItem.fromUri(url))

    /** Re-prepares after an error with backoff (2s, 4s, 8s … capped at 30s). */
    fun retrying(p: ExoPlayer, h: Handler, url: () -> String, onState: (String) -> Unit = {}) =
        object : androidx.media3.common.Player.Listener {
            var attempt = 0
            override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                attempt++
                val delay = (2000L shl (attempt - 1).coerceAtMost(4)).coerceAtMost(30_000L)
                onState("Reconnecting in ${delay / 1000}s…")
                h.postDelayed({
                    runCatching { p.setMediaSource(rtsp(url())); p.prepare(); p.playWhenReady = true }
                }, delay)
            }
            override fun onPlaybackStateChanged(state: Int) {
                when (state) {
                    androidx.media3.common.Player.STATE_READY -> { attempt = 0; onState("") }
                    androidx.media3.common.Player.STATE_BUFFERING -> onState("Connecting…")
                    else -> {}
                }
            }
        }
}
