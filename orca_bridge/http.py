"""Authenticated loopback HTTP transport for the existing four MCP tools."""

import hmac
import ipaddress
import json
import logging
import os
import re
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from .bridge import Bridge, BridgeError, load_config, private_read
from .server import create_server

LOOPBACK = '127.0.0.1'
TOKEN_NAME = 'ORCA_BRIDGE_HTTP_TOKEN'
HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?\Z')
RPC_METHODS = {'initialize', 'ping', 'notifications/initialized', 'notifications/cancelled',
               'tools/list', 'tools/call', 'resources/list', 'resources/templates/list', 'prompts/list'}
TOOLS = {'status', 'send_request', 'list_replies', 'get_reply'}


@dataclass(frozen=True)
class HttpSettings:
    port: int
    allowed_hosts: tuple
    requests_per_minute: int = 60
    max_request_bytes: int = 65536

    def __post_init__(self):
        if (type(self.port) is not int or not 1024 <= self.port <= 65535
                or type(self.requests_per_minute) is not int or not 1 <= self.requests_per_minute <= 600
                or type(self.max_request_bytes) is not int or not 1024 <= self.max_request_bytes <= 1048576
                or not isinstance(self.allowed_hosts, (list, tuple)) or not 1 <= len(self.allowed_hosts) <= 20
                or any(not isinstance(host, str) or not HOST.fullmatch(host)
                       or (':' in host and not 1 <= int(host.rsplit(':', 1)[1]) <= 65535)
                       for host in self.allowed_hosts)):
            raise BridgeError('Invalid private HTTP settings')


def load_settings(path=None):
    if path is None:
        path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'orca-bridge' / 'http.json'
    value = private_read(path)
    if (not isinstance(value, dict) or set(value) !=
            {'port', 'allowed_hosts', 'requests_per_minute', 'max_request_bytes'}):
        raise BridgeError('Invalid private HTTP settings schema')
    return HttpSettings(**value)


def read_token():
    token = os.environ.pop(TOKEN_NAME, '')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
        raise BridgeError('HTTP token is missing or invalid')
    return token


class AuditLog:
    def __init__(self, directory):
        self.path = Path(directory) / 'http.log'
        self.old = self.path.with_name('http.log.1')
        self.lock = threading.Lock()
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError('HTTP log directory must be private')
        if self.old.exists() or self.old.is_symlink():
            self.check(self.old.lstat())
        os.close(self.open())

    @staticmethod
    def check(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError('HTTP log must be a private regular file')

    def open(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                self.check(os.fstat(fd))
            except Exception:
                os.close(fd)
                raise
            return fd
        except OSError:
            raise BridgeError('Cannot open private HTTP log') from None

    def write(self, record):
        with self.lock:
            fd = self.open()
            if os.fstat(fd).st_size >= 5 * 1024 * 1024:
                os.close(fd)
                os.replace(self.path, self.old)
                fd = self.open()
            with os.fdopen(fd, 'w') as output:
                output.write(json.dumps(record, separators=(',', ':')) + '\n')


class Gateway:
    def __init__(self, app, settings, token, audit, clock):
        self.app, self.settings, self.token = app, settings, token.encode('ascii')
        self.audit, self.clock = audit, clock
        self.requests, self.failures = deque(), deque()
        self.locked_until = 0.0

    def equal(self, value):
        return hmac.compare_digest(value.encode('utf-8'), self.token)

    def identify(self, path, headers):
        header = headers.get(b'authorization', b'').decode('latin-1')
        bearer = header.startswith('Bearer ') and self.equal(header[7:])
        if path == '/mcp':
            return '/mcp', 'bearer' if bearer else ('bad' if header else 'none'), bool(bearer)
        if path.startswith('/') and path.endswith('/mcp') and self.equal(path[1:-4]):
            return 'secret-path', 'path', True
        return 'other', 'bearer' if bearer else ('bad' if header else 'none'), bool(bearer)

    @staticmethod
    def client(scope, headers):
        forwarded = headers.get(b'x-forwarded-for', b'').decode('latin-1').split(',', 1)[0].strip()
        peer = (scope.get('client') or ('unknown',))[0]
        try:
            return str(ipaddress.ip_address(forwarded or peer))
        except ValueError:
            return 'unknown'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        route, auth, authenticated = self.identify(scope['path'], headers)
        method = scope['method'] if scope['method'] in {'GET', 'POST', 'DELETE', 'PUT', 'PATCH', 'HEAD', 'OPTIONS'} else 'OTHER'
        record = dict(time=datetime.now(timezone.utc).isoformat(), method=method, route=route,
                      status=500, request_bytes=0, response_bytes=0, duration_ms=0,
                      client=self.client(scope, headers), auth=auth)
        response_started = False

        async def tracked(message):
            nonlocal response_started
            if message['type'] == 'http.response.start':
                response_started = True
                record['status'] = message['status']
            elif message['type'] == 'http.response.body':
                record['response_bytes'] += len(message.get('body', b''))
            await send(message)

        async def reject(status):
            reason = {400: 'Bad request', 401: 'Unauthorized', 404: 'Not found', 405: 'Method not allowed',
                      413: 'Request too large', 429: 'Rate limited', 500: 'Internal error'}[status]
            await tracked({'type': 'http.response.start', 'status': status,
                           'headers': [(b'content-type', b'application/json')]})
            await tracked({'type': 'http.response.body', 'body': json.dumps({'error': reason}).encode()})

        try:
            now = self.clock()
            if not authenticated:
                while self.failures and self.failures[0] <= now - 600:
                    self.failures.popleft()
                if now < self.locked_until:
                    await reject(429)
                    return
                self.failures.append(now)
                if len(self.failures) > 20:
                    self.locked_until = now + 600
                    await reject(429)
                    return
                await reject(404 if route == 'other' else 401)
                return
            # Only authenticated requests use the shared budget, so strangers cannot exhaust it.
            while self.requests and self.requests[0] <= now - 60:
                self.requests.popleft()
            if len(self.requests) >= self.settings.requests_per_minute:
                await reject(429)
                return
            self.requests.append(now)
            if route == 'other':
                await reject(404)
                return
            if method not in {'GET', 'POST', 'DELETE'}:
                await reject(405)
                return
            try:
                declared = int(headers.get(b'content-length', b'0'))
            except ValueError:
                await reject(400)
                return
            if declared < 0:
                await reject(400)
                return
            if declared > self.settings.max_request_bytes:
                await reject(413)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] != 'http.request':
                    await reject(400)
                    return
                chunk = message.get('body', b'')
                record['request_bytes'] += len(chunk)
                if record['request_bytes'] > self.settings.max_request_bytes:
                    await reject(413)
                    return
                body.extend(chunk)
                if not message.get('more_body', False):
                    break
            try:
                rpc = json.loads(body)
                if isinstance(rpc, dict) and rpc.get('jsonrpc') == '2.0' and isinstance(rpc.get('method'), str):
                    rpc_method = rpc['method']
                    record['rpc_method'] = rpc_method if rpc_method in RPC_METHODS else 'other'
                    params = rpc.get('params')
                    if rpc_method == 'tools/call' and isinstance(params, dict):
                        tool = params.get('name')
                        record['tool'] = tool if isinstance(tool, str) and tool in TOOLS else 'other'
            except (ValueError, UnicodeError):
                pass
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()

            rewritten = dict(scope, path='/mcp', raw_path=b'/mcp', query_string=b'')
            await self.app(rewritten, replay, tracked)
        except Exception:
            if not response_started:
                await reject(500)
            else:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
        finally:
            record['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
            try:
                self.audit.write(record)
            except Exception:
                # Never expose request context through a logging traceback.
                pass


def create_app(bridge, settings, token, clock=time.monotonic):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
        raise BridgeError('HTTP token is missing or invalid')
    # SDK and server diagnostics can include request arguments or exception data.
    logging.disable(logging.CRITICAL)
    server = create_server(bridge)
    hosts = list(settings.allowed_hosts) + [LOOPBACK + ':' + str(settings.port), 'localhost:' + str(settings.port)]
    server.settings.stateless_http = True
    server.settings.json_response = True
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts,
        allowed_origins=['https://' + host for host in settings.allowed_hosts] +
        ['http://' + LOOPBACK + ':' + str(settings.port), 'http://localhost:' + str(settings.port)])
    return Gateway(server.streamable_http_app(), settings, token, AuditLog(bridge.state_dir), clock)


def main():
    logging.disable(logging.CRITICAL)
    try:
        token = read_token()
        settings = load_settings()
        app = create_app(Bridge(load_config()), settings, token)
        uvicorn.run(app, host=LOOPBACK, port=settings.port, access_log=False, log_level='warning',
                    log_config=None, proxy_headers=False, server_header=False)
    except BridgeError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit('HTTP server could not start') from None


if __name__ == '__main__':
    main()
