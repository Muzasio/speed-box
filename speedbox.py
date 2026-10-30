#!/usr/bin/env python3
"""SpeedBox: upload and download speed limits per app, and smoothing for the whole PC.

This window only edits the rules file (~/.config/speedbox/rules.json). The SpeedBox service
(speedboxd.py, run as root by systemd) applies it within a second or two, also with this window closed.
The password is asked only to install, update or remove the service.
  speedbox.py                  open the window
  speedbox.py --install-menu   add SpeedBox to the app menu
Needs: python3 (3.6+) with tkinter, pkexec (polkit), iproute2.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
from subprocess import PIPE, STDOUT
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "settings.json")) as _f:
    CFG = json.load(_f)


def xdg(var, fallback):
    path = os.environ.get(var, "")
    return path if os.path.isabs(path) else os.path.expanduser(fallback)  # the XDG spec says ignore relative paths


CONF = os.path.join(xdg("XDG_CONFIG_HOME", "~/.config"), "speedbox")
RULES = os.path.join(CONF, "rules.json")
DATA = xdg("XDG_DATA_HOME", "~/.local/share")
MENU = os.path.join(DATA, "applications", "speedbox.desktop")
ICON = os.path.join(HERE, "icons", "speedbox.svg")
ICON_PNG = os.path.join(HERE, "icons", "speedbox-%d.png")
STATUS = "/run/speedbox/status.json"
UNIT = "/etc/systemd/system/speedbox.service"
SAFE_EXEC = re.compile(r"^[\w@.+/ -]+$")  # anything else would need desktop-file escaping, so it is refused
# kbit (1000 bits) per one of each unit
UNITS = {"Mbit/s": 1000.0, "KB/s": 8 * 1024 / 1000.0, "MB/s": 8 * 1024 * 1024 / 1000.0}
MATCH = {"name": "Program name", "cmdline": "Command line contains", "flatpak": "Flatpak app ID"}
DESKTOP_DIRS = ["/usr/share/applications", "/usr/local/share/applications", os.path.join(DATA, "applications"),
                "/var/lib/flatpak/exports/share/applications", os.path.join(DATA, "flatpak/exports/share/applications")]


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def run(*cmd):
    try:
        r = subprocess.run(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True)
        return r.returncode == 0, r.stdout.strip()
    except OSError as e:
        return False, str(e)


def load_rules():
    try:
        data = json.loads(read(RULES) or "{}")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault("iface", "auto")
    data.setdefault("unit", "Mbit/s")
    data.setdefault("smooth", {"on": False, "up_kbit": 0, "down_kbit": 0})
    data.setdefault("apps", [])
    return data


def save_rules(data):
    """Atomic write, so the service never reads half a file."""
    os.makedirs(CONF, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONF, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, RULES)
    except BaseException:
        os.unlink(tmp)
        raise


def show(kbit, unit, zero="no limit"):
    if not kbit:
        return zero
    v = kbit / UNITS[unit]
    return ("%.1f %s" if v < 100 else "%.0f %s") % (v, unit)


def tc_bytes(dev):
    """{class minor: bytes sent} for SpeedBox's HTB classes on one device. Reading needs no root."""
    if not dev:
        return {}
    ok, out = run("tc", "-s", "class", "show", "dev", dev)
    return {int(m, 16): int(b) for m, b in re.findall(r"class htb 1:([0-9a-f]+) [^\n]*\n\s*Sent (\d+) bytes", out)} if ok else {}


def devices():
    """Real network cards only (Docker, VM bridges and VPNs are left out; they can still be typed in)."""
    return sorted(d for d in os.listdir("/sys/class/net") if os.path.exists("/sys/class/net/%s/device" % d))


def running_programs():
    """{program name: example command line} for the user's own running processes."""
    found = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if os.stat("/proc/" + pid).st_uid != os.getuid():
                continue
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if argv:
            name = read("/proc/%s/comm" % pid).strip() or os.path.basename(argv[0])
            if len(name) == 15 and os.path.basename(argv[0]).startswith(name):
                name = os.path.basename(argv[0])  # the kernel cuts names at 15 letters
            found.setdefault(name, " ".join(argv)[:120])
    return found


def installed_apps():
    """[(menu name, match mode, program)] from the app menu (.desktop files)."""
    apps = {}
    for d in DESKTOP_DIRS:
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not f.endswith(".desktop"):
                continue
            text = read(os.path.join(d, f)).split("\n[", 1)[0]  # main section only, not actions
            name = re.search(r"^Name=(.+)$", text, re.M)
            exe = re.search(r"^Exec=(.+)$", text, re.M)
            if not name or not exe or re.search(r"^NoDisplay=true", text, re.M):
                continue
            try:
                args = [a for a in shlex.split(exe.group(1)) if not a.startswith("%")]
            except ValueError:
                continue
            while args and (os.path.basename(args[0]) == "env" or "=" in args[0]):
                args = args[1:]
            if not args:
                continue
            if os.path.basename(args[0]) == "flatpak" and "run" in args:
                flat = re.search(r"^X-Flatpak=(.+)$", text, re.M)
                ids = [a for a in args[args.index("run") + 1:] if not a.startswith("-")]
                app_id = flat.group(1).strip() if flat else (ids[0] if ids else "")
                if app_id:
                    apps[name.group(1)] = ("flatpak", app_id)
            else:
                apps[name.group(1)] = ("name", os.path.basename(args[0]))
    return sorted((n, m, p) for n, (m, p) in apps.items())


def menu_entry():
    py, script = sys.executable or "python3", os.path.abspath(__file__)
    if not all(SAFE_EXEC.match(p) for p in (py, script, ICON)):
        raise OSError("The SpeedBox path has characters a menu entry can't hold safely: " + script)
    quote = lambda s: '"%s"' % s if " " in s else s
    return ("[Desktop Entry]\nType=Application\nName=SpeedBox\n"
            "Comment=Upload and download speed limits per app\n"
            "Exec=%s %s\nIcon=%s\nTerminal=false\nCategories=Network;System;\n"
            % (quote(py), quote(script), ICON if os.path.exists(ICON) else "network-transmit-receive"))


def install_menu():
    os.makedirs(os.path.dirname(MENU), exist_ok=True)
    with open(MENU, "w") as f:
        f.write(menu_entry())
    return MENU


class AppDialog(tk.Toplevel):
    """Add or edit one app rule. self.result is the new rule, or None if cancelled."""

    def __init__(self, master, rule, unit):
        tk.Toplevel.__init__(self, master)
        self.title("App limit")
        self.transient(master)
        self.result, self.rule = None, rule
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        self.name = tk.StringVar(value=rule.get("name", ""))
        self.mode = tk.StringVar(value=MATCH[rule.get("match", "name")])
        self.progs = tk.StringVar(value=", ".join(rule.get("programs", [])))
        ttk.Label(f, text="Name (only shown here)").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.name, width=40).grid(row=0, column=1, columnspan=3, sticky="we", pady=2)
        ttk.Label(f, text="Find the app by").grid(row=1, column=0, sticky="w")
        ttk.Combobox(f, textvariable=self.mode, values=list(MATCH.values()), state="readonly", width=24).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=2)
        ttk.Label(f, text="Programs (comma separated)").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.progs, width=40).grid(row=2, column=1, columnspan=3, sticky="we", pady=2)
        row = ttk.Frame(f)
        row.grid(row=3, column=1, columnspan=3, sticky="w")
        ttk.Button(row, text="Pick a running program...", command=self.pick_running).pack(side="left")
        ttk.Button(row, text="Pick from app menu...", command=self.pick_installed).pack(side="left", padx=4)
        self.limits = {}
        for r, (key, label) in enumerate((("up_kbit", "Upload limit"), ("down_kbit", "Download limit")), 4):
            k = rule.get(key, 0)
            val = tk.StringVar(value=("%g" % round(k / UNITS[unit], 2)) if k else "0")
            u = tk.StringVar(value=unit)
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w")
            ttk.Spinbox(f, textvariable=val, from_=0, to=100000, increment=0.5, width=10).grid(
                row=r, column=1, sticky="w", pady=2)
            ttk.Combobox(f, textvariable=u, values=list(UNITS), state="readonly", width=8).grid(row=r, column=2, sticky="w")
            self.limits[key] = (val, u)
        ttk.Label(f, text="0 = no limit. Programs this app starts are limited too.\n"
                          "Program name: the name in the process list, like stremio or steam.\n"
                          "Torrent apps (Stremio): keep upload at 1 Mbit/s or more, or other\n"
                          "people stop sending to you and the download slows down.",
                  foreground="gray").grid(row=6, column=0, columnspan=4, sticky="w", pady=6)
        b = ttk.Frame(f)
        b.grid(row=7, column=0, columnspan=4, sticky="e")
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Save", command=self.ok).pack(side="right", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def mode_key(self):
        return next(k for k, v in MATCH.items() if v == self.mode.get())

    def add_programs(self, names):
        have = [p.strip() for p in self.progs.get().split(",") if p.strip()]
        self.progs.set(", ".join(have + [n for n in names if n not in have]))

    def pick_running(self):
        progs = running_programs()
        rows = sorted(progs.items(), key=lambda kv: kv[0].lower())
        picked = pick_list(self, "Running programs (your own)", ("Program", "Command line"), rows, many=True)
        if picked:
            if self.mode_key() != "name" and self.progs.get().strip():
                self.progs.set("")
            self.mode.set(MATCH["name"])
            self.add_programs([rows[i][0] for i in picked])

    def pick_installed(self):
        apps = installed_apps()
        picked = pick_list(self, "App menu", ("App", "Found by"), [(n, "%s: %s" % (MATCH[m], p)) for n, m, p in apps])
        if picked:
            n, m, p = apps[picked[0]]
            if m != self.mode_key():
                self.progs.set("")
            self.mode.set(MATCH[m])
            self.add_programs([p])
            if not self.name.get().strip():
                self.name.set(n)

    def ok(self):
        progs = [p.strip() for p in self.progs.get().split(",") if p.strip()]
        out = {}
        for key, (val, u) in self.limits.items():
            try:
                v = float(val.get().replace(",", "."))
                if v < 0:
                    raise ValueError
            except ValueError:
                return messagebox.showerror("SpeedBox", "Speed limits must be numbers (0 = no limit).", parent=self)
            out[key] = int(round(v * UNITS[u.get()]))
            if 0 < out[key] < 8:
                return messagebox.showerror("SpeedBox", "The smallest limit is 8 kbit/s (1 KB/s).", parent=self)
        if not progs:
            return messagebox.showerror("SpeedBox", "Add at least one program.", parent=self)
        if not self.name.get().strip():
            self.name.set(progs[0])
        self.result = dict(self.rule, name=self.name.get().strip(), match=self.mode_key(), programs=progs, **out)
        self.destroy()


def pick_list(master, title, cols, rows, many=False):
    """Searchable list; returns the picked row indexes (empty if cancelled)."""
    win = tk.Toplevel(master)
    win.title(title)
    win.transient(master)
    picked = []
    f = ttk.Frame(win, padding=8)
    f.pack(fill="both", expand=True)
    search = tk.StringVar()
    ttk.Label(f, text="Search").pack(anchor="w")
    entry = ttk.Entry(f, textvariable=search)
    entry.pack(fill="x")
    tree = ttk.Treeview(f, columns=cols, show="headings", height=16, selectmode="extended" if many else "browse")
    for c, w in zip(cols, (180, 420)):
        tree.heading(c, text=c)
        tree.column(c, width=w)
    tree.pack(fill="both", expand=True, pady=4)

    def fill(*_):
        tree.delete(*tree.get_children())
        q = search.get().lower()
        for i, r in enumerate(rows):
            if q in (r[0] + " " + r[1]).lower():
                tree.insert("", "end", iid=str(i), values=r)

    def done(*_):
        picked.extend(int(i) for i in tree.selection())
        win.destroy()
    search.trace_add("write", fill)
    fill()
    tree.bind("<Double-1>", done)
    b = ttk.Frame(f)
    b.pack(fill="x")
    ttk.Label(b, text="Ctrl+click to pick more than one." if many else "", foreground="gray").pack(side="left")
    ttk.Button(b, text="Cancel", command=win.destroy).pack(side="right")
    ttk.Button(b, text="Add", command=done).pack(side="right", padx=4)
    entry.focus_set()
    win.grab_set()
    master.wait_window(win)
    return picked


class SpeedBox(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("SpeedBox")
        self.minsize(820, 480)
        try:
            self.icons = [tk.PhotoImage(file=ICON_PNG % n) for n in (64, 32, 16)]
            self.iconphoto(True, *self.icons)
        except tk.TclError:
            pass  # no icon files: the window still works
        self.rules = load_rules()
        self.last, self.busy = {}, False

        top = ttk.LabelFrame(self, text="Service (applies the limits, also with this window closed)", padding=6)
        top.pack(fill="x", padx=6, pady=4)
        self.svc = ttk.Label(top, text="...")
        self.svc.pack(side="left")
        ttk.Button(top, text="Add to app menu", command=self.menu).pack(side="right")
        ttk.Button(top, text="Remove service", command=lambda: self.root_cmd("uninstall")).pack(side="right", padx=4)
        ttk.Button(top, text="Install / Update service", command=lambda: self.root_cmd("install")).pack(side="right")

        sm = ttk.LabelFrame(self, text="Whole PC smoothing (keeps games and calls fast while something uses "
                                       "the full internet)", padding=6)
        sm.pack(fill="x", padx=6, pady=4)
        s = self.rules["smooth"]
        u = self.rules["unit"]
        self.s_on = tk.BooleanVar(value=bool(s.get("on")))
        self.s_up = tk.StringVar(value="%g" % round(s.get("up_kbit", 0) / UNITS[u], 2))
        self.s_down = tk.StringVar(value="%g" % round(s.get("down_kbit", 0) / UNITS[u], 2))
        self.iface = tk.StringVar(value=self.rules["iface"])
        ttk.Checkbutton(sm, text="On", variable=self.s_on).grid(row=0, column=0, sticky="w")
        ttk.Label(sm, text="Upload total").grid(row=0, column=1, padx=(12, 2))
        ttk.Spinbox(sm, textvariable=self.s_up, from_=0, to=100000, increment=0.5, width=8).grid(row=0, column=2)
        ttk.Label(sm, text="Download total").grid(row=0, column=3, padx=(12, 2))
        ttk.Spinbox(sm, textvariable=self.s_down, from_=0, to=100000, increment=0.5, width=8).grid(row=0, column=4)
        self.s_unit = ttk.Label(sm, text=u)
        self.s_unit.grid(row=0, column=5, padx=2)
        ttk.Label(sm, text="Network").grid(row=0, column=6, padx=(12, 2))
        ttk.Combobox(sm, textvariable=self.iface, values=["auto"] + devices(), width=12).grid(row=0, column=7)
        ttk.Button(sm, text="Apply", command=self.apply_smooth).grid(row=0, column=8, padx=(12, 2))
        ttk.Button(sm, text="Test my speed", command=self.speedtest).grid(row=0, column=9, padx=2)
        self.other = ttk.Label(sm, text="", foreground="gray")
        self.other.grid(row=1, column=0, columnspan=10, sticky="w", pady=(4, 0))

        box = ttk.LabelFrame(self, text="App limits", padding=6)
        box.pack(fill="both", expand=True, padx=6, pady=4)
        cols = ("On", "App", "Found by", "Upload limit", "Download limit", "Upload now", "Download now", "Processes")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (40, 120, 200, 95, 105, 90, 100, 70)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        for text, cmd in (("Add app", self.add), ("Edit", self.edit), ("On / Off", self.toggle), ("Delete", self.delete)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)
        self.unit = tk.StringVar(value=u)
        self.unit.trace_add("write", lambda *_: self.set_unit())
        ttk.Combobox(row, textvariable=self.unit, values=list(UNITS), state="readonly", width=8).pack(side="right", padx=4)
        ttk.Label(row, text="Show speeds in").pack(side="right")

        self.msg = ttk.Label(self, padding=6, wraplength=800, text=(
            "An app that is already open gets limited at once for new connections; its old connections keep "
            "their speed until they close. Restart the app for a full limit."))
        self.msg.pack(fill="x")
        self.fill()
        self.tick()

    # ---- rules ----
    def save(self):
        try:
            save_rules(self.rules)
        except OSError as e:
            messagebox.showerror("SpeedBox", "Could not save the rules:\n%s" % e)

    def set_unit(self):
        old, new = self.rules["unit"], self.unit.get()
        for var in (self.s_up, self.s_down):
            try:
                var.set("%g" % round(float(var.get()) * UNITS[old] / UNITS[new], 2))
            except ValueError:
                pass
        self.rules["unit"] = new
        self.s_unit.config(text=new)
        self.save()
        self.fill()

    def apply_smooth(self):
        u = self.rules["unit"]
        try:
            up, down = (int(round(float(v.get().replace(",", ".")) * UNITS[u])) for v in (self.s_up, self.s_down))
            if up < 0 or down < 0:
                raise ValueError
        except ValueError:
            return messagebox.showerror("SpeedBox", "Upload and download totals must be numbers.")
        iface = self.iface.get().strip() or "auto"
        if iface != "auto" and iface not in devices():
            return messagebox.showerror("SpeedBox", "No network device called %s." % iface)
        if self.s_on.get() and not (up or down):
            return messagebox.showerror("SpeedBox", "Set an upload or download total first (or press Test my speed).")
        self.rules["smooth"] = {"on": self.s_on.get(), "up_kbit": up, "down_kbit": down}
        self.rules["iface"] = iface
        self.save()
        self.msg.config(text="Saved. The service applies it within 2 seconds.")

    def selected(self):
        sel = self.tree.selection()
        return next((a for a in self.rules["apps"] if str(a["id"]) == (sel[0] if sel else None)), None)

    def add(self):
        used = {a["id"] for a in self.rules["apps"]}
        free = [i for i in range(1, CFG["max_apps"] + 1) if i not in used]
        if not free:
            return messagebox.showerror("SpeedBox", "The most is %d apps." % CFG["max_apps"])
        d = AppDialog(self, {"id": free[0], "on": True}, self.rules["unit"])
        self.wait_window(d)
        if d.result:
            self.rules["apps"].append(d.result)
            self.save()
            self.fill()

    def edit(self):
        a = self.selected()
        if not a:
            return messagebox.showinfo("SpeedBox", "Select an app first.")
        d = AppDialog(self, a, self.rules["unit"])
        self.wait_window(d)
        if d.result:
            a.update(d.result)
            self.save()
            self.fill()

    def toggle(self):
        a = self.selected()
        if not a:
            return messagebox.showinfo("SpeedBox", "Select an app first.")
        a["on"] = not a.get("on")
        self.save()
        self.fill()

    def delete(self):
        a = self.selected()
        if a and messagebox.askyesno("SpeedBox", "Delete the limit for %s?" % a.get("name")):
            self.rules["apps"].remove(a)
            self.save()
            self.fill()

    def fill(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        u = self.rules["unit"]
        for a in self.rules["apps"]:
            self.tree.insert("", "end", iid=str(a["id"]), values=(
                "Yes" if a.get("on") else "No", a.get("name", ""),
                "%s: %s" % (MATCH.get(a.get("match"), "?"), ", ".join(a.get("programs", []))),
                show(a.get("up_kbit", 0), u), show(a.get("down_kbit", 0), u), "", "", ""))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    # ---- live view ----
    def status(self):
        try:
            st = json.loads(read(STATUS) or "{}")
        except ValueError:
            st = {}
        return st if time.time() - st.get("time", 0) < 15 else {}

    def tick(self):
        st = self.status()
        installed = os.path.exists(UNIT)
        if st:
            self.svc.config(text="Service: running on %s" % (st.get("iface") or "(nothing to limit)"),
                            foreground="dark green")
        else:
            self.svc.config(text="Service: %s" % ("installed but not running" if installed else "not installed"),
                            foreground="red")
        if st.get("error"):
            self.msg.config(text="Service problem: " + st["error"])
        now, u = time.monotonic(), self.rules["unit"]
        cur = {"up": tc_bytes(st.get("iface")), "down": tc_bytes(st.get("ifb"))}
        rate = {}
        if self.last:
            dt = now - self.last["t"]
            for k in ("up", "down"):
                rate[k] = {c: (b - self.last[k].get(c, b)) * 8 / 1000.0 / dt for c, b in cur[k].items()}
        self.last = dict(cur, t=now)
        procs = st.get("processes", {})
        for a in self.rules["apps"]:
            iid = str(a["id"])
            if not self.tree.exists(iid):
                continue
            vals = list(self.tree.item(iid, "values"))
            live = a["id"] in st.get("limited", [])
            for i, k in ((5, "up"), (6, "down")):
                r = rate.get(k, {}).get(0x100 + a["id"])
                vals[i] = show(max(r, 0), u, "0 " + u) if live and r is not None else "-"
            vals[7] = procs.get(iid, 0) if live else "-"
            self.tree.item(iid, values=vals)
        if st and rate:
            o_up, o_down = rate.get("up", {}).get(2), rate.get("down", {}).get(2)
            self.other.config(text="All other apps now:  upload %s,  download %s" % (
                show(max(o_up, 0), u, "0 " + u) if o_up is not None else "not measured",
                show(max(o_down, 0), u, "0 " + u) if o_down is not None else "not measured"))
        self.after(1000, self.tick)

    # ---- service and menu ----
    def root_cmd(self, cmd):
        if self.busy:
            return
        if cmd == "uninstall" and not messagebox.askyesno(
                "SpeedBox", "Remove the service? All limits and the whole PC smoothing stop.\n"
                            "Your rules stay saved for a later install."):
            return
        if cmd == "install":
            self.save()
        self.busy = True
        self.msg.config(text="Waiting for your password...")
        args = ["pkexec", sys.executable, "-I", os.path.join(HERE, "speedboxd.py"), cmd]
        if cmd == "install":
            args += ["--rules", RULES]

        def work():
            ok, out = run(*args)
            self.after(0, lambda: self.root_done(ok, out))
        threading.Thread(target=work, daemon=True).start()

    def root_done(self, ok, out):
        self.busy = False
        self.msg.config(text=out or ("Done." if ok else "Cancelled."))
        if not ok:
            messagebox.showerror("SpeedBox", out or "Password cancelled.")

    def menu(self):
        try:
            messagebox.showinfo("SpeedBox", "Added to the app menu:\n" + install_menu())
        except OSError as e:
            messagebox.showerror("SpeedBox", str(e))

    def speedtest(self):
        if self.busy:
            return
        self.busy = True
        self.msg.config(text="Testing your speed (about 15 seconds, uses about 33 MB)...")

        def work():
            try:
                hdr = {"User-Agent": "SpeedBox"}
                t = time.time()
                with urllib.request.urlopen(urllib.request.Request(CFG["speedtest_down_url"], headers=hdr),
                                            timeout=60) as r:
                    n = len(r.read())
                down = n * 8 / 1000.0 / (time.time() - t)
                data = os.urandom(CFG["speedtest_up_bytes"])  # random, so nothing on the way can compress it
                t = time.time()
                with urllib.request.urlopen(urllib.request.Request(CFG["speedtest_up_url"], data=data,
                                                                   headers=hdr), timeout=60) as r:
                    r.read()
                up = len(data) * 8 / 1000.0 / (time.time() - t)
                self.after(0, lambda: self.speed_done(up, down, None))
            except Exception as e:  # network errors come in many types; all end the same way here
                self.after(0, lambda: self.speed_done(0, 0, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def speed_done(self, up, down, err):
        self.busy = False
        if err:
            self.msg.config(text="Speed test failed: " + err)
            return
        u, share = self.rules["unit"], CFG["speedtest_share"]
        note = ("\n\nSmoothing was ON, so this shows the smoothed speed, not your real line. "
                "Turn it off, Apply, and test again for the real number." if self.rules["smooth"].get("on") else "")
        self.msg.config(text="Measured: upload %s, download %s." % (show(up, u), show(down, u)))
        if messagebox.askyesno("SpeedBox", "Upload: %s\nDownload: %s%s\n\nUse %d%% of this for smoothing "
                                           "(upload %s, download %s)?" % (
                show(up, u), show(down, u), note, share * 100, show(up * share, u), show(down * share, u))):
            self.s_up.set("%g" % round(up * share / UNITS[u], 1))
            self.s_down.set("%g" % round(down * share / UNITS[u], 1))
            self.s_on.set(True)
            self.apply_smooth()


if __name__ == "__main__":
    if "--install-menu" in sys.argv:
        print("Menu entry written: " + install_menu())
    else:
        SpeedBox().mainloop()
