"""Run with: python -m room_bot --port /dev/ttyACM0 --mode bot"""
import argparse
import importlib
import logging
import math
import json
from pathlib import Path
import time

from .client import RoomClient, SerialTransport, RoomError, ProtocolError
from .worker import BotWorker, BotState
from .config import DEFAULT_ROOT, ConfigError, load_config, write_new_config
from .clock import SessionClock
from .commands import Dispatcher
from .offline import install_offline_commands
from .community import install_community_commands

BOT_HELP = 'Commands: /bot ping | /bot help | /bot about'


def ping(post):
    """Handle the sample bot's explicitly addressed commands only."""
    words = post.text.split()
    if not words or words[0].casefold() != '/bot':
        return None
    if len(words) == 1:
        return BOT_HELP
    if len(words) != 2:
        return 'Usage: /bot ping | /bot help | /bot about'
    verb = words[1].casefold()
    if verb == 'ping':
        return 'pong'
    if verb == 'help':
        return BOT_HELP
    if verb == 'about':
        return 'Squishyware room bot: Python over USB; replies use the room identity.'
    return 'Unknown bot command. Try /bot help.'

def monitor(post):
    print(f'{post.sequence} {post.author[:12]}: {post.text}', flush=True)

def parse_options(argv=None):
    parser = argparse.ArgumentParser(description='USB-connected MeshCore room bot (no companion radio)')
    parser.add_argument('--port', help='Room USB port, e.g. /dev/ttyACM0 or COM26')
    parser.add_argument('--state', type=Path, default=DEFAULT_ROOT / 'state.sqlite3')
    parser.add_argument('--config', type=Path, help='Explicit versioned JSON configuration')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--init-config', type=Path, help='Create a default config without overwriting a file')
    actions.add_argument('--check-config', action='store_true', help='Validate configuration without opening USB')
    parser.add_argument('--clock-trusted', action='store_true',
                        help='Confirm the OS clock for this process only (not saved across restart)')
    parser.add_argument('--mode', choices=['monitor', 'bot', 'ping'], default='monitor')
    parser.add_argument('--handler', help='Custom callback as importable_module:function')
    parser.add_argument('--name', default='Bot', help='Prefix name, up to 32 UTF-8 bytes')
    parser.add_argument('--replay', action='store_true', help='On a NEW state file only, process retained posts')
    parser.add_argument('--allow-gap', action='store_true', help='Explicitly accept loss before oldest retained post')
    parser.add_argument('--poll-interval', type=float, default=1)
    parser.add_argument('--reply-interval', type=float, default=5)
    args = parser.parse_args(argv)
    if not args.name.strip() or len(args.name.encode('utf-8')) > 32 or any(ord(c) < 32 or ord(c)==127 for c in args.name):
        parser.error('Name must be nonblank, <=32 UTF-8 bytes and contain no control characters')
    if not math.isfinite(args.poll_interval) or not math.isfinite(args.reply_interval) or not args.poll_interval >= 0.2 or not args.reply_interval >= 1:
        parser.error('Poll interval must be >=0.2 seconds; reply interval must be >=1 second')
    try:
        args.config_value = load_config(args.config)
        args.data_path = args.config_value.data_path(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if not args.port and not (args.init_config or args.check_config):
        parser.error('--port is required to run the bot')
    return args


def main(argv=None):
    args = parse_options(argv)
    if args.init_config:
        try:
            write_new_config(args.init_config, args.config_value)
        except OSError as exc:
            print(f'Cannot create config: {exc}')
            return 1
        print(f'Created {args.init_config}')
        return 0
    if args.check_config:
        print(json.dumps(args.config_value.to_dict(), ensure_ascii=False, indent=2))
        print(f'Data directory: {args.data_path}')
        print(f'Clock confirmed for this process: {args.clock_trusted}')
        return 0
    prefix = f'[{args.name}] '
    handler = Dispatcher(args.config_value, prefix,
                         SessionClock(args.clock_trusted, args.config_value.clock_max_jump_seconds)) if args.mode in ('bot', 'ping') else monitor
    if isinstance(handler, Dispatcher):
        install_offline_commands(handler, args.data_path)
        install_community_commands(handler, args.data_path)
    if args.handler:
        module, separator, name = args.handler.partition(':')
        if not separator:
            parser.error('--handler must be module:function')
        handler = getattr(importlib.import_module(module), name)
        if not callable(handler):
            parser.error('--handler must refer to a callable')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    state = BotState(args.state)
    retry = 1
    try:
        while True:
            transport = None
            try:
                transport = SerialTransport(args.port)
                worker = BotWorker(RoomClient(transport), state, handler, prefix=prefix,
                                   replay=args.replay, allow_gap=args.allow_gap, reply_interval=args.reply_interval)
                if isinstance(handler,Dispatcher):
                    handler.bind_room(worker.info['room_key'])
                logging.info('Connected to room %s; saved cursor %s', worker.info['room_key'][:12], state.cursor)
                retry = 1
                while True:
                    count, more = worker.step()
                    time.sleep(0.05 if more else args.poll_interval)
            except (OSError, TimeoutError) as exc:
                logging.warning('USB disconnected/timed out: %s; retry in %ss', exc, retry)
            finally:
                if transport is not None:
                    transport.close()
            time.sleep(retry)
            retry = min(30, retry * 2)
    except KeyboardInterrupt:
        logging.info('Stopped; progress and pending replies retained')
    except (RoomError, ValueError) as exc:
        logging.error('Stopped without discarding state: %s', exc)
        return 1
    finally:
        state.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
