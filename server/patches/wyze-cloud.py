#!/usr/bin/env python3
"""Adds per-camera "stream through Wyze's cloud" to a wyze-bridge source tree (CouchCam patch).

wyze-bridge can already stream a camera over Wyze's cloud WebRTC path (the one the phone app falls back
to), but only for whole models or after its own auto-fallback kicks in. This patch adds:
  * WEBRTC_CAMERAS=cam_a,cam_b   env var: these cameras start on the cloud path
  * POST /api/cameras/<name>/cloud {"enabled": true|false}   switch one camera at runtime
  * "cloud": true|false in each camera's JSON
Anchor-based and idempotent; exits non-zero (without half-applying) if upstream code moved.
Usage: wyze-cloud.py <wyze-bridge source dir>
"""
import os
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "."
MARK = "CouchCam cloud patch"


def path(*p):
    return os.path.join(src, *p)


edits = {
    path("internal", "camera", "manager.go"): [(
        "\t\t\tm.applyQualityLocked(cam)\n\t\t\tm.cameras[name] = cam\n",
        "\t\t\tm.applyQualityLocked(cam)\n"
        "\t\t\tif couchcamCloudCams()[name] { // " + MARK + "\n"
        "\t\t\t\tcam.SetForceWebRTC(true)\n"
        "\t\t\t}\n"
        "\t\t\tm.cameras[name] = cam\n",
    )],
    path("internal", "camera", "camera.go"): [(
        '\t\t"stream_ok": c.streamOk(),\n',
        '\t\t"stream_ok": c.streamOk(),\n'
        '\t\t"cloud":     c.forceWebRTC, // ' + MARK + "\n",
    )],
    path("internal", "webui", "api.go"): [(
        '\tcase action == "audio" && r.Method == "POST":\n',
        '\tcase action == "cloud" && r.Method == "POST":\n'
        "\t\t// " + MARK + ": stream through Wyze's cloud (WebRTC/KVS) instead of LAN P2P.\n"
        "\t\tvar body struct {\n"
        '\t\t\tEnabled bool `json:"enabled"`\n'
        "\t\t}\n"
        "\t\tif err := json.NewDecoder(r.Body).Decode(&body); err != nil {\n"
        '\t\t\twriteJSONError(w, http.StatusBadRequest, "invalid body")\n'
        "\t\t\treturn\n"
        "\t\t}\n"
        "\t\tcam.SetForceWebRTC(body.Enabled)\n"
        "\t\ts.camMgr.RestartStream(reqCtx, name)\n"
        '\t\twriteJSON(w, map[string]interface{}{"status": "ok", "camera": name, "cloud": body.Enabled})\n'
        "\n"
        '\tcase action == "audio" && r.Method == "POST":\n',
    )],
}

new_file = path("internal", "camera", "couchcam_cloud.go")
new_src = '''package camera

// ''' + MARK + ''': cameras listed in WEBRTC_CAMERAS stream over Wyze's cloud WebRTC path.

import (
	"os"
	"strings"
)

func couchcamCloudCams() map[string]bool {
	cams := map[string]bool{}
	for _, n := range strings.Split(os.Getenv("WEBRTC_CAMERAS"), ",") {
		if n = strings.ToLower(strings.TrimSpace(n)); n != "" {
			cams[n] = true
		}
	}
	return cams
}
'''

# Check everything first so a moved anchor never leaves a half-patched tree.
pending = {}
for f, reps in edits.items():
    try:
        text = open(f).read()
    except OSError as e:
        sys.exit(f"cloud patch: can't read {f}: {e}")
    if MARK in text:
        continue
    for old, _new in reps:
        if text.count(old) != 1:
            sys.exit(f"cloud patch: upstream code changed in {os.path.relpath(f, src)}; skipping the patch")
    for old, new in reps:
        text = text.replace(old, new)
    pending[f] = text

for f, text in pending.items():
    with open(f, "w") as fh:
        fh.write(text)
with open(new_file, "w") as fh:
    fh.write(new_src)
print("cloud patch applied" if pending else "cloud patch already applied")
