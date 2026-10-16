"""Optional second copy of each backup (BACKUP_COPY_DIR, e.g. Google Drive)."""
from pathlib import Path

import backup
import config


def _setup(tmp_path, monkeypatch, fresh_db):
    for name in ("PHOTO_DIR", "INVOICE_DIR", "EBAY_BATCH_ARCHIVE_DIR", "FB_MARKETPLACE_BATCH_ARCHIVE_DIR",
                 "PIRATE_SHIP_EXPORT_ARCHIVE_DIR"):
        monkeypatch.setattr(config, name, str(tmp_path / "none" / name))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "local"))
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)


def test_verified_backup_is_copied_and_copy_folder_keeps_its_own_count(tmp_path, monkeypatch, fresh_db):
    _setup(tmp_path, monkeypatch, fresh_db)
    drive = tmp_path / "My Drive" / "Pallet Bot Backups"
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", str(drive))
    monkeypatch.setattr(config, "BACKUP_COPY_KEEP_COUNT", 2)
    drive.mkdir(parents=True)
    for n in range(3):
        old = drive / f"backup_2000010{n}_000000.zip"
        old.write_bytes(b"old")
        import os
        os.utime(old, (n, n))
    (drive / "notes.txt").write_text("mine")

    path = backup.create_backup()

    copied = drive / path.name
    assert backup.last_copy == copied and copied.read_bytes() == path.read_bytes()
    assert backup.verify_backup(copied) == []
    assert sorted(p.name for p in drive.glob("backup_*.zip")) == ["backup_20000102_000000.zip", path.name]
    assert (drive / "notes.txt").exists() and not list(drive.glob("*.partial"))


def test_copy_failure_never_fails_the_backup(tmp_path, monkeypatch, fresh_db):
    _setup(tmp_path, monkeypatch, fresh_db)
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", str(blocker / "sub"))  # can't make a folder under a file
    path = backup.create_backup()
    assert path.exists() and isinstance(backup.last_copy, str)


def test_copy_is_off_by_default(tmp_path, monkeypatch, fresh_db):
    _setup(tmp_path, monkeypatch, fresh_db)
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", "")
    backup.create_backup()
    assert backup.last_copy is None
