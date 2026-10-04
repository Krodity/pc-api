"""Hyprland, media, audio, notifications, screenshots.

All of these are thin wrappers over the CLI tools already on the box
(hyprctl, playerctl, pactl, notify-send, grim). Hyprland's environment is
re-resolved on every call, the same way ~/bin/hypr-live does it, because a
long-running service's inherited HYPRLAND_INSTANCE_SIGNATURE goes stale the
moment Hyprland restarts.
"""
import json
import os
import subprocess

RUNTIME = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'


class ToolError(RuntimeError):
    pass


def _run(argv, env=None, timeout=8, binary=False):
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout,
                           env=env or _base_env(), text=not binary)
    except FileNotFoundError:
        raise ToolError(f'{argv[0]} is not installed')
    except subprocess.TimeoutExpired:
        raise ToolError(f'{argv[0]} timed out')
    if r.returncode != 0:
        err = r.stderr if not binary else r.stderr.decode(errors='replace')
        if not err.strip() and not binary:
            err = r.stdout                 # hyprctl reports errors on stdout
        raise ToolError(f'{argv[0]} failed: {err.strip() or r.returncode}')
    return r.stdout


def _base_env():
    env = dict(os.environ)
    env.setdefault('XDG_RUNTIME_DIR', RUNTIME)
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path={RUNTIME}/bus')
    return env


# ── Hyprland ─────────────────────────────────────────────────────────────────
def hypr_env():
    env = _base_env()
    env.pop('HYPRLAND_INSTANCE_SIGNATURE', None)
    out = _run(['hyprctl', 'instances', '-j'], env=env)
    for inst in sorted(json.loads(out or '[]'), key=lambda i: -i.get('time', 0)):
        if os.path.isdir(f"/proc/{inst.get('pid')}"):
            env['HYPRLAND_INSTANCE_SIGNATURE'] = inst['instance']
            env['WAYLAND_DISPLAY'] = inst.get('wl_socket', 'wayland-1')
            return env
    raise ToolError('no running Hyprland instance')


def hyprctl_json(what):
    return json.loads(_run(['hyprctl', what, '-j'], env=hypr_env()) or 'null')


def dispatch(lua):
    """Run a Lua dispatcher, e.g. hl.dsp.focus({ workspace = "3" }).
    The Lua config wraps this as `return hl.dispatch(<lua>)`."""
    out = _run(['hyprctl', 'dispatch', lua], env=hypr_env()).strip()
    if out != 'ok':
        raise ToolError(out or 'dispatch returned nothing')
    return out


def lua_str(s):
    """Quote a Python string as a Lua string literal."""
    s = str(s)
    for a, b in (('\\', '\\\\'), ('"', '\\"'), ('\n', '\\n'), ('\r', '\\r'),
                 ('\0', '')):
        s = s.replace(a, b)
    return f'"{s}"'


def _addr(a):
    a = str(a)
    if not a.startswith('0x'):
        raise ValueError(f'bad window address {a!r}')
    return lua_str(f'address:{a}')


def hypr_state():
    clients = hyprctl_json('clients') or []
    active = hyprctl_json('activewindow') or {}
    return {
        'monitors': [{k: m.get(k) for k in (
            'id', 'name', 'description', 'x', 'y', 'width', 'height',
            'refreshRate', 'scale', 'focused', 'dpmsStatus')}
            | {'workspace': (m.get('activeWorkspace') or {}).get('name')}
            for m in hyprctl_json('monitors') or []],
        'workspaces': sorted(
            ({k: w.get(k) for k in ('id', 'name', 'monitor', 'windows')}
             for w in hyprctl_json('workspaces') or []),
            key=lambda w: w['id']),
        'active': active.get('address'),
        'windows': [{
            'address': c.get('address'),
            'class': c.get('class'),
            'title': c.get('title'),
            'workspace': (c.get('workspace') or {}).get('name'),
            'monitor': c.get('monitor'),
            'floating': c.get('floating'),
            'fullscreen': c.get('fullscreen'),
            'pid': c.get('pid'),
        } for c in clients if c.get('mapped', True)],
    }


def focus_workspace(ws):
    return dispatch(f'hl.dsp.focus({{ workspace = {lua_str(ws)} }})')


def focus_window(address):
    return dispatch(f'hl.dsp.focus({{ window = {_addr(address)} }})')


def close_window(address=None):
    arg = f'{{ window = {_addr(address)} }}' if address else ''
    return dispatch(f'hl.dsp.window.close({arg})')


def move_window(workspace, address=None, follow=False):
    parts = [f'workspace = {lua_str(workspace)}',
             f'follow = {"true" if follow else "false"}']
    if address:
        parts.append(f'window = {_addr(address)}')
    return dispatch(f'hl.dsp.window.move({{ {", ".join(parts)} }})')


def fullscreen(address=None):
    arg = f'{{ window = {_addr(address)} }}' if address else ''
    return dispatch(f'hl.dsp.window.fullscreen({arg})')


def launch(cmd):
    """Start a program the way a keybind would (honours [workspace N] rules)."""
    return dispatch(f'hl.dsp.exec_cmd({lua_str(cmd)})')


def cursor_to(x, y):
    """Absolute cursor position in global layout coordinates."""
    return dispatch(f'hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})')


def cursor_pos():
    x, y = _run(['hyprctl', 'cursorpos'], env=hypr_env()).split(',')
    return {'x': int(x), 'y': int(y)}


def screenshot(output=None):
    argv = ['grim']
    if output:
        argv += ['-o', str(output)]
    return _run(argv + ['-t', 'png', '-'], env=hypr_env(), timeout=15,
                binary=True)


# ── media (MPRIS via playerctl) ──────────────────────────────────────────────
_FMT = '\x1f'.join([
    '{{playerName}}', '{{playerInstance}}', '{{status}}', '{{title}}',
    '{{artist}}', '{{album}}', '{{mpris:length}}', '{{position}}',
    '{{volume}}', '{{mpris:artUrl}}'])

MEDIA_ACTIONS = {'play', 'pause', 'play-pause', 'stop', 'next', 'previous'}


def media_list():
    try:
        out = _run(['playerctl', '-a', 'metadata', '--format', _FMT])
    except ToolError as e:
        # playerctl's two wordings for "nothing is playing anywhere"
        if 'No players found' in str(e) or 'No player could' in str(e):
            return []
        raise
    players = []
    for line in out.splitlines():
        f = line.split('\x1f')
        if len(f) != 10:
            continue

        def num(v, scale=1.0):
            try:
                return round(int(v) / scale, 2)
            except ValueError:
                return None
        players.append({
            'name': f[0], 'player': f[1], 'status': f[2], 'title': f[3],
            'artist': f[4], 'album': f[5],
            'length': num(f[6], 1e6), 'position': num(f[7], 1e6),
            'volume': float(f[8]) if f[8] else None, 'art': f[9] or None,
        })
    return players


def media_action(action, player=None):
    if action not in MEDIA_ACTIONS:
        raise ValueError(f'unknown media action {action!r}')
    argv = ['playerctl'] + (['-p', player] if player else []) + [action]
    _run(argv)
    return 'ok'


def media_seek(player=None, offset=None, position=None):
    base = ['playerctl'] + (['-p', player] if player else [])
    if position is not None:
        _run(base + ['position', str(float(position))])
    elif offset is not None:
        o = float(offset)
        _run(base + ['position', f'{abs(o)}{"+" if o >= 0 else "-"}'])
    else:
        raise ValueError('seek needs offset or position')
    return 'ok'


# ── audio (PipeWire via pactl) ───────────────────────────────────────────────
def _pactl_json(*args):
    return json.loads(_run(['pactl', '-f', 'json', *args]) or 'null')


def _vol_pct(dev):
    vols = [int(str(c.get('value_percent', '0')).rstrip('%'))
            for c in (dev.get('volume') or {}).values()]
    return round(sum(vols) / len(vols)) if vols else None


def audio_state():
    info = _pactl_json('info')

    def describe(devs, default):
        return [{'name': d['name'], 'description': d.get('description'),
                 'volume': _vol_pct(d), 'muted': d.get('mute'),
                 'default': d['name'] == default}
                for d in devs if not d['name'].endswith('.monitor')]
    return {
        'sinks': describe(_pactl_json('list', 'sinks'),
                          info.get('default_sink_name')),
        'sources': describe(_pactl_json('list', 'sources'),
                            info.get('default_source_name')),
    }


def _target(kind, name):
    if kind not in ('sink', 'source'):
        raise ValueError("kind must be 'sink' or 'source'")
    return name or f'@DEFAULT_{kind.upper()}@'


def set_volume(kind='sink', name=None, set_pct=None, delta=None, cap=150):
    t = _target(kind, name)
    if set_pct is not None:
        v = max(0, min(int(set_pct), cap))
        _run(['pactl', f'set-{kind}-volume', t, f'{v}%'])
    elif delta is not None:
        cur = next((d['volume'] for d in audio_state()[kind + 's']
                    if (d['name'] == name if name else d['default'])), None)
        if cur is None:
            raise ToolError(f'no such {kind}')
        v = max(0, min(cur + int(delta), cap))
        _run(['pactl', f'set-{kind}-volume', t, f'{v}%'])
    else:
        raise ValueError('volume needs set or delta')
    return v


def set_mute(kind='sink', name=None, muted=None):
    state = 'toggle' if muted is None else ('1' if muted else '0')
    _run(['pactl', f'set-{kind}-mute', _target(kind, name), state])
    return 'ok'


def set_default(kind, name):
    _run(['pactl', f'set-default-{kind}', _target(kind, name)])
    return 'ok'


# ── notifications ────────────────────────────────────────────────────────────
def notify(title, body='', urgency='normal', icon=None, timeout_ms=None):
    if urgency not in ('low', 'normal', 'critical'):
        raise ValueError('urgency must be low, normal or critical')
    argv = ['notify-send', '-a', 'pc-api', '-u', urgency]
    if icon:
        argv += ['-i', icon]
    if timeout_ms is not None:
        argv += ['-t', str(int(timeout_ms))]
    _run(argv + ['--', str(title), str(body)])
    return 'ok'
