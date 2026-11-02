#!/usr/bin/env python3
"""SpeedBox service: upload and download limits per app, and smoothing for the whole PC.

Runs as root under systemd. It reads the user's rules file, moves matching apps (and every program they
start) into their own cgroup, marks their traffic with nftables and shapes it with tc: HTB + fq_codel on
the network device for upload, and the same on an ifb device for download.

Commands (all as root):
  speedboxd.py run --uid <uid> --rules <file>   the service itself (systemd starts it)
  speedboxd.py install --rules <file>           copy to /usr/local/lib/speedbox, start at boot, remove cake-qos
  speedboxd.py uninstall                        stop, remove the service, undo every network change
  speedboxd.py cleanup                          undo every network change (emergency)
Needs: python3 (3.6+), iproute2 (tc, ip), nftables, systemd, cgroup v2.
"""
import errno
import json
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from subprocess import PIPE, STDOUT

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "settings.json")) as _f:
    CFG = json.load(_f)
LIB = "/usr/local/lib/speedbox"
UNIT = "/etc/systemd/system/speedbox.service"
OLD_UNIT = "/etc/systemd/system/cake-qos.service"  # the earlier whole-PC CAKE service this tool replaces
RUN = "/run/speedbox"
STATUS = RUN + "/status.json"
MOVED = RUN + "/moved.json"  # pid -> cgroup it came from; survives a service restart, not a reboot
CG = "/sys/fs/cgroup"
TABLE = "speedbox"
IFB = CFG["ifb_device"]
BASE_MARK = CFG["mark_base"]
NO_LIMIT = CFG["no_limit_kbit"]
SAFE_CG = re.compile(r"^[\w@.\-/]+$")  # our own cgroup path goes into an nft script
SAFE_DEV = re.compile(r"^[\w.\-]{1,15}$")
SAFE_LEAF = re.compile(r"^[\w .\-]+$")
SAFE_UNIT_PATH = re.compile(r"^[\w@.+/ \-]+$")
SAFE_NET = re.compile(r"^[0-9a-fA-F.:]+/\d{1,3}$")
if not isinstance(CFG["small_packet_bytes"], int) or not 0 <= CFG["small_packet_bytes"] < 1500:
    sys.exit("settings.json: small_packet_bytes must be a number from 0 to 1499")
if not all(SAFE_NET.match(n) for n in CFG["lan_ipv4"] + CFG["lan_ipv6"]):
    sys.exit("settings.json: lan_ipv4 / lan_ipv6 must be networks like 192.168.0.0/16")
if BASE_MARK % 256 or not 0 < BASE_MARK < 0x10000 or not 0 < CFG["max_apps"] < 256:
    sys.exit("settings.json: mark_base must be a multiple of 256 below 65536, max_apps below 256")


def log(msg):
    print(msg, flush=True)  # systemd puts it in the journal: journalctl -u speedbox


def run(*cmd, data=None):
    r = subprocess.run(cmd, input=data, stdout=PIPE, stderr=STDOUT, universal_newlines=True)
    return r.returncode == 0, r.stdout.strip()


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---- rules file (written by the window, so every value is checked) ----
def kbit(v):
    return max(0, min(int(v), NO_LIMIT)) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def clean_rules(data):
    if not isinstance(data, dict):
        raise ValueError("rules file is not a JSON object")
    smooth = data.get("smooth") if isinstance(data.get("smooth"), dict) else {}
    iface = data.get("iface", "auto")
    apps, seen = [], set()
    for a in data.get("apps", []) if isinstance(data.get("apps"), list) else []:
        if not isinstance(a, dict) or not isinstance(a.get("id"), int) or a["id"] in seen:
            continue
        if not 1 <= a["id"] <= CFG["max_apps"]:
            continue
        names = [n.strip().lower() for n in a.get("programs", []) if isinstance(n, str) and n.strip()]
        seen.add(a["id"])
        apps.append({"id": a["id"], "on": a.get("on") is True, "names": names,
                     "mode": a.get("match") if a.get("match") in ("cmdline", "flatpak") else "name",
                     "up": kbit(a.get("up_kbit")), "down": kbit(a.get("down_kbit"))})
    return {"iface": iface if isinstance(iface, str) and (iface == "auto" or SAFE_DEV.match(iface)) else "auto",
            "smooth": {"on": smooth.get("on") is True, "up": kbit(smooth.get("up_kbit")),
                       "down": kbit(smooth.get("down_kbit"))},
            "apps": apps}


def load_rules(path, uid):
    """None if there is no rules file yet. Refuses symlinks and files that are not the user's."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        raise ValueError("can't open rules file: %s" % e.strerror)
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if st.st_uid != uid or st.st_size > 1 << 20:
            raise ValueError("rules file is not owned by the user, or bigger than 1 MB")
        return clean_rules(json.load(f))


EMPTY = clean_rules({})


# ---- processes ----
def own_cgroup(pid="self"):
    for line in read("/proc/%s/cgroup" % pid).splitlines():
        if line.startswith("0::"):
            return line[3:].lstrip("/")
    return None


def proc_info(pid):
    """{uid, ppid, cg, names, cmd} or None (gone, or a kernel thread)."""
    try:
        status = read("/proc/%d/status" % pid)
        uid = int(re.search(r"^Uid:\s+(\d+)", status, re.M).group(1))
        ppid = int(re.search(r"^PPid:\s+(\d+)", status, re.M).group(1))
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except (OSError, AttributeError, ValueError):
        return None
    if not argv:
        return None  # kernel thread or zombie
    names = {read("/proc/%d/comm" % pid).strip().lower(), os.path.basename(argv[0]).lower()}
    try:
        names.add(os.path.basename(os.readlink("/proc/%d/exe" % pid)).replace(" (deleted)", "").lower())
    except OSError:
        pass
    return {"uid": uid, "ppid": ppid, "cg": own_cgroup(pid), "names": names, "cmd": " ".join(argv).lower()}


def matches(app, info):
    if app["mode"] == "cmdline":
        return any(n in info["cmd"] for n in app["names"])
    if app["mode"] == "flatpak":  # every Flatpak app runs in its own scope: app-flatpak-<app id>-<number>.scope
        cg = (info["cg"] or "").lower()
        return any("/app-flatpak-%s-" % n in cg for n in app["names"])
    return bool(info["names"].intersection(app["names"]))


def proc_listener():
    """Netlink socket that reports every new program start at once (proc connector), or None."""
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 11)  # NETLINK_CONNECTOR
        s.bind((os.getpid(), 1))  # CN_IDX_PROC
        op = struct.pack("=I", 1)  # PROC_CN_MCAST_LISTEN
        cn = struct.pack("=IIIIHH", 1, 1, 0, 0, len(op), 0) + op
        s.send(struct.pack("=IHHII", 16 + len(cn), 3, 0, 0, os.getpid()) + cn)  # NLMSG_DONE
        s.setblocking(False)
        return s
    except OSError as e:
        log("No instant start events (%s); using the regular scan only." % e)
        return None


def exec_events(sock):
    pids = []
    while True:
        try:
            data = sock.recv(65536)
        # Nothing left to read (BlockingIOError), or ENOBUFS: too many events at once.
        # The regular scan catches the rest.
        except OSError:
            return pids
        off = 0
        while off + 16 <= len(data):
            length = struct.unpack_from("=I", data, off)[0]
            what = struct.unpack_from("=I", data, off + 36)[0] if off + 40 <= len(data) else 0
            if what == 2 and off + 60 <= len(data):  # PROC_EVENT_EXEC: pid, tgid after the event header
                pids.append(struct.unpack_from("=I", data, off + 56)[0])
            off += max(length, 16)


# ---- network device ----
def default_iface():
    best = None
    for line in read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 7 and f[1] == "00000000" and f[7] == "00000000":
            if best is None or int(f[6]) < best[1]:
                best = (f[0], int(f[6]))
    if best:
        return best[0]
    for line in read("/proc/net/ipv6_route").splitlines():
        f = line.split()
        if len(f) > 9 and f[0] == "0" * 32 and f[1] == "00" and f[9] != "lo":
            return f[9]
    return None


def dev_exists(name):
    try:
        socket.if_nametoindex(name)  # asks the kernel, so it also works inside a network namespace
        return True
    except OSError:
        return False


def htb_tree(dev, total, apps, leaf_default, leaf_app, side):
    """tc batch lines for one direction. apps: [(id, kbit)]. Unmarked internet traffic goes to 1:2, first in
    line. LAN traffic (side = "dst" for upload, "src" for download) goes to 1:3 and is never limited."""
    top = total or NO_LIMIT
    used = sum(min(k, top) for _, k in apps)
    lines = ["qdisc replace dev %s root handle 1: htb default 2 r2q 10" % dev,
             "class add dev %s parent 1: classid 1:1 htb rate %dkbit ceil %dkbit quantum 1514" % (dev, top, top),
             "class add dev %s parent 1:1 classid 1:2 htb rate %dkbit ceil %dkbit prio 0 quantum 1514"
             % (dev, max(top - used, 8), top),
             "qdisc add dev %s parent 1:2 handle 2: %s" % (dev, leaf_default)]
    if CFG["lan_unlimited"]:
        lines += ["class add dev %s parent 1: classid 1:3 htb rate %dkbit ceil %dkbit quantum 1514"
                  % (dev, NO_LIMIT, NO_LIMIT), "qdisc add dev %s parent 1:3 handle 3: %s" % (dev, leaf_default)]
        for proto, prio, nets in (("ip", 1, CFG["lan_ipv4"]), ("ipv6", 2, CFG["lan_ipv6"])):
            lines += ["filter add dev %s parent 1: protocol %s prio %d u32 match %s %s %s classid 1:3"
                      % (dev, proto, prio, "ip" if proto == "ip" else "ip6", side, net) for net in nets]
    for app_id, k in apps:
        c = 0x100 + app_id
        lines += ["class add dev %s parent 1:1 classid 1:%x htb rate %dkbit ceil %dkbit prio 1 quantum 1514"
                  % (dev, c, min(k, top), min(k, top)),
                  "qdisc add dev %s parent 1:%x handle %x: %s" % (dev, c, c, leaf_app),
                  "filter add dev %s parent 1: protocol all prio 3 handle 0x%x/0xffff fw classid 1:%x"
                  % (dev, BASE_MARK + app_id, c)]
    return lines


def teardown(dev):
    if dev and SAFE_DEV.match(dev):
        run("tc", "qdisc", "del", "dev", dev, "root")
        run("tc", "qdisc", "del", "dev", dev, "ingress")
    run("ip", "link", "del", IFB)
    run("nft", "delete", "table", "inet", TABLE)


class Service:
    def __init__(self, uid, rules_path):
        self.uid, self.path = uid, rules_path
        self.rules, self.rules_key, self.error = EMPTY, None, None
        self.applied, self.failed, self.counts, self.stopping = None, None, {}, False
        base = own_cgroup()
        if not base or not SAFE_CG.match(base):
            raise SystemExit("Unusable cgroup for the service: %r" % base)
        if base.endswith("/daemon"):
            base = base[:-len("/daemon")]  # restarted inside our own tree
        self.base = base
        os.makedirs(os.path.join(CG, base, "daemon"), exist_ok=True)
        with open(os.path.join(CG, base, "daemon", "cgroup.procs"), "w") as f:
            f.write(str(os.getpid()))  # cgroup v2: processes only in leaves, so the service lives in daemon/
        os.makedirs(RUN, exist_ok=True)
        os.chmod(RUN, 0o755)
        try:
            self.moved = {int(k): v for k, v in json.loads(read(MOVED) or "{}").items()}
        except (ValueError, AttributeError):
            self.moved = {}
        teardown(default_iface())  # leftovers of a crash; the first loop applies the rules again
        self.fallback = "user.slice/user-%d.slice/user@%d.service/app.slice" % (uid, uid)

    def group(self, app_id):
        return "%s/app-%d" % (self.base, app_id)

    # ---- what should be live ----
    def plan(self):
        r = self.rules
        iface = default_iface() if r["iface"] == "auto" else r["iface"]
        if not iface or not dev_exists(iface):
            return None
        apps = tuple((a["id"], a["up"], a["down"]) for a in r["apps"] if a["on"] and a["names"] and (a["up"] or a["down"]))
        up = r["smooth"]["up"] if r["smooth"]["on"] else 0
        down = r["smooth"]["down"] if r["smooth"]["on"] else 0
        if not (up or down or apps):
            return None
        return {"iface": iface, "up": up, "down": down, "apps": apps}

    def limited(self):
        ids = {a[0] for a in (self.applied or {}).get("apps", ())}
        return [a for a in self.rules["apps"] if a["id"] in ids]

    def live_ok(self, want):
        dev = want["iface"]
        need_up = want["up"] or any(a[1] for a in want["apps"])
        need_down = want["down"] or any(a[2] for a in want["apps"])
        if need_up and "htb 1:" not in run("tc", "qdisc", "show", "dev", dev, "root")[1]:
            return False
        if need_down and ("ingress ffff:" not in run("tc", "qdisc", "show", "dev", dev, "ingress")[1]
                          or "htb 1:" not in run("tc", "qdisc", "show", "dev", IFB, "root")[1]):
            return False
        return not want["apps"] or run("nft", "list", "table", "inet", TABLE)[0]

    def apply(self, want):
        old = (self.applied or {}).get("iface")
        teardown(old)
        if want and want["iface"] != old:
            teardown(want["iface"])
        self.applied = None
        if not want:
            return True
        dev = want["iface"]
        ups = [(i, u) for i, u, _ in want["apps"] if u]
        downs = [(i, d) for i, _, d in want["apps"] if d]
        try:
            for app_id, _, _ in want["apps"]:
                os.makedirs(os.path.join(CG, self.group(app_id)), exist_ok=True)
            if want["apps"]:
                # Tiny packets (mostly TCP "got it" replies) stay unmarked: an app's upload limit then counts
                # real uploads only, and a low upload limit can't choke the app's own downloads.
                big = "meta length > %d " % CFG["small_packet_bytes"] if CFG["small_packet_bytes"] else ""
                mark = ["  ct mark and 0x%x == 0x%x %smeta mark set ct mark" % (0xffff & ~0xff, BASE_MARK, big)]
                for app_id, _, _ in want["apps"]:
                    path = self.group(app_id)
                    match = 'socket cgroupv2 level %d "%s"' % (path.count("/") + 1, path)
                    mark += ["  %s ct mark set 0x%x" % (match, BASE_MARK + app_id),
                             "  %s %smeta mark set 0x%x" % (match, big, BASE_MARK + app_id)]
                script = ("table inet %s\ndelete table inet %s\ntable inet %s {\n chain out {\n"
                          "  type filter hook output priority mangle; policy accept;\n%s\n }\n}\n"
                          % (TABLE, TABLE, TABLE, "\n".join(mark)))
                ok, out = run("nft", "-f", "-", data=script)
                if not ok:
                    raise RuntimeError("nftables: " + out)
            leaves = [CFG[k] for k in ("default_leaf", "app_leaf")]
            if not all(SAFE_LEAF.match(l) for l in leaves):
                raise RuntimeError("settings.json: bad default_leaf or app_leaf")
            batch = []
            if want["up"] or ups:
                batch += htb_tree(dev, want["up"], ups, *leaves, side="dst")
            if want["down"] or downs:
                if not dev_exists(IFB):
                    ok, out = run("ip", "link", "add", IFB, "type", "ifb")
                    if not ok:
                        raise RuntimeError("ifb device: " + out)
                ok, out = run("ip", "link", "set", IFB, "up")
                if not ok:
                    raise RuntimeError("ifb device: " + out)
                batch += ["qdisc add dev %s handle ffff: ingress" % dev,
                          "filter add dev %s parent ffff: protocol all prio 1 matchall "
                          "action connmark action mirred egress redirect dev %s" % (dev, IFB)]
                batch += htb_tree(IFB, want["down"], downs, *leaves, side="src")
            ok, out = run("tc", "-batch", "-", data="\n".join(batch) + "\n")
            if not ok:
                raise RuntimeError("tc: " + out)
        except (RuntimeError, OSError) as e:
            teardown(dev)
            self.error, self.failed = str(e), (want, time.monotonic())
            log("Apply failed, all limits removed: %s" % e)
            return False
        self.applied, self.failed, self.error = want, None, None
        log("Applied on %s: smoothing up %s / down %s kbit, %d app limit(s)"
            % (dev, want["up"] or "off", want["down"] or "off", len(want["apps"])))
        return True

    def reload(self, check):
        try:
            st = os.stat(self.path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key != self.rules_key:
            self.rules_key = key
            try:
                self.rules = load_rules(self.path, self.uid) or EMPTY
                self.error = None
            except (ValueError, OSError) as e:
                self.error = "Rules not read, keeping the old ones: %s" % e
                log(self.error)
        want = self.plan()
        if self.failed and self.failed[0] == want and time.monotonic() - self.failed[1] < CFG["retry_seconds"]:
            return  # same plan failed a moment ago: don't hammer the network setup
        if want != self.applied or (check and want and not self.live_ok(want)):
            if want == self.applied:
                log("Network setup was reset by something else (cable, Wi-Fi, other tool); applying again.")
            self.apply(want)

    # ---- moving processes ----
    def move(self, pid, cg):
        try:
            with open(os.path.join(CG, cg, "cgroup.procs"), "w") as f:
                f.write(str(pid))
            return True
        except OSError:
            return False  # process ended, or the cgroup is gone

    def target(self, pid, info, procs, apps, memo):
        """App id this process belongs to: its own match, else its parent's, else the app it is already in."""
        if pid in memo:
            return memo[pid]
        memo[pid] = None  # guard against loops
        t = next((a["id"] for a in apps if matches(a, info)), None)
        if t is None and info["ppid"] in procs:
            t = self.target(info["ppid"], procs[info["ppid"]], procs, apps, memo)
        if t is None:
            cur = self.current_app(info["cg"])
            t = cur if cur in {a["id"] for a in apps} else None
        memo[pid] = t
        return t

    def current_app(self, cg):
        prefix = self.base + "/app-"
        if cg and cg.startswith(prefix) and cg[len(prefix):].isdigit():
            return int(cg[len(prefix):])
        return None

    def home(self, pid, procs):
        """Where a process goes back to: where it (or its nearest known parent) came from."""
        p = pid
        for _ in range(64):
            cg = self.moved.get(p)
            if cg and os.path.isdir(os.path.join(CG, cg)):
                return cg
            p = procs[p]["ppid"] if p in procs else 0
            if not p:
                break
        return self.fallback if os.path.isdir(os.path.join(CG, self.fallback)) else "user.slice"

    def place(self, pid, info, t, procs):
        cur = self.current_app(info["cg"])
        if t == cur:
            return
        if t is not None:
            if cur is None:
                self.moved[pid] = info["cg"]
            self.move(pid, self.group(t))
        elif self.move(pid, self.home(pid, procs)):
            self.moved.pop(pid, None)

    def scan(self):
        apps = self.limited()
        if not apps and not self.moved and not self.groups():
            self.counts = {}
            return  # nothing limited and nothing to move back
        procs = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                info = proc_info(int(name))
                if info and info["uid"] == self.uid:
                    procs[int(name)] = info
        memo, counts = {}, {}
        for pid, info in procs.items():
            t = self.target(pid, info, procs, apps, memo)
            self.place(pid, info, t, procs)
            if t is not None:
                counts[t] = counts.get(t, 0) + 1
        self.moved = {p: c for p, c in self.moved.items() if p in procs}
        self.counts = counts
        write_json(MOVED, self.moved)
        self.clean_groups()

    def groups(self):
        return [n for n in os.listdir(os.path.join(CG, self.base)) if n.startswith("app-")]

    def clean_groups(self):
        """Remove empty app cgroups that no rule uses any more."""
        keep = {"app-%d" % a[0] for a in (self.applied or {}).get("apps", ())}
        for name in self.groups():
            if name not in keep:
                try:
                    os.rmdir(os.path.join(CG, self.base, name))
                except OSError:
                    pass  # still has processes; the next scan moves them back

    def on_exec(self, pid):
        info = proc_info(pid)
        if not info or info["uid"] != self.uid:
            return
        t = next((a["id"] for a in self.limited() if matches(a, info)), None)
        if t is not None:
            self.place(pid, info, t, {})

    def write_status(self):
        a = self.applied or {}
        write_json(STATUS, {"time": time.time(), "pid": os.getpid(), "iface": a.get("iface"),
                            "ifb": IFB, "error": self.error, "smooth_up": a.get("up", 0),
                            "smooth_down": a.get("down", 0), "processes": self.counts,
                            "limited": [x[0] for x in a.get("apps", ())]})

    def loop(self):
        sock = proc_listener()
        next_scan = next_check = 0
        while not self.stopping:
            now = time.monotonic()
            check = now >= next_check
            self.reload(check)
            if check:
                next_check = now + CFG["check_seconds"]
            if now >= next_scan:
                self.scan()
                self.write_status()
                next_scan = now + CFG["scan_seconds"]
            end = time.monotonic() + CFG["loop_seconds"]
            while sock and not self.stopping and time.monotonic() < end:
                if select.select([sock], [], [], max(0, end - time.monotonic()))[0]:
                    for pid in exec_events(sock):
                        self.on_exec(pid)
            if not sock:
                time.sleep(CFG["loop_seconds"])

    def shutdown(self):
        """Put every app back where it came from and undo all network changes."""
        iface = (self.applied or {}).get("iface")
        procs = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                info = proc_info(int(name))
                if info:
                    procs[int(name)] = info
        for pid, info in procs.items():
            if self.current_app(info["cg"]) is not None:
                self.move(pid, self.home(pid, procs))
        teardown(iface)
        self.applied = None
        self.clean_groups()
        for path in (STATUS, MOVED):
            try:
                os.remove(path)
            except OSError:
                pass
        log("Stopped: apps moved back, all limits removed.")


# ---- install / uninstall ----
def need_root():
    if os.geteuid() != 0:
        sys.exit("Run this as root (the SpeedBox window does it with pkexec).")


def caller_uid():
    for var in ("PKEXEC_UID", "SUDO_UID"):
        if os.environ.get(var, "").isdigit():
            return int(os.environ[var])
    sys.exit("Can't tell which user to serve: start it with pkexec or sudo.")


def systemctl(*args):
    ok, out = run("systemctl", *args)
    if not ok and out:
        log(out)
    return ok


def install(rules_path):
    need_root()
    uid = caller_uid()
    rules_path = os.path.abspath(rules_path)
    if not SAFE_UNIT_PATH.match(rules_path) or not SAFE_UNIT_PATH.match(sys.executable):
        sys.exit("The rules path or python path has characters a systemd unit can't hold safely.")
    os.makedirs(LIB, mode=0o755, exist_ok=True)
    for name in ("speedboxd.py", "settings.json"):
        if os.path.abspath(os.path.join(HERE, name)) != os.path.join(LIB, name):
            shutil.copyfile(os.path.join(HERE, name), os.path.join(LIB, name))
        os.chown(os.path.join(LIB, name), 0, 0)
        os.chmod(os.path.join(LIB, name), 0o644)
    if os.path.exists(OLD_UNIT):
        systemctl("disable", "--now", "cake-qos.service")
        os.remove(OLD_UNIT)
        log("Removed the old cake-qos service (SpeedBox smoothing replaces it).")
    q = lambda s: '"%s"' % s if " " in s else s
    with open(UNIT, "w") as f:
        f.write("[Unit]\nDescription=SpeedBox per-app internet speed limits\n"
                "After=network-online.target\nWants=network-online.target\n\n"
                "[Service]\nType=simple\nExecStart=%s -I %s/speedboxd.py run --uid %d --rules %s\n"
                "Restart=always\nRestartSec=2\n"
                "# The service hands its own cgroup tree to limited apps; on stop only the service itself\n"
                "# is stopped (it moves the apps back first), never the apps.\n"
                "Delegate=yes\nKillMode=process\n\n[Install]\nWantedBy=multi-user.target\n"
                % (q(sys.executable), LIB, uid, q(rules_path)))
    os.chmod(UNIT, 0o644)
    systemctl("daemon-reload")
    if systemctl("enable", "speedbox.service") and systemctl("restart", "speedbox.service"):
        log("SpeedBox service installed and running.")
    else:
        sys.exit("The service did not start. See: journalctl -u speedbox")


def uninstall():
    need_root()
    systemctl("disable", "--now", "speedbox.service")
    if os.path.exists(UNIT):
        os.remove(UNIT)
    shutil.rmtree(LIB, ignore_errors=True)
    systemctl("daemon-reload")
    cleanup()
    log("SpeedBox service removed.")


def cleanup():
    """Emergency undo, also when the service is not running."""
    need_root()
    status = json.loads(read(STATUS) or "{}")
    teardown(status.get("iface") or default_iface())
    shutil.rmtree(RUN, ignore_errors=True)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    opts = dict(zip(argv[2::2], argv[3::2]))
    if cmd == "run" and "--uid" in opts and "--rules" in opts:
        need_root()
        svc = Service(int(opts["--uid"]), opts["--rules"])

        def stop(*_):
            svc.stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log("SpeedBox service started (pid %d, cgroup %s)." % (os.getpid(), svc.base))
        try:
            svc.loop()
        finally:
            svc.shutdown()
    elif cmd == "install" and "--rules" in opts:
        install(opts["--rules"])
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "cleanup":
        cleanup()
        log("All SpeedBox network changes removed.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
