#!/usr/bin/env python3
"""CouchCam patch for go2rtc's Wyze (TUTK) client: patience for cameras on weak Wi-Fi.

Stock go2rtc gives a Wyze camera 5 seconds for each connection step (finding it on the LAN, the DTLS
handshake, the login exchange). On a camera with a marginal signal, a few lost packets blow that
budget and the whole connection starts over ("discovery timeout", "av login failed"), which is why
such a camera only connects now and then. The Wyze phone app is far more patient. This raises
those per-step timeouts to TUTK_STEP_SECONDS (default 15) without changing anything once a camera
is streaming. Anchor-based and idempotent; exits non-zero without half-applying if upstream moved.
Usage: go2rtc-weak-wifi.py <go2rtc source dir>
"""
import os
import re
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "."
MARK = "couchcamStep"

helper_file = os.path.join(src, "pkg", "tutk", "couchcam_timeout.go")
helper = '''package tutk

// CouchCam patch: per-step TUTK timeout, overridable with TUTK_STEP_SECONDS (default 15).

import (
	"os"
	"strconv"
	"time"
)

func CouchcamStep() time.Duration {
	if n, err := strconv.Atoi(os.Getenv("TUTK_STEP_SECONDS")); err == nil && n >= 3 && n <= 120 {
		return time.Duration(n) * time.Second
	}
	return 15 * time.Second
}
'''

# (file, exact old text, new text, expected count)
edits = [
    ("pkg/tutk/conn.go",
     "_ = c.SetDeadline(time.Now().Add(5 * time.Second))",
     "_ = c.SetDeadline(time.Now().Add(CouchcamStep())) // couchcamStep", 1),
    ("pkg/tutk/dtls/dtls.go",
     "timeout := 5 * time.Second",
     "timeout := tutk.CouchcamStep() // couchcamStep", 1),
    ("pkg/tutk/dtls/conn_dtls.go",
     "deadline := time.Now().Add(5 * time.Second)",
     "deadline := time.Now().Add(tutk.CouchcamStep()) // couchcamStep", 1),
    ("pkg/tutk/dtls/conn_dtls.go",
     "_ = c.conn.SetDeadline(time.Now().Add(5 * time.Second))",
     "_ = c.conn.SetDeadline(time.Now().Add(tutk.CouchcamStep())) // couchcamStep", 1),
    ("pkg/tutk/dtls/conn_dtls.go",
     "conn.SetReadDeadline(time.Now().Add(5 * time.Second))",
     "conn.SetReadDeadline(time.Now().Add(tutk.CouchcamStep())) // couchcamStep", 1),
    ("pkg/wyze/client.go",
     "5*time.Second)",
     "tutk.CouchcamStep())", None),   # every login/IOCtrl wait in the Wyze client
]

texts = {}
for rel, old, _new, count in edits:
    f = os.path.join(src, rel)
    if f not in texts:
        try:
            texts[f] = open(f).read()
        except OSError as e:
            sys.exit(f"go2rtc patch: can't read {rel}: {e}")
if any(MARK in t for t in texts.values()) or os.path.exists(helper_file):
    print("go2rtc weak-Wi-Fi patch already applied")
    sys.exit(0)

for rel, old, new, count in edits:
    f = os.path.join(src, rel)
    n = texts[f].count(old)
    if (count is not None and n != count) or n == 0:
        sys.exit(f"go2rtc patch: upstream code changed in {rel}; skipping the patch")
    texts[f] = texts[f].replace(old, new)

# Files that now say tutk.CouchcamStep() need the tutk package imported.
imp = '"github.com/AlexxIT/go2rtc/pkg/tutk"'
for f, t in texts.items():
    if "tutk.CouchcamStep()" in t and imp not in t:
        t2, n = re.subn(r'import \(\n', 'import (\n\t' + imp + '\n', t, count=1)
        if n != 1:
            sys.exit(f"go2rtc patch: can't add import to {os.path.relpath(f, src)}; skipping the patch")
        texts[f] = t2

for f, t in texts.items():
    with open(f, "w") as fh:
        fh.write(t)
with open(helper_file, "w") as fh:
    fh.write(helper)
print("go2rtc weak-Wi-Fi patch applied")
