# Credits and notices

`tvdebloat` is an independent, unofficial helper tool. It contains **no code, no assets and
no build output** from any other project - it only drives `adb` to do things to a TV that
you could do by hand.

## The home app it installs

The home screen this tool installs is **[CleanYourTV](https://github.com/Gathin23/cleanyourtv)**,
written by **Gathin** (github.com/Gathin23), and it is **not** included in this repository.

| | |
|---|---|
| Project | CleanYourTV - https://github.com/Gathin23/cleanyourtv |
| Author | Gathin - https://github.com/Gathin23 |
| Licence | MIT, Copyright (c) 2026 Gathin - see the `LICENSE` file in that repository |
| Website | https://cleanyour.tv |

`tvdebloat` downloads nothing and bundles nothing from that project. You clone and build it
yourself (see the README), and this tool installs the APK you built. The idea - a small,
clean, Apple-TV-style launcher for Android TV, plus the per-model debloat lists - is theirs.

If you share an APK that you built with their project, keep their `LICENSE` file and
copyright notice with it. That is what the MIT licence asks for, and it costs you nothing.
The Outfit typeface in their launcher is under the SIL Open Font License 1.1
(`launcher/FONT-LICENSE-OFL.txt` in their repository); it is not redistributed here either.

## Where the debloat lists came from

The per-vendor package lists in `profiles/` were written for this tool, but they follow the
approach of these two MIT-licensed projects:

| | |
|---|---|
| [android-tv-debloat-toolkit](https://github.com/seun-novodev/android-tv-debloat-toolkit) | the original idea of a Windows debloat tool with a safe list for TCL/Google TV sets, MIT, Copyright (c) 2026 seun-novodev |
| [CleanYourTV](https://github.com/Gathin23/cleanyourtv) | its `examples/` folder holds per-model debloat lists for 30 TVs across 16 brands, MIT, Copyright (c) 2026 Gathin |

Package names themselves are not copyrightable, and every entry here was checked against a
real device before it was added.

## Android platform-tools

This tool drives `adb` but does **not** ship it. If you bundle the Android SDK
platform-tools yourself, they are licensed under the Apache License 2.0
(https://developer.android.com/studio/releases/platform-tools) and you should keep their
`NOTICE` file with them.

## Trademarks

"CleanYourTV" and the names of the apps this tool touches (Netflix, YouTube, Google TV,
TCL, Xiaomi, Skyworth, ...) belong to their respective owners and are used here only to
identify the software being installed or the devices being configured.

## No endorsement

This project is not affiliated with, sponsored by, or endorsed by Gathin, by the
android-tv-debloat-toolkit project, or by any of the manufacturers whose TVs it configures.
It is a third-party tool that happens to install their work.
