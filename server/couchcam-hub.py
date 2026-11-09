#!/usr/bin/env python3
"""couchcam-hub: one API for CouchCam in front of every camera source.

Speaks the same REST API as wyze-bridge (so the TV and Linux apps need one address), and adds
Ring cameras from ring-mqtt:
  GET  /api/version                 identifies the hub (and keeps old app probes happy)
  GET  /api/cameras                 Wyze cams (proxied) + Ring cams (from MQTT discovery)
  GET  /api/snapshot/<name>         Wyze: live frame via the bridge. Ring: latest snapshot ring-mqtt published
  POST /api/cameras/<name>/<action> Wyze: proxied. Ring: snapshot / restart (no-op) / quality (refused)

Runs natively (systemd) on a desktop or in a Proxmox LXC. Needs python3; Ring needs python3-paho-mqtt
(1.x or 2.x).

Extra fields on each camera: "source" (wyze|ring), "battery" (bool), "live_ok" (False = don't put
it in a live grid), and "rtsp" (a URL with a {host} placeholder the apps fill in).
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.4.0"
PORT = int(os.environ.get("HUB_PORT", "5090"))
WYZE = os.environ.get("WYZE_BRIDGE_URL", "http://127.0.0.1:5080").rstrip("/")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
RING_ENABLED = os.environ.get("RING_ENABLED", "false").lower() == "true"
RING_TOPIC = os.environ.get("RING_TOPIC", "ring")
RING_RTSP_PORT = os.environ.get("RING_RTSP_PORT", "8555")
RING_USER = os.environ.get("RING_LIVESTREAM_USER", "")
RING_PASS = os.environ.get("RING_LIVESTREAM_PASS", "")
# Live-streaming a battery Ring cam costs battery and Ring cloud time; keep them out of live grids.
RING_LIVE_GRID = os.environ.get("RING_LIVE_GRID", "wired").lower()  # wired | none | all
WYZE_ENV_FILE = os.environ.get("WYZE_ENV_FILE", "")   # where WEBRTC_CAMERAS is kept across restarts


def save_cloud_choice(name, enabled):
    """Remember a camera's cloud/local choice in wyze-bridge.env (WEBRTC_CAMERAS=a,b) so the bridge
    starts it the same way next time. Best effort: the live switch already happened."""
    if not WYZE_ENV_FILE:
        return
    try:
        with open(WYZE_ENV_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    cams = set()
    for ln in lines:
        if ln.startswith("WEBRTC_CAMERAS="):
            cams = {c.strip() for c in ln.split("=", 1)[1].split(",") if c.strip()}
    (cams.add if enabled else cams.discard)(name)
    lines = [ln for ln in lines if not ln.startswith("WEBRTC_CAMERAS=")]
    if cams:
        lines.append("WEBRTC_CAMERAS=" + ",".join(sorted(cams)))
    tmp = WYZE_ENV_FILE + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, WYZE_ENV_FILE)
    except OSError as e:
        print(f"[hub] couldn't save cloud choice: {e}", flush=True)


def slug(s):
    s = re.sub(r"[^\w\-]+", "", s.strip().replace(" ", "_")).lower()
    return s or "camera"


RING_CACHE = os.environ.get("RING_CACHE_FILE") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "couchcam", "ring-cameras.json")


class RingState:
    """Ring cameras as learned from ring-mqtt's Home Assistant discovery + state topics.
    The list is cached on disk, so Ring cameras show up at once after a restart instead of only
    after ring-mqtt has logged in to Ring and re-announced them (30-60 s)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cams = {}        # device_id -> dict(name, model, online, battery, topic)
        self.saved = ""
        try:
            with open(RING_CACHE) as f:
                for did, c in json.load(f).items():
                    if c.get("name"):
                        self.cams[did] = {**c, "online": True}
        except (OSError, ValueError):
            pass
        self.snaps = {}       # device_id -> (jpeg bytes, time)
        self.connected = False
        self.client = None

    def start(self):
        import paho.mqtt.client as mqtt   # only needed when Ring is on
        try:                               # paho-mqtt 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="couchcam-hub")
        except AttributeError:             # paho-mqtt 1.x (Ubuntu 24.04 / Debian 12 packages)
            c = mqtt.Client(client_id="couchcam-hub")
        c.on_connect = self.on_connect
        c.on_disconnect = lambda *a: setattr(self, "connected", False)
        c.on_message = self.on_message
        c.reconnect_delay_set(2, 30)
        c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        c.loop_start()
        self.client = c

    def on_connect(self, client, _ud, _flags, rc, _props=None):
        if rc != 0:
            print(f"[hub] MQTT connect failed: {rc}", flush=True)
            return
        self.connected = True
        print("[hub] MQTT connected; asking ring-mqtt to (re)publish its cameras", flush=True)
        client.subscribe([
            ("homeassistant/camera/+/+/config", 1),
            ("homeassistant/sensor/+/+/config", 1),
            (f"{RING_TOPIC}/+/camera/+/status", 1),
            (f"{RING_TOPIC}/+/camera/+/snapshot/image", 0),
        ])
        # ring-mqtt doesn't retain discovery; announcing "HA online" makes it resend (after ~15 s).
        client.publish("homeassistant/status", "online")

    def on_message(self, _c, _ud, msg):
        t = msg.topic
        try:
            if t.startswith("homeassistant/") and t.endswith("/config"):
                if not msg.payload:
                    return
                cfg = json.loads(msg.payload)
                dev = cfg.get("device") or cfg.get("dev") or {}
                ids = dev.get("ids") or dev.get("identifiers") or []
                if not ids:
                    return
                did = ids[0] if isinstance(ids, list) else ids
                with self.lock:
                    if t.startswith("homeassistant/camera/") and t.endswith("_snapshot/config"):
                        cam = self.cams.setdefault(did, {"online": True, "battery": False})
                        cam["name"] = dev.get("name") or did
                        cam["model"] = dev.get("mdl") or dev.get("model") or "Ring camera"
                        cam["topic"] = cfg.get("topic", "").rsplit("/snapshot/image", 1)[0]
                    elif t.startswith("homeassistant/sensor/") and t.endswith("_battery/config"):
                        self.cams.setdefault(did, {"online": True})["battery"] = True
                self.save()
            elif t.endswith("/status"):
                did = t.split("/")[3]
                with self.lock:
                    self.cams.setdefault(did, {"battery": False})["online"] = msg.payload.decode() == "online"
            elif t.endswith("/snapshot/image"):
                did = t.split("/")[3]
                if msg.payload[:2] == b"\xff\xd8":   # JPEG
                    with self.lock:
                        self.snaps[did] = (bytes(msg.payload), time.time())
        except (ValueError, IndexError) as e:
            print(f"[hub] skipped {t}: {e}", flush=True)

    def save(self):
        with self.lock:
            data = {d: {k: c.get(k) for k in ("name", "model", "battery", "topic")}
                    for d, c in self.cams.items() if c.get("name")}
        text = json.dumps(data, sort_keys=True)
        if text == self.saved:
            return
        try:
            os.makedirs(os.path.dirname(RING_CACHE), exist_ok=True)
            with open(RING_CACHE + ".tmp", "w") as f:
                f.write(text)
            os.replace(RING_CACHE + ".tmp", RING_CACHE)
            self.saved = text
        except OSError as e:
            print(f"[hub] couldn't cache Ring cameras: {e}", flush=True)

    def list(self):
        out = []
        with self.lock:
            items = [(d, dict(c)) for d, c in self.cams.items() if c.get("name")]
        used = set()
        for did, c in sorted(items, key=lambda x: x[1]["name"]):
            name = "ring_" + slug(c["name"])
            if name in used:
                name += "_" + did[-4:]
            used.add(name)
            auth = f"{RING_USER}:{RING_PASS}@" if RING_USER else ""
            battery = bool(c.get("battery"))
            live_ok = RING_LIVE_GRID == "all" or (RING_LIVE_GRID == "wired" and not battery)
            out.append({
                "name": name,
                "nickname": c["name"],
                "model": c.get("model", ""),
                "model_name": c.get("model", "Ring camera"),
                "state": "streaming" if c.get("online", True) else "offline",
                "stream_ok": bool(c.get("online", True)),
                "quality": "",
                "audio": True,
                "source": "ring",
                "battery": battery,
                "live_ok": live_ok,
                "rtsp": f"rtsp://{auth}{{host}}:{RING_RTSP_PORT}/{did}_live",
                "snapshot_age": int(time.time() - self.snaps[did][1]) if did in self.snaps else None,
                "_id": did,
                "_topic": c.get("topic", ""),
            })
        return out

    def find(self, name):
        return next((c for c in self.list() if c["name"] == name), None)


RING = RingState()


# ---- "keep connected": hold a camera's stream open so it's ready the moment you look ----------

HUB_ENV_FILE = os.environ.get("HUB_ENV_FILE", "")


def _env_lines(path):
    try:
        with open(path) as f:
            return f.read().splitlines()
    except OSError:
        return []


def _write_env(path, lines):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


class Keeper:
    """For cameras on weak Wi-Fi: keep one viewer (ffmpeg, video copied to nowhere) attached to the
    camera's stream all the time. Connecting is the fragile part on a marginal signal; once a session
    is up it is shared by every app that opens the camera, and if it drops it is re-dialled within
    seconds instead of waiting for the next viewer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}          # name -> Popen | None
        self.wanted = set()
        for ln in _env_lines(HUB_ENV_FILE):
            if ln.startswith("KEEP_CONNECTED="):
                self.wanted = {c.strip() for c in ln.split("=", 1)[1].split(",") if c.strip()}
        for name in sorted(self.wanted):
            self._start(name)

    def _rtsp(self, name):
        auth = ""
        for ln in _env_lines(os.environ.get("WYZE_ENV_FILE", "")):
            if ln.startswith("STREAM_AUTH=") and ":" in ln:
                auth = ln.split("=", 1)[1].strip() + "@"
        return f"rtsp://{auth}127.0.0.1:8554/{name}"

    def _start(self, name):
        def loop():
            backoff = 3
            while name in self.wanted:
                started = time.time()
                try:
                    p = subprocess.Popen(
                        ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
                         "-i", self._rtsp(name), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError as e:
                    print(f"[hub] keep-connected needs ffmpeg: {e}", flush=True)
                    return
                with self.lock:
                    self.procs[name] = p
                p.wait()
                if name not in self.wanted:
                    break
                # Reconnect quickly after a session that ran a while; back off on instant failures.
                backoff = 3 if time.time() - started > 60 else min(backoff * 2, 30)
                time.sleep(backoff)
        threading.Thread(target=loop, daemon=True, name=f"keep-{name}").start()

    def set(self, name, on):
        with self.lock:
            if on and name not in self.wanted:
                self.wanted.add(name)
                start = True
            else:
                start = False
            if not on and name in self.wanted:
                self.wanted.discard(name)
                p = self.procs.pop(name, None)
                if p and p.poll() is None:
                    p.terminate()
        if start:
            self._start(name)
        if HUB_ENV_FILE:
            lines = [ln for ln in _env_lines(HUB_ENV_FILE) if not ln.startswith("KEEP_CONNECTED=")]
            if self.wanted:
                lines.append("KEEP_CONNECTED=" + ",".join(sorted(self.wanted)))
            try:
                _write_env(HUB_ENV_FILE, lines)
            except OSError as e:
                print(f"[hub] couldn't save keep-connected: {e}", flush=True)

    def stop_all(self):
        with self.lock:
            self.wanted.clear()
            for p in self.procs.values():
                if p and p.poll() is None:
                    p.terminate()


KEEPER = None


def wyze_request(path, method="GET", body=None, auth=None, timeout=8):
    req = urllib.request.Request(WYZE + path, data=body, method=method)
    if auth:
        req.add_header("Authorization", auth)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", "application/json"), r.read()


class Handler(BaseHTTPRequestHandler):
    server_version = f"couchcam-hub/{VERSION}"

    def log_message(self, fmt, *args):
        if os.environ.get("HUB_DEBUG"):
            super().log_message(fmt, *args)

    def reply(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def proxy(self, method="GET", body=None):
        try:
            code, ctype, data = wyze_request(self.path, method, body, self.headers.get("Authorization"),
                                             timeout=12 if "snapshot" in self.path else 8)
            self.reply(code, data, ctype)
        except urllib.error.HTTPError as e:
            self.reply(e.code, e.read() or b"{}", e.headers.get("Content-Type", "application/json"))
        except OSError as e:
            self.reply(502, {"error": f"wyze-bridge unreachable: {e}"})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/version":
            return self.reply(200, {"version": f"couchcam-hub {VERSION}", "go2rtc_version": "hub",
                                    "hub": True, "ring": RING_ENABLED})
        if p == "/api/health":
            return self.reply(200, {"status": "ok", "ring_mqtt_connected": RING.connected if RING_ENABLED else None})
        if p == "/api/cameras":
            cams, wyze_err = [], None
            try:
                code, _ct, data = wyze_request("/api/cameras", auth=self.headers.get("Authorization"))
                for c in json.loads(data):
                    c["keep"] = bool(KEEPER and c.get("name") in KEEPER.wanted)
                    c.setdefault("source", "wyze")
                    c.setdefault("battery", False)
                    c.setdefault("live_ok", True)
                    cams.append(c)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    return self.reply(401, {"error": "wyze-bridge wants a login"})
                wyze_err = f"HTTP {e.code}"
            except (OSError, ValueError) as e:
                wyze_err = str(e)
            if RING_ENABLED:
                cams += [{k: v for k, v in c.items() if not k.startswith("_")} for c in RING.list()]
            if wyze_err and not cams:
                return self.reply(502, {"error": f"wyze-bridge unreachable: {wyze_err}"})
            return self.reply(200, cams)
        if p.startswith("/api/snapshot/ring_") and RING_ENABLED:
            cam = RING.find(p.rsplit("/", 1)[1])
            snap = RING.snaps.get(cam["_id"]) if cam else None
            if not snap:
                return self.reply(503, {"error": "no Ring snapshot yet"})
            return self.reply(200, snap[0], "image/jpeg")
        if p.startswith("/api/"):
            return self.proxy()
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        m = re.match(r"^/api/cameras/(ring_[^/]+)/([a-z]+)$", self.path)
        if m and RING_ENABLED:
            cam, action = RING.find(m.group(1)), m.group(2)
            if not cam:
                return self.reply(404, {"error": "camera not found"})
            if action == "quality":
                return self.reply(400, {"error": "Ring cameras pick their own quality"})
            if action in ("snapshot", "restart"):
                # A fresh snapshot is cheap on wired cams; battery cams only snapshot on motion/ding.
                if action == "snapshot" and not cam["battery"] and cam["_topic"] and RING.client:
                    RING.client.publish(f"{cam['_topic']}/take_snapshot/command", "PRESS")
                return self.reply(200, {"status": "ok", "camera": cam["name"]})
            return self.reply(400, {"error": f"{action} isn't supported for Ring cameras"})
        m = re.match(r"^/api/cameras/([^/]+)/keep$", self.path)
        if m:
            try:
                enabled = bool(json.loads(body or b"{}").get("enabled"))
            except ValueError:
                return self.reply(400, {"error": "invalid body"})
            KEEPER.set(m.group(1), enabled)
            return self.reply(200, {"status": "ok", "camera": m.group(1), "keep": enabled})
        m = re.match(r"^/api/cameras/([^/]+)/cloud$", self.path)
        if m:
            try:
                enabled = bool(json.loads(body or b"{}").get("enabled"))
                code, ctype, data = wyze_request(self.path, "POST", body, self.headers.get("Authorization"))
            except urllib.error.HTTPError as e:
                msg = b"this Wyze bridge has no cloud option; use Update server" if e.code == 400 else e.read()
                return self.reply(e.code, {"error": msg.decode(errors="replace")})
            except (OSError, ValueError) as e:
                return self.reply(502, {"error": str(e)})
            save_cloud_choice(m.group(1), enabled)
            return self.reply(code, data, ctype)
        return self.proxy("POST", body)


def main():
    global KEEPER
    if RING_ENABLED:
        RING.start()
    KEEPER = Keeper()
    signal.signal(signal.SIGTERM, lambda *_: (KEEPER.stop_all(), os._exit(0)))
    print(f"[hub] couchcam-hub {VERSION} on :{PORT} -> wyze {WYZE}, ring={'on' if RING_ENABLED else 'off'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
