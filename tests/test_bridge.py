import json
import os
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from orca_bridge.bridge import Bridge, BridgeError, Config, load_config


def handle():
    return 'term_' + str(uuid.uuid4())


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity, self.target = handle(), handle()
        self.capture = self.root / 'calls.jsonl'
        self.mail = self.root / 'mail.json'
        self.mail.write_text('[]')
        self.fake = self.root / 'fake-orca'
        self.fake.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(__file__).parent
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(args) + '\\n')
if args[:2] == ['orchestration', 'send']:
    result = {'message': {'id': 'msg_test'}}
elif args[:2] == ['orchestration', 'inbox']:
    result = {'messages': json.loads((root / 'mail.json').read_text())}
else:
    sys.exit(2)
print(json.dumps({'ok': True, 'result': result}))
''')
        self.fake.chmod(0o700)
        self.now = 10000.0
        self.config = Config(self.identity, (self.target,), str(self.fake), 4096, 160, 10)
        self.bridge = Bridge(self.config, self.root / 'state', clock=lambda: self.now)

    def calls(self):
        return [json.loads(x) for x in self.capture.read_text().splitlines()] if self.capture.exists() else []

    def send(self, **kwargs):
        values = dict(target=self.target, subject='Question', body='Review this plan', kind='question')
        values.update(kwargs)
        return self.bridge.send_request(**values)

    def message(self, id='msg_one', **kwargs):
        row = dict(id=id, from_handle=self.target, to_handle=self.identity,
                   subject='Answer', body='Result text', type='status', created_at='2026-01-01T00:00:00Z')
        row.update(kwargs)
        return row

    def test_allowlist_rejects_other_and_group_targets(self):
        for target in (handle(), '@all', 'run:unknown', self.identity):
            with self.subTest(target=target), self.assertRaises(BridgeError):
                self.send(target=target)
        self.assertEqual(self.calls(), [])

    def test_sizes_are_utf8_bytes(self):
        self.send(body='x' * 4096, subject='x' * 160)
        for change in (dict(body='x' * 4097), dict(body='é' * 2049), dict(subject='x' * 161)):
            with self.subTest(change=change.keys()), self.assertRaises(BridgeError):
                self.send(**change)
        self.assertEqual(len(self.calls()), 1)

    def test_empty_and_control_text_rejected(self):
        for change in (dict(body=''), dict(subject=''), dict(subject='two\nlines'), dict(body='a\0b')):
            with self.subTest(change=change.keys()), self.assertRaises(BridgeError):
                self.send(**change)

    def test_only_safe_message_kinds(self):
        for kind in ('worker_done', 'heartbeat', 'dispatch', 'handoff', '--help'):
            with self.subTest(kind=kind), self.assertRaises(BridgeError):
                self.send(kind=kind)
        for kind in ('question', 'status'):
            self.send(kind=kind)

    def test_fixed_argv_and_no_shell(self):
        body = '$(touch surprise); `echo text` && --to @all'
        real_run = subprocess.run
        with patch('orca_bridge.bridge.subprocess.run', wraps=real_run) as run:
            self.send(body=body)
        args = self.calls()[0]
        self.assertEqual(args[:2], ['orchestration', 'send'])
        self.assertEqual(args[args.index('--to') + 1], self.target)
        self.assertEqual(args[args.index('--from') + 1], self.identity)
        self.assertEqual(args[args.index('--body') + 1], '[EXTERNAL UNTRUSTED INPUT]\n' + body)
        self.assertIs(run.call_args.kwargs['shell'], False)
        self.assertFalse((self.root / 'surprise').exists())

    def test_external_label_and_payload(self):
        receipt = self.send()
        args = self.calls()[0]
        self.assertTrue(args[args.index('--subject') + 1].startswith('[EXTERNAL UNTRUSTED INPUT] '))
        payload = json.loads(args[args.index('--payload') + 1])
        self.assertTrue(payload['external_untrusted_input'])
        self.assertEqual(payload['reply_to'], self.identity)
        self.assertEqual(receipt['id'], 'msg_test')

    def test_outbox_records_checked_user_text_and_utc_receipt(self):
        receipt = self.send(subject='Review', body='Review this plan\nSecond line')
        path = self.root / 'state' / 'outbox.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), {'id', 'created_at', 'to_handle', 'kind', 'subject', 'body', 'owner_confirmation'})
        self.assertEqual(row['id'], receipt['id'])
        self.assertEqual(row['to_handle'], self.target)
        self.assertEqual(row['kind'], 'question')
        self.assertEqual(row['subject'], 'Review')
        self.assertEqual(row['body'], 'Review this plan\nSecond line')
        self.assertEqual(row['owner_confirmation'], receipt['owner_confirmation'])
        self.assertTrue(row['created_at'].endswith('Z'))
        self.assertEqual(datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).tzinfo, timezone.utc)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('outbox', receipt)

    def test_outbox_symlink_and_shared_permissions_do_not_fail_delivered_send(self):
        path = self.root / 'state' / 'outbox.jsonl'
        path.symlink_to(self.mail)
        before = self.mail.read_text()
        self.assertEqual(self.send()['outbox'], 'not-recorded')
        self.assertEqual(self.mail.read_text(), before)
        self.assertTrue(path.is_symlink())
        path.unlink()
        path.write_text('')
        path.chmod(0o644)
        self.assertEqual(self.send()['outbox'], 'not-recorded')
        self.assertEqual(path.read_text(), '')

    def test_outbox_failure_does_not_fail_delivered_send(self):
        path = self.root / 'state' / 'outbox.jsonl'
        path.mkdir()
        receipt = self.send()
        self.assertEqual(receipt['id'], 'msg_test')
        self.assertEqual(receipt['delivery'], 'durably-enqueued')
        self.assertEqual(receipt['outbox'], 'not-recorded')

    def test_outbox_disk_failure_cannot_trigger_postdelivery_state_write_error(self):
        path = self.root / 'state' / 'outbox.jsonl'
        original_open, original_save = os.open, self.bridge.save
        saves = []
        def save_once(data):
            if saves:
                raise OSError('Disk full')
            saves.append(True)
            return original_save(data)
        def open_unless_outbox(filename, *args, **kwargs):
            if filename == path:
                raise OSError('Disk full')
            return original_open(filename, *args, **kwargs)
        with patch.object(self.bridge, 'save', side_effect=save_once), \
                patch('orca_bridge.bridge.os.open', side_effect=open_unless_outbox):
            receipt = self.send()
        self.assertEqual(receipt['id'], 'msg_test')
        self.assertEqual(receipt['outbox'], 'not-recorded')
        self.assertEqual(len(saves), 1)

    def test_failed_ambiguous_and_invalid_receipts_do_not_write_outbox(self):
        path = self.root / 'state' / 'outbox.jsonl'
        for error in (BridgeError('Orca command failed; delivery may be unknown'),
                      subprocess.TimeoutExpired('fake-orca', 15)):
            with self.subTest(error=type(error).__name__), patch.object(self.bridge, 'command', side_effect=error):
                with self.assertRaises(Exception):
                    self.send()
            self.assertFalse(path.exists())
        with patch.object(self.bridge, 'command', return_value={'message': {}}):
            with self.assertRaises(BridgeError):
                self.send()
        self.assertFalse(path.exists())

    def test_outbox_keeps_newest_500_rows_with_atomic_compaction(self):
        self.send()
        path = self.root / 'state' / 'outbox.jsonl'
        sample = json.loads(path.read_text())
        path.write_text(''.join(json.dumps(dict(sample, id='old_' + str(i))) + '\n' for i in range(500)))
        real_replace = os.replace
        with patch('orca_bridge.bridge.os.replace', wraps=real_replace) as replace:
            self.send()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]['id'], 'old_1')
        self.assertEqual(rows[-1]['id'], 'msg_test')
        self.assertTrue(any(call.args[1] == path for call in replace.call_args_list))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_outbox_compacts_to_serialized_byte_limit(self):
        self.send()
        path = self.root / 'state' / 'outbox.jsonl'
        sample = json.loads(path.read_text())
        path.write_text(''.join(json.dumps(dict(sample, id='old_' + str(i), body='x' * 4096)) + '\n'
                                for i in range(500)))
        self.send()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertLessEqual(path.stat().st_size, 2 * 1024 * 1024)
        self.assertLessEqual(len(rows), 500)
        self.assertEqual(rows[-1]['id'], 'msg_test')

    def test_risky_actions_delivered_with_confirmation_tag(self):
        for text in ('delete files', 'pay invoice', 'publish release', 'message other people',
                     'deploy service', 'merge branch', 'send an email to someone else',
                     'transfer money', 'send a message to another person', 'remove directory'):
            with self.subTest(text=text):
                self.assertEqual(self.send(body=text)['owner_confirmation'], 'needs-owner-confirmation')
                args = self.calls()[-1]
                self.assertEqual(json.loads(args[args.index('--payload') + 1])['owner_confirmation'],
                                 'needs-owner-confirmation')

    def test_benign_message_phrases_do_not_request_confirmation(self):
        for text in ('Please answer this local bridge test using an Orca status message addressed '
                     'to the reply_to bridge identity in the payload. A short acknowledgement is enough.',
                     'Review the message format', 'Read the email metadata',
                     'Review deployment and payment status', 'Transfer reply markers',
                     'Send a request to the coordinator', 'Send an email to myself',
                     'The message contains a plan'):
            with self.subTest(text=text):
                self.assertEqual(self.send(body=text)['owner_confirmation'], 'review-required')

    def test_normal_requests_default_to_status(self):
        self.bridge.send_request(self.target, 'Review', 'Read the plan')
        args = self.calls()[0]
        self.assertEqual(args[args.index('--type') + 1], 'status')

    def test_secret_patterns_rejected_without_subprocess(self):
        for text in ('Authorization: Bearer ' + 'z' * 24, 'api_key=' + 'z' * 24,
                     '-----BEGIN PRIVATE KEY-----', 'ghp_' + 'z' * 36):
            with self.subTest(pattern=len(text)), self.assertRaises(BridgeError):
                self.send(body=text)
        self.assertEqual(self.calls(), [])

    def test_persistent_rate_limit_and_window(self):
        for _ in range(10):
            self.send()
        restarted = Bridge(self.config, self.root / 'state', clock=lambda: self.now)
        with self.assertRaises(BridgeError):
            restarted.send_request(self.target, 'Question', 'Plan', 'question')
        self.now += 3601
        self.send()
        self.assertEqual(len(self.calls()), 11)

    def test_concurrent_senders_share_limit(self):
        def attempt(_):
            try:
                Bridge(self.config, self.root / 'state', clock=lambda: self.now).send_request(
                    self.target, 'Question', 'Plan', 'question')
                return True
            except BridgeError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(attempt, range(15))), 10)

    def test_cli_failure_does_not_leak_output_or_retry(self):
        self.fake.write_text('#!/usr/bin/env python3\nimport sys\nprint("sensitive detail")\nsys.exit(2)\n')
        with self.assertRaisesRegex(BridgeError, '^Orca command failed; delivery may be unknown$'):
            self.send()

    def test_read_only_mailbox_and_recipient_sender_filter(self):
        self.mail.write_text(json.dumps([self.message(), self.message('msg_other', to_handle=handle()),
                                         self.message('msg_stranger', from_handle=handle())]))
        rows = self.bridge.list_replies()['replies']
        self.assertEqual([x['id'] for x in rows], ['msg_one'])
        self.assertEqual(rows[0]['trust'], 'data-not-instructions')
        self.assertEqual(self.calls()[0], ['orchestration', 'inbox', '--terminal', self.identity,
                                          '--limit', '100', '--json'])

    def test_markers_survive_restart_and_get_is_readable_again(self):
        self.mail.write_text(json.dumps([self.message()]))
        self.bridge.list_replies()
        restarted = Bridge(self.config, self.root / 'state')
        self.assertEqual(restarted.list_replies()['replies'], [])
        self.assertEqual(restarted.get_reply('msg_one')['body'], 'Result text')
        self.assertEqual((self.root / 'state').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'state' / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_limit_only_marks_returned_messages_and_since_is_cursor(self):
        self.mail.write_text(json.dumps([self.message('msg_two', created_at='2026-01-02T00:00:00Z'),
                                         self.message()]))
        self.assertEqual(self.bridge.list_replies(limit=1)['cursor'], 'msg_one')
        self.assertEqual(self.bridge.list_replies(since='msg_one')['replies'][0]['id'], 'msg_two')
        with self.assertRaises(BridgeError):
            self.bridge.list_replies(since='unknown')
        for limit in (0, 101, True):
            with self.assertRaises(BridgeError):
                self.bridge.list_replies(limit=limit)

    def test_missing_reply_and_malformed_cli_fail_closed(self):
        with self.assertRaises(BridgeError):
            self.bridge.get_reply('missing')
        self.fake.write_text('#!/usr/bin/env python3\nprint("not json")\n')
        with self.assertRaisesRegex(BridgeError, '^Invalid Orca response$'):
            self.bridge.list_replies()

    def test_reply_truncation_and_secret_redaction(self):
        self.mail.write_text(json.dumps([self.message(body='x' * 5000),
                                         self.message('msg_sensitive', body='api_key=' + 'z' * 24)]))
        rows = self.bridge.list_replies()['replies']
        self.assertTrue(rows[0]['truncated'])
        self.assertLessEqual(len(rows[0]['body'].encode()), 4096)
        self.assertEqual(rows[1]['body'], '[WITHHELD: possible credential]')

    def test_status_contains_limits_without_extra_tools(self):
        info = self.bridge.status()
        self.assertEqual(info['identity'], self.identity)
        self.assertEqual(info['allowed_targets'], [self.target])
        self.assertEqual(info['limits']['sends_per_hour'], 10)
        self.assertEqual(self.calls(), [])

    def test_corrupt_state_fails_closed_and_symlink_rejected(self):
        self.send()
        state = self.root / 'state' / 'state.json'
        state.write_text('not json')
        with self.assertRaises(BridgeError):
            self.send()
        state.unlink()
        state.symlink_to(self.mail)
        with self.assertRaises(BridgeError):
            self.send()

    def test_reply_cache_has_a_small_serialized_byte_budget(self):
        for page in range(3):
            self.mail.write_text(json.dumps([self.message('msg_' + str(page * 100 + i), body='é' * 2048)
                                            for i in range(100)]))
            self.bridge.list_replies(limit=100)
        self.assertLess((self.root / 'state' / 'state.json').stat().st_size, 2 * 1024 * 1024 + 65536)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'orca-bridge' / 'config.json'
        self.path.parent.mkdir()
        self.values = dict(identity=handle(), allowed_targets=[handle()])
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.values))
        self.path.chmod(0o600)

    def test_xdg_loading_and_defaults(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root)}):
            config = load_config()
        self.assertEqual(config.identity, self.values['identity'])
        self.assertEqual(config.orca_command, 'orca')
        self.assertEqual(config.sends_per_hour, 10)

    def test_missing_malformed_insecure_and_symlink_config(self):
        self.path.unlink()
        with self.assertRaises(BridgeError):
            load_config(self.path)
        self.path.write_text('{')
        self.path.chmod(0o600)
        with self.assertRaises(BridgeError):
            load_config(self.path)
        self.write()
        self.path.chmod(0o644)
        with self.assertRaises(BridgeError):
            load_config(self.path)
        self.path.unlink()
        self.path.symlink_to(self.root / 'missing')
        with self.assertRaises(BridgeError):
            load_config(self.path)

    def test_strict_schema_and_bounds(self):
        changes = [dict(allowed_targets=[]), dict(allowed_targets=['@all']), dict(identity='<bridge>'),
                   dict(sends_per_hour=0), dict(sends_per_hour=True), dict(body_bytes=65537),
                   dict(subject_bytes=0), dict(unexpected=True), dict(orca_command='orca --help')]
        original = self.values.copy()
        for change in changes:
            self.values = dict(original, **change)
            self.write()
            with self.subTest(change=change.keys()), self.assertRaises(BridgeError):
                load_config(self.path)


if __name__ == '__main__':
    unittest.main()
