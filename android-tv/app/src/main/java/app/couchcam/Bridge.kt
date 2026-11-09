package app.couchcam

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.ConnectivityManager
import android.net.Uri
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicReference

/** One camera as the bridge's /api/cameras reports it. */
data class Cam(
    val name: String,       // stream path, e.g. "front_door"
    val nickname: String,   // "Front Door"
    val model: String,      // "Wyze Cam v3"
    val state: String,      // streaming | connecting | offline | error | discovering
    val streamOk: Boolean,
    val quality: String,
    val audio: Boolean,
    val source: String = "wyze",   // wyze | ring (ring comes through couchcam-hub)
    val battery: Boolean = false,
    val liveOk: Boolean = true,    // false: keep out of the live grid (battery Ring cams)
    val rtsp: String = "",         // hub-supplied URL with a {host} slot; empty = bridge default
    val snapshotAge: Int = -1,     // seconds, Ring only
    val cloud: Boolean? = null,    // streaming via Wyze's cloud; null = this bridge has no cloud option
) {
    val label get() = nickname.ifBlank { name }
    val isRing get() = source == "ring"
    /** Name plus a short source/battery/staleness note for tiles. */
    val tileLabel: String get() {
        if (!isRing) return if (cloud == true) "$label  · ☁" else label
        val age = if (snapshotAge > 90) "  · ${snapshotAge / 60}m ago" else ""
        return "$label  · Ring" + (if (battery) "  🔋" else "") + age
    }
    val healthy get() = streamOk || state == "streaming"
}

/** Settings, kept in plain SharedPreferences (LAN-only values, nothing secret leaves the TV). */
class Prefs(ctx: Context) {
    private val p = ctx.getSharedPreferences("couchcam", Context.MODE_PRIVATE)
    private fun str(k: String, d: String = "") = p.getString(k, d) ?: d
    private fun put(k: String, v: String) = p.edit().putString(k, v).apply()

    var bridgeUrl: String get() = str("bridge"); set(v) = put("bridge", Bridge.normalize(v))
    var apiToken: String get() = str("token"); set(v) = put("token", v.trim())
    var streamUser: String get() = str("suser"); set(v) = put("suser", v.trim())
    var streamPass: String get() = str("spass"); set(v) = put("spass", v)
    var liveGrid: Boolean get() = p.getBoolean("live", false); set(v) = p.edit().putBoolean("live", v).apply()
    /** auto = SD for the grid, HD while a camera is full screen; or always "sd" / "hd" (Wyze only). */
    var qualityMode: String get() = str("qmode", "auto"); set(v) = put("qmode", v)
    var muted: Boolean get() = p.getBoolean("muted", true); set(v) = p.edit().putBoolean("muted", v).apply()
    var tourSeconds: Int get() = p.getInt("tour", 15); set(v) = p.edit().putInt("tour", v.coerceIn(5, 300)).apply()
    var snapSeconds: Int get() = p.getInt("snap", 5); set(v) = p.edit().putInt("snap", v.coerceIn(2, 120)).apply()
    /** Camera names in the user's chosen order; unknown cams are appended. */
    var order: List<String>
        get() = str("order").split(',').filter { it.isNotBlank() }
        set(v) = put("order", v.joinToString(","))
    /** Cameras the user took off the screen (also skipped by the tour). New cameras show by default. */
    var hidden: Set<String>
        get() = str("hidden").split(',').filter { it.isNotBlank() }.toSet()
        set(v) = put("hidden", v.sorted().joinToString(","))

    fun sorted(list: List<Cam>): List<Cam> {
        val o = order
        return list.sortedWith(compareBy<Cam>({ o.indexOf(it.name).let { i -> if (i < 0) Int.MAX_VALUE else i } }, { it.label }))
    }

    /** Save a new order for the shown cameras without losing where hidden ones were. */
    fun saveShownOrder(shown: List<String>) {
        order = shown + order.filter { it !in shown }
    }

    fun bridge() = Bridge(bridgeUrl, apiToken, streamUser, streamPass)
}

class Bridge(val base: String, private val token: String, private val user: String, private val pass: String) {

    val host: String get() = Uri.parse(base).host ?: ""

    fun rtspUrl(cam: Cam): String {
        if (cam.rtsp.isNotEmpty()) return cam.rtsp.replace("{host}", host)
        val auth = if (user.isNotEmpty()) "${enc(user)}:${enc(pass)}@" else ""
        return "rtsp://$auth$host:8554/${cam.name}"
    }

    fun cameras(): List<Cam> {
        val arr = JSONArray(String(get("/api/cameras", 6000), Charsets.UTF_8))
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Cam(
                name = o.optString("name"),
                nickname = o.optString("nickname"),
                model = o.optString("model_name", o.optString("model")),
                state = o.optString("state", "unknown"),
                streamOk = o.optBoolean("stream_ok", false),
                quality = o.optString("quality"),
                audio = o.optBoolean("audio", true),
                source = o.optString("source", "wyze"),
                battery = o.optBoolean("battery", false),
                liveOk = o.optBoolean("live_ok", true),
                rtsp = o.optString("rtsp", ""),
                snapshotAge = if (o.isNull("snapshot_age")) -1 else o.optInt("snapshot_age", -1),
                cloud = if (o.has("cloud") && !o.isNull("cloud")) o.optBoolean("cloud") else null,
            )
        }.filter { it.name.isNotEmpty() }
    }

    /** Live JPEG from go2rtc, decoded no larger than needed for a tile. */
    fun snapshot(cam: String, wantW: Int): Bitmap? {
        val bytes = get("/api/snapshot/$cam", 8000)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        var sample = 1
        while (wantW > 0 && bounds.outWidth / (sample * 2) >= wantW) sample *= 2
        val opts = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.RGB_565 // half the memory; fine for a preview
        }
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
    }

    fun setQuality(cam: String, q: String) = post("/api/cameras/$cam/quality", JSONObject().put("quality", q))
    fun restart(cam: String) = post("/api/cameras/$cam/restart", JSONObject())
    /** Stream a Wyze camera through Wyze's cloud (like the phone app) instead of directly on the LAN. */
    fun setCloud(cam: String, on: Boolean) = post("/api/cameras/$cam/cloud", JSONObject().put("enabled", on))

    private fun open(path: String, timeout: Int): HttpURLConnection =
        (URL(base + path).openConnection() as HttpURLConnection).apply {
            connectTimeout = 3000
            readTimeout = timeout
            if (token.isNotEmpty()) setRequestProperty("Authorization", "Bearer $token")
            else if (user.isNotEmpty()) {
                // BRIDGE_AUTH basic credentials, if the user reused them for streams
                val b = Base64.encodeToString("$user:$pass".toByteArray(), Base64.NO_WRAP)
                setRequestProperty("Authorization", "Basic $b")
            }
        }

    private fun get(path: String, timeout: Int): ByteArray {
        val c = open(path, timeout)
        try {
            if (c.responseCode == 401) throw BridgeError("The bridge wants a login. Add the API token in Settings.")
            if (c.responseCode !in 200..299) throw BridgeError("Bridge said HTTP ${c.responseCode} for $path")
            return c.inputStream.use { it.readBytes() }
        } finally { c.disconnect() }
    }

    private fun post(path: String, body: JSONObject): Boolean {
        val c = open(path, 8000)
        return try {
            c.requestMethod = "POST"
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body.toString().toByteArray()) }
            c.responseCode in 200..299
        } catch (_: Exception) { false } finally { c.disconnect() }
    }

    class BridgeError(msg: String) : Exception(msg)

    companion object {
        /** couchcam-hub (Wyze + Ring). A bare wyze-bridge on 5080 also works. */
        const val PORT = 5090
        val PORTS = listOf(5090, 5080)

        private fun enc(s: String) = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

        fun normalize(raw: String): String {
            var s = raw.trim().trimEnd('/')
            if (s.isEmpty()) return ""
            if (!s.contains("://")) s = "http://$s"
            val u = Uri.parse(s)
            return if (u.port == -1) "${u.scheme}://${u.host}:$PORT" else "${u.scheme}://${u.host}:${u.port}"
        }

        /** True if a Wyze bridge answers at this base URL (/api/version is never behind auth). */
        fun probe(base: String): Boolean {
            val u = Uri.parse(base)
            try {
                Socket().use { it.connect(InetSocketAddress(u.host, if (u.port > 0) u.port else PORT), 300) }
            } catch (_: Exception) { return false }
            return try {
                val c = (URL("$base/api/version").openConnection() as HttpURLConnection).apply {
                    connectTimeout = 1000; readTimeout = 1500
                }
                val body = c.inputStream.use { String(it.readBytes()) }
                c.disconnect()
                body.contains("go2rtc_version")
            } catch (_: Exception) { false }
        }

        /** Sweep the TV's /24 for the hub (:5090) or a bridge (:5080). A few seconds; call off the main thread. */
        fun discover(ctx: Context): String? {
            val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return null
            val lp = cm.getLinkProperties(cm.activeNetwork) ?: return null
            val me = lp.linkAddresses.map { it.address }.filterIsInstance<Inet4Address>()
                .firstOrNull { !it.isLoopbackAddress } ?: return null
            val b = me.address
            val prefix = "${b[0].toInt() and 255}.${b[1].toInt() and 255}.${b[2].toInt() and 255}."
            val found = AtomicReference<String?>(null)
            val pool = Executors.newFixedThreadPool(48)
            try {
                // Hub first: a full pass on 5090, then 5080 only if nothing answered.
                for (port in PORTS) {
                    val jobs = (1..254).map { i ->
                        pool.submit {
                            if (found.get() == null) {
                                val url = "http://$prefix$i:$port"
                                if (probe(url)) found.compareAndSet(null, url)
                            }
                        }
                    }
                    jobs.forEach { runCatching { it.get() } }
                    if (found.get() != null) break
                }
            } finally { pool.shutdownNow() }
            return found.get()
        }
    }
}
