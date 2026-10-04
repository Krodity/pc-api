# pc-api

One HTTP API for controlling this PC: keyboard/mouse input, Hyprland windows and
workspaces, media players, audio, notifications and screenshots. The plan is for
every remote (PC Remote, SSHRemote via `yk`, Deckboard, n8n, Beam, cr-remote) to
call this instead of carrying its own backend. Those connectors come later.

- User service `pc-api.service`, port **8782**, Python stdlib only.
- Separate from `pc-agent` (PC Remote) on purpose: PC Remote keeps working
  until it's migrated.

```bash
systemctl --user {status,restart} pc-api
journalctl --user -u pc-api -f
python3 -m unittest discover -s tests      # no /dev/uinput needed
```

## Access

1. **Peer filter**: only loopback, `100.64.0.0/10` and `fd7a:115c:a1e0::/48`
   (the tailnet). Everyone else gets 403. This doesn't depend on ufw.
2. **Token**: `~/.config/pc-api/token` (0600, minted on first start). Send
   `Authorization: Bearer <token>`, `X-Token: <token>`, or `?token=`.
   `/api/health` is the only open route.

The token is effectively remote control of the desktop (`/api/hypr/launch` and
`/api/hypr/dispatch` can start any program), so treat it like an SSH key.

```bash
T=$(cat ~/.config/pc-api/token)
curl -H "Authorization: Bearer $T" -d '{"keys":"ctrl+shift+t"}' localhost:8782/api/input/key
```

Responses are `{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`
with 400 (bad input), 401, 403, 404, 502 (the underlying tool failed) or 500.

## Routes

All POST bodies are JSON. `client` is optional everywhere and only matters for holds.

### Input (`/dev/uinput`, device name "pc-api virtual input")

| Route | Body | Notes |
|---|---|---|
| `POST /api/input/key` | `{keys: "ctrl+t" \| ["ctrl+a","ctrl+c"], delay_ms?: 12, gap_ms?}` | Press in order, release in reverse. Keys someone is holding stay down. |
| `POST /api/input/hold` | `{keys, ttl?: 30, client}` | Holding again refreshes the TTL. Max 600 s. |
| `POST /api/input/release` | `{keys?, client}` | No `keys` = everything this client holds. A key goes up only when its last holder releases it. |
| `POST /api/input/panic` | | Every key and mouse button up, all holds dropped. |
| `GET /api/input/holds` | | `{code: {client: seconds_left}}` |
| `POST /api/input/type` | `{text, delay_ms?: 6}` | US QWERTY. Unmappable characters come back in `skipped`. |
| `POST /api/input/click` | `{button?: left\|right\|middle\|back\|forward, count?: 1}` | |
| `POST /api/input/move` | `{dx, dy}` | Relative, so pointer acceleration applies. |
| `POST /api/input/moveto` | `{x, y}` | Absolute, in global layout coordinates (via Hyprland, no acceleration). |
| `GET /api/input/cursor` | | `{x, y}` |
| `POST /api/input/scroll` | `{dy?, dx?}` | `dy > 0` scrolls up. |
| `GET /api/input/keys?filter=vol` | | Searchable list of key names. |

**Key names** are the same set `yk` accepts: yk's aliases (`lctrl`, `volup`,
`next`, `bs`, `backtick`…), PC Remote's names, any kernel `KEY_*` symbol
lowercased without the prefix (`f24`, `kpenter`), `btn_*` symbols, and raw
codes (`30`, `0x1e`). Mouse buttons can also be held as `mouse_left` etc.
Joystick/gamepad buttons are rejected because the virtual device doesn't
declare them (declaring them makes libinput treat it as a joystick).

### Hyprland (Lua config: everything goes through `hl.dsp.*`)

| Route | Body |
|---|---|
| `GET /api/hypr/state` | Monitors, workspaces, windows, active window address |
| `POST /api/hypr/workspace` | `{workspace: "3"}` |
| `POST /api/hypr/focus` | `{address: "0x…"}` |
| `POST /api/hypr/close` | `{address?}` (default: the focused window) |
| `POST /api/hypr/move` | `{workspace, address?, follow?: false}` |
| `POST /api/hypr/fullscreen` | `{address?}` |
| `POST /api/hypr/launch` | `{cmd: "[workspace 2] obs"}` |
| `POST /api/hypr/dispatch` | `{lua: 'hl.dsp.global("quickshell:overviewWorkspacesToggle")'}` |

The live Hyprland instance is re-resolved on every call (like `~/bin/hypr-live`),
so a Hyprland restart doesn't leave the service stale.

### Media (playerctl / MPRIS)

`GET /api/media` · `POST /api/media/{play,pause,play-pause,stop,next,previous}` `{player?}` ·
`POST /api/media/seek` `{player?, offset? | position?}` (seconds)

### Audio (pactl)

`GET /api/audio` · `POST /api/audio/volume` `{kind?: sink|source, name?, set? | delta?}` (capped at 150%) ·
`POST /api/audio/mute` `{kind?, name?, muted?}` (no `muted` = toggle) ·
`POST /api/audio/default` `{kind?, name}`

### System

`POST /api/system/notify` `{title, body?, urgency?, icon?, timeout_ms?}` ·
`GET /api/system/screenshot?output=DP-2` (PNG; no output = all monitors) ·
`GET /api/info` (version, uptime, current holds, route list) · `GET /api/health`

## Not here yet

- **Lock screen.** The live `base` shell has no lock IPC, and `loginctl lock-session`
  doesn't lock on this box.
- **Connectors.** `yk` → this API (with a ydotool fallback), then PC Remote, Deckboard and n8n.
- Window close/move/fullscreen weren't live-tested; focus, workspace state,
  dispatch errors and `moveto` were.
