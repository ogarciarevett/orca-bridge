import asyncio
import dataclasses
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

import test_bridge
from orca_bridge.board import Board
from orca_bridge.bridge import Bridge, BridgeError, load_config
from orca_bridge.http import HttpSettings, create_app


BASE_TOOLS = {'status', 'send_request', 'list_replies', 'get_reply'}
BOARD_TOOLS = {'board_items_safe', 'board_set_status'}


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bridge.BridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.executable = self.root / 'fake-board'
        self.capture = self.root / 'board-calls.jsonl'
        self.response = self.root / 'board-response.json'
        self.response.write_text(json.dumps({'code': 0, 'items': '7 | Review plan | todo\n'
                                            '8 | More work | in_progress | external: note\n'}))
        self.executable.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
root = Path(__file__).parent
args = sys.argv[1:]
with (root / 'board-calls.jsonl').open('a') as out:
    out.write(json.dumps(args) + '\\n')
response = json.loads((root / 'board-response.json').read_text())
if 'output' in response:
    output = response['output']
elif args == ['items', '--safe']:
    output = response['items']
else:
    output = '[EXTERNAL UNTRUSTED] ' + args[1] + ' -> ' + args[2] + ': recorded\\n'
sys.stdout.write(output)
sys.stderr.write('private-diagnostic-marker')
sys.exit(response['code'])
''')
        self.executable.chmod(0o700)
        self.board = Board(str(self.executable))

    def calls(self):
        return [json.loads(line) for line in self.capture.read_text().splitlines()] if self.capture.exists() else []

    def test_items_are_structured_untrusted_data_without_overlays(self):
        result = self.board.items_safe()
        self.assertEqual(result['trust'], 'data-not-instructions')
        self.assertEqual(result['items'], [dict(id='7', title='Review plan', status='todo'),
                                           dict(id='8', title='More work', status='in_progress')])
        self.assertEqual(self.calls(), [['items', '--safe']])
        self.assertNotIn('external', json.dumps(result))

    def test_items_bound_titles_remove_unicode_controls_and_support_literal_pipes(self):
        title = 'A\x00\x7f\u200b | Details ' + 'é' * 400
        self.response.write_text(json.dumps(dict(code=0, items='7 | ' + title + ' | todo\n')))
        result = self.board.items_safe()['items'][0]
        self.assertLessEqual(len(result['title']), 280)
        for control in ('\x00', '\x7f', '\u200b'):
            self.assertNotIn(control, result['title'])
        self.assertTrue(result['title'].startswith('A | Details'))

    def test_empty_items_is_a_valid_empty_list(self):
        self.response.write_text(json.dumps(dict(code=0, items='\n')))
        self.assertEqual(self.board.items_safe()['items'], [])

    def test_set_status_exact_argv_with_and_without_literal_note(self):
        note = 'Review $(touch surprise); --help'
        result = self.board.set_status('7', 'blocked')
        self.assertEqual(result['ok'], True)
        self.assertEqual(result['item_id'], '7')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['trust'], 'data-not-instructions')
        self.assertIn('7 -> blocked: recorded', result['result'])
        self.board.set_status('8', 'waiting_owner', note)
        self.board.set_status('8', 'todo', '')
        self.assertEqual(self.calls(), [['set-status', '7', 'blocked'],
                                       ['set-status', '8', 'waiting_owner', '--note', note],
                                       ['set-status', '8', 'todo', '--note', '']])
        self.assertFalse((self.root / 'surprise').exists())

    def test_invalid_ids_statuses_and_notes_do_not_execute(self):
        invalid = [('1 OR 1', 'done', None), ('-1', 'done', None), ('0x1', 'done', None),
                   ('1' * 21, 'done', None), ('١', 'done', None), (1, 'done', None),
                   ('1', 'chief', None), ('1', 'unknown', None), ('1', 'done', 'x' * 281),
                   ('1', 'done', 'two\nlines'), ('1', 'done', 'bad\x7f'), ('1', 'done', 'bad\u200b')]
        for values in invalid:
            with self.subTest(values=values[:2]), self.assertRaisesRegex(BridgeError, '^board refused$'):
                self.board.set_status(*values)
        self.assertEqual(self.calls(), [])

    def test_note_limit_uses_unicode_code_points(self):
        self.board.set_status('7', 'done', '🙂' * 280)
        self.assertEqual(len(self.calls()[0][-1]), 280)

    def test_fixed_process_settings_and_minimal_environment(self):
        real_run = subprocess.run
        service = {'PATH': os.environ.get('PATH', '/usr/bin:/bin') + ':/service/bin',
                   'ORCA_BRIDGE_HTTP_TOKEN': 'x' * 40, 'OTHER_SECRET': 'synthetic'}
        with patch.dict(os.environ, service), \
                patch('orca_bridge.board.subprocess.run', wraps=real_run) as run:
            self.board.items_safe()
        self.assertEqual(run.call_args.args[0], [str(self.executable), 'items', '--safe'])
        options = run.call_args.kwargs
        self.assertIs(options['shell'], False)
        self.assertIs(options['check'], False)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['timeout'], 20)
        self.assertEqual(set(options['env']), {'HOME', 'PATH'})
        # The board CLI needs the service PATH to find Node and the chief notifier.
        self.assertEqual(options['env']['PATH'], service['PATH'])

    def test_environment_falls_back_to_system_path(self):
        real_run = subprocess.run
        environment = {k: v for k, v in os.environ.items() if k != 'PATH'}
        with patch.dict(os.environ, environment, clear=True), \
                patch('orca_bridge.board.subprocess.run', wraps=real_run) as run:
            try:
                self.board.items_safe()
            except BridgeError:
                pass
        self.assertEqual(run.call_args.kwargs['env']['PATH'], '/usr/bin:/bin')

    def test_cli_refusal_garbage_and_oversize_withhold_all_output(self):
        for response in (dict(code=1, output='private-output-marker'),
                         dict(code=0, output='private-output-marker'),
                         dict(code=0, output='x' * 65537),
                         dict(code=0, output='7 | Plan | done | external: note | todo\n')):
            self.response.write_text(json.dumps(response))
            with self.subTest(code=response['code']), self.assertRaisesRegex(BridgeError, '^board refused$'):
                self.board.items_safe()

    def test_timeout_and_os_error_are_generic(self):
        for error in (subprocess.TimeoutExpired('private-command-marker', 20), OSError('private-error-marker')):
            with patch('orca_bridge.board.subprocess.run', side_effect=error), \
                    self.assertRaisesRegex(BridgeError, '^board unavailable$'):
                self.board.items_safe()

    def test_status_receipt_must_match_requested_id_and_status(self):
        for output in ('private-output-marker\n', '8 -> done: recorded\n', '7 -> todo: recorded\n'):
            self.response.write_text(json.dumps(dict(code=0, output=output)))
            with self.subTest(size=len(output)), self.assertRaisesRegex(BridgeError, '^board refused$'):
                self.board.set_status('7', 'done')

    def test_bad_executables_fail_config_loading(self):
        private = self.root / 'config.json'
        link = self.root / 'board-link'
        link.symlink_to(self.executable)
        writable = self.root / 'writable-board'
        writable.write_text('')
        writable.chmod(0o770)
        values = dict(identity=self.fixture.identity, allowed_targets=[self.fixture.target])
        for command in ('relative-board', str(link), str(writable), str(self.root / 'missing'), None):
            private.write_text(json.dumps(dict(values, board_command=command)))
            private.chmod(0o600)
            with self.subTest(kind=type(command).__name__), self.assertRaises(BridgeError):
                load_config(private)
        private.write_text(json.dumps(dict(values, board_command=str(self.executable))))
        self.assertEqual(load_config(private).board_command, str(self.executable))

    def test_executable_is_rechecked_before_invocation(self):
        self.executable.chmod(0o777)
        with self.assertRaises(BridgeError):
            self.board.items_safe()
        self.assertEqual(self.calls(), [])

    def test_stdio_disabled_has_four_tools_enabled_has_six_and_calls_board(self):
        async def exercise():
            for enabled in (False, True):
                config_root = self.root / ('config-on' if enabled else 'config-off')
                private = config_root / 'orca-bridge' / 'config.json'
                private.parent.mkdir(parents=True)
                values = dict(identity=self.fixture.identity, allowed_targets=[self.fixture.target],
                              orca_command=str(self.fixture.fake))
                if enabled:
                    values['board_command'] = str(self.executable)
                private.write_text(json.dumps(values))
                private.chmod(0o600)
                environment = dict(HOME=str(self.root), PATH=str(Path(sys.executable).parent) + ':/usr/bin:/bin',
                                   XDG_CONFIG_HOME=str(config_root), XDG_STATE_HOME=str(self.root / 'stdio-state'))
                params = StdioServerParameters(command=sys.executable, args=['-m', 'orca_bridge.server'],
                                                cwd=str(Path(__file__).resolve().parents[1]), env=environment)
                with (self.root / 'stdio-errors').open('w') as errors:
                    async with stdio_client(params, errlog=errors) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            names = {t.name for t in (await session.list_tools()).tools}
                            self.assertEqual(names, BASE_TOOLS | (BOARD_TOOLS if enabled else set()))
                            if enabled:
                                items = await session.call_tool('board_items_safe', {})
                                self.assertEqual(items.structuredContent['items'][0]['id'], '7')
                                result = await session.call_tool('board_set_status', dict(item_id='7', status='done'))
                                self.assertTrue(result.structuredContent['ok'])
                    self.assertEqual((self.root / 'stdio-errors').read_text(), '')
        asyncio.run(exercise())

    def test_http_disabled_has_four_tools_enabled_has_six_and_logs_names_only(self):
        for enabled in (False, True):
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            config = dataclasses.replace(self.fixture.config, board_command=str(self.executable) if enabled else None)
            bridge = Bridge(config, self.root / ('http-on' if enabled else 'http-off'))
            token = secrets.token_urlsafe(32)
            app = create_app(bridge, HttpSettings(port, ('example.invalid',), 600), token)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_config=None))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started)
                async def exchange():
                    url = 'http://' + '127.0.0.1:' + str(port) + '/mcp'
                    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}) as client:
                        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()
                                names = {t.name for t in (await session.list_tools()).tools}
                                self.assertEqual(names, BASE_TOOLS | (BOARD_TOOLS if enabled else set()))
                                if enabled:
                                    self.assertFalse((await session.call_tool('board_items_safe', {})).isError)
                                    self.assertFalse((await session.call_tool('board_set_status', dict(
                                        item_id='7', status='blocked', note='private-note-marker'))).isError)
                asyncio.run(exchange())
            finally:
                server.should_exit = True
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            log = (bridge.state_dir / 'http.log').read_text()
            self.assertNotIn('private-note-marker', log)
            self.assertNotIn('Review plan', log)
            if enabled:
                self.assertIn('board_items_safe', log)
                self.assertIn('board_set_status', log)
