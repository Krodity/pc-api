# pc-api

One small HTTP API for controlling a Linux desktop: **keyboard and mouse
input, Hyprland windows and workspaces, media players, audio, notifications
and screenshots**. Point your phone remotes, stream decks, macro pads or
automations (n8n, Home Assistant, shortcuts…) at one endpoint instead of each
one carrying its own backend.

- Python 3 **standard library only**, no pip installs
- Input goes through its own **`/dev/uinput`** virtual device, so it works at
  the kernel level (any compositor, X11 or a bare VT). Per-client **holds
  with TTLs** mean a remote that drops off Wi-Fi can't leave a key stuck down
- Runs as a systemd **user** service on port **8782**
- Only loopback and **Tailscale** peers are let in, plus a bearer token

```bash
T=$(cat ~/.config/pc-api/token)
curl -H "Authorization: Bearer $T" -d '{"keys":"ctrl+shift+t"}' localhost:8782/api/input/key
curl -H "Authorization: Bearer $T" -d '{"text":"hello"}'       localhost:8782/api/input/type
curl -H "Authorization: Bearer $T" -X POST                      localhost:8782/api/media/play-pause
curl -H "Authorization: Bearer $T" -d '{"delta":5}'             localhost:8782/api/audio/volume
```

---

## Requirements

| Feature | Needs |
|---|---|
| Server | Linux, systemd, Python 3.10+ |
| Input routes | Write access to `/dev/uinput` (a `uaccess` udev rule, below) |
| Hyprland routes | **Hyprland with a Lua config.** Dispatches use the `hl.dsp.*` API through `hyprctl dispatch`. On a classic `hyprland.conf` setup these routes return errors, and everything else still works |
| Media | `playerctl` (MPRIS) |
| Audio | `pactl` (PipeWire or PulseAudio) |
| Notifications | `notify-send` (libnotify) |
| Screenshots | `grim` (Wayland) |
| Remote access | [Tailscale](https://tailscale.com/). Without it, only loopback is allowed |

A missing tool only breaks its own routes, which return 502.

## Install

```bash
git clone https://github.com/Krodity/pc-api.git
cd pc-api
./install.sh
```

The installer writes `~/.config/systemd/user/pc-api.service` pointing at your
clone, enables and starts it, and warns about any missing tools or
`/dev/uinput` access. On first start the server creates a random token in
`~/.config/pc-api/token` (mode 0600).

uinput access, if the installer warned about it:

```bash
echo 'KERNEL=="uinput", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/50-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger     # then log out and back in
```

Day to day:

```bash
systemctl --user {status,restart} pc-api
journalctl --user -u pc-api -f
python3 -m unittest discover -s tests      # no /dev/uinput needed
```

Settings (environment variables, set with `systemctl --user edit pc-api`):
`PC_API_PORT` (8782), `PC_API_LOG` (INFO), `XDG_CONFIG_HOME` (where `pc-api/token` lives).

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

**Key names**: friendly aliases (`lctrl`, `volup`, `next`, `bs`, `backtick`…),
names like `enter`, `super`, `playpause`, any kernel `KEY_*` symbol
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

The live Hyprland instance is looked up again on every call, so restarting
Hyprland doesn't leave the service talking to a dead socket.

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

- **Lock screen**: there's no reliable cross-shell lock IPC yet.
- **Client connectors**: wrappers so existing tools (a ydotool-style CLI,
  phone remotes, stream decks, n8n) call this API.
- Window close/move/fullscreen haven't been live-tested. Focus, workspace
  state, dispatch errors and `moveto` have.

## Security

The token is effectively **remote control of your desktop**:
`/api/hypr/launch` and `/api/hypr/dispatch` can start any program, and the
input routes can type anything. Treat it like an SSH key. The peer filter
works on its own, without a firewall, but the server listens on all
interfaces (`::`), so keep the filter's address list (`pcapi/server.py`)
limited to networks you trust.

## License

MIT. See [LICENSE](LICENSE).
