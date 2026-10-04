"""Key parsing + hold bookkeeping, against a fake device (no /dev/uinput)."""
import time
import unittest

from pcapi import keys as K
from pcapi.inputdev import InputDevice


class FakeDevice(InputDevice):
    def __init__(self):
        super().__init__()
        self.fd = -1
        self.events = []

    def _emit(self, etype, code, value):
        if etype == 1:
            self.events.append((code, value))

    def pressed(self):
        state = {}
        for code, v in self.events:
            state[code] = v
        return {c for c, v in state.items() if v}


class Keys(unittest.TestCase):
    def test_yk_aliases(self):
        self.assertEqual(K.parse_combo('ctrl+shift+t'), [29, 42, 20])
        self.assertEqual(K.keycode('volup'), 115)
        self.assertEqual(K.keycode('lsuper'), 125)

    def test_header_names(self):
        self.assertEqual(K.keycode('f24'), 194)
        self.assertEqual(K.keycode('btn_left'), 0x110)
        self.assertEqual(K.keycode('KEY_A'.lower()[4:]), 30)

    def test_digits_and_raw_codes(self):
        self.assertEqual(K.keycode('1'), 2)      # the digit key
        self.assertEqual(K.keycode('30'), 30)    # a raw code
        self.assertEqual(K.keycode('0x1e'), 30)

    def test_symbols(self):
        self.assertEqual(K.parse_combo('ctrl+-'), [29, 12])
        self.assertEqual(K.parse_combo('+'), [13])

    def test_unknown(self):
        with self.assertRaises(ValueError):
            K.keycode('nope')

    def test_charmap(self):
        self.assertEqual(K.CHARMAP['A'], (30, True))
        self.assertEqual(K.CHARMAP['?'], (53, True))


class Holds(unittest.TestCase):
    def setUp(self):
        self.d = FakeDevice()

    def test_combo_releases_in_reverse(self):
        self.d.combo([29, 46], delay=0)
        self.assertEqual(self.d.events, [(29, 1), (46, 1), (46, 0), (29, 0)])

    def test_combo_leaves_held_key_down(self):
        self.d.hold('a', [42])
        self.d.combo([42, 30], delay=0)
        self.assertEqual(self.d.pressed(), {42})

    def test_shared_hold_needs_both_releases(self):
        self.d.hold('a', [42])
        self.d.hold('b', [42])
        self.assertEqual(self.d.events.count((42, 1)), 1)
        self.d.release('a')
        self.assertIn(42, self.d.pressed())
        self.d.release('b')
        self.assertNotIn(42, self.d.pressed())

    def test_release_only_own_keys(self):
        self.d.hold('a', [42])
        self.d.hold('b', [29])
        self.d.release('a')
        self.assertEqual(self.d.pressed(), {29})

    def test_expiry(self):
        self.d.hold('a', [42], ttl=0.1)
        time.sleep(0.15)
        self.d.expire()
        self.assertEqual(self.d.pressed(), set())
        self.assertEqual(self.d.holds(), {})

    def test_panic(self):
        self.d.hold('a', [42, 29])
        self.d.panic()
        self.assertEqual(self.d.pressed(), set())

    def test_combo_failure_releases(self):
        with self.assertRaises(ValueError):
            self.d.combo([29, 0x130], delay=0)   # BTN_SOUTH isn't declared
        self.assertEqual(self.d.pressed(), set())

    def test_type_shift(self):
        self.d.type_text('aB', delay=0)
        self.assertEqual(self.d.pressed(), set())
        self.assertIn((42, 1), self.d.events)

    def test_type_skips_unmappable(self):
        self.assertEqual(self.d.type_text('é', delay=0), ['é'])


if __name__ == '__main__':
    unittest.main()
