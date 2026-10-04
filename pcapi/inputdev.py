"""Virtual keyboard + mouse on /dev/uinput, with per-client key holds.

Adapted from pc-remote's uinput_device.py, with three changes:
  * the device declares the whole keyboard range, not a hand-picked table,
    so any key yk can name actually reaches the compositor;
  * every press goes through one place that records it, so release_all()
    never misses a key (pc-remote's combo() didn't record its presses);
  * holds belong to a client and expire. A held key is only released when its
    last holder lets go, and a client that never sends the release (an SSH
    session that dropped) can't leave Shift down forever.
"""
import fcntl
import os
import struct
import threading
import time

from . import keys as K

_IOC_WRITE = 1


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (ord(typ) << 8) | nr | (size << 16)


UI_DEV_CREATE = _IOC(0, 'U', 1, 0)
UI_DEV_DESTROY = _IOC(0, 'U', 2, 0)
UI_DEV_SETUP = _IOC(_IOC_WRITE, 'U', 3, struct.calcsize('HHHH80sI'))
UI_SET_EVBIT = _IOC(_IOC_WRITE, 'U', 100, 4)
UI_SET_KEYBIT = _IOC(_IOC_WRITE, 'U', 101, 4)
UI_SET_RELBIT = _IOC(_IOC_WRITE, 'U', 102, 4)

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x0b, 0x0c

_EVENT_FMT = 'llHHi'

# Keyboard keys + the mouse buttons. Joystick/gamepad/touch BTN_* ranges are
# left out on purpose: advertising them makes libinput classify the device as
# a joystick or tablet and it stops behaving like a keyboard.
SUPPORTED = (set(range(1, 0x100)) | set(range(0x110, 0x118))
             | set(range(0x160, 0x2c0)))

MAX_TTL = 600.0
DEFAULT_TTL = 30.0


class InputDevice:
    NAME = b'pc-api virtual input'

    def __init__(self, path='/dev/uinput'):
        self.path = path
        self.fd = None
        self.lock = threading.RLock()
        self._down = set()            # codes this device currently has pressed
        self._holds = {}              # code -> {client: expiry}

    # ── lifecycle ────────────────────────────────────────────────────────────
    def open(self):
        with self.lock:
            if self.fd is not None:
                return
            fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                for ev in (EV_KEY, EV_REL, EV_SYN):
                    fcntl.ioctl(fd, UI_SET_EVBIT, ev)
                for rel in (REL_X, REL_Y, REL_WHEEL, REL_HWHEEL,
                            REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES):
                    fcntl.ioctl(fd, UI_SET_RELBIT, rel)
                for code in sorted(SUPPORTED):
                    fcntl.ioctl(fd, UI_SET_KEYBIT, code)
                setup = struct.pack('HHHH80sI', 0x03, 0x1d6b, 0x0105, 1,
                                    self.NAME.ljust(80, b'\0')[:80], 0)
                fcntl.ioctl(fd, UI_DEV_SETUP, setup)
                fcntl.ioctl(fd, UI_DEV_CREATE)
            except Exception:
                os.close(fd)
                raise
            self.fd = fd
            # Let udev/libinput attach before the first event, or it's lost.
            time.sleep(0.3)

    def close(self):
        with self.lock:
            if self.fd is None:
                return
            try:
                self.panic()
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None

    # ── raw ──────────────────────────────────────────────────────────────────
    def _emit(self, etype, code, value):
        if self.fd is None:
            self.open()
        os.write(self.fd, struct.pack(_EVENT_FMT, 0, 0, etype, code, value))

    def _syn(self):
        self._emit(EV_SYN, 0, 0)

    def _check(self, code):
        if code not in SUPPORTED:
            raise ValueError(f'keycode {code} is outside what the virtual '
                             'device declares (keyboard + mouse buttons only)')

    def _press(self, code):
        self._check(code)
        self._emit(EV_KEY, code, 1)
        self._syn()
        self._down.add(code)

    def _unpress(self, code):
        self._emit(EV_KEY, code, 0)
        self._syn()
        self._down.discard(code)

    def _held(self, code):
        return bool(self._holds.get(code))

    # ── keyboard ─────────────────────────────────────────────────────────────
    def combo(self, codes, delay=0.012):
        """Press codes in order, release in reverse. Keys that a client is
        holding are left alone, so `hold shift` + `combo shift+a` keeps Shift
        down afterwards."""
        with self.lock:
            pressed = []
            try:
                for c in codes:
                    self._check(c)
                    if self._held(c) or c in pressed:
                        continue
                    self._press(c)
                    pressed.append(c)
                    if delay:
                        time.sleep(delay)
            finally:
                for c in reversed(pressed):
                    self._unpress(c)

    def type_text(self, text, delay=0.006):
        """Type as US-QWERTY keystrokes; returns characters it couldn't map."""
        skipped = []
        shift = K.keycode('leftshift')
        with self.lock:
            shifted = False
            try:
                for ch in text:
                    entry = K.CHARMAP.get(ch)
                    if entry is None:
                        skipped.append(ch)
                        continue
                    code, need = entry
                    if need != shifted:
                        (self._press if need else self._unpress)(shift)
                        shifted = need
                    self._press(code)
                    self._unpress(code)
                    if delay:
                        time.sleep(delay)
            finally:
                if shifted and not self._held(shift):
                    self._unpress(shift)
        return skipped

    # ── holds ────────────────────────────────────────────────────────────────
    def hold(self, client, codes, ttl=DEFAULT_TTL):
        ttl = max(0.1, min(float(ttl), MAX_TTL))
        expiry = time.monotonic() + ttl
        with self.lock:
            for c in codes:
                self._check(c)
            for c in codes:
                holders = self._holds.setdefault(c, {})
                if not holders and c not in self._down:
                    self._press(c)
                holders[client] = expiry
        return ttl

    def release(self, client, codes=None):
        """Drop this client's holds (all of them when codes is None).
        A key goes up only once nobody holds it any more."""
        released = []
        with self.lock:
            targets = list(self._holds) if codes is None else codes
            for c in targets:
                holders = self._holds.get(c)
                if not holders or client not in holders:
                    continue
                del holders[client]
                if not holders:
                    del self._holds[c]
                    self._unpress(c)
                    released.append(c)
        return released

    def expire(self):
        now = time.monotonic()
        with self.lock:
            for c in list(self._holds):
                holders = self._holds[c]
                for client, exp in list(holders.items()):
                    if exp <= now:
                        del holders[client]
                if not holders:
                    del self._holds[c]
                    self._unpress(c)

    def holds(self):
        now = time.monotonic()
        with self.lock:
            return {c: {cl: round(exp - now, 1) for cl, exp in h.items()}
                    for c, h in self._holds.items()}

    def panic(self):
        """Everything up: every hold, every pressed key, every mouse button."""
        with self.lock:
            self._holds.clear()
            for c in set(self._down) | set(range(0x110, 0x118)):
                try:
                    self._emit(EV_KEY, c, 0)
                except OSError:
                    pass
            self._down.clear()
            try:
                self._syn()
            except OSError:
                pass

    # ── mouse ────────────────────────────────────────────────────────────────
    def move(self, dx, dy):
        dx, dy = int(dx), int(dy)
        if not (dx or dy):
            return
        with self.lock:
            if dx:
                self._emit(EV_REL, REL_X, dx)
            if dy:
                self._emit(EV_REL, REL_Y, dy)
            self._syn()

    def click(self, code, count=1, interval=0.04):
        with self.lock:
            for i in range(count):
                if i:
                    time.sleep(interval)
                if self._held(code):        # a held button: just let it be
                    continue
                self._press(code)
                self._unpress(code)

    def scroll(self, dy=0, dx=0):
        """dy > 0 scrolls up (kernel convention). Hi-res axes too, so GTK
        apps don't feel notchy."""
        dy, dx = int(dy), int(dx)
        if not (dy or dx):
            return
        with self.lock:
            if dy:
                self._emit(EV_REL, REL_WHEEL, dy)
                self._emit(EV_REL, REL_WHEEL_HI_RES, dy * 120)
            if dx:
                self._emit(EV_REL, REL_HWHEEL, dx)
                self._emit(EV_REL, REL_HWHEEL_HI_RES, dx * 120)
            self._syn()


def start_reaper(dev, interval=0.25):
    def loop():
        while True:
            time.sleep(interval)
            try:
                dev.expire()
            except Exception:
                pass
    t = threading.Thread(target=loop, name='hold-reaper', daemon=True)
    t.start()
    return t
