"""Only supported entry point. Defaults to offline configuration checking."""
import argparse
import asyncio
import logging
from pathlib import Path

from combined_settings import SetupError, activate_intake, check_state, initialize, load_settings

def main():
    parser = argparse.ArgumentParser(description='Four Guys combined intake + website bot')
    parser.add_argument('--settings', default=str(Path(__file__).parent / 'private' / 'setup.json'))
    parser.add_argument('--initialize', action='store_true', help='Create isolated NEW inventory state once; no network')
    parser.add_argument('--connect', action='store_true', help='Connect to Discord and enable intake/review controls')
    parser.add_argument('--send', action='store_true', help='Send explicitly approved changes and withdrawals to website')
    parser.add_argument('--sync-commands', action='store_true', help='Update only this application\'s commands in the configured guild')
    args = parser.parse_args()
    if args.send and not args.connect or args.sync_commands and not args.connect or args.initialize and args.connect:
        parser.error('Use --initialize separately. --send/--sync-commands require --connect.')
    try:
        settings = load_settings(args.settings, connect=args.connect, send=args.send)
        if args.initialize:
            initialize(settings)
            print('New inventory storage initialized. No Discord or website connection was made.')
            return
        check_state(settings)
        if not args.connect:
            print('Configuration and storage identity match. No network connection was made.')
            return
        activate_intake(settings)
        import instance_lock
        from combined_runtime import GuardedBot
        logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
        for handler in logging.getLogger().handlers:
            handler.addFilter(lambda record: record.name == 'four_guys')
        logging.getLogger('four_guys').setLevel(logging.INFO)
        instance_lock.acquire(settings.state_directory / 'combined.lock')
        async def run():
            async with GuardedBot(settings, send=args.send, sync_commands=args.sync_commands) as bot:
                await bot.start(settings.token)
        try:
            asyncio.run(run())
        finally:
            instance_lock.release()
    except KeyboardInterrupt:
        print('Bot stopped. Saved inventory and queued changes were retained.')
    except SetupError as error:
        print('Setup needed: ' + str(error))
        raise SystemExit(1)
    except Exception:
        print('Could not start or continue. Check local setup, bot permissions and connection. Credentials were not logged.')
        raise SystemExit(1)

if __name__ == '__main__':
    main()
