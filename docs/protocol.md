# multi_deck wire protocol

**Version:** `proto = 1`

Newline-delimited JSON, UTF-8, bidirectional. One frame per line, no embedded newlines. Every frame
carries a `t` (type) field.

The protocol is deliberately **transport-agnostic**. Today it runs over USB CDC (the native USB port,
port B). Adding WiFi later means implementing `Link` on both sides — the frames do not change.

## Session lifecycle

```
device                                   host
  |  {"t":"hello","proto":1,...}   ---->  |
  |  <---- {"t":"welcome","proto":1,...}  |
  |                                       |
  |  <---- {"t":"ping","seq":N}           |   every 2s
  |  {"t":"pong","seq":N}          ---->  |
```

The device opens with `hello` as soon as it sees DTR asserted, and repeats every 2s until it gets a
`welcome`. A `proto` mismatch is a **hard failure on both sides**: log loudly, show it on-screen, and
do not attempt to interoperate.

### Both ends must end a session on a clock, not on an error

The two ways a link dies are both silent. Reads that return nothing and writes that vanish into a
closed port raise no exception on either side, so neither end can wait to be told.

| Deadline | Where | What it catches |
|---|---|---|
| `MD_LINK_TIMEOUT_MS` (5s) | device | Host stopped answering without closing the port |
| `SILENCE_TIMEOUT_S` (10s) | agent | Device stopped answering — five missed pings |
| `HANDSHAKE_TIMEOUT_S` (10s) | agent | **Port open, no `hello` ever.** Close it and reopen |

The third is the one that cost the most to find. When a laptop wakes from Modern Standby the COM
port comes back within a couple of seconds, but the device's CDC does not always come back with
it — so the agent held an open port, sent `identify` into it every 2s, and waited. Forever, in
principle; in practice until some unrelated stale-handle error surfaced, which one morning took
2, then 4.5, then 6 minutes across three cycles.

**Reopening is the remedy, not merely the retry.** A fresh `serial.Serial()` re-asserts DTR, and
that is what prods the device into announcing itself again.

Two further rules on the agent side, both learned the same way:

- **Re-enumerate on every attempt.** Caching the discovered port meant that when Windows handed
  the deck a different COM number after a suspend, the agent reopened a port that no longer
  existed until it was restarted — 1273 consecutive times in one overnight run.
- **Back off, and log the first failure only.** That same run buried an hour of history under
  identical warnings, and the log rotates at 512KB, so the spam also destroyed the evidence.

### Either side may start the conversation

The device only announces itself *while it believes no session exists*. On its own that makes the
handshake depend on who arrives first, and produces a failure that looks intermittent:

1. The agent connects, handshakes, and later exits — without the device noticing.
2. The device keeps `session_up` for up to `MD_LINK_TIMEOUT_MS` (5s) afterwards, so it stays quiet.
3. A new agent starts inside that window, listens for a `hello`, and hears nothing.

Two mechanisms close this, and both are needed:

- **The host sends `identify` on connecting**, and keeps sending it every 2s until it gets a `hello`.
  The device answers `identify` with a `hello` regardless of session state.
- **The device drops its session the moment the port closes** (DTR deasserted) rather than waiting
  out the inactivity timeout, so an agent restart finds it already announcing.

The inactivity timeout still matters — it covers a host that stops responding without closing the
port — but it is no longer what recovery depends on.

**Link-down detection:** no inbound traffic for 5s means the link is down. The device then:

- greys out every tile whose action needs the agent,
- keeps all device-local tiles (numpad, media, page navigation) fully live,
- shows a disconnected indicator.

This degradation path is the visible proof that the standalone design works, so it is worth testing
deliberately rather than only encountering it by accident.

## Frames: device → host

| Frame | Fields | Meaning |
|---|---|---|
| `hello` | `proto`, `fw`, `dev`, `rev`, `assets` | Session open. `rev` is the layout revision the device currently holds; `assets` is the card's asset stamp — see below |
| `press` | `id`, `page` | A tile needing the agent was pressed |
| `release` | `id`, `page`, `held_ms` | Release, for hold-to-repeat and long-press actions |
| `pong` | `seq` | Reply to `ping` |
| `log` | `lvl`, `msg` | Device log forwarded to the agent's log file. `lvl`: `debug`/`info`/`warn`/`error` |
| `layout_req` | — | Device has no layout, or its `rev` disagrees with the host's. Asks for a push |
| `error` | `code`, `msg` | Device-side failure the user should know about |

Device-local presses (`hid`, `hid_text`, `media`, `page`, `theme`) do **not** emit `press`. They are executed
on-device and never touch the wire. Only `log` may mention them.

### The `assets` field — telling a stale SD card from a current one

Layout and colours cannot go stale: they are pushed whenever `rev` disagrees. Images can, because
they only reach the card by hand. A wallpaper regenerated and never copied still *exists* on the
card, so it loads, and nothing anywhere reports that the picture on screen is not the one you made.

`assets` closes that. `tools/make_assets.py` writes a content hash of `sdcard/` to
`sdcard/assets.ver`; copying the tree carries it along, so the card declares its own generation.
The device reads the file and repeats it here — **it hashes nothing and compares nothing.** The
agent has the originals, so the agent decides.

Three states, deliberately distinct:

| `assets` | Means | Agent does |
|---|---|---|
| absent | No card mounted, or firmware older than the stamp | nothing — neither is evidence, and a cardless deck already says so on screen |
| `""` | Card mounted, no `assets.ver` on it | asks for a copy, without claiming the images are wrong |
| a hash | The card's generation | compares; warns and toasts only on a genuine mismatch |

A content hash rather than a number you bump: a version you have to remember to increment is only
correct while you remember, and this exists for the times you forgot.

## Frames: host → device

| Frame | Fields | Meaning |
|---|---|---|
| `welcome` | `proto`, `host`, `rev` | Session accepted. `rev` is the host's layout revision |
| `identify` | — | "Send your `hello` now." See below |
| `ping` | `seq` | Heartbeat |
| `stats` | see below | System stats, pushed at 1Hz whenever the session is up |
| `layout` | `rev`, `data` | Full layout push. Device writes it to SD and rebuilds the UI |
| `hid_exec` | `action` | Host asks the device to perform a device-local action — used to sequence mixed macros (see below) |
| `toast` | `msg`, `lvl` | Transient on-screen message |
| `backlight` | `v` | Brightness 0–100, literal on a board rewired for PWM. Unmodified, only 0 vs non-zero has any effect — see [hardware-notes.md](hardware-notes.md) |
| `time` | `epoch`, `tz_min` | Wall clock. Sent on connect and every 60s |
| `power` | `state` | `"sleep"` or `"wake"` |

### `time` frame

```json
{"t": "time", "epoch": 1785657600, "tz_min": -300}
```

`epoch` is seconds since 1970 **UTC**; `tz_min` is the local offset in minutes, positive east of
Greenwich. The device has no battery-backed clock and no timezone database, so it holds the last
value plus the `millis()` at which it arrived and does arithmetic in between.

The **offset is recomputed on every send**, which is what carries a daylight-saving change across
to a device that has no idea what one is. Until the first sync the deck renders a clock as `--:--`
and the calendar as "waiting for the PC" — deliberately, because a wrong date looks like a bug and
sends you looking in the wrong place.

Drift between syncs is the ESP32 crystal's ~20ppm, about 1.7 seconds a day. It only matters across
a long spell with the agent closed.

### `power` frame

```json
{"t": "power", "state": "sleep"}
```

Sent when the PC suspends and resumes. The deck shows its sleep screen on `sleep`, and returns to
the deck on `wake`, on a touch, or on any new session.

**This frame is an accelerator, not a requirement — do not build anything that depends on it
arriving.** It was originally the only way into the sleep screen, on the reasoning that a
sleeping PC, a quit agent and an unplugged cable are indistinguishable silences and guessing
between them would be wrong.

Measured, that did not survive contact. On a Modern Standby machine, deliberately sleeping the
laptop produces the display-off notification and Kernel-Power 506 *in the same second* — the
agent gets no margin, its event loop is already being frozen, and the frame never goes out. The
first real sleep test showed the deck running its ordinary idle sequence, having been told
nothing.

So the device **also** enters the sleep screen on its own, `settings.sleep_clock_s` after the link
goes down (default 20s, and it must have gone untouched for that long too). The ambiguity turned
out not to matter: a closed agent, a sleeping PC and an unplugged cable all otherwise end in a
black screen, and a clock beats a black screen in all of them. The frame still earns its place by
making it *immediate* when it does arrive.

That fallback was first written as "when the backlight would switch off anyway with the link
down", which tied it to `idle_off_s`. On this laptop that made the clock unreachable in ordinary
use — it needed five untouched minutes *and* a dead link, and the two never lined up — so the two
timers are now independent. Details in `editing-the-deck.md`.

The Windows trigger is `GUID_CONSOLE_DISPLAY_STATE` rather than `PBT_APMSUSPEND`, which is later
still. Note that it is genuinely a *display* signal, not a sleep signal: on AC, where the display
blanks an hour before the machine sleeps, it fires — and delivers — with the PC wide awake. On
battery, where both timers are 3 minutes here, the bus is already gone. Recovering the link
afterwards is a separate matter — see below.

### `stats` frame

```json
{
  "t": "stats",
  "cpu": 37.2,
  "cpu_cores": [12.0, 55.1, 8.3, 41.0],
  "cpu_temp": 48.0,
  "mem": 61.0,
  "mem_used_gb": 19.4,
  "mem_total_gb": 32.0,
  "gpu": 22.0,
  "gpu_temp": 54.0,
  "gpu_mem": 31.0,
  "disk": 12.0,
  "net_up_mbps": 1.2,
  "net_down_mbps": 8.4,
  "uptime_s": 384210
}
```

**Only `t` and `cpu` are guaranteed.** Every other field is optional and will be absent when the
provider is unavailable — no NVIDIA GPU, LibreHardwareMonitor not running, and so on. The device
must render an absent field as `--` and must not crash or show a stale value. Test this by stopping
a provider while the stats page is open.

The agent pushes unconditionally rather than tracking which page is on screen; the device discards
the frame when the stats page is not built. At 1Hz over USB CDC the wasted bandwidth is irrelevant,
and it avoids a second piece of shared state that could fall out of sync.

## Who executes what

Action types split cleanly by which side can perform them:

| Type | Executed by | Payload |
|---|---|---|
| `hid` | **device** | `{"keys": ["CTRL","SHIFT","ESC"]}` |
| `hid_text` | **device** | `{"text": "literal string"}` |
| `media` | **device** | `{"key": "play_pause"}` |
| `page` | **device** | `{"target": "numpad"}` |
| `theme` | **device** | `{"target": "next"}` — `"next"`, `"prev"`, or a theme name |
| `delay` | either | `{"ms": 200}` — only meaningful inside `seq` |
| `launch` | agent | `{"target": "code", "args": ["."], "cwd": "..."}` |
| `ahk` | agent | `{"fn": "SnapLeft", "args": []}` |
| `shell` | agent | `{"cmd": "..."}` |
| `seq` | see below | `{"steps": [ ...actions... ]}` |

### Sequences that cross the boundary

At layout-parse time the device computes, per button, whether its whole action tree is device-local.

- **Entirely local** → executed on-device the instant it is touched. No wire traffic, no latency, and
  it works with the agent closed. This is the guarantee that makes the numpad useful.
- **Contains any agent step** → the device sends `press` and does nothing else. The **agent becomes
  the sequencer**: it walks the steps in order, running its own, and sending `hid_exec` back to the
  device for the local ones.

Handing sequencing to the agent for mixed macros is what keeps step ordering correct. The
alternative — each side racing through its own steps — produces macros that work intermittently.

## Layout: `deck.json`

Lives at the root of the SD card. The agent holds the master copy and can push it over `layout`.

```json
{
  "rev": 1,
  "themes": [
    { "name": "Midnight", "bg": "#0d1117", "tile": "#1b2129", "accent": "#4aa3ff",
      "text": "#e6edf3", "text_muted": "#8b949e", "ok": "#3fb950", "idle": "#6e7681",
      "tile_opa": 100, "border": "#8fa6c0", "border_opa": 22, "radius": 14 }
  ],
  "settings": { "theme": "Midnight", "brightness": 80, "idle_dim_s": 120, "idle_off_s": 600 },
  "pages": [
    {
      "id": "launch",
      "title": "Launch",
      "type": "grid",
      "grid": { "cols": 4, "rows": 3 },
      "buttons": [
        {
          "id": "launch.vscode",
          "label": "VS Code",
          "icon": "icons/code.bin",
          "action": { "type": "launch", "target": "code" }
        }
      ]
    },
    { "id": "numpad", "title": "Ten-Key", "type": "numpad" },
    { "id": "stats",  "title": "System",  "type": "stats"  }
  ]
}
```

### Themes

`themes` is a list; `settings.theme` names the active one, and a `theme` action steps through them
on the device. The legacy single-object form (`"theme": { ... }`) is still accepted and read as a
one-element list, so older layouts need no migration.

Every field is optional and every absent one keeps its default — including a colour, so `"#000000"`
is an actual black rather than an indistinguishable parse failure. Full field reference with
defaults: [editing-the-deck.md](editing-the-deck.md#themes).

The active theme survives a reboot in `/theme.txt` on the SD card, which the device owns; a layout
push never overwrites it, and the theme you were on stays selected across an edit as long as its
name still exists.

### Page types

| `type` | Built by | Notes |
|---|---|---|
| `grid` | JSON | The general case. Tiles flow into `grid.cols` x `grid.rows` |
| `numpad` | firmware | Fixed layout — expressing a ten-key as a generic grid buys nothing |
| `stats` | firmware | Fixed layout, driven by `stats` frames |
| `calendar` | firmware | Month view. Needs `time` for today's date; works with the agent closed once told |
| `colortest` | firmware | Bench diagnostic: flat patches and the resolved theme values. See [editing-the-deck.md](editing-the-deck.md#the-colours-page) |

An **unrecognised `type` silently becomes `grid`** on the device — the strcmp chain in
`DeckConfig::parse()` ends with an unconditional fallback — so `"calender"` builds and navigates
as an empty grid with nothing anywhere reporting the typo. The agent validates the list above for
exactly that reason.

### Button fields

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Stable and unique. This is what crosses the wire, so renaming a label never breaks a binding |
| `label` | yes | Shown on the tile |
| `icon` | no | Path on SD to an RGB565 `.bin` — see `tools/make_icons.py` |
| `display` | no | `icon_text` \| `icon` \| `text`. Overrides the theme's default for this tile |
| `action` | yes | One of the action types above |
| `pos` | no | `{"col":0,"row":0,"w":1,"h":1}` to place/span explicitly. Omit for auto-flow |
| `hold` | no | A second action fired on long-press (>600ms) |

### Revisions

`rev` is a monotonically increasing integer owned by the agent. On connect, host and device compare:
if they differ, the host pushes the full layout and the device writes it to SD. The device may also
ask at any time with `layout_req`.

The device sends the button `id` on the wire, not the action payload — so the agent stays
authoritative about what a button *does*, and the two copies cannot drift into disagreeing about
behaviour while agreeing about names.

## Design notes

- **Newline-delimited JSON over a binary framing.** Debuggable by eye in a serial monitor, which
  matters more here than the bandwidth saved. 1Hz stats over USB CDC is not a throughput problem.
- **`id` on the wire, not the action.** See above.
- **No request/response correlation IDs.** There are no request/response pairs except `ping`/`pong`,
  which carries `seq`. If that changes, add an `rid` field rather than relying on ordering.
