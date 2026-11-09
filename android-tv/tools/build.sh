#!/usr/bin/env bash
# Build the release APK (and optionally push it to the TV).
#   tools/build.sh                 build only -> dist/couchcam-<ver>-universal.apk
#   tools/build.sh <tv-ip>        build, then adb-install to the TV at that IP
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(grep -oP 'versionName = "\K[^"]+' app/build.gradle.kts)

[[ -f local.properties ]] || echo "sdk.dir=${ANDROID_HOME:-$HOME/Android/Sdk}" > local.properties

./gradlew --no-daemon -q assembleRelease
NAME="couchcam-${VERSION}-universal.apk"
mkdir -p dist
cp app/build/outputs/apk/release/app-release.apk "dist/$NAME"
(cd dist && sha256sum "$NAME" > "$NAME.sha256")
echo "Built dist/$NAME"

if [[ -n "${1:-}" ]]; then
  adb connect "$1:5555" >/dev/null
  adb -s "$1:5555" install -r "dist/$NAME"
  adb -s "$1:5555" shell monkey -p app.couchcam -c android.intent.category.LEANBACK_LAUNCHER 1 >/dev/null 2>&1 || true
  echo "Installed and launched on $1"
fi
