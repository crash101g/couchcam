# CouchCam

> ⚠️ **Very experimental.** This is a personal hobby project. Expect bugs, breaking changes and rough
> edges. Use at your own risk, and don't rely on it for security monitoring.

CouchCam lets you watch your **Wyze** and **Ring** cameras on a TV or Linux desktop, from the couch.

It has three parts:

- **Linux desktop app** (`linux/`): a GTK app that shows a grid of cameras, full-screen view, a camera
  tour and keyboard controls. It can also set up and run the camera server itself, so you don't need a
  separate machine.
- **Camera server** (`server/`): puts one simple API (`couchcam-hub`, port 5090) in front of
  [docker-wyze-bridge](https://github.com/IDisposable/docker-wyze-bridge) / go2rtc (Wyze, over your LAN) and
  [ring-mqtt](https://github.com/tsightler/ring-mqtt) (Ring, through Ring's cloud). It can run on the
  desktop or as always-on system services on a Debian box/LXC. No Docker needed. It includes small
  patches for Wyze cameras on weak Wi-Fi.
- **Android / Google TV app** (`android-tv/`): a remote-friendly app for the same camera grid and
  full-screen view. It finds the server on your network by itself.

```
Wyze cams ──P2P (LAN)──▶ wyze-bridge  RTSP ─┐
Ring cams ──Ring cloud──▶ ring-mqtt   RTSP ─┼─▶ couchcam-hub :5090 ──▶ CouchCam TV / Linux
```

## Quick start

```bash
cd linux && ./install.sh                 # Linux app; open "CouchCam" and choose "Set up this PC"
cd android-tv && tools/build.sh <tv-ip>  # build the TV app and adb-install it
```

You'll need your own Wyze account (plus a free Wyze API ID/key) and, optionally, a Ring account.
Credentials are stored locally on your machine only. Don't expose the server's ports to the internet.

Not affiliated with Wyze or Ring.

## License

MIT. See [LICENSE](LICENSE).
