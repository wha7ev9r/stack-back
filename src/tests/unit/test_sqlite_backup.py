"""Tests for the portable SQLite online-backup helpers."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from restic_compose_backup import sqlite_backup

pytestmark = pytest.mark.unit


def create_database(path: Path, *, wal: bool = True, keep_open: bool = False):
    connection = sqlite3.connect(str(path))
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT)")
    connection.execute("INSERT INTO events (note) VALUES ('first')")
    connection.commit()
    if not keep_open:
        connection.close()
        return None
    return connection


def test_find_sqlite_databases(tmp_path):
    create_database(tmp_path / "app.db")
    create_database(tmp_path / "extensionless", wal=False)
    (tmp_path / "notes.txt").write_text("not a database")
    (tmp_path / "app.db-wal").write_bytes(b"sidecar")
    (tmp_path / "link.db").symlink_to(tmp_path / "app.db")

    found = {path.name for path in sqlite_backup.find_sqlite_databases(tmp_path)}
    assert found == {"app.db", "extensionless"}


def test_export_database_read_only_live_wal(tmp_path):
    database = tmp_path / "live.db"
    connection = create_database(database, keep_open=True)
    try:
        assert (tmp_path / "live.db-wal").exists()
        destination = tmp_path / "read-only-export.db"
        sqlite_backup.export_database(
            database,
            destination,
            read_only=True,
            verify="full",
        )
        sqlite_backup.check_database(destination, verify="full")
    finally:
        connection.close()


def test_normalize_paths():
    paths, errors = sqlite_backup.normalize_sqlite_paths(
        " /data/app.db, /data/cache.db ,, /data/app.db "
    )
    assert paths == ["/data/app.db", "/data/cache.db"]
    assert errors == []

    _paths, errors = sqlite_backup.normalize_sqlite_paths(
        "relative.db, /data/../etc/passwd, /data/*.db, /"
    )
    assert len(errors) == 4


def test_normalize_paths_non_string():
    paths, errors = sqlite_backup.normalize_sqlite_paths(["not", "a", "string"])
    assert paths == []
    assert errors


def test_mount_relative_path():
    assert sqlite_backup.mount_relative_path("/data/app.db", "/data") == "app.db"
    assert sqlite_backup.mount_relative_path("/data", "/data") == ""
    assert sqlite_backup.mount_relative_path("/database/app.db", "/data") is None
    assert sqlite_backup.mount_relative_path("/data/app.db", "/") == "data/app.db"


def test_find_mount_for_path_prefers_longest_match():
    mounts = [
        SimpleNamespace(source="vol", destination="/data", type="volume"),
        SimpleNamespace(source="bind", destination="/data/nested", type="bind"),
    ]
    selected = sqlite_backup.find_mount_for_path("/data/nested/app.db", mounts)
    assert selected.source == "bind"

    selected = sqlite_backup.find_mount_for_path(
        "/data/nested/app.db", mounts, exclude_bind_mounts=True
    )
    assert selected.source == "vol"

    assert sqlite_backup.find_mount_for_path("/database/app.db", mounts) is None


def test_mount_target_is_deterministic():
    first = sqlite_backup.sqlite_mount_target("volume-name")
    second = sqlite_backup.sqlite_mount_target("volume-name")
    assert first == second
    assert first.startswith("/sqlite/")
    assert first != sqlite_backup.sqlite_mount_target("other-volume")


def test_escape_restic_pattern():
    escaped = sqlite_backup.escape_restic_pattern("/data/app [1] #?.db")
    assert escaped == "/data/app \\[1] #\\?.db"


def test_export_active_wal_database(tmp_path):
    source = tmp_path / "app.db"
    destination = tmp_path / "export.db"
    connection = create_database(source, keep_open=True)
    try:
        sqlite_backup.export_database(source, destination, verify="full")
    finally:
        connection.close()

    assert destination.is_file()
    assert not sqlite_backup.sidecar_paths(destination)[0].exists()

    with sqlite3.connect(str(destination)) as exported:
        assert exported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert exported.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert exported.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"

    # The exported file is a standalone rollback-journal database and can be
    # opened read-only.
    read_only = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
    try:
        assert read_only.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    finally:
        read_only.close()


def test_export_idle_wal_database(tmp_path):
    source = tmp_path / "idle.db"
    destination = tmp_path / "idle-export.db"
    create_database(source, keep_open=False)
    assert not Path(str(source) + "-wal").exists()

    sqlite_backup.export_database(source, destination, verify="quick")

    with sqlite3.connect(str(destination)) as exported:
        assert exported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_export_rejects_empty_or_missing_file(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")

    with pytest.raises(sqlite_backup.SqliteBackupError):
        sqlite_backup.export_database(empty, tmp_path / "out.db")

    with pytest.raises(sqlite_backup.SqliteBackupError):
        sqlite_backup.export_database(tmp_path / "missing.db", tmp_path / "out.db")


def test_export_rejects_directory_and_plain_file(tmp_path):
    with pytest.raises(sqlite_backup.SqliteBackupError):
        sqlite_backup.export_database(tmp_path, tmp_path / "out.db")

    plain = tmp_path / "not-sqlite.db"
    plain.write_text("not a database")
    with pytest.raises(sqlite_backup.SqliteBackupError):
        sqlite_backup.export_database(plain, tmp_path / "out.db")


def test_required_tables_validation(tmp_path):
    source = tmp_path / "plain.db"
    destination = tmp_path / "plain-export.db"
    create_database(source, keep_open=False)

    with pytest.raises(sqlite_backup.SqliteBackupError):
        sqlite_backup.export_database(
            source,
            destination,
            required_tables=("users",),
        )
    assert not destination.exists()


def test_export_special_characters_in_filename(tmp_path):
    source = tmp_path / "app [1] #.db"
    destination = tmp_path / "export [1] #.db"
    create_database(source, keep_open=False)
    sqlite_backup.export_database(source, destination)
    assert destination.is_file()


def test_journal_mode_delete_database_is_supported(tmp_path):
    source = tmp_path / "delete-mode.db"
    destination = tmp_path / "delete-mode-export.db"
    connection = sqlite3.connect(str(source))
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("CREATE TABLE t (v TEXT)")
    connection.execute("INSERT INTO t VALUES ('x')")
    connection.commit()
    connection.close()

    sqlite_backup.export_database(source, destination)
    sqlite_backup.check_database(destination, verify="full")
