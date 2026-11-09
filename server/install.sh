#!/usr/bin/env bash
# CouchCam server, no Docker: wyze-bridge + couchcam-hub (+ ring-mqtt) as systemd services.
#
#   ./install.sh                 install/upgrade Wyze + hub
#   ./install.sh --ring          ...and Ring (ring-mqtt + mosquitto)
#   ./install.sh --update        rebuild everything at the latest releases, keep settings
#   ./install.sh --status        show services and cameras
#   ./install.sh --uninstall     stop and remove services + programs (keeps settings; add --purge to wipe)
#   ./install.sh --export        save settings + Wyze state + Ring login to couchcam-server-settings.tar.gz
#   ./install.sh --import FILE   install using a saved settings file (e.g. inside the Proxmox LXC later)
#   ./install.sh --app [--ring]  components only, no services: the CouchCam desktop app starts and stops
#                                them itself (its setup wizard runs this; credentials come in via env)
#
# Run as your normal user on the desktop: programs go in ~/.local/share/couchcam-server, settings in
# ~/.config/couchcam-server, and services are systemd *user* units kept running with lingering.
# Run as root (e.g. in a Proxmox LXC): /opt/couchcam-server, /etc/couchcam-server, system units
# running as a 'couchcam' user. Same script, same settings files, so moving later is a copy.
set -euo pipefail

GO2RTC_VERSION="${GO2RTC_VERSION:-1.9.14}"
NODE_VERSION="${NODE_VERSION:-22.21.1}"
WYZE_REPO="https://github.com/IDisposable/docker-wyze-bridge.git"
RING_REPO="https://github.com/tsightler/ring-mqtt.git"
HERE="$(cd "$(dirname "$0")" && pwd)"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

WANT_RING=0; UPDATE=0; ACTION=install; PURGE=0; IMPORT=""; APP=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  a="${args[$i]}"
  case "$a" in
    --export) ACTION=export ;;
    --app) APP=1 ;;
    --import) i=$((i + 1)); IMPORT="$(realpath "${args[$i]:-}")"; [[ -f "$IMPORT" ]] || fail "No such file: ${args[$i]:-}" ;;
    --ring) WANT_RING=1 ;;
    --update) UPDATE=1 ;;
    --status) ACTION=status ;;
    --uninstall) ACTION=uninstall ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    *) fail "Unknown option: $a" ;;
  esac
done

# ---- mode ------------------------------------------------------------------------------------
if [[ $EUID -eq 0 ]]; then
  MODE=system
  PREFIX=/opt/couchcam-server; CONF=/etc/couchcam-server; UNITDIR=/etc/systemd/system
  RUN_USER=couchcam; SUDO=""
  sc() { systemctl "$@"; }
  jc() { journalctl "$@"; }
else
  MODE=user
  PREFIX="$HOME/.local/share/couchcam-server"; CONF="$HOME/.config/couchcam-server"
  UNITDIR="$HOME/.config/systemd/user"; RUN_USER="$USER"
  # From a terminal use sudo; from the app (no terminal) use the desktop's password dialog.
  if [[ -t 0 ]]; then SUDO=sudo; else SUDO=pkexec; fi
  sc() { systemctl --user "$@"; }
  jc() { journalctl --user "$@"; }
fi
BIN="$PREFIX/bin"; DATA="$PREFIX/data"
SERVICES=(wyze-bridge couchcam-hub ring-mqtt)

case "$(uname -m)" in
  x86_64) GOARCH=amd64; NODEARCH=x64 ;;
  aarch64) GOARCH=arm64; NODEARCH=arm64 ;;
  *) fail "Unsupported CPU: $(uname -m)" ;;
esac

lan_ip() { ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'; }
latest_tag() {  # newest vX.Y.Z (no betas) of a git repo
  git ls-remote --tags --refs "$1" | awk -F/ '{print $3}' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1
}
ring_enabled() { [[ -f "$CONF/hub.env" ]] && grep -q '^RING_ENABLED=true' "$CONF/hub.env"; }

# ---- status / uninstall -----------------------------------------------------------------------
if [[ $ACTION == status ]]; then
  for s in "${SERVICES[@]}"; do
    [[ -f "$UNITDIR/$s.service" ]] && printf '%-14s %s\n' "$s" "$(sc is-active "$s" || true)"
  done
  echo; curl -fsS http://127.0.0.1:5090/api/cameras 2>/dev/null | python3 -c '
import json,sys
for c in json.load(sys.stdin):
    print("  %-26s %-6s %-24s %s" % (c["name"], c.get("source","wyze"), c.get("model_name",""), c["state"]))
' || echo "  hub not answering on :5090"
  exit 0
fi

if [[ $ACTION == export ]]; then
  out="$PWD/couchcam-server-settings.tar.gz"; tmp="$(mktemp -d)"
  mkdir -p "$tmp/conf" "$tmp/ring" "$tmp/wyze"
  cp -a "$CONF"/*.env "$tmp/conf/" 2>/dev/null || fail "No settings in $CONF"
  cp -a "$PREFIX/ring-mqtt/config.json" "$PREFIX/ring-mqtt/ring-state.json" "$tmp/ring/" 2>/dev/null || true
  cp -a "$DATA/wyze/." "$tmp/wyze/" 2>/dev/null || true
  (umask 077; tar -czf "$out" -C "$tmp" .)
  rm -rf "$tmp"
  echo "Saved $out (contains your Wyze password and Ring token: keep it private)."
  echo "On the new machine/LXC:  ./install.sh --import couchcam-server-settings.tar.gz"
  exit 0
fi

if [[ $ACTION == uninstall ]]; then
  for s in "${SERVICES[@]}"; do
    sc disable --now "$s" 2>/dev/null || true
    rm -f "$UNITDIR/$s.service"
  done
  sc daemon-reload
  if (( PURGE )); then rm -rf "$PREFIX" "$CONF"; echo "Removed programs, data and settings.";
  else rm -rf "$BIN" "$PREFIX/hub" "$PREFIX/node" "$PREFIX/build"
       echo "Services removed. Kept settings in $CONF and state in $PREFIX (ring-mqtt token, camera data)."; fi
  exit 0
fi

# ---- import saved settings ------------------------------------------------------------------
if [[ -n "$IMPORT" ]]; then
  say "Importing settings from $IMPORT"
  mkdir -p "$CONF" "$DATA/wyze" "$PREFIX/ring-import"
  tmp="$(mktemp -d)"; tar -xzf "$IMPORT" -C "$tmp"
  cp -a "$tmp/conf/." "$CONF/"; cp -a "$tmp/wyze/." "$DATA/wyze/" 2>/dev/null || true
  cp -a "$tmp/ring/." "$PREFIX/ring-import/" 2>/dev/null || true
  rm -rf "$tmp"
  # BRIDGE_IP belongs to the old machine.
  sed -i "s/^BRIDGE_IP=.*/BRIDGE_IP=$(lan_ip)/" "$CONF/wyze-bridge.env"
fi

# ---- packages ---------------------------------------------------------------------------------
command -v apt-get >/dev/null || fail "This installer expects Debian/Ubuntu/Pop!_OS (apt)."
PKGS=(ca-certificates curl git ffmpeg python3 iproute2)
if ring_enabled; then WANT_RING=1; fi
if (( WANT_RING )); then PKGS+=(python3-paho-mqtt mosquitto mosquitto-clients jq bash); fi
missing=(); for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
  say "Installing packages: ${missing[*]}"
  # One elevated call, so the app only asks for the password once.
  $SUDO env DEBIAN_FRONTEND=noninteractive sh -c "apt-get update -qq && apt-get install -y -qq ${missing[*]}" \
    || fail "Package install failed or was cancelled."
fi
if (( WANT_RING && ! APP )); then
  # Debian's mosquitto 2.x listens on localhost only with its stock config: exactly what we want.
  # (In --app mode CouchCam uses that one if it's running, or starts its own private broker.)
  $SUDO systemctl enable --now mosquitto >/dev/null 2>&1 || warn "Couldn't start mosquitto (no systemd in this container?)"
fi

if [[ $MODE == system ]] && ! id "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home "$PREFIX" --shell /usr/sbin/nologin "$RUN_USER"
fi
mkdir -p "$BIN" "$DATA/wyze" "$DATA/media" "$PREFIX/hub" "$PREFIX/build" "$CONF"
chmod 700 "$CONF"

# ---- go2rtc ----------------------------------------------------------------------------------
if [[ ! -x "$BIN/go2rtc" ]] || (( UPDATE )) || ! "$BIN/go2rtc" -version 2>/dev/null | grep -q "$GO2RTC_VERSION"; then
  say "Downloading go2rtc $GO2RTC_VERSION"
  curl -fsSL -o "$BIN/go2rtc.new" \
    "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GOARCH}"
  chmod +x "$BIN/go2rtc.new" && mv "$BIN/go2rtc.new" "$BIN/go2rtc"
fi

# ---- Go toolchain: fetched privately for building, not installed system-wide ----
ensure_go() {   # ensure_go <version>  -> prints GOROOT
  local gover="$1" goroot="$PREFIX/build/go$1"
  if [[ ! -x "$goroot/bin/go" ]]; then
    say "Fetching Go $gover toolchain (build only, ~70 MB, kept in $PREFIX/build)" >&2
    rm -rf "$PREFIX"/build/go1.*
    mkdir -p "$goroot"
    curl -fsSL "https://go.dev/dl/go${gover}.linux-${GOARCH}.tar.gz" | tar -xz -C "$goroot" --strip-components=1
  fi
  echo "$goroot"
}

# ---- wyze-bridge (built from source) ----
build_wyze() {
  local tag src gover goroot
  # Default: the main branch. It finds cameras by MAC when the Wyze cloud has a stale IP
  # (the usual cause of "discovery timeout"), which the last tagged release (4.5.0) can't.
  # WYZE_REF=v4.5.0 (or any tag) pins a release instead.
  tag="${WYZE_REF:-main}"
  [[ -n "$tag" ]] || fail "Couldn't look up wyze-bridge releases (network?)"
  say "Building wyze-bridge $tag"
  src="$PREFIX/build/wyze-bridge"
  rm -rf "$src"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$tag" "$WYZE_REPO" "$src"
  gover="$(awk '/^go [0-9]/{print $2; exit}' "$src/go.mod")"
  ver="${tag#v}"; if [[ "$tag" == main ]]; then ver="main-$(git -C "$src" rev-parse --short HEAD)"; fi
  # CouchCam patch: per-camera "stream through Wyze's cloud" (like the phone app does).
  if python3 "$HERE/patches/wyze-cloud.py" "$src"; then ver="$ver+couchcam"
  else warn "Couldn't add the Wyze cloud option to this bridge version; local streaming still works."; fi
  goroot="$(ensure_go "$gover")"
  # The web UI's WebRTC player ships with go2rtc; the Docker build swaps it in the same way.
  curl -fsSL -o "$src/internal/webui/static/video-rtc.js" \
    "https://raw.githubusercontent.com/AlexxIT/go2rtc/v${GO2RTC_VERSION}/www/video-rtc.js" || true
  (
    cd "$src"
    export GOROOT="$goroot" PATH="$goroot/bin:$PATH" GOPATH="$PREFIX/build/gopath" GOTOOLCHAIN=local CGO_ENABLED=0
    go build -trimpath -ldflags="-s -w -X main.Version=${ver}" -o "$BIN/wyze-bridge.new" ./cmd/wyze-bridge
    if [[ -d cmd/gwell-proxy ]]; then   # sidecar for Wyze Cam OG models
      go build -trimpath -ldflags="-s -w" -o "$BIN/gwell-proxy" ./cmd/gwell-proxy
    fi
  )
  mv "$BIN/wyze-bridge.new" "$BIN/wyze-bridge"
  echo "${ver}" > "$PREFIX/wyze-bridge.version"
}
if [[ ! -x "$BIN/wyze-bridge" ]] || (( UPDATE )); then build_wyze; fi

# ---- go2rtc with CouchCam's weak-Wi-Fi patch (longer connection steps for marginal cameras) ----
build_go2rtc() {
  local src="$PREFIX/build/go2rtc" goroot gover
  say "Building go2rtc $GO2RTC_VERSION with the weak-Wi-Fi patch"
  rm -rf "$src"
  git -c advice.detachedHead=false clone -q --depth 1 --branch "v$GO2RTC_VERSION" https://github.com/AlexxIT/go2rtc.git "$src"
  if ! python3 "$HERE/patches/go2rtc-weak-wifi.py" "$src"; then
    warn "Keeping the stock go2rtc (patch didn't fit this version)."; return 0
  fi
  # Reuse the newest toolchain we already have (it's newer than go2rtc needs).
  gover="$(ls -d "$PREFIX"/build/go1.* 2>/dev/null | sed 's#.*/go##' | sort -V | tail -1)"
  goroot="$(ensure_go "${gover:-$(awk '/^go [0-9]/{print $2; exit}' "$src/go.mod")}")"
  if ( cd "$src"
       export GOROOT="$goroot" PATH="$goroot/bin:$PATH" GOPATH="$PREFIX/build/gopath" GOTOOLCHAIN=local CGO_ENABLED=0
       go build -trimpath -ldflags "-s -w" -o "$BIN/go2rtc.new" . ); then
    mv "$BIN/go2rtc.new" "$BIN/go2rtc"
    echo "$GO2RTC_VERSION+couchcam" > "$PREFIX/go2rtc.version"
  else
    rm -f "$BIN/go2rtc.new"; warn "go2rtc build failed; keeping the stock one."
  fi
}
if ! grep -q couchcam "$PREFIX/go2rtc.version" 2>/dev/null || (( UPDATE )); then build_go2rtc; fi

# ---- settings ---------------------------------------------------------------------------------
WYZE_ENV="$CONF/wyze-bridge.env"
if [[ ! -f "$WYZE_ENV" && -n "${WYZE_EMAIL:-}" ]]; then
  # Non-interactive: the app's setup wizard passes these in the environment, never on the command line.
  ( umask 077; {
    echo "WYZE_EMAIL=$WYZE_EMAIL"
    echo "WYZE_PASSWORD=${WYZE_PASSWORD:-}"
    echo "WYZE_API_ID=${WYZE_API_ID:-}"
    echo "WYZE_API_KEY=${WYZE_API_KEY:-}"
    if [[ -n "${WYZE_TOTP_KEY:-}" ]]; then echo "WYZE_TOTP_KEY=$WYZE_TOTP_KEY"; fi
    echo "BRIDGE_IP=$(lan_ip)"
    echo "TZ=America/Chicago"
    echo "QUALITY=hd"
    echo "AUDIO=true"
  } > "$WYZE_ENV" )
fi
if [[ ! -f "$WYZE_ENV" ]]; then
  [[ -t 0 ]] || fail "No Wyze account details given."
  say "Wyze account. Get an API ID + key at https://developer-api-console.wyze.com/#/apikey/view"
  read -rp "Wyze email: " email
  read -rsp "Wyze password: " pass; echo
  read -rp "Wyze API ID: " api_id
  read -rsp "Wyze API key: " api_key; echo
  read -rp "Authenticator-app TOTP secret (blank if none): " totp
  ( umask 077; cat > "$WYZE_ENV" <<EOF
# wyze-bridge settings (all options: https://github.com/IDisposable/docker-wyze-bridge)
WYZE_EMAIL=$email
WYZE_PASSWORD=$pass
WYZE_API_ID=$api_id
WYZE_API_KEY=$api_key
${totp:+WYZE_TOTP_KEY=$totp}
BRIDGE_IP=$(lan_ip)
TZ=America/Chicago
QUALITY=hd
AUDIO=true
# Optional hardening (enter the same values in the CouchCam apps):
# BRIDGE_API_TOKEN=
# STREAM_AUTH=viewer:secret
EOF
  )
fi
# Paths are owned by the installer so the same file works on the desktop and in an LXC.
grep -v -E '^(STATE_DIR|RECORD_PATH|SNAPSHOT_PATH)=' "$WYZE_ENV" > "$WYZE_ENV.tmp" || true
# Find cameras by MAC on the LAN instead of trusting the Wyze cloud's (often stale) IP.
grep -q '^LAN_IP_RESOLVE=' "$WYZE_ENV.tmp" || printf 'LAN_IP_RESOLVE=true\nLAN_IP_SWEEP=true\n' >> "$WYZE_ENV.tmp"
cat >> "$WYZE_ENV.tmp" <<EOF
STATE_DIR=$DATA/wyze
RECORD_PATH=$DATA/media/recordings/{cam_name}/%Y/%m/%d
SNAPSHOT_PATH=$DATA/media/snapshots/{cam_name}/%Y-%m-%d
EOF
mv "$WYZE_ENV.tmp" "$WYZE_ENV"; chmod 600 "$WYZE_ENV"

HUB_ENV="$CONF/hub.env"
[[ -f "$HUB_ENV" ]] || cat > "$HUB_ENV" <<EOF
HUB_PORT=5090
WYZE_BRIDGE_URL=http://127.0.0.1:5080
RING_ENABLED=false
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
RING_RTSP_PORT=8555
# wired | none | all : which Ring cams may play live in a live grid (battery cams drain otherwise)
RING_LIVE_GRID=wired
# RING_LIVESTREAM_USER=
# RING_LIVESTREAM_PASS=
EOF
if (( WANT_RING )); then sed -i 's/^RING_ENABLED=.*/RING_ENABLED=true/' "$HUB_ENV"; fi
chmod 600 "$HUB_ENV"
install -m 644 "$HERE/couchcam-hub.py" "$PREFIX/hub/couchcam-hub.py"

# ---- ring-mqtt --------------------------------------------------------------------------------
if (( WANT_RING )); then
  NODEDIR="$PREFIX/node"
  if [[ ! -x "$NODEDIR/bin/node" ]] || ! "$NODEDIR/bin/node" -v | grep -q "v$NODE_VERSION"; then
    say "Fetching Node.js $NODE_VERSION (private copy for ring-mqtt)"
    rm -rf "$NODEDIR"; mkdir -p "$NODEDIR"
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODEARCH}.tar.xz" \
      | tar -xJ -C "$NODEDIR" --strip-components=1
  fi
  RM="$PREFIX/ring-mqtt"
  if [[ ! -d "$RM/.git" ]] || (( UPDATE )); then
    tag="${RING_REF:-$(latest_tag "$RING_REPO")}"
    say "Installing ring-mqtt $tag"
    if [[ -d "$RM/.git" ]]; then
      git -C "$RM" checkout -q -- lib/go2rtc.js
      git -C "$RM" fetch -q --depth 1 origin "refs/tags/$tag:refs/tags/$tag"
      git -C "$RM" -c advice.detachedHead=false checkout -q "$tag"
    else
      git -c advice.detachedHead=false clone -q --depth 1 --branch "$tag" "$RING_REPO" "$RM"
    fi
    (cd "$RM" && PATH="$NODEDIR/bin:$PATH" npm ci --omit=dev --no-audit --no-fund --loglevel=error)
  fi
  # Native install: wyze-bridge already owns RTSP :8554, so ring-mqtt's go2rtc moves to :8555.
  sed -i "s/listen: ':8554'/listen: ':8555'/" "$RM/lib/go2rtc.js"
  grep -q "listen: ':8555'" "$RM/lib/go2rtc.js" || warn "Couldn't move ring-mqtt RTSP to :8555; check $RM/lib/go2rtc.js"
  mkdir -p "$RM/config"   # standard mode writes go2rtc.yaml here
  if [[ -d "$PREFIX/ring-import" ]]; then   # Ring login carried over by --import
    cp -a "$PREFIX/ring-import/." "$RM/" && rm -rf "$PREFIX/ring-import"
  fi
  [[ -f "$RM/config.json" ]] || cat > "$RM/config.json" <<'EOF'
{
    "mqtt_url": "mqtt://127.0.0.1:1883",
    "mqtt_options": "",
    "livestream_user": "",
    "livestream_pass": "",
    "disarm_code": "",
    "enable_cameras": true,
    "enable_modes": false,
    "enable_panic": false,
    "hass_topic": "homeassistant/status",
    "ring_topic": "ring",
    "location_ids": []
}
EOF
  chmod 600 "$RM/config.json"; chmod 600 "$RM/ring-state.json" 2>/dev/null || true
fi

if [[ $MODE == system ]]; then chown -R "$RUN_USER": "$PREFIX" "$CONF"; fi

if (( APP )); then
  echo "COUCHCAM-COMPONENTS-READY"
  say "Components installed. CouchCam starts them when it opens and stops them when it closes."
  exit 0
fi

# ---- systemd units ----------------------------------------------------------------------------
mkdir -p "$UNITDIR"
TARGET=$([[ $MODE == system ]] && echo multi-user.target || echo default.target)
USERLINE=$([[ $MODE == system ]] && echo "User=$RUN_USER" || true)
HARDEN=$([[ $MODE == system ]] && printf 'NoNewPrivileges=yes\nProtectSystem=strict\nReadWritePaths=%s %s\nPrivateTmp=yes' "$PREFIX" "$CONF" || true)

cat > "$UNITDIR/wyze-bridge.service" <<EOF
[Unit]
Description=Wyze camera bridge (RTSP :8554, API :5080) for CouchCam
After=network-online.target
Wants=network-online.target

[Service]
$USERLINE
WorkingDirectory=$BIN
EnvironmentFile=$WYZE_ENV
Environment=PATH=$BIN:/usr/local/bin:/usr/bin:/bin
ExecStart=$BIN/wyze-bridge
Restart=always
RestartSec=5
$HARDEN

[Install]
WantedBy=$TARGET
EOF

cat > "$UNITDIR/couchcam-hub.service" <<EOF
[Unit]
Description=CouchCam hub (one API for Wyze + Ring, :5090)
After=network-online.target wyze-bridge.service

[Service]
$USERLINE
EnvironmentFile=$HUB_ENV
Environment=WYZE_ENV_FILE=$WYZE_ENV
Environment=HUB_ENV_FILE=$HUB_ENV
ExecStart=/usr/bin/python3 -u $PREFIX/hub/couchcam-hub.py
Restart=always
RestartSec=3
$HARDEN

[Install]
WantedBy=$TARGET
EOF

if (( WANT_RING )); then
cat > "$UNITDIR/ring-mqtt.service" <<EOF
[Unit]
Description=ring-mqtt (Ring cameras -> RTSP :8555) for CouchCam
After=network-online.target

[Service]
$USERLINE
WorkingDirectory=$PREFIX/ring-mqtt
Environment=PATH=$BIN:$PREFIX/node/bin:/usr/bin:/bin
Environment=RUNMODE=standard
Environment=DEBUG=ring-mqtt,ring-rtsp
ExecStart=$PREFIX/node/bin/node $PREFIX/ring-mqtt/ring-mqtt.js
Restart=always
RestartSec=10
$HARDEN

[Install]
WantedBy=$TARGET
EOF
fi

sc daemon-reload
if [[ $MODE == user ]]; then
  # Keep user services running without a login session (and after reboot).
  loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes \
    || loginctl enable-linger "$USER" 2>/dev/null || $SUDO loginctl enable-linger "$USER" \
    || warn "Couldn't enable lingering; services will only run while you're logged in."
fi
ENABLE=(wyze-bridge couchcam-hub); (( WANT_RING )) && ENABLE+=(ring-mqtt)
sc enable "${ENABLE[@]}" >/dev/null 2>&1
sc restart "${ENABLE[@]}"

# ---- report -----------------------------------------------------------------------------------
say "Waiting for the hub"
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:5090/api/version >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:5090/api/version >/dev/null 2>&1 \
  || fail "Hub didn't start. Logs: journalctl $([[ $MODE == user ]] && echo --user) -u couchcam-hub -u wyze-bridge -n 50"

IP="$(lan_ip)"
cat <<MSG

CouchCam server is running ($MODE services; they start at boot).
  Apps connect to:   http://$IP:5090   (found automatically)
  Wyze bridge UI:    http://$IP:5080
  Status:            $0 --status
  Logs:              journalctl $([[ $MODE == user ]] && echo "--user ")-u wyze-bridge -f
MSG
if (( WANT_RING )) && ! grep -q ring_token "$PREFIX/ring-mqtt/ring-state.json" 2>/dev/null; then
  cat <<MSG

  Ring, one time:    open http://$IP:55123 , sign in to Ring and enter the 2FA code.
                     Cameras show up in CouchCam ~30 s later.
MSG
fi
cat <<MSG

If ufw is on:  sudo ufw allow from 192.168.0.0/16 to any port 5080,5090,8554,8555,55123 proto tcp
Discovery on first start can take ~30 s; check with  $0 --status
MSG
