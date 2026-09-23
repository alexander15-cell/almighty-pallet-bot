"""
Verified local backups of the database, photos, and CSV batch archives.

Creates a timestamped zip snapshot under config.BACKUP_DIR, using SQLite's
own backup API for a consistent database copy even while the bot is mid-
write (a plain file copy of a live SQLite file can catch it mid-transaction
and produce a corrupt copy - the backup API never does). A manifest.json
inside the zip records a sha256 hash of every included file, so verify()
can tell a genuine backup from one that got truncated or corrupted.

Runs on a schedule from inside the bot (see cogs/admin_tools.py's
backup_loop) and is also usable standalone from the command line:

    python backup.py create
    python backup.py verify path/to/backup.zip
    python backup.py restore path/to/backup.zip --destination path/to/new-data

Restore only ever writes into a NEW, empty destination directory - never
into a live data directory - so a bad restore can't clobber a working
installation. Stop the bot first; the restored directory has the same
layout as config's data paths (pallet_tracker.db, photos/,
ebay_batch_archive/, pirate_ship_exports/), so pointing DATABASE_PATH/
PHOTO_DIR/etc at it (or renaming it to replace your data/ directory) is
enough to bring a restored copy back online.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import config

# Everything backed up besides the database itself, keyed by the name it's
# stored under inside the zip.
_ARCHIVE_DIRS = {
    "photos": lambda: Path(config.PHOTO_DIR),
    "ebay_batch_archive": lambda: Path(config.EBAY_BATCH_ARCHIVE_DIR),
    "pirate_ship_exports": lambda: Path(config.PIRATE_SHIP_EXPORT_ARCHIVE_DIR),
}
_DB_ARCHIVE_NAME = "pallet_tracker.db"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_database(dest_path: Path):
    source_path = Path(config.DATABASE_PATH)
    if not source_path.exists():
        raise FileNotFoundError(f"Database not found at {source_path} - nothing to back up yet.")
    with closing(sqlite3.connect(str(source_path))) as source, \
         closing(sqlite3.connect(str(dest_path))) as dest:
        source.backup(dest)


def create_backup() -> Path:
    """Builds one timestamped, verified zip and applies retention. Returns
    the new zip's path."""
    backup_dir = Path(config.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    staging = backup_dir / f".staging_{timestamp}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_path": str(config.DATABASE_PATH),
            "files": {},
        }

        db_dest = staging / _DB_ARCHIVE_NAME
        _snapshot_database(db_dest)
        manifest["files"][_DB_ARCHIVE_NAME] = _sha256(db_dest)

        for archive_name, resolve_dir in _ARCHIVE_DIRS.items():
            source_dir = resolve_dir()
            if not source_dir.exists():
                continue
            for source_file in source_dir.rglob("*"):
                if not source_file.is_file():
                    continue
                rel = Path(archive_name) / source_file.relative_to(source_dir)
                dest_file = staging / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, dest_file)
                manifest["files"][str(rel.as_posix())] = _sha256(dest_file)

        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        zip_path = backup_dir / f"backup_{timestamp}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in staging.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(staging).as_posix())
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    problems = verify_backup(zip_path)
    if problems:
        # A backup that fails its own just-written manifest means something
        # went wrong mid-write (disk full, a file changed under us) - better
        # to fail loudly than leave a silently-broken "backup" on disk.
        zip_path.unlink(missing_ok=True)
        raise RuntimeError("New backup failed verification and was discarded:\n" + "\n".join(problems))

    _apply_retention()
    return zip_path


def _apply_retention():
    """Keeps at most BACKUP_KEEP_COUNT snapshots, newest first, and drops
    anything older than BACKUP_MAX_AGE_DAYS - run after every create_backup()
    so the backup directory doesn't grow without bound."""
    backup_dir = Path(config.BACKUP_DIR)
    backups = sorted(backup_dir.glob("backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = datetime.now(timezone.utc).timestamp() - config.BACKUP_MAX_AGE_DAYS * 86400
    for index, path in enumerate(backups):
        if index >= config.BACKUP_KEEP_COUNT or path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def verify_backup(zip_path: Path) -> list:
    """Checks every file listed in the backup's manifest.json against its
    recorded sha256 hash. Returns a list of problems - empty means clean."""
    problems = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except KeyError:
                return ["manifest.json missing - not a valid backup archive"]
            for rel_path, expected_hash in manifest.get("files", {}).items():
                try:
                    data = zf.read(rel_path)
                except KeyError:
                    problems.append(f"missing from archive: {rel_path}")
                    continue
                if hashlib.sha256(data).hexdigest() != expected_hash:
                    problems.append(f"hash mismatch (corrupted): {rel_path}")
    except zipfile.BadZipFile:
        return ["not a valid zip file"]
    return problems


def restore_backup(zip_path: Path, destination: Path) -> Path:
    """
    Verifies the backup, then extracts it into a NEW, empty destination
    directory. Refuses to touch a destination that already has files in
    it, so this can never silently overwrite a live installation.
    """
    problems = verify_backup(zip_path)
    if problems:
        raise ValueError("Refusing to restore - backup failed verification:\n" + "\n".join(problems))

    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(
            f"{destination} already exists and is not empty - restore only ever writes into a new, empty directory."
        )

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(destination)
    (destination / "manifest.json").unlink(missing_ok=True)
    return destination


def _cli():
    parser = argparse.ArgumentParser(description="Backup/restore for the pallet tracking bot's data.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("create", help="Create a new backup now.")

    verify_parser = sub.add_parser("verify", help="Verify a backup archive's integrity.")
    verify_parser.add_argument("path", type=Path)

    restore_parser = sub.add_parser("restore", help="Restore a backup into a NEW, empty directory.")
    restore_parser.add_argument("path", type=Path)
    restore_parser.add_argument("--destination", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "create":
        path = create_backup()
        print(f"Backup created and verified: {path}")
    elif args.command == "verify":
        problems = verify_backup(args.path)
        if problems:
            print("Backup FAILED verification:")
            for problem in problems:
                print(f"  - {problem}")
            sys.exit(1)
        print("Backup verified OK.")
    elif args.command == "restore":
        result = restore_backup(args.path, args.destination)
        print(
            f"Restored into {result}. Stop the bot first if it's running. This directory has the "
            "same layout as your data/ folder (pallet_tracker.db, photos/, ebay_batch_archive/, "
            "pirate_ship_exports/) - point DATABASE_PATH/PHOTO_DIR/etc at it, or move its contents "
            "into your data/ directory, then restart the bot."
        )


if __name__ == "__main__":
    _cli()
