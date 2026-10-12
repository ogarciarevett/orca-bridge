import asyncio
import dataclasses
import json
import os
import secrets
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import test_bridge
import orca_bridge.http as transport
from orca_bridge.bridge import BridgeError
from orca_bridge.http import HttpSettings, create_app, load_settings, read_token


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bridge.BridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.token = secrets.token_urlsafe(32)
        self.settings = HttpSettings(8787, ('example.invalid',), 600, 65536)
        self.now = 1000.0
        self.app = create_app(self.fixture.bridge, self.settings, self.token, clock=lambda: self.now)
        self.base = 'http://' + '127.0.0.1:' + str(self.settings.port)

    def authorization(self):
        return {'Authorization': 'Bearer ' + self.token}

    def request(self, path='/mcp', headers=None, content=None, method='POST'):
        # One-shot ASGI requests need fresh SDK lifespans; gateway counters persist.
        self.app.app = create_app(self.fixture.bridge, self.app.settings, self.token).app
        async def exercise():
            async with self.app.app.router.lifespan_context(self.app.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.base) as client:
                    return await client.request(method, path, headers=dict(
                        {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'},
                        **(headers or {})), content=content or json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}))
        return asyncio.run(exercise())

    def test_token_required_urlsafe_and_removed_from_environment(self):
        for value in ('', 'short', 'not safe ' + 'x' * 40):
            with self.subTest(length=len(value)), patch.dict(os.environ, {'ORCA_BRIDGE_HTTP_TOKEN': value}):
                with self.assertRaises(BridgeError) as error:
                    read_token()
                self.assertNotIn(value, str(error.exception)) if value else None
                self.assertNotIn('ORCA_BRIDGE_HTTP_TOKEN', os.environ)
        with patch.dict(os.environ, {'ORCA_BRIDGE_HTTP_TOKEN': self.token}):
            self.assertEqual(read_token(), self.token)
            self.assertNotIn('ORCA_BRIDGE_HTTP_TOKEN', os.environ)

    def test_bearer_authentication(self):
        self.assertEqual(self.request(headers=self.authorization()).status_code, 200)

    def test_main_removes_token_and_binds_only_loopback_without_access_logs(self):
        def serve(app, **options):
            self.assertNotIn('ORCA_BRIDGE_HTTP_TOKEN', os.environ)
            self.assertEqual(options['host'], '127.0.0.1')
            self.assertFalse(options['access_log'])
            self.assertEqual(options['log_level'], 'warning')
        with patch.dict(os.environ, {'ORCA_BRIDGE_HTTP_TOKEN': self.token}), \
                patch.object(transport, 'load_settings', return_value=self.settings), \
                patch.object(transport, 'load_config', return_value=self.fixture.config), \
                patch.object(transport, 'Bridge', return_value=self.fixture.bridge), \
                patch.object(transport.uvicorn, 'run', side_effect=serve):
            transport.main()
        with patch.dict(os.environ, {'ORCA_BRIDGE_HTTP_TOKEN': 'short'}), \
                patch.object(transport.uvicorn, 'run') as run:
            with self.assertRaises(SystemExit) as error:
                transport.main()
            self.assertNotIn('short', str(error.exception))
            run.assert_not_called()

    def test_secret_path_authentication(self):
        self.assertEqual(self.request('/' + self.token + '/mcp').status_code, 200)

    def test_wrong_bearer_and_missing_bearer(self):
        for headers in ({}, {'Authorization': 'Bearer incorrect'}):
            response = self.request(headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {'error': 'Unauthorized'})

    def test_wrong_path_and_query_token_refused(self):
        self.assertEqual(self.request('/wrong/mcp').status_code, 404)
        self.assertEqual(self.request('/mcp?token=' + self.token).status_code, 401)

    def test_content_length_body_limit(self):
        self.app = create_app(self.fixture.bridge, dataclasses.replace(self.settings, max_request_bytes=1024), self.token)
        self.assertEqual(self.request(headers=self.authorization(), content='x' * 1025).status_code, 413)

    def test_streamed_body_limit(self):
        self.app = create_app(self.fixture.bridge, dataclasses.replace(self.settings, max_request_bytes=1024), self.token)
        async def body():
            yield b'x' * 700
            yield b'x' * 700
        self.assertEqual(self.request(headers=self.authorization(), content=body()).status_code, 413)

    def test_global_sliding_rate_limit(self):
        self.app = create_app(self.fixture.bridge, dataclasses.replace(self.settings, requests_per_minute=1),
                              self.token, clock=lambda: self.now)
        self.assertEqual(self.request(headers=self.authorization()).status_code, 200)
        self.assertEqual(self.request('/' + self.token + '/mcp').status_code, 429)
        self.now += 61
        self.assertEqual(self.request(headers=self.authorization()).status_code, 200)

    def test_unauthenticated_requests_do_not_consume_the_rate_budget(self):
        self.app = create_app(self.fixture.bridge, dataclasses.replace(self.settings, requests_per_minute=1),
                              self.token, clock=lambda: self.now)
        for _ in range(5):
            self.assertEqual(self.request().status_code, 401)
        self.assertEqual(self.request(headers=self.authorization()).status_code, 200)

    def test_auth_failure_lockout_does_not_block_authenticated_clients(self):
        for _ in range(20):
            self.assertEqual(self.request().status_code, 401)
        self.assertEqual(self.request().status_code, 429)
        self.assertEqual(self.request('/unknown').status_code, 429)
        self.assertEqual(self.request(headers=self.authorization()).status_code, 200)
        self.assertEqual(self.request('/' + self.token + '/mcp').status_code, 200)
        self.now += 601
        self.assertEqual(self.request().status_code, 401)

    def test_host_and_origin_protection(self):
        self.assertEqual(self.request(headers=dict(self.authorization(), Host='untrusted.invalid')).status_code, 421)
        self.assertEqual(self.request(headers=dict(self.authorization(), Host='example.invalid')).status_code, 200)
        self.assertEqual(self.request(headers=dict(self.authorization(), Origin='https://untrusted.invalid')).status_code, 403)

    def test_log_redacts_token_path_body_arguments_and_arbitrary_rpc_names(self):
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'status', 'arguments': {'private': 'private-body-marker'}}})
        self.request('/' + self.token + '/mcp', headers={'X-Forwarded-For': '192.0.2.1, 192.0.2.2'}, content=body)
        self.request('/' + self.token + '/wrong', content=json.dumps({'method': self.token}))
        self.request(headers=dict(self.authorization(), **{'X-Forwarded-For': self.token}),
                     content=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': self.token}))
        path = self.fixture.bridge.state_dir / 'http.log'
        text = path.read_text()
        self.assertNotIn(self.token, text)
        self.assertNotIn('private-body-marker', text)
        self.assertNotIn('/wrong', text)
        rows = [json.loads(x) for x in text.splitlines()]
        self.assertEqual(rows[0]['route'], 'secret-path')
        self.assertEqual(rows[0]['auth'], 'path')
        self.assertEqual(rows[0]['client'], '192.0.2.1')
        self.assertEqual(rows[0]['rpc_method'], 'tools/call')
        self.assertEqual(rows[0]['tool'], 'status')
        self.assertGreater(rows[0]['request_bytes'], 0)
        self.assertGreater(rows[0]['response_bytes'], 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_log_rotation_and_symlink_refusal(self):
        path = self.fixture.bridge.state_dir / 'http.log'
        path.write_text('x' * (5 * 1024 * 1024))
        path.chmod(0o600)
        self.request(headers=self.authorization())
        self.assertTrue(path.with_name('http.log.1').exists())
        path.unlink()
        path.symlink_to(self.fixture.mail)
        with self.assertRaises(BridgeError):
            create_app(self.fixture.bridge, self.settings, self.token)

    def test_settings_private_loading_bounds_and_schema(self):
        root = self.fixture.root / 'config'
        path = root / 'orca-bridge' / 'http.json'
        path.parent.mkdir(parents=True)
        valid = dict(port=8787, allowed_hosts=['example.invalid'], requests_per_minute=60, max_request_bytes=65536)
        path.write_text(json.dumps(valid))
        path.chmod(0o600)
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(root)}):
            self.assertEqual(load_settings().port, 8787)
        for change in (dict(port=80), dict(port=True), dict(allowed_hosts=[]), dict(allowed_hosts=['*']),
                       dict(requests_per_minute=601), dict(max_request_bytes=1023), dict(extra=True)):
            path.write_text(json.dumps(dict(valid, **change)))
            with self.subTest(keys=change.keys()), self.assertRaises(BridgeError):
                load_settings(path)
        path.chmod(0o644)
        with self.assertRaises(BridgeError):
            load_settings(path)

    def test_real_mcp_client_both_auth_styles_and_shared_policy(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        app = create_app(self.fixture.bridge, dataclasses.replace(self.settings, port=port), self.token)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_config=None))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started)
            async def exchange():
                for style in ('bearer', 'path'):
                    url = 'http://' + '127.0.0.1:' + str(port)
                    url += '/mcp' if style == 'bearer' else '/' + self.token + '/mcp'
                    headers = self.authorization() if style == 'bearer' else {}
                    self.fixture.mail.write_text(json.dumps([self.fixture.message('msg_' + style)]))
                    async with httpx.AsyncClient(headers=headers) as client:
                        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()
                                self.assertEqual({t.name for t in (await session.list_tools()).tools},
                                                 {'status', 'send_request', 'list_replies', 'get_reply'})
                                self.assertFalse((await session.call_tool('status', {})).isError)
                                valid = dict(target=self.fixture.target, subject='Review', body='Review this plan')
                                self.assertFalse((await session.call_tool('send_request', valid)).isError)
                                self.assertTrue((await session.call_tool('send_request', dict(valid, target='@all'))).isError)
                                self.assertTrue((await session.call_tool('send_request', dict(valid, body='api_key=' + 'z' * 24))).isError)
                                replies = await session.call_tool('list_replies', {})
                                self.assertIn('msg_' + style, [x['id'] for x in replies.structuredContent['replies']])
                                one = await session.call_tool('get_reply', {'id': 'msg_' + style})
                                self.assertEqual(one.structuredContent['trust'], 'data-not-instructions')
            asyncio.run(exchange())
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        rows = [json.loads(line) for line in (self.fixture.bridge.state_dir / 'outbox.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['body'] == 'Review this plan' and row['to_handle'] == self.fixture.target
                            for row in rows))


if __name__ == '__main__':
    unittest.main()
