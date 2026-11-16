#!/usr/bin/env python3
"""Internet check: finds where a slow connection breaks. No root, Python standard library only.

Order: traffic already running -> cable / Wi-Fi -> router -> internet line -> DNS
       -> download -> upload -> lag when the line is busy.
Privacy: no address (yours, the router's or a server's) is shown, saved or sent anywhere,
and error text has addresses masked. Outside contacts: DNS lookups, pings to diag_ping_host and the
speed-test server, which see your public address as every website does.
  netcheck.py     run the check in a terminal
"""
import ipaddress
import json
import os
import re
import signal
import socket
import struct
import subprocess
import time
import urllib.request
from subprocess import DEVNULL, PIPE

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "settings.json")) as _f:
    CFG = json.load(_f)

OK, WARN, BAD, SKIP = "ok", "warn", "bad", "skip"
STEPS = [("busy", "Traffic already running"), ("link", "Cable / Wi-Fi"), ("router", "PC to router"),
         ("internet", "Internet line"), ("dns", "DNS (finding websites)"), ("down", "Download speed"),
         ("up", "Upload speed"), ("lag", "Lag when the line is busy")]
TOPIC = {"busy": "other traffic on this PC", "link": "your cable or Wi-Fi", "router": "between this PC and the router",
         "internet": "your internet provider (ISP) line", "dns": "DNS (finding websites by name)",
         "down": "low download speed", "up": "low upload speed", "lag": "lag when the line is busy"}
ADDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\b", re.I)
ENV = dict(os.environ, LC_ALL="C")  # ping output in English, so it can be read


def mask(text):
    return ADDR.sub("x.x.x.x", str(text))


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def num(path):
    try:
        return int(read(path).strip())
    except ValueError:
        return None


def mbit(kbit):
    return "%.1f Mbit/s" % (kbit / 1000.0)


def default_route():
    """(device, router address) from the kernel's IPv4 table. The address stays in memory only."""
    for line in read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[1] == "00000000" and int(f[3], 16) & 2:
            return f[0], socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    return None, None


def ping_args(host, count):
    ipaddress.ip_address(host)  # only plain addresses reach the command line, never a name or an option
    return ["ping", "-n", "-q", "-i", "0.2", "-W", "1", "-c", str(count), host]


def parse_ping(out):
    """(loss %, average ms, jitter ms); the times are None when nothing came back."""
    m = re.search(r"([\d.]+)% packet loss", out)
    r = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/([\d.]+) ms", out)
    return (float(m.group(1)) if m else 100.0), (float(r.group(1)) if r else None), (float(r.group(2)) if r else None)


def ping(host, count=10):
    r = subprocess.run(ping_args(host, count), stdout=PIPE, stderr=DEVNULL, universal_newlines=True, env=ENV)
    return parse_ping(r.stdout)


def under_load(host, work):
    """Runs work() while pinging host. Returns (work's result, average ping during it)."""
    p = subprocess.Popen(ping_args(host, 1000), stdout=PIPE, stderr=DEVNULL, universal_newlines=True, env=ENV)
    try:
        result = work()
    finally:
        p.send_signal(signal.SIGINT)  # ping prints its summary on Ctrl+C
        try:
            out = p.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            p.kill()
            out = p.communicate()[0]
    return result, parse_ping(out)[1]


def request(url, data=None):
    return urllib.request.Request(url, data=data, headers={"User-Agent": "SpeedBox"})


def download(url, limit):
    """kbit/s, reading for at most `limit` seconds."""
    n, t = 0, time.monotonic()
    with urllib.request.urlopen(request(url), timeout=15) as r:
        while time.monotonic() - t < limit:
            chunk = r.read(65536)
            if not chunk:
                break
            n += len(chunk)
    return n * 8 / 1000.0 / (time.monotonic() - t)


def upload(url, limit, total):
    """kbit/s from 2 MB uploads until `total` bytes or `limit` seconds (random data, so nothing compresses it)."""
    n, t = 0, time.monotonic()
    while n < total and time.monotonic() - t < limit:
        data = os.urandom(min(2000000, total - n))
        with urllib.request.urlopen(request(url, data), timeout=30) as r:
            r.read()
        n += len(data)
    return n * 8 / 1000.0 / (time.monotonic() - t)


# ---- the steps: each returns (shown value, level, what it means) ----
def check_busy(dev):
    if not dev:
        return "-", SKIP, "No network device."
    path = "/sys/class/net/%s/statistics/%s_bytes" % (dev, "%s")
    a = [num(path % k) or 0 for k in ("rx", "tx")]
    time.sleep(2)
    b = [num(path % k) or 0 for k in ("rx", "tx")]
    down, up = ((y - x) * 8 / 1000.0 / 2 for x, y in zip(a, b))
    value = "down %s, up %s" % (mbit(down), mbit(up))
    if max(down, up) > 1000:
        return value, WARN, ("This PC was already using the internet, so the speeds below are lower. "
                             "Pause downloads, torrents and updates, then run the check again.")
    return value, OK, "This PC is quiet, so the test gets the full line."


def check_link(dev):
    if not dev:
        return "not connected", BAD, "No connection. Plug in the cable or join a Wi-Fi network."
    base = "/sys/class/net/%s/" % dev
    errors = sum(num(base + "statistics/%s_errors" % k) or 0 for k in ("rx", "tx"))
    packets = sum(num(base + "statistics/%s_packets" % k) or 0 for k in ("rx", "tx"))
    if os.path.isdir(base + "wireless"):
        m = re.search(r"^\s*%s:\s+\S+\s+\S+\s+(-?\d+)" % re.escape(dev), read("/proc/net/wireless"), re.M)
        dbm = int(m.group(1)) if m else None
        if dbm is None:
            value, level, hint = "Wi-Fi", WARN, "The Wi-Fi signal could not be read."
        elif dbm >= -65:
            value, level, hint = "Wi-Fi, signal %d dBm" % dbm, OK, "Good Wi-Fi signal."
        elif dbm >= -75:
            value, level, hint = "Wi-Fi, signal %d dBm" % dbm, WARN, "Weak Wi-Fi signal. Move closer to the router or use a cable."
        else:
            value, level, hint = "Wi-Fi, signal %d dBm" % dbm, BAD, "Very weak Wi-Fi signal. Move closer to the router or use a cable."
    else:
        speed, duplex = num(base + "speed"), read(base + "duplex").strip()
        value = "Cable, %s Mb/s, %s duplex" % (speed if speed and speed > 0 else "?", duplex or "?")
        if (speed and 0 < speed < 100) or duplex == "half":
            level, hint = BAD, "The cable link is slow. Replug the cable, or try another cable or router port."
        else:
            level, hint = OK, "The cable link is healthy."
    if packets and errors / packets > 0.001:
        value += ", %d errors" % errors
        level, hint = (BAD if level == BAD else WARN), "The network card sees damaged packets. Try another cable or port."
    return value, level, hint


def check_router(gw):
    if not gw:
        return "-", SKIP, "No IPv4 router found."
    loss, avg, _ = ping(gw)
    if avg is None:
        return "no answer", WARN, "The router did not answer. Some routers block ping, so this is not always a problem."
    value = "%.0f ms, %g%% lost" % (avg, loss)
    if loss > 0:
        return value, BAD, "Packets are lost before they even reach the router: a Wi-Fi or cable problem in your home."
    if avg > 20:
        return value, WARN, "The router answers slowly. It may be overloaded: restart it."
    return value, OK, "Your home network is healthy."


def check_internet(host):
    loss, avg, jitter = ping(host, 15)
    if avg is None:
        return "no answer", BAD, ("Nothing comes back from the internet. The problem is the router's internet "
                                  "link or your provider. Restart the router, then call the provider.")
    value = "%.0f ms, jitter %.0f ms, %g%% lost" % (avg, jitter, loss)
    if loss >= 2:
        return value, BAD, "Packets are lost on the way to the internet: a problem on your provider's line."
    if avg > 100:
        return value, WARN, "High ping. Games and calls will feel slow. Often busy hours at the provider."
    if jitter > 30:
        return value, WARN, "The ping jumps around (jitter). Calls and games will stutter."
    return value, OK, "The line to the internet is steady."


def check_dns(domains):
    """Middle time of lookups for random new names, which no cache can answer, like a site you never opened."""
    times = []
    for domain in domains:
        t = time.monotonic()
        try:
            socket.getaddrinfo("%s.%s" % (os.urandom(6).hex(), domain), 443, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            if e.errno not in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
                return "failed", BAD, "Websites can't be found by name. Check the DNS setting in your network settings."
        times.append((time.monotonic() - t) * 1000)  # "no such name" is a real answer, so it counts
    ms = sorted(times)[len(times) // 2]
    if ms > 300:
        return "%.0f ms" % ms, BAD, ("Very slow DNS: every new website waits before it starts loading. Check your DNS "
                                     "setting (a VPN, DNS filter or DNS service can cause this).")
    if ms > 100:
        return "%.0f ms" % ms, WARN, "Slow DNS: new websites start a little late. Check your DNS setting."
    return "%.0f ms" % ms, OK, "Websites are found fast."


def check_speed(key, kbit, smooth_on):
    if key == "down":
        levels = ((5000, BAD, "Too slow for HD video (needs about 5 Mbit/s)."),
                  (25000, WARN, "Enough for HD video, too slow for 4K (needs about 25 Mbit/s)."))
        good = "Enough for 4K video."
    else:
        levels = ((1000, BAD, "Too slow for video calls and uploads."),
                  (3000, WARN, "Enough for calls, slow for big uploads."))
        good = "Enough for video calls and uploads."
    level, hint = next(((lv, h) for limit, lv, h in levels if kbit < limit), (OK, good))
    if smooth_on:
        hint += " Whole-PC smoothing is on, so this is the smoothed speed."
    return mbit(kbit), level, hint


def check_lag(idle, loaded):
    worst = max([v - idle for v in loaded if v is not None] or [0])
    value = "+%.0f ms" % worst
    if worst < 30:
        return value, OK, "The ping stays steady while the line is busy."
    if worst < 100:
        return value, WARN, "The ping grows while the line is busy. Turn on whole-PC smoothing."
    return value, BAD, ("Big lag while anything downloads or uploads (bufferbloat): games, calls and videos "
                        "stutter. Turn on whole-PC smoothing in SpeedBox.")


def run(report, smooth_on=False):
    """Runs every step in order. report(key, value, level, hint) is called as each one ends."""
    try:
        host = socket.getaddrinfo(CFG["diag_ping_host"], None, socket.AF_INET)[0][4][0]
    except OSError:
        host = None
    limit = CFG["diag_max_seconds"]
    dev, gw = default_route()
    results, state = {}, {}

    def step(key, fn):
        try:
            res = fn()
        except Exception as e:  # network errors come in many types; one failed step must not stop the rest
            res = ("failed", BAD if key in ("down", "up") else WARN, "Could not test: " + mask(e))
        results[key] = res
        report(key, *res)
        return res

    def speed(key):
        work = (lambda: download(CFG["speedtest_down_url"], limit)) if key == "down" else \
            (lambda: upload(CFG["speedtest_up_url"], limit, CFG["speedtest_up_bytes"]))
        kbit, state[key] = under_load(host, work)
        return check_speed(key, kbit, smooth_on)

    if not host:
        for key, _ in STEPS:
            results[key] = ("-", BAD if key == "dns" else SKIP, "Websites can't be found by name, so nothing else "
                            "could be tested. Check the cable, Wi-Fi and DNS setting." if key == "dns" else "Skipped.")
            report(key, *results[key])
        return results
    step("busy", lambda: check_busy(dev))
    step("link", lambda: check_link(dev))
    step("router", lambda: check_router(gw))
    _, level, _ = step("internet", lambda: check_internet(host))
    idle = ping(host, 5)[1]
    for key, fn in (("dns", lambda: check_dns(CFG["diag_dns_domains"])),
                    ("down", lambda: speed("down")), ("up", lambda: speed("up")),
                    ("lag", lambda: check_lag(idle, [state.get("down"), state.get("up")]))):
        if idle is None and level == BAD:
            results[key] = ("-", SKIP, "Skipped: no internet.")
            report(key, *results[key])
        elif key == "lag" and idle is None:
            results[key] = ("-", SKIP, "Skipped: no ping to compare with.")
            report(key, *results[key])
        else:
            step(key, fn)
    return results


def verdict(results):
    """(level, headline, advice) from the first bad step, else the first warning."""
    for level in (BAD, WARN):
        for key, _ in STEPS:
            if key in results and results[key][1] == level:
                head = ("Problem found: " if level == BAD else "Mostly fine. Watch: ") + TOPIC[key]
                return level, head, results[key][2]
    return OK, "No problem found", ("Your line, router and speed look healthy. If a video still lags, the cause is "
                                    "the website, the browser (YouTube: Stats for nerds, Dropped frames) "
                                    "or another device on your network.")


if __name__ == "__main__":
    names = dict(STEPS)
    res = run(lambda k, v, lv, h: print("%-5s %-28s %-36s %s" % (lv.upper(), names[k], v, h)))
    level, head, advice = verdict(res)
    print("\n%s\n%s" % (head, advice))
