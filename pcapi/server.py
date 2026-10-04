"""HTTP front door. Stdlib only, JSON in and out.

Access control is two layers, neither of which depends on ufw being on:
  1. peer address: loopback or the tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48)
     only -- anything else gets 403 before auth is even looked at;
  2. a bearer token from ~/.config/pc-api/token (Authorization: Bearer ...,
     X-Token, or ?token=). /api/health is the only unauthenticated route.
"""
import ipaddress
import json
import logging
import os
import pathlib
import secrets
import signal
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import desktop as D
from . import keys as K
from .inputdev import InputDevice, start_reaper, DEFAULT_TTL

VERSION = '0.1.0'
log = logging.getLogger('pc-api')

CONF = pathlib.Path(os.environ.get('XDG_CONFIG_HOME',
                                   pathlib.Path.home() / '.config')) / 'pc-api'
TOKEN_FILE = CONF / 'token'
ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    '127.0.0.0/8', '::1/128', '100.64.0.0/10', 'fd7a:115c:a1e0::/48')]
STARTED = time.time()


def load_token():
    CONF.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(tok + '\n')
    log.info('minted a new token at %s', TOKEN_FILE)
    return tok


class HTTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _need(body, key):
    if key not in body or body[key] in (None, ''):
        raise HTTPError(400, f'missing "{key}"')
    return body[key]


def _combos(value):
    """'ctrl+c' or ['ctrl+a', 'ctrl+c'] -> list of code lists."""
    items = value if isinstance(value, list) else [value]
    return [K.parse_combo(i) for i in items]


def _flat_codes(value):
    out = []
    for combo in _combos(value):
        out.extend(c for c in combo if c not in out)
    return out


class App:
    """Route table. Each handler takes (body, query) and returns JSON-able
    data, or (bytes, content_type) for binary responses."""

    def __init__(self, dev: InputDevice, token: str):
        self.dev = dev
        self.token = token
        self.routes = {
            ('GET', '/api/health'): self.health,
            ('GET', '/api/info'): self.info,
            # input
            ('POST', '/api/input/key'): self.key,
            ('POST', '/api/input/hold'): self.hold,
            ('POST', '/api/input/release'): self.release,
            ('POST', '/api/input/panic'): self.panic,
            ('GET', '/api/input/holds'): self.holds,
            ('POST', '/api/input/type'): self.type_text,
            ('POST', '/api/input/click'): self.click,
            ('POST', '/api/input/move'): self.move,
            ('POST', '/api/input/moveto'): self.moveto,
            ('GET', '/api/input/cursor'): lambda b, q: D.cursor_pos(),
            ('POST', '/api/input/scroll'): self.scroll,
            ('GET', '/api/input/keys'): self.key_names,
            # hyprland
            ('GET', '/api/hypr/state'): lambda b, q: D.hypr_state(),
            ('POST', '/api/hypr/dispatch'):
                lambda b, q: D.dispatch(_need(b, 'lua')),
            ('POST', '/api/hypr/workspace'):
                lambda b, q: D.focus_workspace(_need(b, 'workspace')),
            ('POST', '/api/hypr/focus'):
                lambda b, q: D.focus_window(_need(b, 'address')),
            ('POST', '/api/hypr/close'):
                lambda b, q: D.close_window(b.get('address')),
            ('POST', '/api/hypr/move'):
                lambda b, q: D.move_window(_need(b, 'workspace'),
                                           b.get('address'),
                                           bool(b.get('follow'))),
            ('POST', '/api/hypr/fullscreen'):
                lambda b, q: D.fullscreen(b.get('address')),
            ('POST', '/api/hypr/launch'):
                lambda b, q: D.launch(_need(b, 'cmd')),
            # media
            ('GET', '/api/media'): lambda b, q: D.media_list(),
            ('POST', '/api/media/seek'):
                lambda b, q: D.media_seek(b.get('player'), b.get('offset'),
                                          b.get('position')),
            ('POST', '/api/media/volume'):
                lambda b, q: {'volume': D.media_volume(
                    b.get('player'), b.get('set'), b.get('delta'))},
            # audio
            ('GET', '/api/audio'): lambda b, q: D.audio_state(),
            ('POST', '/api/audio/volume'):
                lambda b, q: {'volume': D.set_volume(
                    b.get('kind', 'sink'), b.get('name'), b.get('set'),
                    b.get('delta'))},
            ('POST', '/api/audio/mute'):
                lambda b, q: D.set_mute(b.get('kind', 'sink'), b.get('name'),
                                        b.get('muted')),
            ('POST', '/api/audio/default'):
                lambda b, q: D.set_default(b.get('kind', 'sink'),
                                           _need(b, 'name')),
            # system
            ('POST', '/api/system/notify'):
                lambda b, q: D.notify(_need(b, 'title'), b.get('body', ''),
                                      b.get('urgency', 'normal'),
                                      b.get('icon'), b.get('timeout_ms')),
            ('GET', '/api/system/screenshot'):
                lambda b, q: (D.screenshot(q.get('output')), 'image/png'),
        }
        for action in D.MEDIA_ACTIONS:
            self.routes[('POST', f'/api/media/{action}')] = (
                lambda b, q, a=action: D.media_action(a, b.get('player')))

    # ── meta ─────────────────────────────────────────────────────────────────
    def health(self, body, q):
        return {'ok': True, 'version': VERSION}

    def info(self, body, q):
        return {
            'version': VERSION,
            'host': socket.gethostname(),
            'uptime_s': round(time.time() - STARTED),
            'input_device': self.dev.fd is not None,
            'holds': self.dev.holds(),
            'routes': sorted(f'{m} {p}' for m, p in self.routes),
        }

    # ── input ────────────────────────────────────────────────────────────────
    @staticmethod
    def _client(body):
        return str(body.get('client') or 'default')[:64]

    def key(self, body, q):
        """{keys: "ctrl+t" | ["ctrl+a", "ctrl+c"], delay_ms: 12, gap_ms: 0}
        delay_ms is between presses inside a combo, gap_ms between combos
        (yk's -d sets both)."""
        combos = _combos(_need(body, 'keys'))
        delay = float(body.get('delay_ms', 12)) / 1000
        gap = float(body.get('gap_ms', body.get('delay_ms', 0))) / 1000
        for i, codes in enumerate(combos):
            if i and gap:
                time.sleep(gap)
            self.dev.combo(codes, delay)
        return {'sent': len(combos)}

    def hold(self, body, q):
        """{keys: "shift" | "ctrl+alt" | [...], ttl: 30, client: "sshremote"}
        Holding again refreshes the TTL."""
        codes = _flat_codes(_need(body, 'keys'))
        ttl = self.dev.hold(self._client(body), codes,
                            body.get('ttl', DEFAULT_TTL))
        return {'held': codes, 'ttl': ttl}

    def release(self, body, q):
        """{keys?: ..., client: ...}  no keys = everything this client holds."""
        codes = _flat_codes(body['keys']) if body.get('keys') else None
        return {'released': self.dev.release(self._client(body), codes)}

    def panic(self, body, q):
        self.dev.panic()
        return 'ok'

    def holds(self, body, q):
        return self.dev.holds()

    def type_text(self, body, q):
        text = str(body.get('text') or '')
        if len(text) > 20000:
            raise HTTPError(413, 'text too long (20000 chars max)')
        skipped = self.dev.type_text(text,
                                     float(body.get('delay_ms', 6)) / 1000)
        return {'typed': len(text) - len(skipped),
                'skipped': ''.join(sorted(set(skipped)))}

    def click(self, body, q):
        code = K.button_code(body.get('button', 'left'))
        count = max(1, min(int(body.get('count', 1)), 10))
        self.dev.click(code, count)
        return 'ok'

    def move(self, body, q):
        self.dev.move(body.get('dx', 0), body.get('dy', 0))
        return 'ok'

    def moveto(self, body, q):
        return D.cursor_to(_need(body, 'x'), _need(body, 'y'))

    def scroll(self, body, q):
        self.dev.scroll(body.get('dy', 0), body.get('dx', 0))
        return 'ok'

    def key_names(self, body, q):
        return K.search(q.get('filter', ''))


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f'pc-api/{VERSION}'
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            log.debug('%s %s', self.client_address[0], fmt % args)

        def _send(self, code, data, ctype='application/json'):
            if ctype == 'application/json':
                data = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def _peer_ok(self):
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            if getattr(ip, 'ipv4_mapped', None):
                ip = ip.ipv4_mapped
            return any(ip in n for n in ALLOWED_NETS)

        def _token_ok(self, query):
            auth = self.headers.get('Authorization', '')
            given = (auth[7:].strip() if auth.lower().startswith('bearer ')
                     else self.headers.get('X-Token')
                     or query.get('token', ''))
            return secrets.compare_digest(str(given), app.token)

        def _handle(self, method):
            url = urllib.parse.urlsplit(self.path)
            query = {k: v[-1] for k, v in
                     urllib.parse.parse_qs(url.query).items()}
            path = url.path.rstrip('/') or '/'
            try:
                if not self._peer_ok():
                    raise HTTPError(403, 'not from loopback or the tailnet')
                route = app.routes.get((method, path))
                if route is None:
                    known = any(p == path for _, p in app.routes)
                    raise HTTPError(405 if known else 404,
                                    'method not allowed' if known
                                    else 'no such route')
                if path != '/api/health' and not self._token_ok(query):
                    raise HTTPError(401, 'bad or missing token')
                body = {}
                n = int(self.headers.get('Content-Length') or 0)
                if n > 1_000_000:
                    raise HTTPError(413, 'body too large')
                if n:
                    try:
                        body = json.loads(self.rfile.read(n) or b'{}')
                    except ValueError:
                        raise HTTPError(400, 'body is not JSON')
                    if not isinstance(body, dict):
                        raise HTTPError(400, 'body must be a JSON object')
                result = route(body, query)
                if isinstance(result, tuple):
                    self._send(200, result[0], result[1])
                else:
                    self._send(200, {'ok': True, 'result': result})
                if method == 'POST':
                    log.info('%s %s %s', self.client_address[0], path,
                             body.get('client', ''))
            except HTTPError as e:
                self._send(e.code, {'ok': False, 'error': str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, {'ok': False, 'error': str(e)})
            except D.ToolError as e:
                self._send(502, {'ok': False, 'error': str(e)})
            except Exception as e:  # noqa: BLE001 -- report, don't die
                log.exception('handler failed')
                self._send(500, {'ok': False,
                                 'error': f'{type(e).__name__}: {e}'})

        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

    return Handler


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # Accept IPv4 too; the peer filter does the real gatekeeping.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main():
    logging.basicConfig(level=os.environ.get('PC_API_LOG', 'INFO'),
                        format='%(levelname)s %(name)s: %(message)s')
    port = int(os.environ.get('PC_API_PORT', 8782))
    dev = InputDevice()
    try:
        dev.open()
    except OSError as e:
        log.error('cannot open /dev/uinput (%s); input routes will retry', e)
    start_reaper(dev)
    app = App(dev, load_token())
    srv = DualStackServer(('::', port), make_handler(app))
    log.info('pc-api %s listening on :%d', VERSION, port)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()       # releases anything still held before exiting
