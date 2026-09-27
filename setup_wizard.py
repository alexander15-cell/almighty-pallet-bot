"""Local-only guided setup. Secrets are typed privately, never echoed or bundled."""
from getpass import getpass
import json
from pathlib import Path
import os

from combined_settings import load_settings, initialize, safe_path

def main():
    root = Path(__file__).resolve().parent
    target = safe_path(root / 'private' / 'setup.json')
    if target.exists():
        print('Setup already exists. Use Check or edit private/setup.json locally; nothing was overwritten.')
        return
    print('This only prepares local files. It does not log into Discord or publish anything.')
    print('Use the replacement Almighty application, not the separate Publisher application.')
    value = json.loads((root / 'setup.example.json').read_text(encoding='utf-8'))
    value['operatorUserIds'] = [v.strip() for v in input('Staff Discord user IDs, separated by commas: ').split(',') if v.strip()]
    for name in value['roleIds']:
        value['roleIds'][name] = input(f'Copy the {name} role ID: ').strip()
    value['sourceId'] = input('Existing website DISCORD_SYNC_SOURCE_ID (copy exactly): ').strip()
    value['botToken'] = getpass('Replacement Almighty bot token (hidden while typing): ').strip()
    value['websiteSecret'] = getpass('Existing website DISCORD_SYNC_SECRET (hidden while typing): ').strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
    try:
        settings = load_settings(target, connect=True)
        initialize(settings)
    except Exception:
        print('Saved privately, but setup needs correction. Edit private/setup.json, then run Initialize. No connection was made.')
        return
    print('Saved in private/setup.json. New inventory state is in private/state.')
    print('Website publishing is OFF. Read START HERE before the one-sender handover.')

if __name__ == '__main__':
    main()
