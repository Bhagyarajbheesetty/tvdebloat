#!/usr/bin/env python3
"""tvdebloat - debloat an Android TV / Google TV and install the CleanYourTV home app.

Two jobs, one command:

    python tvdebloat.py setup --ip 192.168.1.50 --apk app-release.apk

That connects over WiFi, disables the bloatware, installs the launcher, makes it the
home screen and verifies it.  Everything is reversible - the tool only ever uses
`pm disable-user`, never uninstalls, and writes an undo log.

The steps can also be run one at a time: connect, probe, debloat, install-launcher, undo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES = HERE / "profiles"
STATE = HERE / "state"

LAUNCHER_PKG = "com.gathin.tvhome"
LAUNCHER_ACTIVITY = f"{LAUNCHER_PKG}/.MainActivity"

# Google TV's setup wizard claims HOME with priority=1 and steals it back even after
# `cmd package set-home-activity` reported Success.  It must be disabled BEFORE the
# home app is set, and the result verified afterwards.
GOOGLE_SETUP_WIZARD = "com.google.android.tungsten.setupwraith"

# Launchers we know how to disable when replacing Home.
KNOWN_STOCK_LAUNCHERS = (
    "com.google.android.tvlauncher",         # AOSP TV launcher
    "com.google.android.apps.tv.launcherx",  # Google TV launcher
    "com.google.android.leanbacklauncher",
)

# Never disable these - they break the TV.
NEVER_DISABLE = {
    "com.android.systemui",
    "com.google.android.gms",       # YouTube, Play Store, casting
    "com.android.tv.settings",
    "com.mediatek.tv.service",      # vendor framework on MediaTek sets
    "com.android.providers.settings",
    "com.android.permissioncontroller",
    LAUNCHER_PKG,
}


class TVError(Exception):
    """Anything the user needs to read and act on."""


# --------------------------------------------------------------------------------------
# adb
# --------------------------------------------------------------------------------------


def adb_version(path: str) -> int:
    """The adb client number, e.g. 41 for platform-tools 37.x.  0 = unknown."""
    try:
        p = subprocess.run([path, "version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    blob = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"version (\d+)\.(\d+)\.(\d+)", blob)
    if m:
        return int(m.group(3))
    m = re.search(r"version \((\d+)\)", blob)
    return int(m.group(1)) if m else 0


def find_adb(explicit: str | None = None) -> str:
    """Locate the adb binary, preferring the newest one.

    An ancient adb on PATH is a real trap: adb 31 and adb 41 cannot share a server
    ("adb server version (31) doesn't match this client (41); killing..."), and old
    builds even mangle `adb connect IP:PORT`.  So collect every candidate and use the
    newest rather than the first one that exists.
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        if (p / "adb.exe").is_file():
            return str(p / "adb.exe")
        raise TVError(f"adb not found at {explicit}")

    candidates: list[Path] = []
    env = os.environ.get("ADB")
    if env:
        candidates.append(Path(env))
    which = shutil.which("adb")
    if which:
        candidates.append(Path(which))
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        candidates.append(Path(sdk) / "platform-tools" / "adb.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    candidates.append(HERE / "platform-tools" / "adb.exe")
    if HERE.parent.is_dir():
        for sibling in sorted(HERE.parent.glob("*/adb.exe")):
            candidates.append(sibling)
    candidates.append(Path(r"C:\adb\adb.exe"))

    best, best_version = None, -1
    for c in candidates:
        if not c.is_file():
            continue
        v = adb_version(str(c))
        if v > best_version:
            best, best_version = c, v
    if best is None:
        raise TVError(
            "adb not found. Install Android platform-tools and put adb on PATH, or pass --adb <path>."
        )
    if 0 < best_version < 34:
        warn(f"using an old adb (v{best_version}) at {best} - platform-tools 34+ is recommended")
    else:
        info(f"adb {best_version} at {best}")
    return str(best)


class Adb:
    """Thin wrapper around the adb binary."""

    def __init__(self, adb_path: str, timeout: int = 30):
        self.path = adb_path
        self.timeout = timeout
        self.serial: str | None = None

    @staticmethod
    def clean(text: str) -> str:
        """Drop the adb client's own banner noise (server restart, device errors)."""
        noise = (
            "adb server is out of date",
            "* daemon started successfully *",
            "* server ",
            "error: device not found",
            "error: device offline",
            "error: no devices/emulators found",
            "error: closed",
            "adb: ",
            "List of devices attached",
        )
        kept = [l for l in text.splitlines() if not any(l.strip().startswith(n) for n in noise)]
        return "\n".join(kept)

    def run(self, *args: str, timeout: int | None = None) -> tuple[int, str, str]:
        try:
            p = subprocess.run([self.path, *args], capture_output=True, text=True,
                               timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout or self.timeout}s"
        return p.returncode, p.stdout, p.stderr

    def shell(self, cmd: str, timeout: int | None = None) -> str:
        """Run a shell command on the device; returns stdout with stderr folded in."""
        _, out, err = self.run("-s", self.serial, "shell", cmd, timeout=timeout)
        return self.clean((out or "") + (err or ""))

    def shell_lines(self, cmd: str) -> list[str]:
        return [l.strip() for l in self.shell(cmd).splitlines() if l.strip()]

    def getprop(self, name: str, default: str = "") -> str:
        return self.shell(f"getprop {name}").strip() or default

    def push(self, local: str, remote: str) -> tuple[bool, str]:
        _, out, err = self.run("-s", self.serial, "push", local, remote, timeout=180)
        text = (out + err).strip()
        return ("1 file pushed" in text or "0.0 MB/s" in text), text

    def devices(self) -> list[tuple[str, str]]:
        _, out, _ = self.run("devices")
        result = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line and "\t" in line:
                ser, state = line.split("\t", 1)
                result.append((ser.strip(), state.strip()))
        return result

    def online_devices(self) -> list[str]:
        return [s for s, st in self.devices() if st == "device"]

    def kill_server(self) -> None:
        self.run("kill-server")

    def start_server(self) -> None:
        self.run("start-server")

    def connect(self, target: str) -> tuple[bool, str]:
        _, out, err = self.run("connect", target)
        text = self.clean((out + err).strip())
        ok = "connected to" in text and "cannot connect" not in text
        return ok, text

    def pair(self, target: str, code: str) -> tuple[bool, str]:
        _, out, err = self.run("pair", target, code)
        text = self.clean((out + err).strip())
        return "successfully paired" in text.lower(), text

    def wait_ready(self, timeout: int = 30) -> bool:
        """Wait until the device answers shell commands (right after connect it may not)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.serial in self.online_devices():
                _, out, _ = self.run("-s", self.serial, "shell", "echo ready", timeout=10)
                if "ready" in (out or ""):
                    return True
            time.sleep(1.0)
        return False


def scan_port(ip: str, port: int, timeout: float = 0.35) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def scan_ports(ip: str, ports: range, workers: int = 400) -> list[int]:
    """Return the open ports in `ports` (fast, parallel)."""
    found: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for port, ok in zip(ports, pool.map(lambda p: scan_port(ip, p), ports)):
            if ok:
                found.append(port)
    return found


# --------------------------------------------------------------------------------------
# The TV
# --------------------------------------------------------------------------------------


class Device:
    """Everything we can learn about the TV."""

    def __init__(self, adb: Adb):
        self.adb = adb
        self._packages: set[str] | None = None

    @property
    def model(self) -> str:
        return self.adb.getprop("ro.product.model") or self.adb.getprop("ro.product.vendor.model")

    @property
    def manufacturer(self) -> str:
        return self.adb.getprop("ro.product.manufacturer")

    @property
    def android_release(self) -> str:
        return self.adb.getprop("ro.build.version.release")

    def packages(self) -> set[str]:
        if self._packages is None:
            names = set()
            for line in self.adb.shell_lines("pm list packages"):
                names.add(line.split(":", 1)[-1].strip())
            self._packages = names
        return self._packages

    def has(self, pkg: str) -> bool:
        return pkg in self.packages()

    def is_disabled(self, pkg: str) -> bool:
        for line in self.adb.shell_lines("pm list packages -d"):
            if line.split(":", 1)[-1].strip() == pkg:
                return True
        return False

    def home_claimants(self) -> list[str]:
        """Every activity that claims HOME, in the order the system prefers them."""
        out = self.adb.shell(
            "cmd package query-activities --brief -a android.intent.action.MAIN "
            "-c android.intent.category.HOME"
        )
        comps = []
        for line in out.splitlines():
            line = line.strip()
            if "/" in line and " " not in line and "priority" not in line and "match=" not in line:
                comps.append(line)
        return comps

    def home_owner(self) -> str:
        """The component the HOME button actually opens right now."""
        out = self.adb.shell(
            "cmd package resolve-activity --brief -a android.intent.action.MAIN "
            "-c android.intent.category.HOME"
        )
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        for line in reversed(lines):
            if "/" in line and "priority" not in line and "match=" not in line:
                return line
        return ""

    def ram(self) -> tuple[int, int]:
        """(total MB, available MB)."""
        total = avail = 0
        for line in self.adb.shell_lines("cat /proc/meminfo | head -3"):
            m = re.match(r"MemTotal:\s+(\d+) kB", line)
            if m:
                total = int(m.group(1)) // 1024
            m = re.match(r"MemAvailable:\s+(\d+) kB", line)
            if m:
                avail = int(m.group(1)) // 1024
        return total, avail

    def is_google_tv(self) -> bool:
        pkgs = self.packages()
        return ("com.google.android.apps.tv.launcherx" in pkgs
                or "com.google.android.tvrecommendations" in pkgs
                or GOOGLE_SETUP_WIZARD in pkgs)

    def vendor_profile(self) -> str:
        """Which debloat profile fits this TV best."""
        pkgs = self.packages()
        m = (self.manufacturer or "").lower()
        if "skyworth" in m:
            return "skyworth"
        if "tcl" in m or any(p.startswith("com.tcl.") for p in pkgs):
            return "tcl"
        if "xiaomi" in m or any(p.startswith(("com.miui.", "com.xiaomi.")) for p in pkgs):
            return "xiaomi"
        if self.is_google_tv():
            return "google-tv"
        return "generic"

    def summary(self) -> list[str]:
        total, avail = self.ram()
        return [
            f"model        {self.model}",
            f"android      {self.android_release}  ({'Google TV' if self.is_google_tv() else 'AOSP TV'})",
            f"ram          {avail} MB available of {total} MB",
            f"home opens   {self.home_owner() or 'unknown'}",
            f"profile      {self.vendor_profile()}",
        ]


# --------------------------------------------------------------------------------------
# Debloat profiles
# --------------------------------------------------------------------------------------


def load_profile(name: str) -> list[tuple[str, str, str]]:
    """profiles/<name>.txt -> [(package, risk, reason)]."""
    path = PROFILES / f"{name}.txt"
    if not path.is_file():
        return []
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        entries.append((parts[0], parts[1].lower() if len(parts) > 1 else "safe",
                        parts[2] if len(parts) > 2 else ""))
    return entries


def profile_chain(vendor: str) -> list[str]:
    """The profiles that apply to this TV, in order."""
    chain = ["base"]
    if vendor != "base":
        chain.append(vendor)
    if vendor != "google-tv":
        chain.append("google-tv")
    return chain


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def ok(msg: str) -> None:
    print(f"  [ok]   {msg}")


def warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def err(msg: str) -> None:
    print(f"  [err]  {msg}")


def info(msg: str) -> None:
    print(f"         {msg}")


def head(title: str) -> None:
    print()
    print(f"== {title} " + "=" * max(0, 56 - len(title)))


# --------------------------------------------------------------------------------------
# Undo log
# --------------------------------------------------------------------------------------


def state_dir() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE


def undo_path() -> Path:
    return state_dir() / "last-undo.json"


def write_undo(packages: list[str]) -> None:
    if not packages:
        return
    undo_path().write_text(
        json.dumps({"when": datetime.now().isoformat(timespec="seconds"),
                    "packages": packages}, indent=2),
        encoding="utf-8")
    info(f"undo log: {undo_path()}")


def read_undo() -> list[str]:
    p = undo_path()
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("packages", [])
    except (json.JSONDecodeError, OSError):
        return []


def remember_device(serial: str) -> None:
    try:
        (state_dir() / "last-device").write_text(serial, encoding="utf-8")
    except OSError:
        pass


def recall_device() -> str:
    p = state_dir() / "last-device"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_connect(adb: Adb, args) -> int:
    ip = args.ip
    if not ip:
        raise TVError("which TV? pass --ip <tv-ip> (Settings > Network)")

    if args.kill_server:
        # An adb server from another adb build (e.g. 31 vs 41) makes `adb pair` fail with
        # "protocol fault (couldn't read status message)".  Restarting the server fixes it.
        adb.kill_server()
        time.sleep(1.5)
    adb.start_server()

    if args.pair_port and args.pair_code:
        target = f"{ip}:{args.pair_port}"
        good, text = adb.pair(target, args.pair_code)
        (ok if good else err)(f"pair {target}: {text}")
        if not good and "protocol fault" in text.lower():
            adb.kill_server()
            time.sleep(1.5)
            adb.start_server()
            good, text = adb.pair(target, args.pair_code)
            (ok if good else err)(f"pair retry: {text}")

    targets = [f"{ip}:{args.port or 5555}"]
    if args.scan:
        head(f"scanning {ip}")
        found = scan_ports(ip, range(30000, 61000))
        info(f"open ports: {', '.join(map(str, found)) or 'none'}")
        targets += [f"{ip}:{p}" for p in found]

    # Drop stale entries for this host, so a port that no longer exists does not
    # linger as an "offline" device in `adb devices`.
    for serial, state in adb.devices():
        if state != "device" and serial.startswith(f"{ip}:"):
            adb.run("disconnect", serial)

    for target in targets:
        good, text = adb.connect(target)
        (ok if good else warn)(f"connect {target}: {text}")
        if good:
            adb.serial = target
            break

    if not adb.serial:
        raise TVError(
            "no ADB connection. On the TV: Developer options > Wireless debugging > ON, then\n"
            "       run connect with --pair-port and --pair-code from the screen (both rotate)."
        )
    if not adb.wait_ready():
        raise TVError("the TV did not answer - is it awake and on the same network?")
    remember_device(adb.serial)
    ok(f"connected: {adb.serial}")
    return 0


def cmd_probe(adb: Adb, args) -> int:
    dev = Device(adb)
    head("this TV")
    for line in dev.summary():
        info(line)
    head("home screen")
    for c in dev.home_claimants():
        pkg = c.split("/", 1)[0]
        state = "disabled" if dev.is_disabled(pkg) else "enabled"
        mark = "  <- claims HOME with priority 1" if pkg == GOOGLE_SETUP_WIZARD and state == "enabled" else ""
        info(f"  {c:<58} [{state}]{mark}")
    return 0


def cmd_debloat(adb: Adb, args) -> int:
    dev = Device(adb)
    chain = profile_chain(dev.vendor_profile())
    head(f"debloat (profiles: {', '.join(chain)})")

    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for name in chain:
        for pkg, risk, reason in load_profile(name):
            if pkg not in seen:
                seen.add(pkg)
                entries.append((pkg, risk, reason))
    if args.include_caution:
        for pkg, risk, reason in load_profile("caution"):
            if pkg not in seen:
                seen.add(pkg)
                entries.append((pkg, risk, reason))

    changed: list[str] = []
    for pkg, risk, reason in entries:
        if pkg in NEVER_DISABLE:
            info(f"skip {pkg} (never disable)")
            continue
        if not dev.has(pkg):
            continue
        if dev.is_disabled(pkg):
            continue
        if args.dry_run:
            info(f"would disable {pkg}  ({risk}) - {reason}")
            continue
        out = adb.shell(f"pm disable-user --user 0 {pkg}").strip()
        if "new state: disabled" in out or "has been disabled" in out:
            ok(f"disabled {pkg}")
            changed.append(pkg)
        else:
            err(f"{pkg}: {out.splitlines()[-1] if out else 'no output'}")

    head("summary")
    if args.dry_run:
        info(f"{len(entries)} packages considered, nothing changed (dry run)")
    else:
        info(f"disabled {len(changed)} packages")
        write_undo(changed)
    return 0


def resolve_apk(args) -> Path:
    if args.apk:
        p = Path(args.apk)
        if not p.is_file():
            raise TVError(f"APK not found: {p}")
        return p
    if getattr(args, "build", False):
        return build_launcher(Path(args.project))
    raise TVError(
        "no APK given - use --apk <file> or --build --project <cleanyourtv checkout>.\n"
        "       (CleanYourTV publishes no binaries; build it with launcher/build-and-install.sh.)"
    )


def find_build_jdk() -> str:
    """Gradle 8.9 cannot read Java 25 class files.  Find a JDK 17/21/23 instead."""
    candidates: list[Path] = []
    for env in ("JAVA_HOME", "JDK_HOME"):
        if os.environ.get(env):
            candidates.append(Path(os.environ[env]))
    gradle_jdks = Path.home() / ".gradle" / "jdks"
    if gradle_jdks.is_dir():
        candidates += [d for d in gradle_jdks.iterdir() if d.is_dir()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Android Studio" / "jbr")
    candidates.append(Path(r"C:\Program Files\Android\Android Studio\jbr"))
    for c in candidates:
        exe = c / ("bin/java.exe" if os.name == "nt" else "bin/java")
        if not exe.is_file():
            continue
        try:
            p = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=20)
            blob = (p.stdout or "") + (p.stderr or "")
            m = re.search(r'version "(\d+)', blob)
            if m and int(m.group(1)) < 25:
                return str(c)
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise TVError(
        "no JDK 17/21/23 found for the build - Gradle 8.9 cannot read Java 25 class files\n"
        "       (Unsupported class file major version 69). Install JDK 21 and set JAVA_HOME."
    )


def build_launcher(project: Path) -> Path:
    launcher = project / "launcher" if (project / "launcher").is_dir() else project
    if not (launcher / "gradlew.bat").exists() and not (launcher / "gradlew").exists():
        raise TVError(f"no gradle wrapper in {launcher}")
    gradlew = launcher / ("gradlew.bat" if os.name == "nt" else "gradlew")
    java_home = find_build_jdk()
    env = dict(os.environ, JAVA_HOME=java_home)
    env["GRADLE_OPTS"] = env.get("GRADLE_OPTS", "") + " -Djava.net.preferIPv4Stack=true"
    info(f"building {launcher} with JDK {java_home}")
    p = subprocess.run([str(gradlew), "assembleRelease", "--console=plain", "--no-daemon"],
                       cwd=launcher, env=env, text=True)
    if p.returncode != 0:
        raise TVError("the gradle build failed - see the output above")
    apk = launcher / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    if not apk.is_file():
        raise TVError(f"build finished but {apk} is missing")
    ok(f"built {apk} ({apk.stat().st_size // 1024} KB)")
    return apk


def cmd_install_launcher(adb: Adb, args) -> int:
    dev = Device(adb)
    apk = resolve_apk(args)

    head("install the home app")
    out = adb.shell(f"pm install -r {apk}").strip()
    if "Success" not in out:
        raise TVError(f"install failed: {out}")
    ok(f"installed {apk.name} ({apk.stat().st_size // 1024} KB)")
    adb.shell(f"pm grant {LAUNCHER_PKG} android.permission.ACCESS_FINE_LOCATION")
    adb.shell(f"cmd package compile -m speed -f {LAUNCHER_PKG}")

    # ---- make it the home screen, in the order that actually works --------------------
    head("make it the home screen")
    claimants = dev.home_claimants()
    disabled_now: list[str] = []

    # 1. The Google TV setup wizard outranks everything and takes Home back.  Disable first.
    if dev.has(GOOGLE_SETUP_WIZARD) and not dev.is_disabled(GOOGLE_SETUP_WIZARD):
        adb.shell(f"pm disable-user --user 0 {GOOGLE_SETUP_WIZARD}")
        ok(f"disabled {GOOGLE_SETUP_WIZARD} (it claims HOME with priority 1)")
        disabled_now.append(GOOGLE_SETUP_WIZARD)

    # 2. The old launcher.  Well-known Google launchers go by default; a vendor launcher
    #    is only disabled when you ask, because it is not ours to judge.
    stock = None
    for c in claimants:
        pkg = c.split("/", 1)[0]
        if pkg in (LAUNCHER_PKG, GOOGLE_SETUP_WIZARD, "com.android.tv.settings"):
            continue
        if dev.is_disabled(pkg):
            continue
        stock = pkg
        break
    if stock:
        known = stock in KNOWN_STOCK_LAUNCHERS
        if args.disable_stock == "always" or (args.disable_stock == "auto" and known):
            adb.shell(f"pm disable-user --user 0 {stock}")
            ok(f"disabled the old launcher {stock}")
            disabled_now.append(stock)
        else:
            warn(f"the old launcher is '{stock}' (a vendor app) - left enabled")
            warn("re-run with --disable-stock always to disable it too")
    else:
        info("no other launcher claims HOME")

    # 3. Ask for our launcher to be Home, then check that it really took.
    adb.shell(f"cmd package set-home-activity --user 0 {LAUNCHER_ACTIVITY}")
    time.sleep(1.5)
    owner = dev.home_owner()
    if not owner.startswith(LAUNCHER_PKG):
        err(f"Home still opens '{owner or 'nothing'}' - putting things back")
        for pkg in disabled_now:
            adb.shell(f"pm enable --user 0 {pkg}")
            info(f"re-enabled {pkg}")
        write_undo(disabled_now)
        raise TVError(
            "the home app did not take over. Pick it by hand once:\n"
            "       Settings > Apps > Default apps > Home app > CleanYourTV"
        )
    ok(f"Home opens {owner}")
    write_undo(disabled_now)
    adb.shell(f"am start -W -n {LAUNCHER_ACTIVITY}")
    ok("press Home on the remote")
    return 0


def cmd_undo(adb: Adb, args) -> int:
    packages = read_undo()
    if not packages:
        info("nothing to undo")
        return 0
    head("undo")
    for pkg in packages:
        if args.dry_run:
            info(f"would re-enable {pkg}")
            continue
        adb.shell(f"pm enable --user 0 {pkg}")
        ok(f"re-enabled {pkg}")
    if not args.dry_run:
        undo_path().unlink(missing_ok=True)
    return 0


def cmd_setup(adb: Adb, args) -> int:
    """The whole job in one command: connect, debloat, install the home app."""
    head("step 1 of 3 - connect")
    rc = cmd_connect(adb, args)
    if rc != 0:
        return rc

    head("step 2 of 3 - what is this TV")
    dev = Device(adb)
    for line in dev.summary():
        info(line)

    head("step 3 of 3 - debloat")
    cmd_debloat(adb, args)

    head("install the home app")
    cmd_install_launcher(adb, args)

    print()
    ok("done - press Home on the remote")
    info("undo everything with:  python tvdebloat.py undo")
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tvdebloat",
        description="Debloat an Android/Google TV and install the CleanYourTV home app.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:  python tvdebloat.py setup --ip 192.168.1.50 --apk app-release.apk",
    )
    p.add_argument("--adb", help="path to the adb binary")
    p.add_argument("--serial", help="target serial IP:PORT (default: the only device attached)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_connect_args(sp):
        sp.add_argument("--ip", help="TV IP address")
        sp.add_argument("--port", type=int, help="debug port (default 5555)")
        sp.add_argument("--pair-port", type=int, help="pairing port from the TV's Wireless debugging screen")
        sp.add_argument("--pair-code", help="6-digit pairing code (it rotates every minute)")
        sp.add_argument("--scan", action="store_true", help="scan for the ADB port if 5555 is refused")
        sp.add_argument("--kill-server", action="store_true", help="restart the adb server first")

    s = sub.add_parser("setup", help="connect + debloat + install the home app (one command)")
    add_connect_args(s)
    s.add_argument("--apk", help="path to the launcher APK")
    s.add_argument("--build", action="store_true", help="build the launcher from a CleanYourTV checkout")
    s.add_argument("--project", default=str(HERE.parent / "cleanyourtv-2.2.0"))
    s.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    s.add_argument("--include-caution", action="store_true", help="also apply the 'caution' entries")
    s.add_argument("--disable-stock", choices=("auto", "always", "never"), default="auto")

    c = sub.add_parser("connect", help="pair and connect over WiFi")
    add_connect_args(c)

    sub.add_parser("probe", help="show what this TV is")

    d = sub.add_parser("debloat", help="disable the bloatware (reversible)")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--include-caution", action="store_true")

    i = sub.add_parser("install-launcher", help="install the home app and make it Home")
    i.add_argument("--apk", help="path to the launcher APK")
    i.add_argument("--build", action="store_true", help="build the launcher from a CleanYourTV checkout")
    i.add_argument("--project", default=str(HERE.parent / "cleanyourtv-2.2.0"))
    i.add_argument("--disable-stock", choices=("auto", "always", "never"), default="auto")

    u = sub.add_parser("undo", help="re-enable what the last run disabled")
    u.add_argument("--dry-run", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        adb = Adb(find_adb(args.adb))
        if args.cmd == "connect":
            return cmd_connect(adb, args)

        if args.serial:
            adb.serial = args.serial
            if adb.serial not in adb.online_devices():
                good, text = adb.connect(adb.serial)
                (ok if good else warn)(f"connect {adb.serial}: {text}")
        else:
            online = adb.online_devices()
            if not online:
                remembered = recall_device()
                if remembered:
                    good, _ = adb.connect(remembered)
                    if good:
                        ok(f"reconnected to {remembered}")
                        adb.serial = remembered
            if not adb.serial:
                print("No TV connected. Run:  python tvdebloat.py connect --ip <tv-ip>")
                return 1
            online = adb.online_devices()
            if online:
                adb.serial = online[0]
        if adb.serial:
            remember_device(adb.serial)
        if not adb.wait_ready():
            err("the TV did not answer - is it awake and on the same network?")
            return 1

        handlers = {
            "setup": cmd_setup,
            "probe": cmd_probe,
            "debloat": cmd_debloat,
            "install-launcher": cmd_install_launcher,
            "undo": cmd_undo,
        }
        return handlers[args.cmd](adb, args)
    except TVError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
