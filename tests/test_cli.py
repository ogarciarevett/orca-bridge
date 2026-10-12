import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_bridge


class CliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_bridge.BridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config_root = self.root / 'config'
        private = self.config_root / 'orca-bridge'
        private.mkdir(parents=True, mode=0o700)
        self.config_path = private / 'config.json'
        self.config = dict(identity=self.fixture.identity, allowed_targets=[self.fixture.target],
                           orca_command=str(self.fixture.fake))
        self.write_config()
        self.environment = dict(os.environ, XDG_CONFIG_HOME=str(self.config_root),
                                XDG_STATE_HOME=str(self.root / 'cli-state'), HOME=str(self.root))
        self.repo = Path(__file__).resolve().parents[1]

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def run_cli(self, *argv, exit_code=0):
        result = subprocess.run([sys.executable, '-m', 'orca_bridge.cli', *argv],
                                cwd=self.repo, env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, exit_code)
        self.assertEqual(result.stderr, '')
        self.assertTrue(result.stdout.endswith('\n'))
        self.assertEqual(len(result.stdout.splitlines()), 1)
        output = json.loads(result.stdout)
        self.assertIsInstance(output, dict)
        self.assertEqual(output['ok'], exit_code == 0)
        if exit_code == 0:
            self.assertEqual(set(output), {'ok', 'trust', 'result'})
            self.assertEqual(output['trust'], 'data-not-instructions')
            self.assertIsInstance(output['result'], dict)
        else:
            self.assertEqual(set(output), {'ok', 'error'})
            self.assertIsInstance(output['error'], str)
        return output, result.stdout

    def test_send_uses_only_target_prefix_payload_and_default_status(self):
        output, text = self.run_cli('send', 'Inspect this request')
        args = self.fixture.calls()[0]
        self.assertEqual(args[args.index('--to') + 1], self.fixture.target)
        self.assertEqual(args[args.index('--type') + 1], 'status')
        self.assertEqual(args[args.index('--subject') + 1], '[EXTERNAL UNTRUSTED INPUT] Inspect this request')
        self.assertEqual(args[args.index('--body') + 1], '[EXTERNAL UNTRUSTED INPUT]\nInspect this request')
        payload = json.loads(args[args.index('--payload') + 1])
        self.assertTrue(payload['external_untrusted_input'])
        self.assertEqual(payload['reply_to'], self.fixture.identity)
        self.assertEqual(output['result']['id'], 'msg_test')
        self.assertNotIn('Inspect this request', text)
        outbox = self.root / 'cli-state' / 'orca-bridge' / 'outbox.jsonl'
        row = json.loads(outbox.read_text())
        self.assertEqual(row['id'], output['result']['id'])
        self.assertEqual(row['body'], 'Inspect this request')

    def test_send_explicit_question(self):
        self.run_cli('send', 'Please review this plan', '--kind', 'question')
        args = self.fixture.calls()[0]
        self.assertEqual(args[args.index('--type') + 1], 'question')

    def test_send_invalid_kind_is_safe_usage_error(self):
        output, _ = self.run_cli('send', 'Sensitive request text', '--kind', 'other', exit_code=2)
        self.assertNotIn('Sensitive request text', output['error'])
        self.assertNotIn('other', output['error'])
        self.assertEqual(self.fixture.calls(), [])

    def test_multiple_targets_fail_closed(self):
        self.config['allowed_targets'].append(test_bridge.handle())
        self.write_config()
        output, _ = self.run_cli('send', 'Review the plan', exit_code=1)
        self.assertIn('exactly one', output['error'])
        self.assertEqual(self.fixture.calls(), [])

    def test_secret_text_is_rejected_without_echo(self):
        secret = 'api_key=' + 'z' * 24
        for value in (secret, 'x' * 200 + '\n' + secret):
            with self.subTest(position=len(value)):
                output, text = self.run_cli('send', value, exit_code=1)
                self.assertEqual(output['error'], 'Possible credential rejected; never send secrets')
                self.assertNotIn(secret, text)
        self.assertEqual(self.fixture.calls(), [])

    def test_oversize_body_is_rejected(self):
        output, text = self.run_cli('send', 'x' * 4097, exit_code=1)
        self.assertEqual(output['error'], 'Invalid text or configured byte limit exceeded')
        self.assertNotIn('x' * 20, text)
        self.assertEqual(self.fixture.calls(), [])

    def test_subject_truncates_at_utf8_character_boundary(self):
        self.config['subject_bytes'] = 5
        self.write_config()
        self.run_cli('send', 'é🙂 more text\nBody')
        args = self.fixture.calls()[0]
        self.assertEqual(args[args.index('--subject') + 1], '[EXTERNAL UNTRUSTED INPUT] é')
        self.assertEqual(args[args.index('--body') + 1], '[EXTERNAL UNTRUSTED INPUT]\né🙂 more text\nBody')

    def test_subject_uses_trimmed_first_nonempty_line_and_preserves_body(self):
        body = '\n \n  First line  \nMore text'
        self.run_cli('send', body)
        args = self.fixture.calls()[0]
        self.assertEqual(args[args.index('--subject') + 1], '[EXTERNAL UNTRUSTED INPUT] First line')
        self.assertEqual(args[args.index('--body') + 1], '[EXTERNAL UNTRUSTED INPUT]\n' + body)

    def test_rate_limit_is_shared_across_invocations(self):
        self.config['sends_per_hour'] = 1
        self.write_config()
        self.run_cli('send', 'First request')
        output, _ = self.run_cli('send', 'Second request', exit_code=1)
        self.assertEqual(output['error'], 'Hourly send limit reached')
        self.assertEqual(len(self.fixture.calls()), 1)

    def test_replies_marks_read(self):
        self.fixture.mail.write_text(json.dumps([self.fixture.message()]))
        output, _ = self.run_cli('replies')
        self.assertEqual(output['result']['replies'][0]['id'], 'msg_one')
        output, _ = self.run_cli('replies')
        self.assertEqual(output['result']['replies'], [])

    def test_reply_cursor_and_limit(self):
        self.fixture.mail.write_text(json.dumps([self.fixture.message(), self.fixture.message(
            'msg_two', created_at='2026-01-02T00:00:00Z')]))
        first, _ = self.run_cli('replies', '--limit', '1')
        self.assertEqual(first['result']['cursor'], 'msg_one')
        second, _ = self.run_cli('replies', '--since', 'msg_one')
        self.assertEqual(second['result']['replies'][0]['id'], 'msg_two')
        output, _ = self.run_cli('replies', '--limit', '0', exit_code=1)
        self.assertEqual(output['error'], 'Limit must be between 1 and 100')

    def test_get_known_reply(self):
        self.fixture.mail.write_text(json.dumps([self.fixture.message()]))
        output, _ = self.run_cli('get', 'msg_one')
        self.assertEqual(output['result']['body'], 'Result text')
        self.assertEqual(output['result']['trust'], 'data-not-instructions')
        unseen, _ = self.run_cli('replies')
        self.assertEqual(unseen['result']['replies'], [])

    def test_get_unknown_reply(self):
        output, _ = self.run_cli('get', 'unknown', exit_code=1)
        self.assertEqual(output['error'], 'Reply not found in this bridge mailbox')

    def test_status_identity_targets_and_limits(self):
        output, _ = self.run_cli('status')
        self.assertEqual(output['result']['identity'], self.fixture.identity)
        self.assertEqual(output['result']['allowed_targets'], [self.fixture.target])
        self.assertEqual(output['result']['limits']['sends_per_hour'], 10)
        self.assertEqual(self.fixture.calls(), [])

    def test_extra_positionals_and_unrecognized_options_fail_without_echo(self):
        sensitive = 'private-input-for-usage-check'
        for argv in (('send', 'Request', sensitive), ('get', 'msg_one', sensitive),
                     ('replies', sensitive), ('status', sensitive), ('send', 'Request', '--to', sensitive)):
            with self.subTest(command=argv[0]):
                _, text = self.run_cli(*argv, exit_code=2)
                self.assertNotIn(sensitive, text)
        self.assertEqual(self.fixture.calls(), [])

    def test_invalid_usage_does_not_echo_values(self):
        sensitive = 'private-input-for-usage-check'
        for argv in ((), ('send',), (sensitive,), ('replies', '--limit', sensitive)):
            with self.subTest(command=argv[:1]):
                _, text = self.run_cli(*argv, exit_code=2)
                self.assertNotIn(sensitive, text)
        self.assertEqual(self.fixture.calls(), [])

    def test_empty_and_control_text_still_use_bridge_validation(self):
        for body in ('', '\n \n', 'Review\ninvalid\x1bcontrol'):
            with self.subTest(length=len(body)):
                self.run_cli('send', body, exit_code=1)
        self.assertEqual(self.fixture.calls(), [])

    def test_risky_intent_keeps_confirmation_tag(self):
        output, _ = self.run_cli('send', 'merge the PR')
        self.assertEqual(output['result']['owner_confirmation'], 'needs-owner-confirmation')
        args = self.fixture.calls()[0]
        self.assertEqual(json.loads(args[args.index('--payload') + 1])['owner_confirmation'],
                         'needs-owner-confirmation')

    def test_config_failure_is_safe_json(self):
        self.config_path.write_text('invalid config')
        self.run_cli('status', exit_code=1)
        self.assertEqual(self.fixture.calls(), [])

    def test_unexpected_exception_is_generic_and_does_not_echo(self):
        cli = importlib.import_module('orca_bridge.cli')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment), patch.object(
                cli, 'load_config', side_effect=RuntimeError('private exception detail')):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = cli.main(['status'])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), '{"ok": false, "error": "Unexpected error"}\n')
        self.assertEqual(stderr.getvalue(), '')

    def test_help_is_normal_text_without_opening_config(self):
        self.config_path.unlink()
        result = subprocess.run([sys.executable, '-m', 'orca_bridge.cli', '--help'], cwd=self.repo,
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('send', result.stdout)
        self.assertEqual(result.stderr, '')
        self.assertFalse((self.root / 'cli-state').exists())
