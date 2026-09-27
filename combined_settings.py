"""Explicit deployment configuration. No implicit dotenv or credential discovery."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

STAGES = ('data-entry', 'automated-review', 'queue-review', 'awaiting-listing',
          'pending-ebay-upload', 'pending-fb-marketplace-upload', 'listed', 'sold', 'hold', 'website_shop')
ROLES = ('Pallet Admin', 'Queue Review', 'Listing Management')
FIELDS = {'schemaVersion', 'applicationId', 'guildId', 'operatorUserIds', 'roleIds',
          'channels', 'sourceId', 'websiteUrl', 'websiteSecret', 'botToken',
          'publishEnabled', 'cutoverConfirmed', 'pollSeconds'}

class SetupError(ValueError):
    pass

def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{16,19}', value) or int(value) >= 2**64:
        raise SetupError('Use a copied Discord ID as text, not a name or number.')
    return value

def safe_path(path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise SetupError('Linked folders are not supported for private bot state.')
    return path

def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError('Duplicate setup field.')
        result[key] = value
    return result

@dataclass(frozen=True, repr=False)
class Settings:
    path: Path
    application_id: str
    guild_id: str
    operator_ids: tuple
    role_ids: dict
    channels: dict
    source_id: str
    website: str
    secret: str
    token: str
    publish_enabled: bool
    cutover_confirmed: bool
    poll_seconds: int
    sku_prefix: str = 'FGNEW-'
    item_id_floor: int = 1000000000

    @property
    def state_directory(self):
        return safe_path(self.path.parent / 'state')

    @property
    def photo_directory(self):
        return self.state_directory / 'photos'

    def scope(self):
        return {'schema': 'four-guys-combined-v1', 'applicationId': self.application_id,
                'guildId': self.guild_id, 'channels': self.channels, 'sourceId': self.source_id,
                'websiteUrl': self.website, 'skuPrefix': self.sku_prefix, 'itemIdFloor': self.item_id_floor}

def load_settings(path, *, connect=False, send=False):
    from publisher.transport import validate_website
    path = safe_path(path)
    if not path.is_file() or path.stat().st_size > 16384:
        raise SetupError('Run Setup first. The settings file is missing or too large.')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_pairs)
    except (OSError, UnicodeError, ValueError):
        raise SetupError('The settings file is not valid JSON.') from None
    if not isinstance(value, dict) or set(value) != FIELDS or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise SetupError('Settings do not match this package version.')
    app, guild = identity(value['applicationId']), identity(value['guildId'])
    if app == guild or not isinstance(value['channels'], dict) or set(value['channels']) != set(STAGES):
        raise SetupError('The ten workflow channels must be configured explicitly.')
    channels = {key: identity(v) for key, v in value['channels'].items()}
    if len(set(channels.values()) | {app, guild}) != 12:
        raise SetupError('Application, server and workflow channel IDs must differ.')
    operators = value['operatorUserIds']
    if not isinstance(operators, list) or not 1 <= len(operators) <= 100:
        raise SetupError('Add at least one staff user ID.')
    operators = tuple(identity(v) for v in operators)
    if len(set(operators)) != len(operators) or set(operators) & ({app, guild} | set(channels.values())):
        raise SetupError('Staff IDs must be unique user IDs.')
    if not isinstance(value['roleIds'], dict) or set(value['roleIds']) != set(ROLES):
        raise SetupError('Bind the three staff roles by ID.')
    roles = {key: identity(v) for key, v in value['roleIds'].items()}
    if not isinstance(value['sourceId'], str) or not re.fullmatch(r'[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', value['sourceId']):
        raise SetupError('Copy the existing website source ID; do not invent a replacement.')
    website = validate_website(value['websiteUrl'])
    for key in ('websiteSecret', 'botToken'):
        if not isinstance(value[key], str) or len(value[key]) > 300 or any(ch.isspace() for ch in value[key]):
            raise SetupError('A saved credential has invalid formatting.')
    for key in ('publishEnabled', 'cutoverConfirmed'):
        if type(value[key]) is not bool:
            raise SetupError('Publishing and cutover settings must be true or false.')
    if type(value['pollSeconds']) is not int or not 10 <= value['pollSeconds'] <= 300:
        raise SetupError('pollSeconds must be between 10 and 300.')
    if connect and not re.fullmatch(r'[A-Za-z0-9_.-]{30,300}', value['botToken']):
        raise SetupError('Save the replacement bot token privately in setup.json.')
    if send and (not value['publishEnabled'] or not value['cutoverConfirmed'] or not re.fullmatch(r'[0-9a-fA-F]{64}', value['websiteSecret'])):
        raise SetupError('Live publishing needs a website key, publishEnabled and confirmed single-sender cutover.')
    return Settings(path, app, guild, operators, roles, channels, value['sourceId'], website,
                    value['websiteSecret'], value['botToken'], value['publishEnabled'], value['cutoverConfirmed'], value['pollSeconds'])

def activate_intake(settings):
    """Call before importing any upstream module. Disable unrelated integrations."""
    os.environ['FOUR_GUYS_COMBINED'] = '1'
    # Set each external-service and path variable before config can import it.
    for key in ('ANTHROPIC_API_KEY', 'EBAY_APP_ID', 'EBAY_CERT_ID', 'EBAY_DEV_ID', 'EBAY_USER_TOKEN',
                'QUICKBOOKS_CLIENT_ID', 'QUICKBOOKS_CLIENT_SECRET', 'R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID',
                'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME', 'R2_PUBLIC_URL_BASE'):
        os.environ[key] = ''
    paths = {'DATABASE_PATH': 'intake.sqlite', 'PHOTO_DIR': 'photos', 'SETTINGS_PATH': 'role-settings.json',
             'INSTANCE_LOCK_PATH': 'combined.lock', 'EBAY_BATCH_CSV_PATH': 'ebay-batch.csv',
             'EBAY_BATCH_ARCHIVE_DIR': 'ebay-archive', 'FB_MARKETPLACE_BATCH_CSV_PATH': 'fb-batch.csv',
             'FB_MARKETPLACE_BATCH_ARCHIVE_DIR': 'fb-archive', 'PIRATE_SHIP_EXPORT_ARCHIVE_DIR': 'unused-shipping',
             'BACKUP_DIR': 'backups'}
    for key, name in paths.items():
        os.environ[key] = str(safe_path(settings.state_directory / name))
    os.environ.update({'DISCORD_BOT_TOKEN': '', 'DISCORD_GUILD_ID': settings.guild_id,
                       'PRESERVE_DISCORD_HISTORY': 'true', 'AI_REVIEW_BACKEND': 'disabled',
                       'WEBSITE_SKU_PREFIX': settings.sku_prefix})
    import config
    config.COMBINED_MODE = True
    config.PRESERVE_DISCORD_HISTORY = True
    config.COMBINED_ROLE_IDS = dict(settings.role_ids)
    config.GUILD_ID = int(settings.guild_id)
    config.WEBSITE_SKU_PREFIX = settings.sku_prefix
    config.DISCORD_BOT_TOKEN = None
    config.AI_ENABLED = config.R2_ENABLED = config.EBAY_ENABLED = config.QUICKBOOKS_ENABLED = False
    for key in paths:
        setattr(config, key, os.environ[key])
    return config

def initialize(settings):
    """Exclusive isolated NEW inventory database. Never opens an existing DB."""
    root = settings.state_directory
    if root.exists():
        raise SetupError('State already exists. Do not reinitialize or delete it; use Check or Start.')
    config = activate_intake(settings)
    root.mkdir(parents=True, exist_ok=False)
    import database as db
    db.init_db()
    pallet_id = db.create_pallet('New inventory', 0, int(settings.operator_ids[0]))
    db.map_channel(pallet_id, 'data-entry', int(settings.channels['data-entry']))
    for stage in STAGES:
        if stage not in {'data-entry', 'website_shop'}:
            db.set_shared_channel(stage, int(settings.channels[stage]))
    with db.get_conn() as connection:
        connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('items',?)", (settings.item_id_floor - 1,))
    settings.photo_directory.mkdir()
    with (root / 'deployment.json').open('x', encoding='utf-8') as stream:
        json.dump(settings.scope(), stream, indent=2)

def check_state(settings):
    root = settings.state_directory
    marker = safe_path(root / 'deployment.json')
    if not marker.is_file() or marker.stat().st_size > 16384:
        raise SetupError('Initialize this new package first. Never copy an old intake database here.')
    try:
        actual = json.loads(marker.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise SetupError('Deployment identity could not be verified.') from None
    if actual != settings.scope():
        raise SetupError('Settings do not match existing state. Do not reset the journal or change source IDs.')
    if not safe_path(root / 'intake.sqlite').is_file():
        raise SetupError('The inventory database is missing. Restore the complete backup; do not start empty.')
