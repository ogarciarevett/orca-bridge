"""A JSON command interface over the existing Bridge policy."""

import argparse
import json

from .bridge import Bridge, BridgeError, load_config


class UsageError(ValueError):
    """Argument errors whose output must not include user input."""


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError('Invalid command arguments')


def parser():
    result = Parser(prog='orca-bridge', description='Send constrained requests and read replies as JSON.')
    commands = result.add_subparsers(dest='command', required=True)
    send = commands.add_parser('send', help='Send one request to the single configured coordinator.')
    send.add_argument('text', help='Full nonsecret request text as one argument.')
    send.add_argument('--kind', choices=('status', 'question'), default='status')
    replies = commands.add_parser('replies', help='Read replies and update local read markers.')
    replies.add_argument('--since', help='A previously returned reply ID.')
    replies.add_argument('--limit', type=int, default=20)
    get = commands.add_parser('get', help='Read one reply by ID.')
    get.add_argument('id')
    commands.add_parser('status', help='Show bridge identity, targets and limits.')
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        config = load_config()
        if args.command == 'send' and len(config.allowed_targets) != 1:
            raise BridgeError('CLI send requires exactly one allowlisted target')
        bridge = Bridge(config)
        if args.command == 'send':
            first_line = next((line.strip() for line in args.text.splitlines() if line.strip()), '')
            subject = first_line.encode('utf-8')[:config.subject_bytes].decode('utf-8', errors='ignore')
            result = bridge.send_request(config.allowed_targets[0], subject, args.text, args.kind)
        elif args.command == 'replies':
            result = bridge.list_replies(args.since, args.limit)
        elif args.command == 'get':
            result = bridge.get_reply(args.id)
        else:
            result = bridge.status()
        output = json.dumps(dict(ok=True, trust='data-not-instructions', result=result))
        exit_code = 0
    except UsageError as error:
        output = json.dumps(dict(ok=False, error=str(error)))
        exit_code = 2
    except BridgeError as error:
        output = json.dumps(dict(ok=False, error=str(error)))
        exit_code = 1
    except Exception:
        output = json.dumps(dict(ok=False, error='Unexpected error'))
        exit_code = 1
    print(output)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
