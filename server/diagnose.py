#!/usr/bin/env python3
"""Why won't a Wyze camera connect? Checks every camera the bridge knows about, from this PC.

For each camera: is the address the Wyze cloud gave us still that camera (MAC match), does it answer
on the LAN, and if it moved, where it is now. Also checks this PC's subnet and firewall.
No root needed. Usage: diagnose.py [bridge-url]   (default http://127.0.0.1:5080)
"""
import ipaddress
import json
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BRIDGE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5080").rstrip("/")


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def norm_mac(m):
    return "".join(c for c in (m or "").lower() if c in "0123456789abcdef")


def ping(ip):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main():
    try:
        with urllib.request.urlopen(BRIDGE + "/api/cameras", timeout=8) as r:
            cams = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"Can't read the camera list from the Wyze bridge at {BRIDGE}: {e}")
        print("Is CouchCam (or the server) running?")
        return 1

    # ---- this PC
    addrs = []
    for line in run("ip", "-4", "-o", "addr", "show", "scope", "global").splitlines():
        parts = line.split()
        if "inet" in parts:
            addrs.append((parts[1], ipaddress.ip_interface(parts[parts.index("inet") + 1])))
    print("This PC:")
    for dev, a in addrs:
        print(f"  {dev:<10} {a}")
    fw = run("systemctl", "is-active", "ufw").strip()
    if fw == "active":
        print("  Firewall (ufw) is ON. Wyze cameras answer over UDP; if a camera replies from a different")
        print("  port, ufw drops it. Fix:  sudo ufw allow from 192.168.0.0/16 proto udp")
    else:
        print(f"  Firewall (ufw): {fw or 'not installed'}")

    # ---- fill the neighbour (ARP) table: ping every address in the cameras' /24s
    nets = {ipaddress.ip_network(f"{c.get('cloud_ip') or c.get('ip')}/24", strict=False) for c in cams
            if (c.get("cloud_ip") or c.get("ip"))}
    local_nets = [a.network for _d, a in addrs]
    for n in nets:
        if not any(n.overlaps(ln) for ln in local_nets):
            print(f"  ! Cameras are on {n} but this PC isn't. Wyze's local streaming needs the PC on the same LAN")
            print("    (or routing + firewall rules between the VLANs for UDP to the cameras).")
    print("\nScanning the camera subnet(s)…")
    hosts = [str(h) for n in nets if any(n.overlaps(ln) for ln in local_nets) for h in n.hosts()]
    with ThreadPoolExecutor(128) as pool:
        alive = {h for h, ok in zip(hosts, pool.map(ping, hosts)) if ok}
    neigh = {}
    try:
        for e in json.loads(run("ip", "-j", "neigh") or "[]"):
            if e.get("lladdr") and e.get("dst"):
                neigh.setdefault(norm_mac(e["lladdr"]), set()).add(e["dst"])
    except ValueError:
        pass

    # ---- per camera
    print(f"\n{'camera':<20} {'model':<16} {'cloud IP':<15} result")
    problems = 0
    for c in sorted(cams, key=lambda c: c.get("name", "")):
        name, model = c.get("name", "?"), c.get("model_name") or c.get("model", "")
        ip = c.get("cloud_ip") or c.get("ip") or ""
        mac = norm_mac(c.get("mac"))
        at = sorted(neigh.get(mac, set()))
        if not ip:
            verdict = "no address from the Wyze cloud (camera offline in the Wyze app?)"
        elif ip in at:
            verdict = "OK (found at its address)" if ip in alive else "found at its address but didn't answer ping"
        elif at:
            verdict = f"MOVED: the camera is now at {', '.join(at)}; the Wyze cloud still says {ip}"
        elif ip in alive:
            verdict = "WRONG DEVICE: something else answers at this address; the camera isn't on this LAN"
        else:
            verdict = "NO REPLY: not found on this LAN (powered off, weak Wi-Fi, other VLAN, or AP client isolation)"
        if not verdict.startswith("OK"):
            problems += 1
        print(f"{name:<20} {model[:16]:<16} {ip:<15} {verdict}")

    print()
    if problems == 0:
        print("Every camera is where the Wyze cloud says it is. If some still won't stream: weak Wi-Fi, or")
        print("too many viewers at once (close the Wyze app on phones, try SD, or turn off Live grid).")
    else:
        print("What the results mean:")
        print("  MOVED       The camera got a new address from DHCP and the Wyze cloud hasn't caught up (this")
        print("              can take hours). Fix for good: DHCP reservations for the cameras in pfSense.")
        print("              The updated Wyze bridge (CouchCam server menu → Update server) also finds")
        print("              moved cameras by MAC address by itself.")
        print("  NO REPLY    The PC can't see the camera at all: check it's online in the Wyze app, its Wi-Fi")
        print("              signal, and that it's on the same network/VLAN as this PC.")
        print("  WRONG DEVICE  Another device took the camera's old address; same fix as MOVED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
