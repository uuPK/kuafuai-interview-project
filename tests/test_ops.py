import io
import json
import sqlite3
import stat
import zipfile
from contextlib import closing

import pytest

from app import ops
from app.config import Settings
from app.storage.database import Database
from app.storage.models import RunRequest, SubscriptionSnapshot
from app.storage.runs import RunStore


@pytest.fixture
def offline(tmp_path):
    root = tmp_path / "original"
    settings = Settings(_env_file=None, data_dir=root)
    db = Database(settings.database_path)
    db.initialize()
    run = RunStore(db, settings).create_run(
        RunRequest(
            user_id="backup-reader",
            subscription_id="test",
            subscription=SubscriptionSnapshot(
                nickname="读者", email="reader@example.com", topics=("AI",)
            ),
        )
    )
    for tree, name in (
        ("runs", "scratch/中文.txt"),
        ("source_jobs", "source.json"),
        ("file_jobs", "intent.json"),
    ):
        path = root / tree / run.id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("固定正文、索引或恢复意图", encoding="utf-8")
    (root / ".env").write_text("SECRET=must-not-be-in-archive", encoding="utf-8")
    (root / "tool_runtime").mkdir()
    (root / "tool_runtime/config.json").write_text("excluded", encoding="utf-8")
    return root, tmp_path / "copy.zip", run, settings


def test_round_trip_preserves_db_window_counts_files_and_recovery_journals(offline, tmp_path):
    root, archive, run, settings = offline
    # Leave committed content in WAL while a connection remains open.
    with closing(sqlite3.connect(settings.database_path, autocommit=True)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE runs SET budget_usage_json=json_set(budget_usage_json,'$.model_requests',2) "
            "WHERE id=?",
            (run.id,),
        )
        result = ops.backup(root, archive, writers_stopped=True)
        target = tmp_path / "restored"
        assert ops.restore(archive, target, writers_stopped=True) == result
    restored = RunStore(Database(target / "news.sqlite3"), settings).get_run(run.id, run.user_id)
    assert restored.budget_usage.model_requests == 2
    assert restored.window_start == run.window_start and restored.window_end == run.window_end
    assert not (target / ".env").exists() and not (target / "tool_runtime").exists()
    for tree in ops.TREES:
        for path in (root / tree).rglob("*"):
            if path.is_file():
                assert (target / path.relative_to(root)).read_bytes() == path.read_bytes()
    assert not list(target.glob("*.sqlite3-wal"))


def mutate(archive, edit):
    with zipfile.ZipFile(archive) as bundle:
        entries = [(item, bundle.read(item)) for item in bundle.infolist()]
    with zipfile.ZipFile(archive, "w") as bundle:
        for item, content in edit(entries):
            bundle.writestr(item, content)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/outside",
        "runs/a/../../outside",
        "runs/a\\evil",
        "runs/a/C:evil",
        "runs/a/NUL.txt",
        "runs/a/foo.",
        "runs//a/foo",
        ".env",
    ],
)
def test_restore_rejects_unsafe_paths_without_publishing(offline, tmp_path, name):
    root, archive, *_ = offline
    ops.backup(root, archive, writers_stopped=True)

    def edit(entries):
        manifest = json.loads(
            next(value for item, value in entries if item.filename == "manifest.json")
        )
        manifest["files"][name] = {"size": 0, "sha256": "0" * 64}
        return [
            (item, json.dumps(manifest).encode() if item.filename == "manifest.json" else value)
            for item, value in entries
        ] + [(zipfile.ZipInfo(name), b"")]

    mutate(archive, edit)
    target = tmp_path / "safe"
    with pytest.raises(ops.BackupError):
        ops.restore(archive, target, writers_stopped=True)
    assert list(target.iterdir()) == [] and not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    "mode", ["changed_body", "extra_entry", "symlink", "corrupt_db", "large_file", "future_schema"]
)
def test_corruption_special_entries_and_limits_never_publish(offline, tmp_path, monkeypatch, mode):
    root, archive, *_ = offline
    ops.backup(root, archive, writers_stopped=True)

    def edit(entries):
        if mode == "extra_entry":
            return entries + [(zipfile.ZipInfo("runs/a/extra"), b"bad")]
        changed = []
        for item, value in entries:
            if mode == "changed_body" and item.filename.endswith("中文.txt"):
                value = b"changed"
            if mode == "symlink" and item.filename.endswith("中文.txt"):
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
            if mode == "corrupt_db" and item.filename == "news.sqlite3":
                value = b"not a database"
            changed.append((item, value))
        return changed

    if mode == "future_schema":
        with closing(sqlite3.connect(root / "news.sqlite3", autocommit=True)) as db:
            db.execute(f"PRAGMA user_version={ops.SCHEMA_VERSION + 1}")
        with pytest.raises(ops.BackupError):
            ops.backup(root, tmp_path / "future.zip", writers_stopped=True)
        return
    mutate(archive, edit)
    if mode == "large_file":
        monkeypatch.setattr(ops, "MAX_BYTES", 1)
    target = tmp_path / "restore"
    with pytest.raises((ops.BackupError, sqlite3.DatabaseError)):
        ops.restore(archive, target, writers_stopped=True)
    assert list(target.iterdir()) == []


def test_refuses_active_mode_overwrite_and_nonempty_restore(offline, tmp_path):
    root, archive, *_ = offline
    with pytest.raises(ops.BackupError, match="stop all writers"):
        ops.backup(root, archive)
    with pytest.raises(ops.BackupError):
        ops.backup(root, root / "copy.zip", writers_stopped=True)
    ops.backup(root, archive, writers_stopped=True)
    original = archive.read_bytes()
    with pytest.raises(ops.BackupError):
        ops.backup(root, archive, writers_stopped=True)
    assert archive.read_bytes() == original
    with pytest.raises(ops.BackupError):
        ops.restore(archive, root, writers_stopped=True)
    with pytest.raises(ops.BackupError):
        ops.restore(archive, tmp_path / "new")


def test_backup_rejects_links_and_empty_or_invalid_database(offline, tmp_path):
    root, archive, *_ = offline
    link = root / "runs/linked"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ops.BackupError):
        ops.backup(root, archive, writers_stopped=True)
    assert not archive.exists()


def test_file_limit_and_main_sanitize_errors(offline, monkeypatch, capsys):
    root, archive, *_ = offline
    monkeypatch.setattr(ops, "MAX_FILES", 1)
    assert (
        ops.main(
            ["backup", "--data-dir", str(root), "--archive", str(archive), "--writers-stopped"]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "BACKUP_OR_RESTORE_FAILED"}
    assert not archive.exists()


def test_stream_hash_counts_and_size_limit():
    output = io.BytesIO()
    assert ops.copy_stream(io.BytesIO(b"abc"), output, 3)["size"] == 3
    with pytest.raises(ops.BackupError):
        ops.copy_stream(io.BytesIO(b"abc"), io.BytesIO(), 2)


def test_partial_publication_is_rolled_back(offline, tmp_path, monkeypatch):
    root, archive, *_ = offline
    ops.backup(root, archive, writers_stopped=True)
    original = type(root).rename

    def fail_database(self, target):
        if self.name == "news.sqlite3":
            raise OSError("fixture promotion interrupted")
        return original(self, target)

    monkeypatch.setattr(type(root), "rename", fail_database)
    target = tmp_path / "empty"
    with pytest.raises(OSError):
        ops.restore(archive, target, writers_stopped=True)
    assert list(target.iterdir()) == []


def test_material_tree_must_be_directory(offline):
    root, archive, *_ = offline
    (root / "runs").rename(root / "other")
    (root / "runs").write_text("invalid", encoding="utf-8")
    with pytest.raises(ops.BackupError):
        ops.backup(root, archive, writers_stopped=True)


def test_cli_success(offline, tmp_path, capsys):
    root, archive, *_ = offline
    assert (
        ops.main(
            ["backup", "--data-dir", str(root), "--archive", str(archive), "--writers-stopped"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert (
        ops.main(
            [
                "restore",
                "--data-dir",
                str(tmp_path / "restored"),
                "--archive",
                str(archive),
                "--writers-stopped",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True
