"""
Shared pytest setup: points every path this bot reads from config.py at a
throwaway temp directory before any test module imports config/database/etc,
so running the suite never touches a real .env, a real database, or real
local files. Nothing here is imported directly by tests - pytest loads
conftest.py automatically before collecting test modules in this directory.
"""
import os
import sys
import tempfile
from pathlib import Path

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="pallet_bot_tests_"))

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ["DATABASE_PATH"] = str(_TEST_DATA_DIR / "test.db")
os.environ["PHOTO_DIR"] = str(_TEST_DATA_DIR / "photos")
os.environ["EBAY_BATCH_CSV_PATH"] = str(_TEST_DATA_DIR / "ebay_batch.csv")
os.environ["EBAY_BATCH_ARCHIVE_DIR"] = str(_TEST_DATA_DIR / "ebay_batch_archive")
os.environ["PIRATE_SHIP_EXPORT_ARCHIVE_DIR"] = str(_TEST_DATA_DIR / "pirate_ship_exports")
os.environ["BACKUP_DIR"] = str(_TEST_DATA_DIR / "backups")
os.environ["SETTINGS_PATH"] = str(_TEST_DATA_DIR / "settings.json")
os.environ["INSTANCE_LOCK_PATH"] = str(_TEST_DATA_DIR / ".bot.lock")

# Repo root, so `import config`/`import database` etc. work no matter where
# pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """
    A brand-new, empty database for one test - isolated from every other
    test, since database.py reads config.DATABASE_PATH fresh on every call
    (not cached at import time), so pointing it at a new tmp_path file per
    test is enough for real isolation.
    """
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db
