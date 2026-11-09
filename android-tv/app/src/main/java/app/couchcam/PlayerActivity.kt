package app.couchcam

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import java.util.concurrent.Executors

/**
 * Full-screen live view.
 *   ◀ ▶ / CH+ CH-   previous / next camera      1-9   jump to camera
 *   OK / ▼          show the control bar          ⏯     start/stop tour
 *   BACK            hide the bar, then back to the grid
 */
@OptIn(markerClass = [UnstableApi::class])
class PlayerActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var names: List<String>
    private lateinit var labels: List<String>
    private lateinit var urls: List<String>
    private var ring = BooleanArray(0)
    private var cloud = BooleanArray(0)
    private var index = 0
    private var touring = false

    private lateinit var player: ExoPlayer
    private lateinit var osdTitle: TextView
    private lateinit var status: TextView
    private lateinit var bar: LinearLayout
    private lateinit var soundBtn: TextView
    private lateinit var qualityBtn: TextView
    private lateinit var tourBtn: TextView

    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private val quality = mutableMapOf<String, String>()

    private val hideTitle = Runnable { osdTitle.animate().alpha(0f).setDuration(300).start() }
    private val hideBar = Runnable { bar.visibility = View.GONE }
    private val tourStep = object : Runnable {
        override fun run() { if (touring) { go(index + 1, fromTour = true); main.postDelayed(this, prefs.tourSeconds * 1000L) } }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        prefs = Prefs(this)
        names = intent.getStringArrayListExtra("names") ?: arrayListOf()
        labels = intent.getStringArrayListExtra("labels") ?: ArrayList(names)
        urls = intent.getStringArrayListExtra("urls") ?: names.map { "rtsp://${prefs.bridge().host}:8554/$it" }
        ring = intent.getBooleanArrayExtra("ring") ?: BooleanArray(names.size)
        cloud = intent.getBooleanArrayExtra("cloud") ?: BooleanArray(names.size)
        if (names.isEmpty()) { finish(); return }
        index = intent.getIntExtra("index", 0).coerceIn(0, names.size - 1)

        val root = FrameLayout(this).apply { setBackgroundColor(0xFF000000.toInt()) }
        player = ExoPlayer.Builder(this).setLoadControl(Players.lowLatency()).build()
        player.volume = if (prefs.muted) 0f else 1f
        val pv = PlayerView(this).apply {
            useController = false
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            isFocusable = false
            keepScreenOn = true
            this.player = this@PlayerActivity.player
        }
        root.addView(pv, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))

        val pad = Ui.dp(this, 28)
        osdTitle = Ui.label(this, "", 22f, bold = true).apply {
            setPadding(Ui.dp(this@PlayerActivity, 16), Ui.dp(this@PlayerActivity, 8), Ui.dp(this@PlayerActivity, 16), Ui.dp(this@PlayerActivity, 8))
            background = Ui.rounded(Ui.SCRIM, Ui.dp(this@PlayerActivity, 8).toFloat())
        }
        root.addView(osdTitle, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.TOP or Gravity.START).apply {
            setMargins(pad, pad, pad, pad)
        })
        status = Ui.label(this, "", 18f).apply {
            setPadding(Ui.dp(this@PlayerActivity, 16), Ui.dp(this@PlayerActivity, 8), Ui.dp(this@PlayerActivity, 16), Ui.dp(this@PlayerActivity, 8))
            background = Ui.rounded(Ui.SCRIM, Ui.dp(this@PlayerActivity, 8).toFloat())
            visibility = View.GONE
        }
        root.addView(status, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.CENTER))

        bar = LinearLayout(this).apply {
            gravity = Gravity.CENTER
            val p = Ui.dp(this@PlayerActivity, 12)
            setPadding(p, p, p, p)
            background = Ui.rounded(Ui.SCRIM, Ui.dp(this@PlayerActivity, 30).toFloat())
            visibility = View.GONE
        }
        fun add(t: String, f: () -> Unit) = Ui.pill(this, t, f).also {
            bar.addView(it, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply {
                leftMargin = Ui.dp(this@PlayerActivity, 6); rightMargin = Ui.dp(this@PlayerActivity, 6)
            })
        }
        add("◀ Prev") { go(index - 1) }
        soundBtn = add("") { toggleSound() }
        qualityBtn = add("HD / SD") { toggleQuality() }
        tourBtn = add("") { setTour(!touring) }
        add("⟳ Reconnect") { reconnect(restartBridge = true) }
        add("Next ▶") { go(index + 1) }
        root.addView(bar, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).apply {
            bottomMargin = pad
        })
        setContentView(root)

        player.addListener(Players.retrying(player, main, { urls[index] }) { msg ->
            status.text = msg
            status.visibility = if (msg.isEmpty()) View.GONE else View.VISIBLE
        })
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                // After a couple of failures, ask the bridge to restart that camera's stream too.
                if (++failures == 2) io.execute { prefs.bridge().restart(names[index]) }
            }
            override fun onPlaybackStateChanged(state: Int) { if (state == Player.STATE_READY) failures = 0 }
        })

        refreshLabels()
        loadQualities()
        play()
        if (intent.getBooleanExtra("tour", false)) setTour(true)
    }

    private var failures = 0

    override fun onStop() {
        super.onStop()
        player.pause()
        player.stop()
        if (prefs.qualityMode == "auto") releaseHd()
    }

    override fun onRestart() {
        super.onRestart()
        play()
    }

    override fun onDestroy() {
        super.onDestroy()
        main.removeCallbacksAndMessages(null)
        if (::player.isInitialized) player.release()
        io.shutdownNow()
    }

    // ---- playback ------------------------------------------------------------------------

    private var playToken = 0
    private var hdCam: String? = null      // camera we switched to HD (auto mode), to put back to SD

    private fun play() {
        failures = 0
        val token = ++playToken
        val cam = names[index]
        val isRing = ring.getOrElse(index) { false }
        val start = Runnable {
            if (token != playToken) return@Runnable
            player.setMediaSource(Players.rtsp(urls[index]))
            player.prepare()
            player.playWhenReady = true
            if (isRing) {   // Ring goes through Ring's cloud: a few seconds to start
                status.text = "Starting Ring live view…"; status.visibility = View.VISIBLE
            }
        }
        showTitle()
        if (prefs.qualityMode == "auto" && !isRing && !cloud.getOrElse(index) { false }) {
            // Full screen gets HD; the camera we just left goes back to SD.
            val prev = hdCam
            hdCam = cam
            player.stop()
            status.text = "Connecting…"; status.visibility = View.VISIBLE
            io.execute {
                val b = prefs.bridge()
                if (prev != null && prev != cam) b.setQuality(prev, "sd")
                val changed = quality[cam] != "hd" && b.setQuality(cam, "hd")
                main.post {
                    if (changed) { quality[cam] = "hd"; refreshLabels() }
                    main.postDelayed(start, if (changed) 1500L else 0L)   // bridge restarts the stream
                }
            }
        } else {
            start.run()
        }
    }

    private fun releaseHd() {
        val cam = hdCam ?: return
        hdCam = null
        val b = prefs.bridge()
        Thread { b.setQuality(cam, "sd") }.start()
    }

    private fun go(i: Int, fromTour: Boolean = false) {
        index = ((i % names.size) + names.size) % names.size
        if (!fromTour && touring) { main.removeCallbacks(tourStep); main.postDelayed(tourStep, prefs.tourSeconds * 1000L) }
        refreshLabels()
        play()
    }

    private fun reconnect(restartBridge: Boolean) {
        status.text = "Reconnecting…"; status.visibility = View.VISIBLE
        val cam = names[index]
        io.execute {
            if (restartBridge) prefs.bridge().restart(cam)
            main.postDelayed({ if (names[index] == cam) play() }, if (restartBridge) 2500L else 0L)
        }
    }

    private fun toggleSound() {
        prefs.muted = !prefs.muted
        player.volume = if (prefs.muted) 0f else 1f
        refreshLabels()
    }

    private fun loadQualities() {
        io.execute {
            val list = runCatching { prefs.bridge().cameras() }.getOrNull() ?: return@execute
            main.post { list.forEach { quality[it.name] = it.quality }; refreshLabels() }
        }
    }

    private fun toggleQuality() {
        val cam = names[index]
        val next = if (quality[cam] == "sd") "hd" else "sd"
        status.text = "Switching to ${next.uppercase()}…"; status.visibility = View.VISIBLE
        io.execute {
            val ok = prefs.bridge().setQuality(cam, next)
            main.post {
                if (ok) { quality[cam] = next; refreshLabels(); main.postDelayed({ if (names[index] == cam) play() }, 1500) }
                else { status.text = "This camera picks its own quality"; main.postDelayed({ status.visibility = View.GONE }, 2500) }
            }
        }
    }

    private fun setTour(on: Boolean) {
        touring = on
        main.removeCallbacks(tourStep)
        if (on) main.postDelayed(tourStep, prefs.tourSeconds * 1000L)
        refreshLabels()
        showTitle()
    }

    // ---- overlay -------------------------------------------------------------------------

    private fun refreshLabels() {
        if (!::soundBtn.isInitialized) return
        soundBtn.text = if (prefs.muted) "🔇 Sound off" else "🔊 Sound on"
        tourBtn.text = if (touring) "⏸ Tour on" else "▶ Tour off"
        qualityBtn.text = when (quality[names[index]]) { "sd" -> "SD → HD"; "hd" -> "HD → SD"; else -> "HD / SD" }
    }

    private fun showTitle() {
        val tour = if (touring) "   ⟳ tour ${prefs.tourSeconds}s" else ""
        osdTitle.text = "${labels.getOrElse(index) { names[index] }}   ${index + 1}/${names.size}$tour"
        osdTitle.animate().cancel(); osdTitle.alpha = 1f
        main.removeCallbacks(hideTitle); main.postDelayed(hideTitle, 3500)
    }

    private fun showBar() {
        bar.visibility = View.VISIBLE
        if (bar.findFocus() == null) bar.getChildAt(1).requestFocus()
        poke()
    }

    private fun poke() { main.removeCallbacks(hideBar); main.postDelayed(hideBar, 7000) }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (bar.visibility == View.VISIBLE) poke()
        return super.dispatchKeyEvent(event)
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE || keyCode == KeyEvent.KEYCODE_MEDIA_PLAY ||
            keyCode == KeyEvent.KEYCODE_MEDIA_PAUSE) { setTour(!touring); return true }
        if (keyCode in KeyEvent.KEYCODE_1..KeyEvent.KEYCODE_9) {
            val i = keyCode - KeyEvent.KEYCODE_1
            if (i < names.size) go(i)
            return true
        }
        if (keyCode == KeyEvent.KEYCODE_CHANNEL_UP) { go(index + 1); return true }
        if (keyCode == KeyEvent.KEYCODE_CHANNEL_DOWN) { go(index - 1); return true }
        if (bar.visibility != View.VISIBLE) {
            when (keyCode) {
                KeyEvent.KEYCODE_DPAD_LEFT -> { go(index - 1); return true }
                KeyEvent.KEYCODE_DPAD_RIGHT -> { go(index + 1); return true }
                KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_DPAD_DOWN,
                KeyEvent.KEYCODE_DPAD_UP -> { showBar(); showTitle(); return true }
            }
        }
        return super.onKeyDown(keyCode, event)
    }

    @Deprecated("Activity back handling; fine for minSdk 26 TV apps")
    override fun onBackPressed() {
        if (bar.visibility == View.VISIBLE) { bar.visibility = View.GONE; return }
        @Suppress("DEPRECATION") super.onBackPressed()
    }
}
