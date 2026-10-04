#!/usr/bin/env python3
"""Point Deckboard's buttons at pc-api.

Rewrites `run-command` buttons in Deckboard's SQLite database:

  hypr-live '<lua>'               -> pcctl dispatch '<lua>'      (same Lua)
  hypr-live -- wtype -M ctrl -k 4 -> yk ctrl+4                   (wtype -> yk)
  playerctl [-p P] play-pause     -> pcctl toggle [P]            (and next/prev/stop/
  playerctl [-p P] position 10-   -> pcctl seek -10 [P]           seek/player volume)
  wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%+ -> pcctl vol +5
  key-type buttons (X11 injection, dead on Wayland) -> run-command yk

Anything it doesn't recognise is left exactly as it was.

  migrate.py [--db PATH]            dry run: print old -> new
  migrate.py --apply [--db PATH]    write it (Deckboard must be stopped; backs up first)
"""
import argparse
import json
import pathlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

HOME = pathlib.Path.home()
BIN = HOME / '.local' / 'bin'
PCCTL = str(BIN / 'pcctl')
YK = str(BIN / 'yk')

# wtype keysyms / Deckboard key names -> yk names
KEYSYMS = {
    'return': 'enter', 'enter': 'enter', 'escape': 'esc', 'esc': 'esc',
    'space': 'space', 'tab': 'tab', 'backspace': 'backspace',
    'delete': 'delete', 'up': 'up', 'down': 'down', 'left': 'left',
    'right': 'right', 'home': 'home', 'end': 'end', 'prior': 'pageup',
    'next': 'pagedown', 'page_up': 'pageup', 'page_down': 'pagedown',
    'slash': 'slash', 'minus': 'minus', 'equal': 'equal', 'comma': 'comma',
    'period': 'dot',
}
MODS = {'ctrl': 'ctrl', 'control': 'ctrl', 'alt': 'alt', 'shift': 'shift',
        'logo': 'super', 'super': 'super', 'win': 'super', 'meta': 'super',
        'command': 'super', 'cmd': 'super', 'altgr': 'altgr'}


def ykname(sym):
    s = sym.lower()
    if s in KEYSYMS:
        return KEYSYMS[s]
    if len(s) == 1 and (s.isalnum()):
        return s
    if s.startswith('f') and s[1:].isdigit():
        return s
    raise ValueError(f'no yk name for keysym {sym!r}')


def from_wtype(args):
    """wtype argv -> list of yk argv lists."""
    out, held, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a in ('-M', '-m', '-k', '-P', '-p'):
            if i + 1 >= len(args):
                raise ValueError('dangling wtype flag')
            v = args[i + 1]
            i += 2
            if a == '-M':
                held.append(MODS[v.lower()])
            elif a == '-m':
                m = MODS[v.lower()]
                if m in held:
                    held.remove(m)
            elif a == '-k':
                out.append(['+'.join(held + [ykname(v)])])
            else:
                raise ValueError('wtype -P/-p (press/release) not converted')
        elif a.startswith('-'):
            raise ValueError(f'wtype flag {a} not converted')
        else:
            if held:
                raise ValueError('typing text with modifiers held')
            out.append(['type', a])
            i += 1
    if held:
        raise ValueError('wtype leaves modifiers held')
    return out


def join(cmds):
    return ' && '.join(shlex.join(c) for c in cmds)


def from_playerctl(args):
    player = []
    if args[:1] == ['-p'] and len(args) > 2:
        player, args = [args[1]], args[2:]
    elif args and args[0].startswith('--player='):
        player, args = [args[0].split('=', 1)[1]], args[1:]
    if not args:
        raise ValueError('bare playerctl')
    verb = args[0]
    simple = {'play-pause': 'toggle', 'play': 'play', 'pause': 'pause',
              'next': 'next', 'previous': 'prev', 'stop': 'stop'}
    if verb in simple and len(args) == 1:
        return [PCCTL, simple[verb]] + player
    if verb == 'position' and len(args) == 2:
        v = args[1]
        if v[-1] in '+-':
            return [PCCTL, 'seek', f'{v[-1]}{v[:-1]}'] + player
        return [PCCTL, 'seek', f'={v}'] + player
    if verb == 'volume' and len(args) == 2:
        v = args[1]
        if v[-1] in '+-':
            pct = round(float(v[:-1]) * 100)
            return [PCCTL, 'pvol', f'{v[-1]}{pct}'] + player
        return [PCCTL, 'pvol', str(round(float(v) * 100))] + player
    raise ValueError(f'playerctl {verb} not converted')


def from_wpctl(args):
    if (len(args) == 3 and args[0] == 'set-volume'
            and args[1] in ('@DEFAULT_AUDIO_SINK@', '@DEFAULT_SINK@')):
        v = args[2]
        if v.endswith(('%+', '%-')):
            return [PCCTL, 'vol', f'{v[-1]}{v[:-2]}']
        if v.endswith('%'):
            return [PCCTL, 'vol', v[:-1]]
    if args[:2] == ['set-mute', '@DEFAULT_AUDIO_SINK@'] and args[2:] == ['toggle']:
        return [PCCTL, 'mute']
    raise ValueError('wpctl form not converted')


def convert_command(cmd):
    """One shell command line -> new command line, or None to leave it."""
    try:
        t = shlex.split(cmd)
    except ValueError:
        return None
    if not t or any(x in cmd for x in ('|', ';', '&&', '$(', '`', '>')):
        return None
    tool = t[0].rsplit('/', 1)[-1]
    try:
        if tool == 'hypr-live':
            if t[1:2] == ['--']:
                if len(t) > 2 and t[2].rsplit('/', 1)[-1] == 'wtype':
                    return join([[YK] + c for c in from_wtype(t[3:])])
                return None
            return shlex.join([PCCTL, 'dispatch', ' '.join(t[1:])])
        if tool == 'wtype':
            return join([[YK] + c for c in from_wtype(t[1:])])
        if tool == 'playerctl':
            return shlex.join(from_playerctl(t[1:]))
        if tool == 'wpctl':
            return shlex.join(from_wpctl(t[1:]))
    except (ValueError, KeyError, IndexError):
        return None
    return None


def convert_key_button(chord):
    """Deckboard `key` type ("CONTROL+4", "ENTER") -> yk command."""
    parts = []
    for p in chord.split('+'):
        p = p.strip()
        parts.append(MODS.get(p.lower()) or ykname(p))
    return shlex.join([YK, '+'.join(parts)])


def plan(db):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT s.id, b.name, s.title, s.icon, s.type, s.command "
                       "FROM Shortcuts s LEFT JOIN Boards b ON b.id = s.board_id "
                       "ORDER BY s.board_id, s.y, s.x").fetchall()
    con.close()
    changes, skipped = [], []
    for sid, board, title, icon, typ, command in rows:
        label = f"{board}/{title or '(' + (icon or '?') + ')'}"
        if typ == 'run-command':
            try:
                old = json.loads(command)['commandAction'].strip()
            except (ValueError, KeyError, TypeError):
                continue
            new = convert_command(old)
            if new and new != old:
                changes.append((sid, label, typ, old, 'run-command',
                                json.dumps({'commandAction': new})))
            elif not any(old.startswith(p) for p in (PCCTL, YK)):
                skipped.append((label, old))
        elif typ == 'key':
            try:
                new = convert_key_button(command)
            except (ValueError, KeyError):
                skipped.append((label, f'key {command}'))
                continue
            changes.append((sid, label, typ, f'[key] {command}', 'run-command',
                            json.dumps({'commandAction': new})))
    return changes, skipped


def deckboard_running():
    r = subprocess.run(['systemctl', '--user', 'is-active', 'deckboard.service'],
                       capture_output=True, text=True)
    if r.stdout.strip() == 'active':
        return True
    return subprocess.run(['pgrep', '-x', 'deckboard'],
                          capture_output=True).returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=str(HOME / 'deckboard' / 'database.db'))
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    changes, skipped = plan(a.db)
    for _, label, _, old, _, new in changes:
        print(f'{label}\n  - {old}\n  + {json.loads(new)["commandAction"]}')
    if skipped:
        print('\nleft as-is:')
        for label, old in skipped:
            print(f'  {label}: {old}')
    print(f'\n{len(changes)} to change, {len(skipped)} left as-is')
    if not a.apply or not changes:
        if changes:
            print('dry run: nothing written (use --apply)')
        return
    if deckboard_running():
        sys.exit('Deckboard is running and holds the DB open: '
                 'systemctl --user stop deckboard.service, then re-run')
    backup = f'{a.db}.bak.{int(time.time())}'
    shutil.copy2(a.db, backup)
    con = sqlite3.connect(a.db)
    with con:
        for sid, _, _, _, typ, new in changes:
            con.execute('UPDATE Shortcuts SET type = ?, command = ? WHERE id = ?',
                        (typ, new, sid))
    con.close()
    print(f'written. backup: {backup}')


if __name__ == '__main__':
    main()
