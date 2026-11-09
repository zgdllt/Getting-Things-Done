# Xiaomi Band integration (demo)

Answer GTD clarification questions on a Xiaomi Smart Band 9 / 9 NFC with one tap:
**否掉 (dismiss) / 稍后 (later) / 到电脑处理 (mac)**. The band never writes to
Reminders or Calendar; formal writes still require preview and confirmation on the Mac.

```text
gtd_band.py ──HTTP 127.0.0.1 + token──▶ AstroBox plugin "GTD Band Bridge" (Rust/WASM, API Level 4)
     ▲                                        │ launch quick app + interconnect (JSON frames)
     └──────── GET /v1/events ◀────────────── band quick app com.zgdllt.gtdband
```

| Part | Path | Notes |
| --- | --- | --- |
| AstroBox plugin | `plugin/` | Loopback-only HTTP server, random token rotated on every load, written to `bridge.json` in the plugin directory |
| Band quick app | `watch/` | Vela JS, `designWidth` 192 (Band 9), debug-signed RPK |
| GTD bridge | `gtd_band.py` | One question on the band at a time; journals every step in `band_events` |
| Tests | `test_gtd_band.py` | Fake plugin server and temporary state; never touches a band or real GTD data |

## Build

```bash
brew install rustup        # once; the pinned nightly is in plugin/rust-toolchain.toml
(cd watch && npm install)  # aiot-toolkit 2.0.5
python3 scripts/build.py   # -> dist/GTD-Band-Bridge-0.3.0.abp, dist/com.zgdllt.gtdband.debug.0.3.0.rpk
```

## Install on an Android phone (recommended: the band stays connected away from the Mac)

1. In AstroBox on the phone, connect the band (quit AstroBox on the Mac so it does not hold the band).
2. Copy both files to the phone, e.g. `adb push dist/* /sdcard/Download/GTD-Band/`.
3. 插件 → **+** → pick `GTD-Band-Bridge-0.3.0.abp`, allow its permissions, open the plugin page and note the 6-digit 配对码.
4. Install the `.rpk` on the band (opening it with AstroBox works: `adb shell am start -a android.intent.action.VIEW -t application/zip -d file:///sdcard/Download/GTD-Band/<name>.rpk -n moe.astralsight.astrobox/.MainActivity`).
5. On the Mac: `python3 gtd_band.py pair --adb <phone-ip:port> --code <配对码>`. This forwards Mac `tcp:47913` to the phone over adb and stores the token in `~/.codex/personal-gtd/band.json` (0600). The code is single-use and rotates after 5 wrong attempts.

## Install on the desktop AstroBox

1. Connect **only** the band you want to use; the plugin selects it automatically when exactly one band is connected.
2. Install `dist/com.zgdllt.gtdband.debug.0.3.0.rpk` on the band as a local resource.
3. Plugins page → **+** → import `dist/GTD-Band-Bridge-0.3.0.abp`, then allow its permissions:
   `device`, `interconnect`, `register_interconnect_recv`, `http-server`, `thirdpartyapp`.

## Try it

```bash
python3 gtd_band.py status   # plugin reachable, band selected, receive registered
python3 gtd_band.py ping     # opens GTD on the band, pings twice; reports band_to_host and round_trip
python3 gtd_band.py run      # loop: sync answers, push the next open question
python3 gtd_band.py report   # answer rate, median seconds shown->answer, choices
```

Choices: `dismiss` dismisses the candidate (only while it is still a local candidate) and closes
its open questions; `later` re-asks after 2 hours; `mac` keeps the question for the workbench only.

### Adaptive answer options

Before a question is pushed, `band_options.py` asks the GTD model (the model and provider from GTD
settings, default `gpt-6.1-terra`; reasoning effort lowered to `low`) for 2–3 short answers using
`options.md` / `options.schema.json`. The band shows them as buttons above a small 否掉 / 稍后 / 电脑 row.
Tapping one saves its text as the question's answer and starts GTD's normal answer job
(`gtd_clarify.enqueue` + `launch`), exactly like "save answers and organize" in the workbench.
Options are cached per question in `band_options`; while one question is on the band the loop
prepares the next one (≈20 s per call measured). If generation fails the band falls back to the
three fixed buttons and the question is retried after 30 minutes.

Updating the plugin replaces its directory and therefore its token: pair again with the new code.

## Known limits

- Band→host messages are reported to fail on some firmware. `ping` (`band_to_host`, `round_trip`) is the go/no-go test.
- Verified 2026-11-09 on Band 9 NFC + AstroBox 2.2.5 (Android, Redmi K70 Pro): both directions work; a demo question
  went shown → answered in 8.8 s. Three causes found on the way:
  1. `onmessage` may deliver the string itself, not `{data}` — reading only `.data` silently drops every host frame.
  2. `connect.send({data})` must get an **object** on this firmware; a JSON string fails with code **202**.
  3. A receive registration made before the current band-app build was installed makes band sends fail with **401**.
     The plugin now refreshes the app list before registering and re-registers when the app's version/fingerprint changes.
- The band reconnects to AstroBox every few minutes (possibly competing with 小米运动健康). Questions stay in the plugin
  outbox and are resent on the next hello.
- Hardware has not been verified yet. The quick app needs to be in the foreground; the plugin launches it when pushing.
- The band can only tap. Quick apps cannot record audio on Band 9 / 9 Pro / 10 (`@system.record` unsupported).
- With a desktop host, the Mac is the Bluetooth host, so it only works within range of the Mac. With a phone host, `gtd_band.py run` needs the adb link to the phone (wireless adb drops when the phone changes network).
