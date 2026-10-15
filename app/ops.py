"""Offline, bounded backups and verified restore to an empty data directory.

Operators must stop every API/Worker/tool writer first. Never exposed as a model tool.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from app.storage.schema import SCHEMA_VERSION

TREES = {"runs", "source_jobs", "file_jobs"}
MAX_BYTES = 10 * 1024**3
MAX_FILES = 100_000
MAX_MANIFEST = 16 * 1024**2


class BackupError(ValueError):
    pass


def safe_name(name):
    parts = PurePosixPath(name).parts
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
    }
    if (
        not parts
        or name != "/".join(parts)
        or name.startswith("/")
        or any(
            p in {".", ".."}
            or any(c in p for c in '\\:<>"|?*\x00')
            or any(ord(c) < 32 for c in p)
            or p.endswith((" ", "."))
            or p.split(".")[0].upper() in reserved
            for p in parts
        )
        or (name != "news.sqlite3" and (parts[0] not in TREES or len(parts) < 2))
    ):
        raise BackupError("unsafe archive path")
    return parts


def plain(path):
    if path.is_symlink() or path.is_junction() or not stat.S_ISREG(path.stat().st_mode):
        raise BackupError("non-regular file")


def checked_root(path):
    absolute = Path(path).expanduser().absolute()
    if absolute.resolve() != absolute or absolute.is_symlink() or absolute.is_junction():
        raise BackupError("data directory cannot use links")
    return absolute


def database_valid(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        if (
            connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
            or not 1 <= connection.execute("PRAGMA user_version").fetchone()[0] <= SCHEMA_VERSION
        ):
            raise BackupError("invalid database")


def copy_stream(source, destination, limit):
    digest, count = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        count += len(chunk)
        if count > limit:
            raise BackupError("backup size limit exceeded")
        digest.update(chunk)
        destination.write(chunk)
    return {"size": count, "sha256": digest.hexdigest()}


def backup(data_dir, archive, *, writers_stopped=False):
    if not writers_stopped:
        raise BackupError("stop all writers and acknowledge --writers-stopped")
    root, output = checked_root(data_dir), Path(archive).expanduser().absolute()
    if output.resolve().is_relative_to(root) or output.exists():
        raise BackupError("backup must be new and outside the data directory")
    plain(root / "news.sqlite3")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="news-backup-", dir=output.parent) as folder:
        staging = Path(folder)
        snapshot = staging / "news.sqlite3"
        deadline = time.monotonic() + 60

        def progress(*_):
            if time.monotonic() > deadline:
                raise BackupError("database snapshot timed out")

        with (
            closing(
                sqlite3.connect((root / "news.sqlite3").as_uri() + "?mode=ro", uri=True)
            ) as src,
            closing(sqlite3.connect(snapshot)) as dst,
        ):
            src.backup(dst, pages=256, progress=progress)
        database_valid(snapshot)
        files = [("news.sqlite3", snapshot)]
        for tree in sorted(TREES):
            base = root / tree
            if base.is_symlink() or base.is_junction():
                raise BackupError("linked directories cannot be backed up")
            if not base.exists():
                continue
            if not base.is_dir():
                raise BackupError("material tree must be a directory")
            for directory, dirs, names in os.walk(base, followlinks=False):
                for child in [Path(directory), *(Path(directory) / d for d in dirs)]:
                    if child.is_symlink() or child.is_junction():
                        raise BackupError("linked directories cannot be backed up")
                for name in sorted(names):
                    path = Path(directory) / name
                    plain(path)
                    relative = path.relative_to(root).as_posix()
                    safe_name(relative)
                    files.append((relative, path))
                    if len(files) > MAX_FILES:
                        raise BackupError("backup file limit exceeded")
        temporary = staging / "backup.zip"
        entries, total, canonical = {}, 0, set()
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
            for name, path in files:
                if name.casefold() in canonical:
                    raise BackupError("case-aliased backup paths")
                canonical.add(name.casefold())
                before = path.stat()
                with path.open("rb") as source, bundle.open(name, "w", force_zip64=True) as target:
                    entries[name] = copy_stream(source, target, MAX_BYTES - total)
                total += entries[name]["size"]
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise BackupError("data changed during offline backup")
            manifest = {
                "format": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "files": entries,
            }
            encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_MANIFEST:
                raise BackupError("manifest size limit exceeded")
            bundle.writestr("manifest.json", encoded)
        # Atomic, no-clobber publication on the same filesystem.
        os.link(temporary, output)
        os.chmod(output, 0o600)
    return {"files": len(entries), "bytes": total}


def restore(archive, data_dir, *, writers_stopped=False):
    if not writers_stopped:
        raise BackupError("stop all writers and acknowledge --writers-stopped")
    target = checked_root(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise BackupError("restore destination must be empty")
    with tempfile.TemporaryDirectory(prefix=".news-restore-", dir=target) as folder:
        staging = Path(folder)
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            names = [m.filename for m in members]
            if len(names) > MAX_FILES + 1 or len(names) != len(set(names)):
                raise BackupError("duplicate or excessive archive entries")
            manifest_info = bundle.getinfo("manifest.json")
            if manifest_info.file_size > MAX_MANIFEST:
                raise BackupError("manifest size limit exceeded")
            manifest = json.loads(bundle.read(manifest_info))
            entries = manifest["files"]
            if (
                manifest["format"] != 1
                or not isinstance(entries, dict)
                or set(entries) | {"manifest.json"} != set(names)
                or "news.sqlite3" not in entries
            ):
                raise BackupError("invalid backup manifest")
            total, canonical = 0, set()
            for member in members:
                if member.filename == "manifest.json":
                    continue
                parts = safe_name(member.filename)
                # Reject case aliases and special files on both Windows and Linux.
                folded = member.filename.casefold()
                mode = member.external_attr >> 16
                if folded in canonical or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise BackupError("aliased or special archive entry")
                canonical.add(folded)
                metadata = entries[member.filename]
                if member.file_size != metadata["size"] or member.file_size > MAX_BYTES - total:
                    raise BackupError("invalid archive size")
                destination = staging.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, destination.open("xb") as output:
                    actual = copy_stream(source, output, MAX_BYTES - total)
                if actual != metadata:
                    raise BackupError("backup hash mismatch")
                total += actual["size"]
                os.chmod(destination, 0o600)
        database_valid(staging / "news.sqlite3")
        if set(target.iterdir()) != {staging}:
            raise BackupError("restore destination changed")
        # Files have all been verified. Publish the database last; writers stay stopped.
        promoted = []
        try:
            for name in (*sorted(TREES), "news.sqlite3"):
                source, destination = staging / name, target / name
                if not source.exists():
                    continue
                if source.resolve().parent != staging or destination.resolve().parent != target:
                    raise BackupError("restore path changed")
                source.rename(destination)
                promoted.append(name)
        except BaseException:
            for name in reversed(promoted):
                (target / name).rename(staging / name)
            raise
    return {"files": len(entries), "bytes": total}


def main(argv=None):
    parser = argparse.ArgumentParser(description="停止服务后备份或恢复应用数据")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--writers-stopped", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            backup(args.data_dir, args.archive, writers_stopped=args.writers_stopped)
            if args.action == "backup"
            else restore(args.archive, args.data_dir, writers_stopped=args.writers_stopped)
        )
    except Exception:
        print(json.dumps({"ok": False, "error": "BACKUP_OR_RESTORE_FAILED"}))
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
