#!/usr/bin/python3
"""CouchCam for Linux: a multi-camera viewer for Wyze and Ring cams, with the camera server built in.

Standalone: on this PC CouchCam starts its own camera server (wyze-bridge, couchcam-hub, ring-mqtt)
when it opens and stops it when it closes. Later the same server can run in a Proxmox LXC instead
(server/install.sh), and CouchCam just connects to it.

Grid of live tiles (or snapshots), click/Enter for a single camera, keyboard-driven:
  Grid:    arrows move · Enter open · 1-9 open camera · T tour · F fullscreen · R refresh · S settings
           C choose/arrange cameras · H hide focused camera · M move focused camera (arrows, Enter)
  Single:  ←/→ prev/next · 1-9 jump · M sound · Q HD/SD · T tour · F fullscreen · Esc back
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gio, Gst, Gtk  # noqa: E402

APP_ID = "app.couchcam.Linux"
VERSION = "0.10.1"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "couchcam")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULTS = {
    "bridge": "",
    "api_token": "",
    "stream_user": "",
    "stream_pass": "",
    "live_grid": True,       # a desktop can decode several streams; turn off for snapshots only
    "quality_mode": "auto",  # auto: SD in the grid, HD full screen | hd | sd (Wyze only)
    "snapshot_seconds": 5,
    "tour_seconds": 15,
    "muted": True,
    "order": [],
    "hidden": [],            # camera names not shown in the grid or tour
}


# ---------------------------------------------------------------------------------- config

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------------------------- bridge

def normalize(raw):
    s = (raw or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    u = urllib.parse.urlsplit(s)
    return f"{u.scheme}://{u.hostname}:{u.port or 5090}"


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self, cfg):
        self.base = normalize(cfg["bridge"])
        self.token = cfg.get("api_token", "")
        self.user = cfg.get("stream_user", "")
        self.password = cfg.get("stream_pass", "")

    @property
    def host(self):
        h = urllib.parse.urlsplit(self.base).hostname or ""
        return "127.0.0.1" if h == "localhost" else h

    def rtsp_url(self, cam):
        """cam is a camera dict. Ring cams (via the hub) carry their own URL with a {host} slot."""
        if cam.get("rtsp"):
            return cam["rtsp"].replace("{host}", self.host)
        auth = ""
        if self.user:
            auth = f"{urllib.parse.quote(self.user, safe='')}:{urllib.parse.quote(self.password, safe='')}@"
        return f"rtsp://{auth}{self.host}:8554/{cam['name']}"

    def _req(self, path, data=None, timeout=6):
        req = urllib.request.Request(self.base + path, data=data)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        elif self.user:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise BridgeError("The bridge wants a login: add the API token in Settings.") from e
            raise BridgeError(f"Bridge said HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, OSError) as e:
            raise BridgeError(f"Can't reach {self.base}: {getattr(e, 'reason', e)}") from e

    def cameras(self):
        cams = json.loads(self._req("/api/cameras"))
        out = []
        for c in cams:
            if not c.get("name"):
                continue
            out.append({
                "name": c["name"],
                "label": c.get("nickname") or c["name"],
                "model": c.get("model_name") or c.get("model", ""),
                "state": c.get("state", "unknown"),
                "ok": bool(c.get("stream_ok")) or c.get("state") == "streaming",
                "quality": c.get("quality", ""),
                "source": c.get("source", "wyze"),
                "battery": bool(c.get("battery")),
                "live_ok": c.get("live_ok", True) is not False,
                "rtsp": c.get("rtsp", ""),
                "age": c.get("snapshot_age"),
                "cloud": c.get("cloud"),   # None: this bridge has no cloud option
                "keep": c.get("keep"),     # None: this hub has no keep-connected option
            })
        return out

    def snapshot(self, cam):
        return self._req(f"/api/snapshot/{cam}", timeout=10)

    def post(self, path, body):
        try:
            self._req(path, json.dumps(body).encode(), timeout=8)
            return True
        except BridgeError:
            return False

    def set_quality(self, cam, q):
        return self.post(f"/api/cameras/{cam}/quality", {"quality": q})

    def restart(self, cam):
        return self.post(f"/api/cameras/{cam}/restart", {})

    def set_keep(self, cam, enabled):
        """Ask the server to hold this camera's stream open all the time (weak-Wi-Fi cameras)."""
        return self.post(f"/api/cameras/{cam}/keep", {"enabled": bool(enabled)})

    def set_cloud(self, cam, enabled):
        """Stream this Wyze camera through Wyze's cloud (like the phone app) instead of directly."""
        return self.post(f"/api/cameras/{cam}/cloud", {"enabled": bool(enabled)})


def probe(base):
    u = urllib.parse.urlsplit(base)
    try:
        with socket.create_connection((u.hostname, u.port or 5080), timeout=0.3):
            pass
        with urllib.request.urlopen(base + "/api/version", timeout=1.5) as r:
            return b"go2rtc_version" in r.read()
    except OSError:
        return False


def discover(other_machines=False):
    """localhost first (a server on this PC), then sweep the /24.
    other_machines=True skips this PC: used to find e.g. the Proxmox LXC while the built-in server runs."""
    for port in (5090, 5080):          # couchcam-hub first, then a bare wyze-bridge
        if not other_machines and probe(f"http://127.0.0.1:{port}"):
            return f"http://127.0.0.1:{port}"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        me = s.getsockname()[0]
        s.close()
    except OSError:
        return None
    net = ipaddress.ip_network(f"{me}/24", strict=False)
    urls = [f"http://{h}:{port}" for port in (5090, 5080) for h in net.hosts()
            if not (other_machines and str(h) == me)]
    with ThreadPoolExecutor(64) as pool:
        for ok, url in zip(pool.map(probe, urls), urls):
            if ok:
                return url
    return None


# ---------------------------------------------------------------------------------- video

def has_paintable_sink():
    return Gst.ElementFactory.find("gtk4paintablesink") is not None


class Feed:
    """One RTSP stream rendered into a Gtk.Picture, with automatic reconnect.

    Uses gtk4paintablesink (zero-copy) when installed; otherwise an appsink that hands
    frames to GTK as textures, scaled to `size` so a grid of tiles stays cheap.
    """

    def __init__(self, picture, url, size=None, audio=False, muted=True, on_state=None):
        self.picture = picture
        self.url = url
        self.size = size
        self.audio = audio
        self.on_state = on_state or (lambda s: None)
        self.pipe = None
        self.attempt = 0
        self.retry_id = 0
        self.stopped = False
        self._frame = None
        self._lock = threading.Lock()
        self._pending = False
        self._muted = muted
        self.start()

    def _video_sink(self):
        if has_paintable_sink():
            sink = Gst.ElementFactory.make("gtk4paintablesink")
            self.picture.set_paintable(sink.props.paintable)
            return sink
        caps = "video/x-raw,format=RGBA"
        if self.size:
            caps += f",width={self.size[0]},height={self.size[1]},pixel-aspect-ratio=1/1"
        bin_ = Gst.parse_bin_from_description(
            f"videoconvert n-threads=2 ! videoscale ! {caps} ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false", True)
        bin_.get_by_name("sink").connect("new-sample", self._on_sample)
        return bin_

    def start(self):
        self.stopped = False
        p = Gst.ElementFactory.make("playbin", None)
        p.set_property("uri", self.url)
        p.set_property("video-sink", self._video_sink())
        # playbin flags: video=1, audio=2, soft-volume=16
        p.set_property("flags", 1 | 16 | (2 if self.audio else 0))
        if self.audio:
            p.set_property("mute", self._muted)
        else:
            p.set_property("audio-sink", Gst.ElementFactory.make("fakesink"))
        p.connect("source-setup", self._source_setup)
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.pipe = p
        self.on_state("Connecting…")
        p.set_state(Gst.State.PLAYING)

    @staticmethod
    def _source_setup(_playbin, src):
        if src.find_property("protocols"):
            src.set_property("protocols", "tcp")      # survives Docker NAT
        if src.find_property("latency"):
            src.set_property("latency", 150)
        if src.find_property("tcp-timeout"):
            src.set_property("tcp-timeout", 10_000_000)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            with self._lock:
                self._frame = (w, h, data)
                schedule = not self._pending
                self._pending = True
            if schedule:
                GLib.idle_add(self._present)
        return Gst.FlowReturn.OK

    def _present(self):
        with self._lock:
            frame, self._frame, self._pending = self._frame, None, False
        if frame and not self.stopped:
            w, h, data = frame
            tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), w * 4)
            self.picture.set_paintable(tex)
        return False

    def _on_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR or t == Gst.MessageType.EOS:
            if t == Gst.MessageType.ERROR:
                err, _dbg = msg.parse_error()
                print(f"[couchcam] {self.url.split('@')[-1]}: {err.message}")
            self._teardown()
            if not self.stopped:
                self.attempt += 1
                delay = min(2 ** self.attempt, 30)
                self.on_state(f"Reconnecting in {delay}s…")
                self.retry_id = GLib.timeout_add_seconds(delay, self._retry)
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipe:
            _old, new, _pending = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                self.attempt = 0
                self.on_state("")

    def _retry(self):
        self.retry_id = 0
        if not self.stopped:
            self.start()
        return False

    def _teardown(self):
        if self.pipe:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None

    def set_muted(self, muted):
        self._muted = muted
        if self.pipe and self.audio:
            self.pipe.set_property("mute", muted)

    def restart(self):
        self.stop()
        self.attempt = 0
        self.start()

    def stop(self):
        self.stopped = True
        if self.retry_id:
            GLib.source_remove(self.retry_id)
            self.retry_id = 0
        self._teardown()


# ---------------------------------------------------------------------------------- built-in server

SERVER_PREFIX = os.path.expanduser("~/.local/share/couchcam-server")
SERVER_CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "couchcam-server")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "couchcam")
LOG_DIR = os.path.join(STATE_DIR, "logs")
LOCAL_HUB = "http://127.0.0.1:5090"


def find_server_file(name):
    here = os.path.dirname(os.path.realpath(__file__))
    for p in (os.path.join(here, "..", "server", name),
              os.path.expanduser(f"~/.local/share/couchcam/server/{name}")):
        if os.path.isfile(p):
            return os.path.realpath(p)
    return None


def find_installer():
    return find_server_file("install.sh")


def read_env_file(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v
    except OSError:
        pass
    return env


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def process_running(pattern):
    """True if some process (e.g. a systemd service from server/install.sh) already runs this."""
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if pattern.encode() in f.read():
                        return True
            except OSError:
                pass
    return False


def _die_with_parent():
    # Children get SIGTERM if CouchCam is killed or crashes, so nothing is left running.
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class LocalServer:
    """Runs the camera server (wyze-bridge, couchcam-hub, and for Ring: mosquitto + ring-mqtt)
    as child processes of the app: started when CouchCam opens, stopped when it closes."""

    def __init__(self, on_change=None):
        self.on_change = on_change or (lambda: None)
        self.state = {}       # name -> running | external | restarting | failed | stopped
        self.procs = {}       # name -> Popen
        self.restarts = {}
        self.specs = []
        self.watch_id = 0
        self.stopping = False

    @staticmethod
    def installed():
        return (os.access(os.path.join(SERVER_PREFIX, "bin", "wyze-bridge"), os.X_OK)
                and os.path.isfile(os.path.join(SERVER_CONF, "wyze-bridge.env")))

    @staticmethod
    def ring_enabled():
        return read_env_file(os.path.join(SERVER_CONF, "hub.env")).get("RING_ENABLED") == "true"

    def _specs(self):
        b = os.path.join(SERVER_PREFIX, "bin")
        base = dict(os.environ)
        base["PATH"] = f"{b}:{os.path.join(SERVER_PREFIX, 'node', 'bin')}:/usr/sbin:" + base.get("PATH", "/usr/bin:/bin")
        specs = []
        if self.ring_enabled():
            conf = os.path.join(STATE_DIR, "mosquitto.conf")
            with open(conf, "w") as f:
                f.write("listener 1883 127.0.0.1\nallow_anonymous true\npersistence false\n")
            mosq = shutil.which("mosquitto", path="/usr/sbin:/usr/bin") or "mosquitto"
            specs.append({"name": "mqtt", "label": "MQTT broker", "port": 1883,
                          "cmd": [mosq, "-c", conf], "cwd": STATE_DIR, "env": base})
        specs.append({"name": "wyze-bridge", "label": "Wyze bridge", "port": 5080,
                      "cmd": [os.path.join(b, "wyze-bridge")], "cwd": b,
                      "env": {**base, **read_env_file(os.path.join(SERVER_CONF, "wyze-bridge.env"))}})
        if self.ring_enabled():
            rm = os.path.join(SERVER_PREFIX, "ring-mqtt")
            specs.append({"name": "ring-mqtt", "label": "Ring (ring-mqtt)", "match": "ring-mqtt.js",
                          "cmd": [os.path.join(SERVER_PREFIX, "node", "bin", "node"), os.path.join(rm, "ring-mqtt.js")],
                          "cwd": rm, "env": {**base, "RUNMODE": "standard", "DEBUG": "ring-mqtt,ring-rtsp"}})
        specs.append({"name": "hub", "label": "CouchCam hub", "port": 5090,
                      "cmd": [sys.executable, "-u", os.path.join(SERVER_PREFIX, "hub", "couchcam-hub.py")],
                      "cwd": STATE_DIR, "env": {**base, **read_env_file(os.path.join(SERVER_CONF, "hub.env")),
                                                "WYZE_ENV_FILE": os.path.join(SERVER_CONF, "wyze-bridge.env"),
                                                "HUB_ENV_FILE": os.path.join(SERVER_CONF, "hub.env")}})
        return specs

    def start(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.stopping = False
        self.specs = self._specs()
        for spec in self.specs:
            already = port_open(spec["port"]) if "port" in spec else process_running(spec["match"])
            if already:
                self.state[spec["name"]] = "external"   # e.g. installed as a service; leave it alone
            else:
                self._spawn(spec)
        if not self.watch_id:
            self.watch_id = GLib.timeout_add_seconds(3, self._watch)
        self.on_change()

    def _spawn(self, spec):
        log = open(os.path.join(LOG_DIR, f"{spec['name']}.log"), "ab")
        log.write(f"\n==== started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n".encode())
        log.flush()
        try:
            self.procs[spec["name"]] = subprocess.Popen(
                spec["cmd"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, preexec_fn=_die_with_parent)
            self.state[spec["name"]] = "running"
        except OSError as e:
            log.write(f"could not start: {e}\n".encode())
            self.state[spec["name"]] = "failed"
        finally:
            log.close()

    def _watch(self):
        if self.stopping:
            self.watch_id = 0
            return False
        changed = False
        for spec in self.specs:
            p = self.procs.get(spec["name"])
            if p is not None and p.poll() is not None:
                del self.procs[spec["name"]]
                n = self.restarts.get(spec["name"], 0) + 1
                self.restarts[spec["name"]] = n
                self.state[spec["name"]] = "restarting"
                changed = True
                GLib.timeout_add_seconds(min(2 ** n, 60), lambda s=spec: (not self.stopping and self._respawn(s)) and False)
        if changed:
            self.on_change()
        return True

    def _respawn(self, spec):
        self._spawn(spec)
        self.on_change()

    def stop(self):
        self.stopping = True
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 5
        for p in self.procs.values():
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs.clear()
        for k, v in self.state.items():
            if v != "external":
                self.state[k] = "stopped"
        self.on_change()

    def summary(self):
        names = {s["name"]: s["label"] for s in self.specs}
        return [(names.get(k, k), v) for k, v in self.state.items()]


def open_uri(uri, parent=None):
    try:
        Gtk.UriLauncher.new(uri).launch(parent, None, None, None)
    except (AttributeError, TypeError):
        Gio.AppInfo.launch_default_for_uri(uri, None)


class RunDialog(Gtk.Window):
    """Runs a command and shows its output live (camera check, server update)."""

    def __init__(self, win, title, cmd, intro="", env=None, on_done=None):
        super().__init__(title=title, transient_for=win, modal=True)
        self.set_default_size(820, 560)
        self.on_done = on_done or (lambda ok: None)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16, margin_bottom=16,
                      margin_start=16, margin_end=16)
        top = Gtk.Box(spacing=10)
        self.spinner = Gtk.Spinner(spinning=True)
        top.append(self.spinner)
        self.label = Gtk.Label(label=intro or "Working…", xalign=0, wrap=True, hexpand=True)
        top.append(self.label)
        box.append(top)
        self.view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.sw = Gtk.ScrolledWindow(vexpand=True)
        self.sw.set_child(self.view)
        box.append(self.sw)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        copy = Gtk.Button(label="Copy text")
        copy.connect("clicked", lambda *_: self.get_clipboard().set(
            self.view.get_buffer().get_text(*self.view.get_buffer().get_bounds(), False)))
        self.close_btn = Gtk.Button(label="Close")
        self.close_btn.connect("clicked", lambda *_: self.close())
        row.append(copy)
        row.append(self.close_btn)
        box.append(row)
        self.set_child(box)
        try:
            self.proc = subprocess.Popen(cmd, env=env or dict(os.environ), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            self._append(f"Couldn't run it: {e}\n")
            self._done(False)
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        for raw in iter(self.proc.stdout.readline, b""):
            line = ansi.sub("", raw.decode(errors="replace"))
            if "COUCHCAM-COMPONENTS-READY" not in line:
                GLib.idle_add(self._append, line)
        rc = self.proc.wait()
        GLib.idle_add(self._done, rc == 0)

    def _append(self, text):
        buf = self.view.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        adj = self.sw.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        return False

    def _done(self, ok):
        self.spinner.set_spinning(False)
        self.label.set_label("Done." if ok else "Finished with errors (see above).")
        self.on_done(ok)
        return False


class SetupWizard(Gtk.Window):
    """First-run setup: Wyze account (+ optional Ring), builds the server components, Ring sign-in."""

    def __init__(self, win, ring=False):
        super().__init__(title="Set up CouchCam on this PC", transient_for=win, modal=True)
        self.win = win
        self.set_default_size(640, 520)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        self.set_child(self.stack)
        have_wyze = os.path.isfile(os.path.join(SERVER_CONF, "wyze-bridge.env"))

        # -- page 1: accounts
        p = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=20, margin_bottom=20,
                    margin_start=24, margin_end=24)
        title = Gtk.Label(label="Your Wyze account", xalign=0)
        title.add_css_class("title-2")
        p.append(title)
        p.append(Gtk.Label(wrap=True, xalign=0, label=(
            "CouchCam talks to your Wyze cameras directly on your home network. It needs your Wyze login plus a "
            "free API key to find them. These stay on this PC (~/.config/couchcam-server, readable only by you).")))
        p.append(Gtk.LinkButton(uri="https://developer-api-console.wyze.com/#/apikey/view",
                                label="Get your API ID and key (Wyze developer console)", halign=Gtk.Align.START))
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.fields = {}
        for i, (key, label, secret) in enumerate((("WYZE_EMAIL", "Email", False), ("WYZE_PASSWORD", "Password", True),
                                                  ("WYZE_API_ID", "API ID", False), ("WYZE_API_KEY", "API key", True),
                                                  ("WYZE_TOTP_KEY", "2FA secret (optional)", True))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            e = Gtk.PasswordEntry(show_peek_icon=True) if secret else Gtk.Entry()
            e.set_hexpand(True)
            if have_wyze:
                e.set_property("placeholder-text", "saved, leave blank to keep")
            grid.attach(e, 1, i, 1, 1)
            self.fields[key] = e
        p.append(grid)
        if have_wyze:
            p.append(Gtk.Label(xalign=0, wrap=True, label="A Wyze account is already saved. Leave the fields blank to keep it."))
        self.ring_check = Gtk.CheckButton(label="Also add my Ring cameras (you'll sign in to Ring at the end)",
                                          active=ring or LocalServer.ring_enabled())
        p.append(self.ring_check)
        self.err = Gtk.Label(xalign=0, wrap=True)
        self.err.add_css_class("error")
        p.append(self.err)
        spacer = Gtk.Box(vexpand=True)
        p.append(spacer)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        go = Gtk.Button(label="Set up")
        go.add_css_class("suggested-action")
        go.connect("clicked", self.on_install)
        row.append(cancel)
        row.append(go)
        p.append(row)
        self.stack.add_named(p, "account")

        # -- page 2: progress
        p = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=20, margin_bottom=20,
                    margin_start=24, margin_end=24)
        hb = Gtk.Box(spacing=10)
        self.spinner = Gtk.Spinner(spinning=True)
        hb.append(self.spinner)
        self.progress_label = Gtk.Label(xalign=0, wrap=True, label=(
            "Setting up… The first time takes a few minutes: it downloads the camera tools and builds the Wyze "
            "bridge. You may be asked for your password to install a few system packages."))
        hb.append(self.progress_label)
        p.append(hb)
        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.log)
        self.log_scroller = sw
        p.append(sw)
        self.progress_buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        p.append(self.progress_buttons)
        self.stack.add_named(p, "progress")

        # -- page 3: Ring sign-in
        p = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=20, margin_bottom=20,
                    margin_start=24, margin_end=24)
        t = Gtk.Label(label="Sign in to Ring", xalign=0)
        t.add_css_class("title-2")
        p.append(t)
        p.append(Gtk.Label(wrap=True, xalign=0, label=(
            "One time only. Your browser opens a small local page (served by ring-mqtt on this PC). Enter your Ring "
            "email and password, then the 2FA code Ring sends you. The saved login keeps itself refreshed, and it "
            "shows up in the Ring app under Control Center → Authorized Client Devices.")))
        b = Gtk.Button(label="Open Ring sign-in", halign=Gtk.Align.START)
        b.add_css_class("suggested-action")
        b.connect("clicked", lambda *_: open_uri("http://127.0.0.1:55123", self))
        p.append(b)
        self.ring_status = Gtk.Label(xalign=0, label="Waiting for Ring…")
        p.append(self.ring_status)
        p.append(Gtk.Box(vexpand=True))
        done = Gtk.Button(label="Done", halign=Gtk.Align.END)
        done.connect("clicked", lambda *_: self.finish())
        p.append(done)
        self.stack.add_named(p, "ring")
        self.ring_poll = 0
        self.proc = None
        self.connect("close-request", self.on_close_request)

    # ---- install ----

    def on_install(self, _b):
        have_wyze = os.path.isfile(os.path.join(SERVER_CONF, "wyze-bridge.env"))
        vals = {k: e.get_text().strip() for k, e in self.fields.items()}
        needed = ("WYZE_EMAIL", "WYZE_PASSWORD", "WYZE_API_ID", "WYZE_API_KEY")
        if not have_wyze and not all(vals[k] for k in needed):
            self.err.set_label("Email, password, API ID and API key are all needed.")
            return
        installer = find_installer()
        if not installer:
            self.err.set_label("Can't find server/install.sh. Re-run linux/install.sh from the CouchCam folder.")
            return
        if have_wyze and vals["WYZE_EMAIL"]:
            # New credentials replace the saved ones.
            try:
                os.remove(os.path.join(SERVER_CONF, "wyze-bridge.env"))
            except OSError:
                pass
        self.win.stop_local_server()       # the installer replaces binaries
        self.stack.set_visible_child_name("progress")
        env = dict(os.environ)
        env.update({k: v for k, v in vals.items() if v})
        cmd = ["bash", installer, "--app"] + (["--ring"] if self.ring_check.get_active() else [])
        self._append(f"$ {' '.join(os.path.basename(c) if c == installer else c for c in cmd)}\n")
        try:
            self.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return self._finished(False, str(e))
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        ok = False
        for raw in iter(self.proc.stdout.readline, b""):
            line = ansi.sub("", raw.decode(errors="replace"))
            ok = ok or "COUCHCAM-COMPONENTS-READY" in line
            if "COUCHCAM-COMPONENTS-READY" not in line:
                GLib.idle_add(self._append, line)
        rc = self.proc.wait()
        GLib.idle_add(self._finished, ok and rc == 0, f"exit code {rc}")

    def _append(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        adj = self.log_scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)
        return False

    def _finished(self, ok, why=""):
        self.proc = None
        self.spinner.set_spinning(False)
        while (c := self.progress_buttons.get_first_child()) is not None:
            self.progress_buttons.remove(c)
        if not ok:
            self.progress_label.set_label(f"Setup didn't finish ({why}). The log above says why.")
            back = Gtk.Button(label="Back")
            back.connect("clicked", lambda *_: self.stack.set_visible_child_name("account"))
            retry = Gtk.Button(label="Try again")
            retry.add_css_class("suggested-action")
            retry.connect("clicked", lambda *_: (self.spinner.set_spinning(True), self.on_install(None)))
            self.progress_buttons.append(back)
            self.progress_buttons.append(retry)
            return False
        self.progress_label.set_label("All set. Starting the camera server…")
        self.win.use_local_server()
        if self.ring_check.get_active():
            self.stack.set_visible_child_name("ring")
            self.ring_poll = GLib.timeout_add_seconds(3, self._poll_ring)
        else:
            self.finish()
        return False

    def _poll_ring(self):
        def work():
            try:
                with urllib.request.urlopen(LOCAL_HUB + "/api/cameras", timeout=4) as r:
                    return sum(1 for c in json.loads(r.read()) if c.get("source") == "ring")
            except (OSError, ValueError):
                return None

        def done(n):
            if n:
                self.ring_status.set_label(f"✓ Found {n} Ring camera{'s' if n != 1 else ''}. Press Done.")
            elif n == 0:
                self.ring_status.set_label("Waiting for Ring… (cameras appear ~30 s after you sign in)")
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()
        return True

    def finish(self):
        if self.ring_poll:
            GLib.source_remove(self.ring_poll)
            self.ring_poll = 0
        self.close()
        self.win.load()

    def on_close_request(self, *_):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.ring_poll:
            GLib.source_remove(self.ring_poll)
            self.ring_poll = 0
        return False


# ---------------------------------------------------------------------------------- UI

CSS = b"""
window.couchcam-main { background: #0e1116; }
.tile { padding: 0; border-radius: 12px; background: #1a1f27; border: 3px solid transparent; }
.tile:focus-visible, .tile:hover { border-color: #4da3ff; background: #253042; }
.tile.moving { border-color: #ffb020; }
.tile-bar { background: linear-gradient(to top, rgba(0,0,0,0.8), rgba(0,0,0,0)); padding: 10px 12px; }
.tile-name { color: #e8ecf2; font-weight: bold; font-size: 15px; }
.tile-note { color: #8a94a6; }
.dot { min-width: 10px; min-height: 10px; border-radius: 5px; }
.dot.ok { background: #3ddc84; } .dot.warn { background: #ffb020; } .dot.bad { background: #ff5a5f; }
.osd { background: rgba(0,0,0,0.65); color: #e8ecf2; border-radius: 8px; padding: 8px 16px; font-size: 18px; }
.osd-title { font-size: 20px; font-weight: bold; }
.status-line { color: #8a94a6; }
.message { color: #e8ecf2; font-size: 17px; }
"""


def dot_class(cam):
    if cam["ok"]:
        return "ok"
    return "warn" if cam["state"] in ("connecting", "discovering") else "bad"


class Tile(Gtk.Button):
    def __init__(self, win, index, cam, size):
        super().__init__()
        self.win, self.index, self.cam, self.size = win, index, cam, size
        self.add_css_class("tile")
        self.set_size_request(*size)
        overlay = Gtk.Overlay()
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.set_can_shrink(True)
        overlay.set_child(self.picture)

        self.note = Gtk.Label(label="loading…")
        self.note.add_css_class("tile-note")
        self.note.set_halign(Gtk.Align.CENTER)
        self.note.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.note)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("tile-bar")
        bar.set_valign(Gtk.Align.END)
        self.dot = Gtk.Box()
        self.dot.add_css_class("dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        self.name = Gtk.Label(label=cam["label"], xalign=0)
        self.name.add_css_class("tile-name")
        bar.append(self.dot)
        bar.append(self.name)
        overlay.add_overlay(bar)
        self.set_child(overlay)
        self.feed = None
        self.update(cam)
        self.connect("clicked", lambda *_: win.open_single(self.index))
        menu_click = Gtk.GestureClick(button=3)
        menu_click.connect("pressed", lambda *_: self.show_menu())
        self.add_controller(menu_click)

    def show_menu(self):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        for label, cb in (("Open", lambda: self.win.open_single(self.index)),
                          ("Hide this camera", lambda: self.win.hide_camera(self.cam["name"])),
                          ("Move (arrow keys, Enter to drop)", lambda: self.win.start_move(self.index)),
                          ("Choose & arrange cameras…", lambda: self.win.dialog(CamerasDialog))):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            box.append(b)
        pop.set_child(box)
        pop.set_parent(self)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: p.unparent() and False))
        pop.popup()

    def update(self, cam):
        self.cam = cam
        label = cam["label"]
        if cam.get("keep"):
            label += "  · 📌"
        if cam.get("cloud"):
            label += "  · ☁ Wyze cloud"
        if cam.get("source") == "ring":
            label += "  · Ring" + ("  🔋" if cam.get("battery") else "")
            if cam.get("age") and cam["age"] > 90:
                label += f"  · {cam['age'] // 60}m ago"
        self.name.set_label(label)
        for c in ("ok", "warn", "bad"):
            self.dot.remove_css_class(c)
        self.dot.add_css_class(dot_class(cam))

    def start_live(self, url):
        self.feed = Feed(self.picture, url, size=self.size, on_state=self.note.set_label)

    def show_snapshot(self, data):
        if not data:
            if self.picture.get_paintable() is None:
                self.note.set_label("no picture yet" if self.cam["ok"] else self.cam["state"])
            return
        try:  # decode straight to tile size: less memory, and the tile keeps its layout size
            loader = GdkPixbuf.PixbufLoader()
            loader.connect("size-prepared", lambda ld, w, h: ld.set_size(*self.size))
            loader.write(data)
            loader.close()
            tex = Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())
        except GLib.Error:
            return
        self.picture.set_paintable(tex)
        self.note.set_label("")

    def stop(self):
        if self.feed:
            self.feed.stop()
            self.feed = None


class CamerasDialog(Gtk.Window):
    """Choose which cameras are on screen and in what order.
    Tick = shown. Drag rows (or use ▲ ▼ / Alt+↑↓) to reorder. Hidden cameras also skip the tour."""

    def __init__(self, win):
        super().__init__(title="Cameras on screen", transient_for=win, modal=True)
        self.win = win
        self.set_default_size(520, 600)
        self.cams = []                          # all cameras, in display order
        self.hidden = set(win.cfg.get("hidden", []))
        self.cloud = {}                         # name -> wanted cloud on/off (Wyze cams with support)
        self.keep = {}                          # name -> keep-connected on/off

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16, margin_bottom=16,
                      margin_start=16, margin_end=16)
        hint = Gtk.Label(xalign=0, wrap=True, label=(
            "Tick the cameras to show. Drag a row (or select it and press Alt+↑/↓) to change the order. "
            "📌 Keep connected holds a weak-Wi-Fi camera's stream open so it's ready when you look. "
            "☁ Cloud (beta) tries Wyze's WebRTC cloud path; turn it off if a camera stays on Connecting."))
        hint.add_css_class("dim-label")
        box.append(hint)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("boxed-list")
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.listbox)
        box.append(sw)
        self.count = Gtk.Label(xalign=0)
        box.append(self.count)

        row = Gtk.Box(spacing=8)
        for label, cb in (("Show all", lambda *_: self.set_all(True)), ("Hide all", lambda *_: self.set_all(False)),
                          ("A–Z", lambda *_: self.sort_az())):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            row.append(b)
        row.append(Gtk.Box(hexpand=True))
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        done = Gtk.Button(label="Done")
        done.add_css_class("suggested-action")
        done.connect("clicked", self.on_done)
        row.append(cancel)
        row.append(done)
        box.append(row)
        self.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.listbox.append(Gtk.Label(label="Loading cameras…", margin_top=20, margin_bottom=20))
        bridge = win.bridge()

        def work():
            try:
                res = bridge.cameras()
            except BridgeError as e:
                res = e
            GLib.idle_add(self.loaded, res)
        threading.Thread(target=work, daemon=True).start()

    def loaded(self, res):
        if isinstance(res, Exception):
            self.count.set_label(f"Couldn't load cameras: {res}")
            return False
        order = self.win.cfg.get("order", [])
        res.sort(key=lambda c: (order.index(c["name"]) if c["name"] in order else 10**6, c["label"]))
        self.cams = res
        self.cloud = {c["name"]: bool(c["cloud"]) for c in res if c.get("cloud") is not None}
        self.orig_cloud = dict(self.cloud)
        self.keep = {c["name"]: bool(c["keep"]) for c in res
                     if c.get("keep") is not None and c.get("source", "wyze") == "wyze"}
        self.orig_keep = dict(self.keep)
        self.rebuild()
        return False

    def rebuild(self, select=None):
        while (c := self.listbox.get_first_child()) is not None:
            self.listbox.remove(c)
        for i, cam in enumerate(self.cams):
            self.listbox.append(self.make_row(i, cam))
        if select is not None:
            r = self.listbox.get_row_at_index(select)
            if r:
                self.listbox.select_row(r)
                r.grab_focus()
        shown = sum(1 for c in self.cams if c["name"] not in self.hidden)
        self.count.set_label(f"{shown} of {len(self.cams)} shown")

    def make_row(self, i, cam):
        row = Gtk.ListBoxRow()
        h = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=8, margin_end=8)
        h.append(Gtk.Image(icon_name="list-drag-handle-symbolic"))
        check = Gtk.CheckButton(active=cam["name"] not in self.hidden)
        check.connect("toggled", lambda b, n=cam["name"]: self.toggle(n, b.get_active()))
        h.append(check)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        v.append(Gtk.Label(label=cam["label"], xalign=0))
        src = "Ring" if cam.get("source") == "ring" else "Wyze"
        sub = Gtk.Label(label=f"{src} · {cam.get('model', '')}" + ("  · 🔋" if cam.get("battery") else ""), xalign=0)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        v.append(sub)
        h.append(v)
        if cam["name"] in self.keep:
            k = Gtk.ToggleButton(label="📌 Keep connected", active=self.keep[cam["name"]], valign=Gtk.Align.CENTER,
                                 tooltip_text="The server keeps this camera's stream open all the time and "
                                              "re-dials within seconds if it drops. Best fix for a camera with "
                                              "weak Wi-Fi: connecting is the hard part, staying connected isn't.")
            k.connect("toggled", lambda b, n=cam["name"]: self.keep.__setitem__(n, b.get_active()))
            h.append(k)
        if cam["name"] in self.cloud:
            t = Gtk.ToggleButton(label="☁ Cloud (beta)", active=self.cloud[cam["name"]], valign=Gtk.Align.CENTER,
                                 tooltip_text="Experimental: stream through Wyze's cloud (WebRTC). Works only for "
                                              "cameras Wyze serves that way; if a camera stays on 'Connecting', "
                                              "turn this back off.")
            t.connect("toggled", lambda b, n=cam["name"]: self.cloud.__setitem__(n, b.get_active()))
            h.append(t)
        for icon, d in (("go-up-symbolic", -1), ("go-down-symbolic", 1)):
            b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.set_sensitive(0 <= i + d < len(self.cams))
            b.connect("clicked", lambda _b, i=i, d=d: self.move(i, i + d))
            h.append(b)
        row.set_child(h)

        # drag and drop between rows
        src_ctl = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src_ctl.connect("prepare", lambda *_a, i=i: Gdk.ContentProvider.new_for_value(str(i)))
        row.add_controller(src_ctl)
        drop = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        drop.connect("drop", lambda _t, value, _x, _y, i=i: self.move(int(value), i) or True)
        row.add_controller(drop)
        return row

    def toggle(self, name, shown):
        (self.hidden.discard if shown else self.hidden.add)(name)
        shown_n = sum(1 for c in self.cams if c["name"] not in self.hidden)
        self.count.set_label(f"{shown_n} of {len(self.cams)} shown")

    def move(self, src, dst):
        if src == dst or not (0 <= src < len(self.cams)) or not (0 <= dst < len(self.cams)):
            return
        self.cams.insert(dst, self.cams.pop(src))
        self.rebuild(select=dst)

    def set_all(self, shown):
        self.hidden = set() if shown else {c["name"] for c in self.cams}
        self.rebuild()

    def sort_az(self):
        self.cams.sort(key=lambda c: c["label"].lower())
        self.rebuild()

    def on_key(self, _ctl, keyval, _code, state):
        if not state & Gdk.ModifierType.ALT_MASK:
            return False
        k = Gdk.keyval_name(keyval)
        row = self.listbox.get_selected_row()
        if row and k in ("Up", "Down"):
            i = row.get_index()
            self.move(i, i + (-1 if k == "Up" else 1))
            return True
        return False

    def on_done(self, _b):
        self.win.cfg["order"] = [c["name"] for c in self.cams]
        self.win.cfg["hidden"] = sorted(self.hidden)
        save_config(self.win.cfg)
        changed = {n: v for n, v in self.cloud.items() if v != self.orig_cloud.get(n)}
        keep_changed = {n: v for n, v in self.keep.items() if v != self.orig_keep.get(n)}
        self.close()
        if keep_changed:
            kb = self.win.bridge()
            threading.Thread(target=lambda: [kb.set_keep(n, v) for n, v in keep_changed.items()],
                             daemon=True).start()
        if not changed:
            self.win.load()
            return
        b = self.win.bridge()

        def work():
            for n, v in changed.items():
                b.set_cloud(n, v)
            time.sleep(1.5)                    # the bridge restarts those streams
            GLib.idle_add(lambda: self.win.load() and False)
        threading.Thread(target=work, daemon=True).start()


class SettingsDialog(Gtk.Window):
    def __init__(self, win):
        super().__init__(title="CouchCam settings", transient_for=win, modal=True)
        self.win = win
        cfg = win.cfg
        self.set_default_size(520, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=18, margin_bottom=18,
                      margin_start=18, margin_end=18)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.entries = {}
        rows = [("bridge", "Camera server address", False), ("api_token", "API token (optional)", True),
                ("stream_user", "Stream user (optional)", False), ("stream_pass", "Stream password", True)]
        for i, (key, label, secret) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            e = Gtk.PasswordEntry(show_peek_icon=True) if secret else Gtk.Entry()
            e.set_text(str(cfg.get(key, "")))
            e.set_hexpand(True)
            grid.attach(e, 1, i, 1, 1)
            self.entries[key] = e
        r = len(rows)
        self.snap = Gtk.SpinButton.new_with_range(2, 120, 1)
        self.snap.set_value(cfg["snapshot_seconds"])
        grid.attach(Gtk.Label(label="Snapshot refresh (s)", xalign=0), 0, r, 1, 1)
        grid.attach(self.snap, 1, r, 1, 1)
        self.tour = Gtk.SpinButton.new_with_range(5, 300, 1)
        self.tour.set_value(cfg["tour_seconds"])
        grid.attach(Gtk.Label(label="Tour seconds per camera", xalign=0), 0, r + 1, 1, 1)
        grid.attach(self.tour, 1, r + 1, 1, 1)
        self.live = Gtk.Switch(active=cfg["live_grid"], halign=Gtk.Align.START)
        grid.attach(Gtk.Label(label="Live video in grid", xalign=0), 0, r + 2, 1, 1)
        grid.attach(self.live, 1, r + 2, 1, 1)
        self.quality_modes = ["auto", "sd", "hd"]
        self.quality = Gtk.DropDown.new_from_strings(
            ["Auto: SD in the grid, HD full screen", "Always SD (easiest on Wi-Fi)", "Always HD"])
        self.quality.set_selected(self.quality_modes.index(cfg.get("quality_mode", "auto")))
        grid.attach(Gtk.Label(label="Wyze stream quality", xalign=0), 0, r + 3, 1, 1)
        grid.attach(self.quality, 1, r + 3, 1, 1)
        box.append(grid)

        self.result = Gtk.Label(xalign=0, wrap=True)
        self.result.add_css_class("status-line")
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        for label, cb in (("Find server", self.on_find), ("Test", self.on_test),
                          ("Reset order", self.on_reset), ("Save", self.on_save)):
            b = Gtk.Button(label=label)
            if label == "Save":
                b.add_css_class("suggested-action")
            b.connect("clicked", cb)
            buttons.append(b)
        box.append(self.result)
        box.append(buttons)
        self.set_child(box)

    def _bg(self, work, done):
        def run():
            res = work()
            GLib.idle_add(done, res)
        threading.Thread(target=run, daemon=True).start()

    def on_find(self, _b):
        self.result.set_label("Scanning…")

        def done(url):
            if url:
                self.entries["bridge"].set_text(url)
                self.result.set_label(f"Found {url}")
            else:
                self.result.set_label("Nothing answered on port 5090 or 5080.")
        self._bg(discover, done)

    def _form(self):
        cfg = dict(self.win.cfg)
        for k, e in self.entries.items():
            cfg[k] = e.get_text().strip() if k != "stream_pass" else e.get_text()
        cfg["bridge"] = normalize(cfg["bridge"])
        cfg["snapshot_seconds"] = int(self.snap.get_value())
        cfg["tour_seconds"] = int(self.tour.get_value())
        cfg["live_grid"] = self.live.get_active()
        cfg["quality_mode"] = self.quality_modes[self.quality.get_selected()]
        return cfg

    def on_test(self, _b):
        b = Bridge(self._form())
        self.result.set_label(f"Testing {b.base}…")

        def work():
            try:
                cams = b.cameras()
                return f"OK: {len(cams)} camera(s): " + ", ".join(c["label"] for c in cams)
            except BridgeError as e:
                return f"Failed: {e}"
        self._bg(work, self.result.set_label)

    def on_reset(self, _b):
        self.win.cfg["order"] = []
        save_config(self.win.cfg)
        self.result.set_label("Order reset to A-Z.")

    def on_save(self, _b):
        cfg = self._form()
        url_changed = cfg["bridge"] != self.win.cfg.get("bridge")
        self.win.cfg.update(cfg)
        save_config(self.win.cfg)
        self.close()
        if url_changed and cfg["bridge"] and cfg["bridge"] != LOCAL_HUB:
            self.win.use_remote_server(cfg["bridge"])   # typed another machine's address
        else:
            self.win.load()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title="CouchCam")
        self.add_css_class("couchcam-main")   # dark viewer; dialogs keep the desktop theme
        self.cfg = cfg
        self.set_default_size(1280, 780)
        self.cams = []
        self.tiles = []
        self.generation = 0
        self.single_index = 0
        self.single_feed = None
        self.touring = False
        self.tour_id = 0
        self.osd_id = 0
        self.moving = -1
        self.qualities = {}

        header = Gtk.HeaderBar()
        self.status = Gtk.Label()
        self.status.add_css_class("status-line")
        header.set_title_widget(self.status)
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back to grid (Esc)")
        self.back_btn.connect("clicked", lambda *_: self.show_grid())
        header.pack_start(self.back_btn)
        for icon, tip, cb in (("emblem-system-symbolic", "Settings (S)", lambda *_: self.dialog(SettingsDialog)),
                              ("view-list-symbolic", "Choose & arrange cameras (C)", lambda *_: self.dialog(CamerasDialog)),
                              ("view-fullscreen-symbolic", "Fullscreen (F)", lambda *_: self.toggle_fullscreen()),
                              ("view-refresh-symbolic", "Refresh (R)", lambda *_: self.load())):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", cb)
            header.pack_end(b)
        self.server = None
        self.server_btn = Gtk.MenuButton(icon_name="network-server-symbolic", tooltip_text="Camera server")
        self.server_pop = Gtk.Popover()
        self.server_pop.connect("show", lambda *_: self.fill_server_menu())
        self.server_btn.set_popover(self.server_pop)
        header.pack_end(self.server_btn)
        self.tour_btn = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic", tooltip_text="Tour (T)")
        self.tour_btn.connect("toggled", lambda b: self.set_tour(b.get_active()))
        header.pack_end(self.tour_btn)
        self.set_titlebar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.grid_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self.grid_scroller, "grid")

        single = Gtk.Overlay()
        self.single_pic = Gtk.Picture()
        self.single_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        single.set_child(self.single_pic)
        self.osd_title = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.START, margin_top=24, margin_start=24)
        self.osd_title.add_css_class("osd")
        self.osd_title.add_css_class("osd-title")
        single.add_overlay(self.osd_title)
        self.osd_state = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False)
        self.osd_state.add_css_class("osd")
        single.add_overlay(self.osd_state)
        self.osd_help = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=24,
                                  label="←/→ camera · M sound · Q HD/SD · T tour · F fullscreen · Esc grid")
        self.osd_help.add_css_class("osd")
        single.add_overlay(self.osd_help)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda g, n, *_: self.toggle_fullscreen() if n == 2 else self.flash_osd())
        single.add_controller(click)
        self.stack.add_named(single, "single")

        self.message_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.stack.add_named(self.message_box, "message")
        self.set_child(self.stack)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.connect("close-request", self.on_close)
        self.grid_size = (0, 0)
        self.relayout_id = 0
        for prop in ("notify::default-width", "notify::default-height", "notify::fullscreened", "notify::maximized"):
            self.connect(prop, self.on_resize)

        mode = self.cfg.get("server")
        if mode is None and LocalServer.installed():
            mode = "local"
        if mode == "local" and LocalServer.installed():
            self.use_local_server()
        elif self.cfg["bridge"] and mode != "local":
            self.load()
        else:
            self.welcome()

    # ---- helpers ----

    def bridge(self):
        return Bridge(self.cfg)

    def bg(self, work, done):
        gen = self.generation

        def run():
            try:
                res = work()
            except Exception as e:  # noqa: BLE001 - shown to the user
                res = e
            GLib.idle_add(lambda: (gen == self.generation) and done(res) and False)
        threading.Thread(target=run, daemon=True).start()

    def message(self, text, *actions):
        self.stop_all()
        self.stack.set_visible_child_name("message")
        self.back_btn.set_visible(False)
        while (c := self.message_box.get_first_child()) is not None:
            self.message_box.remove(c)
        lbl = Gtk.Label(label=text, justify=Gtk.Justification.CENTER, wrap=True)
        lbl.add_css_class("message")
        self.message_box.append(lbl)
        row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        for label, cb in actions:
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, cb=cb: cb())
            row.append(b)
        self.message_box.append(row)
        if row.get_first_child():
            row.get_first_child().grab_focus()

    def stop_all(self):
        for t in self.tiles:
            t.stop()
        if self.single_feed:
            self.single_feed.stop()
            self.single_feed = None

    # ---- loading ----

    def find_bridge(self):
        self.generation += 1
        elsewhere = self.server is not None
        self.message("Looking for a camera server on your network…" if elsewhere else "Looking for your camera server…")

        def done(url):
            if isinstance(url, str):
                if elsewhere or not url.startswith("http://127.0.0.1"):
                    self.use_remote_server(url)      # stops the built-in one
                else:
                    self.cfg["bridge"] = url
                    save_config(self.cfg)
                    self.load()
            elif elsewhere:
                self.message("No other camera server found on your network.", ("Back", self.load),
                             ("Search again", self.find_bridge))
            else:
                self.message("No camera server found on your network (ports 5090/5080).",
                             ("Set up this PC", lambda: self.dialog(SetupWizard)),
                             ("Search again", self.find_bridge),
                             ("Settings", lambda: self.dialog(SettingsDialog)))
            return False
        self.bg(lambda: discover(other_machines=elsewhere), done)

    def load(self):
        self.generation += 1
        self.status.set_label(f"Connecting to {self.bridge().host}…")

        def done(res):
            if isinstance(res, Exception):
                if self.server:
                    self.message(f"The camera server on this PC isn't answering yet.\n{res}",
                                 ("Retry", self.load), ("Server logs", self.open_logs),
                                 ("Set up / repair", lambda: self.dialog(SetupWizard)))
                else:
                    self.message(str(res), ("Retry", self.load), ("Find server", self.find_bridge),
                                 ("Settings", lambda: self.dialog(SettingsDialog)))
            elif not res:
                self.message("The bridge is up but has no cameras yet. Give discovery a minute.", ("Retry", self.load))
            else:
                self.qualities = {c["name"]: c["quality"] for c in res}
                self.cams = self.visible_sorted(res)
                if not self.cams:
                    self.message(f"All {len(res)} cameras are hidden.",
                                 ("Choose cameras", lambda: self.dialog(CamerasDialog)))
                    return False
                self.build_grid()
                self.refresh_loop(self.generation)
            return False
        self.bg(lambda: self.bridge().cameras(), done)

    def build_grid(self, focus=0):
        self.stop_all()
        self.tiles = []
        n = len(self.cams)
        cols = 1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4
        width = self.get_width() or self.get_default_size()[0]
        height = (self.get_height() or self.get_default_size()[1]) - 60   # minus header bar
        rows = -(-n // cols)
        w = (max(width, 640) - 48 - (cols - 1) * 12) // cols
        h = w * 9 // 16
        if rows <= 3 and rows * h + (rows + 1) * 12 > height:
            h = max((height - (rows + 1) * 12) // rows, 120)
            w = h * 16 // 9
        self.grid_size = (width, height)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, margin_top=12, margin_bottom=12,
                        margin_start=24, margin_end=24, halign=Gtk.Align.CENTER, column_homogeneous=True)
        for i, cam in enumerate(self.cams):
            t = Tile(self, i, cam, (w, h))
            grid.attach(t, i % cols, i // cols, 1, 1)
            self.tiles.append(t)
        self.grid_cols = cols
        self.start_tiles()
        self.grid_scroller.set_child(grid)
        self.stack.set_visible_child_name("grid")
        self.back_btn.set_visible(False)
        self.update_status()
        if self.tiles:
            self.tiles[min(focus, n - 1)].grab_focus()

    def update_status(self):
        up = sum(1 for c in self.cams if c["ok"])
        extra = "   — moving: arrows, then Enter" if self.moving >= 0 else ""
        self.status.set_label(f"CouchCam · {self.bridge().host} · {up}/{len(self.cams)} online · "
                              f"{time.strftime('%-I:%M:%S %p')}{extra}")

    def visible_sorted(self, cams):
        order = self.cfg.get("order", [])
        hidden = set(self.cfg.get("hidden", []))
        cams = sorted(cams, key=lambda c: (order.index(c["name"]) if c["name"] in order else 10**6, c["label"]))
        return [c for c in cams if c["name"] not in hidden]

    def refresh_loop(self, gen):
        """Snapshots for non-live tiles plus a camera-state poll, one request at a time."""
        b = self.bridge()
        on_grid = self.stack.get_visible_child_name() == "grid"
        tiles = [t for t in self.tiles if t.feed is None] if on_grid else []

        def work():
            for t in tiles:
                if gen != self.generation:
                    return None
                try:
                    data = b.snapshot(t.cam["name"])
                except BridgeError:
                    data = None
                GLib.idle_add(lambda t=t, d=data: gen == self.generation and t.show_snapshot(d) and False)
            return b.cameras()

        def done(res):
            if isinstance(res, list):
                # Cameras that turned up since the grid was built (Ring cams arrive once ring-mqtt has
                # logged in) or went away: rebuild the grid, keeping focus where it was.
                fresh = self.visible_sorted(res)
                # Only grow: a briefly short list (bridge restarting) shouldn't tear tiles down.
                if ({c["name"] for c in fresh} > {t.cam["name"] for t in self.tiles}
                        and self.stack.get_visible_child_name() == "grid" and self.moving < 0):
                    focus = next((t.index for t in self.tiles if t.has_focus()), 0)
                    self.cams = fresh
                    self.qualities.update({c["name"]: c["quality"] for c in res})
                    self.build_grid(focus=focus)
                by = {c["name"]: c for c in res}
                for t in self.tiles:
                    if t.cam["name"] in by:
                        t.update(by[t.cam["name"]])
                self.cams = [t.cam for t in self.tiles]
                self.qualities.update({c["name"]: c["quality"] for c in res})
            self.update_status()
            delay = self.cfg["snapshot_seconds"] if tiles else 15
            GLib.timeout_add_seconds(delay, lambda: gen == self.generation and self.refresh_loop(gen) and False)
            return False
        self.bg(work, done)
        return False

    # ---- single view ----

    def open_single(self, index):
        if self.moving >= 0 or not self.cams:
            return
        for t in self.tiles:          # free decoders while full screen
            t.stop()
        self.single_index = index % len(self.cams)
        self.stack.set_visible_child_name("single")
        self.back_btn.set_visible(True)
        self.play_single()

    def play_single(self):
        if self.single_feed:
            self.single_feed.stop()
            self.single_feed = None
        cam = self.cams[self.single_index]
        self.single_pic.set_paintable(None)
        self.single_gen = getattr(self, "single_gen", 0) + 1
        gen = self.single_gen
        mode = self.cfg.get("quality_mode", "auto")
        prev = getattr(self, "single_hd", None)
        if mode == "auto" and prev and prev != cam["name"]:
            self.set_quality_then([prev], "sd")          # the one we just left goes back to SD
            self.single_hd = None

        def start():
            if gen != self.single_gen or self.stack.get_visible_child_name() != "single":
                return
            # Scale in GStreamer to the window, not the camera: a cloud/2.5K stream copied full size
            # into GTK every frame is what makes a desktop app crawl.
            w = max(640, min(1920, self.get_width() or 1280)) // 2 * 2
            self.single_feed = Feed(self.single_pic, self.bridge().rtsp_url(cam), size=(w, w * 9 // 16 // 2 * 2),
                                    audio=True, muted=self.cfg["muted"], on_state=self.set_osd_state)
            if cam.get("source") == "ring":
                self.set_osd_state("Starting Ring live view…")   # Ring takes a few seconds to answer

        if mode == "auto" and cam.get("source", "wyze") == "wyze" and not cam.get("cloud"):
            self.single_hd = cam["name"]
            self.set_osd_state("Connecting…")
            self.set_quality_then([cam["name"]], "hd", start)
        else:
            start()
        self.flash_osd()

    # ---- stream quality & gentle start-up ----

    def set_quality_then(self, names, q, then=None):
        """Ask the bridge for HD/SD on these Wyze cameras, then call `then` once the stream has
        restarted. Cameras already at that quality are skipped (no restart, no delay)."""
        cloud = {c["name"] for c in self.cams if c.get("cloud")}
        todo = [n for n in names if n not in cloud and self.qualities.get(n) and self.qualities.get(n) != q]
        if not todo:
            if then:
                then()
            return
        b = self.bridge()

        def work():
            res = {n: b.set_quality(n, q) for n in todo}
            GLib.idle_add(done, res)

        def done(res):
            for n, ok in res.items():
                if ok:
                    self.qualities[n] = q
            if then:
                GLib.timeout_add(1500, lambda: then() and False)   # bridge restarts the stream
            return False
        threading.Thread(target=work, daemon=True).start()

    def start_tiles(self):
        """Start live tiles one by one (Wyze cams on Wi-Fi often miss a connection request when
        several arrive at once), after putting them in SD in auto mode."""
        self.grid_gen = getattr(self, "grid_gen", 0) + 1
        gen = self.grid_gen
        if not self.cfg["live_grid"]:
            return
        tiles = [t for t in self.tiles if t.cam.get("live_ok", True)]
        mode = self.cfg.get("quality_mode", "auto")
        target = "hd" if mode == "hd" else "sd"
        b = self.bridge()

        def go():
            for i, t in enumerate(tiles):
                GLib.timeout_add(i * 1500, lambda t=t: (
                    gen == self.grid_gen and t in self.tiles and t.feed is None
                    and self.stack.get_visible_child_name() == "grid"
                    and t.start_live(b.rtsp_url(t.cam))) and False)
        wyze = [t.cam["name"] for t in tiles if t.cam.get("source", "wyze") == "wyze"]
        self.set_quality_then(wyze, target, lambda: gen == self.grid_gen and go())

    def set_osd_state(self, text):
        self.osd_state.set_label(text)
        self.osd_state.set_visible(bool(text))

    def flash_osd(self):
        if self.stack.get_visible_child_name() != "single":
            return
        cam = self.cams[self.single_index]
        q = self.qualities.get(cam["name"], "").upper()
        tour = f"   ⟳ tour {self.cfg['tour_seconds']}s" if self.touring else ""
        sound = "🔇" if self.cfg["muted"] else "🔊"
        self.osd_title.set_label(f"{cam['label']}   {self.single_index + 1}/{len(self.cams)}   {q} {sound}{tour}")
        self.osd_title.set_visible(True)
        self.osd_help.set_visible(True)
        if self.osd_id:
            GLib.source_remove(self.osd_id)

        def hide():
            self.osd_id = 0
            self.osd_title.set_visible(False)
            self.osd_help.set_visible(False)
            return False
        self.osd_id = GLib.timeout_add_seconds(4, hide)

    def step(self, delta):
        self.single_index = (self.single_index + delta) % len(self.cams)
        self.play_single()

    def show_grid(self):
        if self.single_feed:
            self.single_feed.stop()
            self.single_feed = None
        self.set_tour(False)
        self.build_grid(focus=self.single_index)
        self.generation += 1
        self.refresh_loop(self.generation)

    def toggle_quality(self):
        cam = self.cams[self.single_index]["name"]
        nxt = "hd" if self.qualities.get(cam) == "sd" else "sd"
        self.set_osd_state(f"Switching to {nxt.upper()}…")

        def done(ok):
            if ok is True:
                self.qualities[cam] = nxt
                GLib.timeout_add(1500, lambda: self.play_single() and False)
            else:
                self.set_osd_state("This camera picks its own quality")
                GLib.timeout_add(2500, lambda: self.set_osd_state("") and False)
            return False
        self.bg(lambda: self.bridge().set_quality(cam, nxt), done)

    def set_tour(self, on):
        self.touring = on
        if self.tour_btn.get_active() != on:
            self.tour_btn.set_active(on)
        if self.tour_id:
            GLib.source_remove(self.tour_id)
            self.tour_id = 0
        if on:
            if self.stack.get_visible_child_name() != "single" and self.cams:
                self.open_single(0)

            def tick():
                if self.touring and self.stack.get_visible_child_name() == "single":
                    self.step(1)
                    return True
                self.tour_id = 0
                return False
            self.tour_id = GLib.timeout_add_seconds(self.cfg["tour_seconds"], tick)
        self.flash_osd()

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    # ---- keys ----

    def on_key(self, _ctl, keyval, _code, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if isinstance(self.get_focus(), Gtk.Editable):
            return False
        k = Gdk.keyval_name(keyval) or ""
        page = self.stack.get_visible_child_name()
        if k in ("f", "F", "F11"):
            self.toggle_fullscreen()
            return True
        if k in [str(i) for i in range(1, 10)] and self.cams:
            i = int(k) - 1
            if i < len(self.cams):
                self.open_single(i) if page != "single" else (setattr(self, "single_index", i), self.play_single())
            return True
        if page == "single":
            if k in ("Left", "Page_Up"):
                self.step(-1)
            elif k in ("Right", "Page_Down", "space"):
                self.step(1)
            elif k in ("Escape", "BackSpace"):
                if self.is_fullscreen():
                    self.unfullscreen()
                self.show_grid()
            elif k in ("m", "M"):
                self.cfg["muted"] = not self.cfg["muted"]
                save_config(self.cfg)
                if self.single_feed:
                    self.single_feed.set_muted(self.cfg["muted"])
                self.flash_osd()
            elif k in ("q", "Q"):
                self.toggle_quality()
            elif k in ("t", "T"):
                self.set_tour(not self.touring)
            elif k in ("r", "R"):
                cam = self.cams[self.single_index]["name"]
                self.set_osd_state("Reconnecting…")
                self.bg(lambda: self.bridge().restart(cam), lambda _ok: GLib.timeout_add(2500, lambda: self.play_single() and False) and False)
            else:
                self.flash_osd()
                return False
            return True
        if page == "grid":
            if self.moving >= 0:
                return self.move_key(k)
            if k in ("t", "T"):
                self.set_tour(True)
                return True
            if k in ("r", "R"):
                self.load()
                return True
            if k in ("s", "S"):
                self.dialog(SettingsDialog)
                return True
            if k in ("c", "C"):
                self.dialog(CamerasDialog)
                return True
            if k in ("h", "H", "Delete"):     # hide the focused camera
                t = self.focused_tile()
                if t:
                    self.hide_camera(t.cam["name"])
                return True
            if k in ("o", "O", "m", "M"):   # pick up the focused tile to reorder
                t = self.focused_tile()
                if t:
                    self.start_move(t.index)
                    return True
        return False

    def dialog(self, cls, *args):
        """Open a dialog, or bring the already-open one forward (never two of the same kind)."""
        if not hasattr(self, "_dialogs"):
            self._dialogs = {}
        d = self._dialogs.get(cls)
        if d is None or not d.get_visible():
            d = cls(self, *args)
            self._dialogs[cls] = d
            d.connect("close-request", lambda *_: self._dialogs.pop(cls, None) and False)
        d.present()
        return d

    def focused_tile(self):
        focus = self.get_focus()
        return next((t for t in self.tiles if focus is t or (focus and focus.is_ancestor(t))), None)

    def start_move(self, index):
        if 0 <= index < len(self.tiles):
            self.moving = index
            self.tiles[index].add_css_class("moving")
            self.tiles[index].grab_focus()
            self.update_status()

    def hide_camera(self, name):
        hidden = set(self.cfg.get("hidden", []))
        hidden.add(name)
        self.cfg["hidden"] = sorted(hidden)
        save_config(self.cfg)
        self.load()

    def move_key(self, k):
        cols = self.grid_cols
        delta = {"Left": -1, "Right": 1, "Up": -cols, "Down": cols}.get(k)
        if k in ("Return", "KP_Enter", "Escape", "o", "O", "m", "M", "space"):
            shown = [c["name"] for c in self.cams]
            self.cfg["order"] = shown + [n for n in self.cfg.get("order", []) if n not in shown]
            save_config(self.cfg)
            idx, self.moving = self.moving, -1
            self.build_grid(focus=idx)
            self.generation += 1
            self.refresh_loop(self.generation)
            return True
        if delta is not None:
            target = self.moving + delta
            if 0 <= target < len(self.cams):
                cams = list(self.cams)
                cams.insert(target, cams.pop(self.moving))
                self.cams = cams
                self.moving = target
                self.build_grid(focus=target)
                self.tiles[target].add_css_class("moving")
                self.update_status()
        return True

    def on_resize(self, *_):
        """Re-fit the grid after the window settles at a new size."""
        if self.relayout_id:
            GLib.source_remove(self.relayout_id)

        def relayout():
            self.relayout_id = 0
            w, h = self.get_width(), self.get_height()
            ow, oh = self.grid_size
            if (self.stack.get_visible_child_name() == "grid" and self.tiles and self.moving < 0
                    and (abs(w - ow) > ow * 0.08 or abs(h - oh) > oh * 0.08)):
                focus = next((t.index for t in self.tiles if t.has_focus()), 0)
                self.build_grid(focus=focus)
                self.generation += 1
                self.refresh_loop(self.generation)
            return False
        self.relayout_id = GLib.timeout_add(500, relayout)

    def on_close(self, *_):
        self.generation += 1
        self.stop_all()
        self.stop_local_server()
        return False

    # ---- built-in server ----

    def welcome(self):
        self.generation += 1
        self.message("Welcome to CouchCam.\n\nRun the camera server here on this PC (it starts and stops with "
                     "CouchCam),\nor connect to one that's already running on your network.",
                     ("Set up this PC", lambda: self.dialog(SetupWizard)),
                     ("Find a server on my network", self.find_bridge),
                     ("Enter an address", lambda: self.dialog(SettingsDialog)))

    def use_local_server(self):
        """Start the built-in server (if not running) and point the viewer at it."""
        self.cfg["server"] = "local"
        self.cfg["bridge"] = LOCAL_HUB
        save_config(self.cfg)
        if self.server is None:
            self.server = LocalServer(on_change=self.update_server_icon)
        self.server.start()
        self.generation += 1
        gen = self.generation
        self.message("Starting the camera server…")
        started = time.time()

        def wait():
            if gen != self.generation:
                return False
            if port_open(5090):
                GLib.timeout_add(800, lambda: gen == self.generation and self.load() and False)
                return False
            if time.time() - started > 45:
                self.message("The camera server didn't start.", ("Retry", self.use_local_server),
                             ("Server logs", self.open_logs), ("Set up / repair", lambda: self.dialog(SetupWizard)))
                return False
            return True
        GLib.timeout_add(700, wait)

    def stop_local_server(self):
        if self.server:
            self.server.stop()

    def use_remote_server(self, url):
        self.stop_local_server()
        self.server = None
        self.cfg["server"] = "remote"
        self.cfg["bridge"] = url
        save_config(self.cfg)
        self.update_server_icon()
        self.load()

    def check_cameras(self):
        script = find_server_file("diagnose.py")
        if not script:
            return self.message("diagnose.py is missing: re-run linux/install.sh from the CouchCam folder.")
        bridge = "http://127.0.0.1:5080" if self.server else self.bridge().base.replace(":5090", ":5080")
        self.dialog(RunDialog, "Check camera connections", [sys.executable, "-u", script, bridge],
                    "Checking each Wyze camera from this PC (takes ~10 s)…")

    def update_server(self):
        installer = find_installer()
        if not installer:
            return self.message("install.sh is missing: re-run linux/install.sh from the CouchCam folder.")
        self.stop_local_server()
        self.message("Updating the camera server…")
        cmd = ["bash", installer, "--app", "--update"] + (["--ring"] if LocalServer.ring_enabled() else [])
        self.dialog(RunDialog, "Update camera server", cmd,
                    "Rebuilding the Wyze bridge from its latest code and updating Ring. A few minutes; "
                    "CouchCam restarts the server when it's done.",
                    None, lambda ok: self.use_local_server())

    def open_logs(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        open_uri("file://" + LOG_DIR, self)

    def update_server_icon(self):
        bad = self.server and any(v in ("failed", "restarting") for _k, v in self.server.summary())
        self.server_btn.set_icon_name("dialog-warning-symbolic" if bad else "network-server-symbolic")

    def fill_server_menu(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=10,
                      margin_start=12, margin_end=12)
        if self.server:
            head = Gtk.Label(xalign=0, label="Camera server: this PC (runs while CouchCam is open)")
            head.add_css_class("heading")
            box.append(head)
            words = {"running": "running", "external": "running (system service)", "restarting": "restarting…",
                     "failed": "failed to start", "stopped": "stopped"}
            for label, state in self.server.summary():
                row = Gtk.Box(spacing=8)
                dot = Gtk.Box(valign=Gtk.Align.CENTER)
                dot.add_css_class("dot")
                dot.add_css_class("ok" if state in ("running", "external") else "warn" if state == "restarting" else "bad")
                row.append(dot)
                row.append(Gtk.Label(label=f"{label}: {words.get(state, state)}", xalign=0))
                box.append(row)
            actions = [("Server logs", self.open_logs),
                       ("Wyze bridge web page", lambda: open_uri("http://127.0.0.1:5080", self))]
            if LocalServer.ring_enabled():
                actions.append(("Ring sign-in page", lambda: open_uri("http://127.0.0.1:55123", self)))
            actions += [("Check camera connections…", self.check_cameras),
                        ("Update server…", self.update_server),
                        ("Restart server", lambda: (self.stop_local_server(), self.use_local_server())),
                        ("Set up / repair…", lambda: self.dialog(SetupWizard))]
            if not LocalServer.ring_enabled():
                actions.append(("Add Ring cameras…", lambda: self.dialog(SetupWizard, True)))
        else:
            head = Gtk.Label(xalign=0, label=f"Camera server: {self.bridge().base or 'not set'}")
            head.add_css_class("heading")
            box.append(head)
            actions = [("Run the server on this PC instead…",
                        lambda: self.use_local_server() if LocalServer.installed() else self.dialog(SetupWizard)),
                       ("Find a server on my network", self.find_bridge)]
        for label, cb in actions:
            b = Gtk.Button(label=label, halign=Gtk.Align.FILL)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, cb=cb: (self.server_pop.popdown(), cb()))
            box.append(b)
        self.server_pop.set_child(box)


class App(Gtk.Application):
    def __init__(self, cfg):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.cfg = cfg

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = Window(self, self.cfg)
        self.win = win
        win.present()
        # Ctrl+C / kill: shut the built-in server down cleanly too.
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (win.on_close(), self.quit()) and False)
        if self.cfg.pop("_fullscreen", False):
            win.fullscreen()


def setup_app_log():
    """When started from the app menu there's no terminal: send our output (and GStreamer's) to
    ~/.local/state/couchcam/logs/app.log, kept under ~5 MB."""
    if sys.stderr and sys.stderr.isatty():
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "app.log")
    try:
        if os.path.getsize(path) > 5_000_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = os.fdopen(2, "w", buffering=1)
    print(f"\n==== CouchCam {VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====", flush=True)


def start_watchdog():
    """If the window stops responding for 5+ s, write every thread's stack to app.log, so a lock-up
    can be pinned to the exact line instead of guessed at."""
    import faulthandler
    beat = [time.monotonic()]

    def tick():
        beat[0] = time.monotonic()
        return True
    GLib.timeout_add(500, tick)

    def watch():
        reported = False
        while True:
            time.sleep(1)
            stalled = time.monotonic() - beat[0]
            if stalled > 5 and not reported:
                print(f"\n=== UI not responding for {stalled:.0f}s at {time.strftime('%H:%M:%S')}; "
                      "thread stacks follow ===", file=sys.stderr, flush=True)
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                reported = True
            elif stalled < 2:
                if reported:
                    print(f"=== UI responsive again at {time.strftime('%H:%M:%S')} ===", file=sys.stderr, flush=True)
                reported = False
    threading.Thread(target=watch, daemon=True, name="ui-watchdog").start()


def main():
    ap = argparse.ArgumentParser(description="CouchCam: Wyze camera viewer (via wyze-bridge)")
    ap.add_argument("--bridge", help="bridge address, e.g. 192.168.1.50 or http://host:5080 (saved)")
    ap.add_argument("--snapshots", action="store_true", help="snapshot tiles instead of live video in the grid (saved)")
    ap.add_argument("--live", action="store_true", help="live video tiles in the grid (saved)")
    ap.add_argument("--fullscreen", action="store_true", help="start full screen (kiosk-style)")
    ap.add_argument("--version", action="version", version=f"CouchCam {VERSION}")
    args = ap.parse_args()

    cfg = load_config()
    changed = False
    if args.bridge:
        cfg["bridge"] = normalize(args.bridge)
        changed = True
    if args.snapshots or args.live:
        cfg["live_grid"] = bool(args.live)
        changed = True
    if changed:
        save_config(cfg)
    if args.fullscreen:
        cfg["_fullscreen"] = True

    setup_app_log()
    Gst.init(None)
    start_watchdog()
    App(cfg).run([])


if __name__ == "__main__":
    main()
