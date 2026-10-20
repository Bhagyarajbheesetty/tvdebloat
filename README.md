# tvdebloat

Debloat an Android TV / Google TV and install the [CleanYourTV](https://github.com/Gathin23/cleanyourtv) home app.

Two jobs, one command.

```
python tvdebloat.py setup --ip 192.168.1.50 --apk app-release.apk
```

It connects to the TV over WiFi, turns off the bloatware, installs the home app, makes it the
home screen and checks that it worked. Nothing is uninstalled, so everything can be put back.

---

## 1. Prepare the TV

On the TV: **Settings → Device Preferences → About → Build** — press it 7 times to unlock
Developer options. Then **Settings → Device Preferences → Developer options** and turn on
**ADB debugging** (and **Wireless debugging** if it is offered).

Find the TV's IP address under **Settings → Network**.

## 2. Get the home app

CleanYourTV publishes no binaries, so build it once from its source:

```
git clone https://github.com/Gathin23/cleanyourtv
cd cleanyourtv/launcher
./gradlew assembleRelease
```

The APK lands in `launcher/app/build/outputs/apk/release/app-release.apk` (about 250 KB).
On Windows use `gradlew.bat assembleRelease`. It needs a **JDK 17 or 21** — Gradle 8.9 cannot
read Java 25 class files.

Already have an APK? Skip this and point `--apk` at it.

## 3. Run it

```
python tvdebloat.py setup --ip 192.168.1.50 --apk path/to/app-release.apk
```

If the TV shows a **pairing port** and a **6-digit code** under Developer options → Wireless
debugging, add them so the tool can pair:

```
python tvdebloat.py setup --ip 192.168.1.50 --apk path/to/app-release.apk \
    --pair-port 41234 --pair-code 123456
```

Both the port and the code change every time you open that screen, and they are only valid for
about a minute.

**Add `--dry-run` first** to see exactly what would change without touching anything.

## 4. Check the result

```
python tvdebloat.py probe
```

Press **Home** on the remote — the new home app opens.

## Undo

```
python tvdebloat.py undo
```

---

## Commands

| Command | What it does |
|---|---|
| `setup` | connect + debloat + install the home app (everything, in order) |
| `connect --ip <ip>` | pair and connect over WiFi |
| `probe` | what this TV is, what claims HOME, how much RAM is free |
| `debloat` | disable the bloatware (add `--dry-run` to preview) |
| `install-launcher --apk <file>` | install the home app and make it the home screen |
| `undo` | re-enable whatever the last run disabled |

Useful flags: `--dry-run`, `--include-caution` (also apply the entries marked *caution*:
TalkBack, Netflix, Google Play services), `--disable-stock always` (also disable a vendor
launcher, not just the Google ones), `--scan` (find the ADB port if 5555 is refused),
`--kill-server` (fix an `adb pair` that fails with *protocol fault*), `--adb <path>` (use a
specific adb), `--serial IP:PORT` (target a specific device).

## What gets disabled

`profiles/base.txt` applies to every TV; the vendor profile for your set is added
automatically (`skyworth`, `tcl`, `xiaomi`, `google-tv`). Each line is
`package | risk | reason`, so it is easy to edit or to add your own.

Only packages that are actually installed are touched, and only `pm disable-user` is used —
nothing is ever uninstalled, so `undo` always works.

Packages that break a TV are never touched: System UI, Google Play services, TV Settings and
the vendor framework service.

## Requirements

- Python 3.8+
- `adb` from Android platform-tools (34 or newer). The tool looks for it on `PATH`, in
  `ANDROID_HOME`/`ANDROID_HOME`/platform-tools and next to the script.
- The TV and the computer on the same WiFi.

## Notes

- The ADB port of a TV using *Wireless debugging* changes every time the screen is opened.
  Read it from the TV when a command cannot connect.
- One old `adb` on the machine can break a newer one (different server versions refuse to
  share a server). The tool picks the newest adb it can find; `--adb <path>` overrides that.
