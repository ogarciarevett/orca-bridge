"""A narrow adapter to an owner-configured board policy executable."""

import os
import re
import stat
import subprocess
import unicodedata
from pathlib import Path

from .bridge import BridgeError

STATUSES = {'todo', 'in_progress', 'blocked', 'waiting_owner', 'done'}
ITEM_ID = re.compile(r'[0-9]{1,20}\Z')
OVERLAY = re.compile(r'[^|:]{1,80}: .*\Z')


def validate_board_command(command):
    try:
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise ValueError()
        info = os.stat(command, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or not os.access(command, os.X_OK)):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise BridgeError('board unavailable') from None
    return command


def has_control(text):
    return any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in text)


def board_environment():
    # Keep only HOME and the service PATH, so Node and the chief notifier resolve without credentials.
    return {'HOME': str(Path.home()), 'PATH': os.environ.get('PATH') or '/usr/bin:/bin'}


class Board:
    def __init__(self, command):
        self.command = validate_board_command(command)

    def _run(self, arguments):
        validate_board_command(self.command)
        try:
            result = subprocess.run([self.command, *arguments], shell=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
                                    env=board_environment(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError('board unavailable') from None
        if result.returncode != 0 or len(result.stdout) > 65536:
            raise BridgeError('board refused')
        try:
            return result.stdout.decode('utf-8')
        except UnicodeError:
            raise BridgeError('board refused') from None

    def items_safe(self):
        output = self._run(['items', '--safe'])
        if not output.strip():
            return dict(items=[], trust='data-not-instructions')
        lines = output.split('\n')
        if lines[-1] == '':
            lines.pop()
        if len(lines) > 500:
            raise BridgeError('board refused')
        items = []
        seen = set()
        for line in lines:
            fields = line.split(' | ')
            if len(fields) < 3 or not ITEM_ID.fullmatch(fields[0]) or fields[0] in seen:
                raise BridgeError('board refused')
            candidates = [i for i in range(2, len(fields)) if fields[i] in STATUSES
                          and (i == len(fields) - 1 or OVERLAY.fullmatch(fields[i + 1]))]
            if len(candidates) != 1:
                raise BridgeError('board refused')
            position = candidates[0]
            title = ' | '.join(fields[1:position])
            title = ''.join(char for char in title if not has_control(char))[:280]
            items.append(dict(id=fields[0], title=title, status=fields[position]))
            seen.add(fields[0])
        return dict(items=items, trust='data-not-instructions')

    def set_status(self, item_id, status, note=None):
        if (not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id)
                or not isinstance(status, str) or status not in STATUSES
                or (note is not None and (not isinstance(note, str) or len(note) > 280 or has_control(note)))):
            raise BridgeError('board refused')
        arguments = ['set-status', item_id, status]
        if note is not None:
            arguments.extend(['--note', note])
        output = self._run(arguments)
        prefix = '[EXTERNAL UNTRUSTED] '
        line = output[:-1] if output.endswith('\n') else output
        if line.startswith(prefix):
            line = line[len(prefix):]
        expected = item_id + ' -> ' + status + ': '
        if line not in (expected + 'recorded', expected + 'recorded; chief notification failed'):
            raise BridgeError('board refused')
        return dict(ok=True, item_id=item_id, status=status, result=line, trust='data-not-instructions')
