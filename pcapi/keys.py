"""Key names -> kernel keycodes.

Every name `yk` accepts resolves here too, so a yk call can be forwarded to the
API verbatim: yk's short aliases, pc-remote's names, and then any KEY_*/BTN_*
symbol from the kernel header (KEY_FOO -> "foo", BTN_FOO -> "btn_foo").
"""
import re

HEADER = '/usr/include/linux/input-event-codes.h'

# yk's alias table, kept identical so the redirect is transparent.
YK_ALIASES = {
    'esc': 1, 'escape': 1, 'minus': 12, '-': 12, 'equal': 13, '=': 13,
    'backspace': 14, 'bs': 14, 'tab': 15,
    'lbracket': 26, 'rbracket': 27, 'enter': 28, 'return': 28,
    'ctrl': 29, 'lctrl': 29, 'control': 29, 'semicolon': 39,
    'apostrophe': 40, 'quote': 40,
    'grave': 41, 'backtick': 41, 'shift': 42, 'lshift': 42, 'backslash': 43,
    'comma': 51, ',': 51, 'dot': 52, 'period': 52, '.': 52, 'slash': 53,
    '/': 53, 'rshift': 54,
    'alt': 56, 'lalt': 56, 'space': 57, 'capslock': 58, 'caps': 58,
    'rctrl': 97, 'print': 99, 'prtsc': 99, 'printscreen': 99, 'ralt': 100,
    'altgr': 100,
    'pgup': 104, 'pageup': 104, 'pgdn': 109, 'pagedown': 109, 'ins': 110,
    'del': 111,
    'super': 125, 'meta': 125, 'win': 125, 'lsuper': 125, 'lmeta': 125,
    'rsuper': 126, 'rmeta': 126,
    'voldown': 114, 'volup': 115, 'next': 163, 'prev': 165, 'play': 164,
    'stop': 166,
    'brightdown': 224, 'brightup': 225, 'iso': 86, 'scrolllock': 70,
    'numlock': 69,
}

# Names the PC Remote app sends that the header doesn't spell the same way.
EXTRA_ALIASES = {
    'bksp': 14, 'leftbrace': 26, 'rightbrace': 27, 'ctrl_l': 29,
    'menu': 127, 'pause': 119,
    'volumedown': 114, 'volumeup': 115, 'mute': 113,
    'playpause': 164, 'stopcd': 166, 'previoussong': 165, 'nextsong': 163,
    'brightnessdown': 224, 'brightnessup': 225,
    # Mouse buttons by a name that can't collide with the arrow keys.
    'mouse_left': 0x110, 'mouse_right': 0x111, 'mouse_middle': 0x112,
    'mouse_back': 0x113, 'mouse_forward': 0x114,
}

# Mouse button names for /click and friends ("left" here is not the arrow).
BUTTONS = {
    'left': 0x110, 'l': 0x110, '1': 0x110,
    'right': 0x111, 'r': 0x111, '2': 0x111,
    'middle': 0x112, 'm': 0x112, '3': 0x112,
    'back': 0x113, 'side': 0x113, '4': 0x113,
    'forward': 0x114, 'extra': 0x114, '5': 0x114,
}

MODIFIER_CODES = {29, 42, 54, 56, 97, 100, 125, 126}


def _parse_header(path=HEADER):
    """KEY_*/BTN_* symbols from the kernel header, aliases resolved."""
    raw = {}
    try:
        text = open(path).read()
    except OSError:
        return {}
    for m in re.finditer(r'^#define\s+((?:KEY|BTN)_\w+)\s+(\S+)', text, re.M):
        raw[m.group(1)] = m.group(2)

    def value(sym, depth=0):
        v = raw.get(sym)
        if v is None or depth > 5:
            return None
        if re.fullmatch(r'0x[0-9a-fA-F]+|\d+', v):
            return int(v, 0)
        return value(v, depth + 1)          # e.g. KEY_HANGUEL -> KEY_HANGEUL

    out = {}
    for sym in raw:
        v = value(sym)
        if v is None or sym in ('KEY_MAX', 'KEY_CNT', 'KEY_RESERVED'):
            continue
        name = sym[4:].lower() if sym.startswith('KEY_') else sym.lower()
        out.setdefault(name, v)
    return out


HEADER_NAMES = _parse_header()


def keycode(name):
    """Resolve one key name (or a raw decimal/0x code) to a keycode."""
    n = str(name).strip().lower()
    if not n:
        raise ValueError('empty key name')
    if re.fullmatch(r'\d+', n) and len(n) > 1:   # "1" is the digit key
        return int(n)
    if re.fullmatch(r'0x[0-9a-f]+', n):
        return int(n, 16)
    for table in (YK_ALIASES, EXTRA_ALIASES, HEADER_NAMES):
        if n in table:
            return table[n]
    raise ValueError(f"unknown key '{name}'")


def parse_combo(spec):
    """'ctrl+shift+t' -> [29, 42, 20]. A lone '+' or '-' still works."""
    s = str(spec).strip()
    if s in ('+', '-'):
        return [keycode('equal' if s == '+' else 'minus')]
    parts = [p for p in s.replace(' ', '').split('+') if p]
    if not parts:
        raise ValueError(f'empty combo: {spec!r}')
    return [keycode(p) for p in parts]


def button_code(name):
    code = BUTTONS.get(str(name).strip().lower())
    if code is None:
        raise ValueError(f"unknown mouse button '{name}'")
    return code


def search(fragment=''):
    """Names containing fragment, for the /api/input/keys listing."""
    f = fragment.lower()
    names = {**HEADER_NAMES, **EXTRA_ALIASES, **YK_ALIASES}
    return {k: v for k, v in sorted(names.items()) if f in k}


# US QWERTY: character -> (keycode, needs_shift)
_UNSHIFTED = {
    '`': 41, '1': 2, '2': 3, '3': 4, '4': 5, '5': 6, '6': 7, '7': 8, '8': 9,
    '9': 10, '0': 11, '-': 12, '=': 13,
    '[': 26, ']': 27, '\\': 43, ';': 39, "'": 40, ',': 51, '.': 52, '/': 53,
    ' ': 57, '\t': 15, '\n': 28, '\r': 28,
}
_SHIFTED = {
    '~': 41, '!': 2, '@': 3, '#': 4, '$': 5, '%': 6, '^': 7, '&': 8, '*': 9,
    '(': 10, ')': 11, '_': 12, '+': 13,
    '{': 26, '}': 27, '|': 43, ':': 39, '"': 40, '<': 51, '>': 52, '?': 53,
}
CHARMAP = {c: (k, False) for c, k in _UNSHIFTED.items()}
CHARMAP.update({c: (k, True) for c, k in _SHIFTED.items()})
for _c in 'abcdefghijklmnopqrstuvwxyz':
    CHARMAP[_c] = (HEADER_NAMES.get(_c) or keycode(_c), False)
    CHARMAP[_c.upper()] = (CHARMAP[_c][0], True)
