"""Integration tests for SQLite online backups."""

import json

import pytest

pytestmark = pytest.mark.integration


def _snapshots(backup_container):
    exit_code, output = backup_container.exec_run("restic snapshots --json")
    assert exit_code == 0, output.decode()
    return json.loads(output.decode())


def _snapshot_with_path(backup_container, path):
    for snapshot in _snapshots(backup_container):
        if any(
            path == item or path.startswith(item + "/") for item in snapshot["paths"]
        ):
            return snapshot
    raise AssertionError(f"no snapshot containing {path}")


def _restore(backup_container, snapshot_id, target):
    backup_container.exec_run(f"rm -rf {target}")
    exit_code, output = backup_container.exec_run(
        ["restic", "restore", snapshot_id, "--target", target]
    )
    assert exit_code == 0, output.decode()


def test_sqlite_status(run_rcb_command):
    exit_code, output = run_rcb_command("status")
    assert exit_code == 0, output
    assert "service: sqlite" in output
    assert "sqlite (is_ready=True)" in output
    assert "/databases/sqlite/data/app.db" in output


def test_live_wal_sqlite_backup_and_volume_exclusion(
    run_rcb_command, backup_container, sqlite_container
):
    exit_code, output = sqlite_container.exec_run("ls /data")
    assert exit_code == 0
    assert "app.db-wal" in output.decode(), "expected an active WAL file"

    exit_code, output = run_rcb_command("backup")
    assert exit_code == 0, output

    sqlite_snapshot = _snapshot_with_path(
        backup_container, "/databases/sqlite/data/app.db"
    )
    _restore(backup_container, sqlite_snapshot["id"], "/tmp/restore-sqlite")

    code, output = backup_container.exec_run(
        [
            "python",
            "-c",
            "import sqlite3; c=sqlite3.connect('file:/tmp/restore-sqlite/databases/sqlite/data/app.db?mode=ro', uri=True); "
            "print(c.execute('PRAGMA integrity_check').fetchone()[0]); "
            "print(c.execute('SELECT count(*) FROM events').fetchone()[0]); c.close()",
        ]
    )
    assert code == 0, output.decode()
    lines = output.decode().strip().splitlines()
    assert lines[0] == "ok"
    assert int(lines[1]) >= 1

    volume_snapshot = _snapshot_with_path(backup_container, "/volumes")
    code, output = backup_container.exec_run(["restic", "ls", volume_snapshot["id"]])
    assert code == 0, output.decode()
    listed = output.decode()
    assert "/volumes/sqlite/data/keep.txt" in listed
    assert "/volumes/sqlite/data/app.db" not in listed
    assert "/volumes/sqlite/data/app.db-wal" not in listed
    assert "/volumes/sqlite/data/app.db-shm" not in listed


def test_idle_wal_sqlite_only_backup(
    run_rcb_command, backup_container, sqlite_only_container
):
    code, _ = sqlite_only_container.exec_run(
        ["sh", "-c", "test ! -e /data/only.db-wal"]
    )
    assert code == 0, "idle SQLite database should not have a WAL sidecar"

    exit_code, output = run_rcb_command("backup")
    assert exit_code == 0, output

    snapshot = _snapshot_with_path(
        backup_container, "/databases/sqlite_only/data/only.db"
    )
    _restore(backup_container, snapshot["id"], "/tmp/restore-sqlite-only")

    code, output = backup_container.exec_run(
        [
            "python",
            "-c",
            "import sqlite3; c=sqlite3.connect('/tmp/restore-sqlite-only/databases/sqlite_only/data/only.db'); "
            "print(c.execute('PRAGMA integrity_check').fetchone()[0]); "
            "print(c.execute('SELECT value FROM items').fetchone()[0]); c.close()",
        ]
    )
    assert code == 0, output.decode()
    assert output.decode().strip().splitlines() == ["ok", "idle"]

    volume_snapshot = _snapshot_with_path(backup_container, "/volumes")
    code, output = backup_container.exec_run(["restic", "ls", volume_snapshot["id"]])
    assert code == 0
    assert "/volumes/sqlite_only" not in output.decode()
