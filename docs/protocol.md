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
| `hello` | `proto`, `fw`, `dev`, `rev` | Session open. `rev` is the layout revision the device currently holds |
| `press` | `id`, `page` | A tile needing the agent was pressed |
| `release` | `id`, `page`, `held_ms` | Release, for hold-to-repeat and long-press actions |
| `pong` | `seq` | Reply to `ping` |
| `log` | `lvl`, `msg` | Device log forwarded to the agent's log file. `lvl`: `debug`/`info`/`warn`/`error` |
| `layout_req` | — | Device has no layout, or its `rev` disagrees with the host's. Asks for a push |
| `error` | `code`, `msg` | Device-side failure the user should know about |

Device-local presses (`hid`, `hid_text`, `media`, `page`, `theme`) do **not** emit `press`. They are executed
on-device and never touch the wire. Only `log` may mention them.

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
| `backlight` | `v` | Brightness 0–100 |

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
  "settings": { "theme": "Midnight", "brightness": 80, "idle_dim_s": 60, "idle_off_s": 300 },
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
| `colortest` | firmware | Bench diagnostic: flat patches and the resolved theme values. See [editing-the-deck.md](editing-the-deck.md#the-colours-page) |

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
