import asyncio
import json
import os
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import test_bridge


class ProtocolTests(unittest.TestCase):
    def test_packaged_stdio_tools_and_exchange(self):
        fixture = test_bridge.BridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config_root = fixture.root / 'config'
        private = config_root / 'orca-bridge'
        private.mkdir(parents=True)
        config = private / 'config.json'
        config.write_text(json.dumps(dict(identity=fixture.identity, allowed_targets=[fixture.target],
                                          orca_command=str(fixture.fake))))
        config.chmod(0o600)
        fixture.mail.write_text(json.dumps([fixture.message()]))
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / '.cursor-plugin' / 'plugin.json').read_text())
        self.assertEqual(manifest['mcpServers'], 'mcp.json')
        self.assertTrue((root / manifest['skills'] / 'orca-bridge' / 'SKILL.md').exists())
        launch = json.loads((root / 'mcp.json').read_text())['mcpServers']['orca-bridge']
        environment = dict(os.environ, XDG_CONFIG_HOME=str(config_root),
                           XDG_STATE_HOME=str(fixture.root / 'state-root'))
        environment['PATH'] = str(Path(os.sys.executable).parent) + os.pathsep + environment['PATH']

        async def exercise():
            params = StdioServerParameters(command=launch['command'], args=launch['args'], env=environment)
            with (fixture.root / 'diagnostics').open('w') as errors:
                async with stdio_client(params, errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = (await session.list_tools()).tools
                        names = {x.name for x in tools}
                        self.assertEqual(names, {'send_request', 'list_replies', 'get_reply', 'status'})
                        send_schema = next(x.inputSchema for x in tools if x.name == 'send_request')
                        self.assertEqual(send_schema['properties']['kind']['default'], 'status')
                        status = await session.call_tool('status', {})
                        self.assertFalse(status.isError)
                        self.assertEqual(status.structuredContent['identity'], fixture.identity)
                        denied = await session.call_tool('send_request', dict(target='@all', subject='Review', body='Plan'))
                        self.assertTrue(denied.isError)
                        sent = await session.call_tool('send_request', dict(target=fixture.target, subject='Review', body='Plan'))
                        self.assertFalse(sent.isError)
                        self.assertEqual(sent.structuredContent['owner_confirmation'], 'review-required')
                        listed = await session.call_tool('list_replies', {})
                        self.assertEqual(listed.structuredContent['replies'][0]['trust'], 'data-not-instructions')
                        reread = await session.call_tool('get_reply', {'id': 'msg_one'})
                        self.assertEqual(reread.structuredContent['id'], 'msg_one')
                        empty = await session.call_tool('list_replies', {})
                        self.assertEqual(empty.structuredContent['replies'], [])
            self.assertEqual((fixture.root / 'diagnostics').read_text(), '')

        asyncio.run(exercise())
