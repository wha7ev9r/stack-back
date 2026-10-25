"""Integration tests for the dedicated Vaultwarden backup mode."""

import pytest

from tests.integration.helpers import restore, snapshot_with_path, started_at

pytestmark = pytest.mark.integration


def test_vaultwarden_status(run_rcb_command):
    exit_code, output = run_rcb_command("status")
    assert exit_code == 0, output
    assert "service: vaultwarden" in output
    assert "vaultwarden (is_ready=True)" in output
    assert "/vaultwarden/vaultwarden" in output
    assert "vaultwarden mode: online" in output
    assert "vaultwarden mode: consistent" in output
    assert "vaultwarden data: /data" in output
    assert "vaultwarden db: /data/db.sqlite3" in output


def test_vaultwarden_online_backup(
    run_rcb_command,
    backup_container,
    vaultwarden_container,
):
    started_before = started_at(vaultwarden_container)

    exit_code, output = run_rcb_command("backup")
    assert exit_code == 0, output

    assert started_at(vaultwarden_container) == started_before, (
        "online mode must not restart Vaultwarden"
    )

    snapshot = snapshot_with_path(backup_container, "/vaultwarden/vaultwarden")
    code, output = backup_container.exec_run(["restic", "ls", snapshot["id"]])
    assert code == 0, output.decode()
    listed = output.decode()
    assert "/vaultwarden/vaultwarden/db.sqlite3" in listed
    assert "/vaultwarden/vaultwarden/attachments/cipher-1/attachment-1" in listed
    assert "/vaultwarden/vaultwarden/sends/send-1/file-1" in listed
    assert "/vaultwarden/vaultwarden/config.json" in listed
    assert "/vaultwarden/vaultwarden/rsa_key.pem" in listed
    assert "/vaultwarden/vaultwarden/rsa_key.pub.pem" in listed
    assert "/vaultwarden/vaultwarden/tmp" not in listed
    assert "/vaultwarden/vaultwarden/icon_cache" not in listed

    restore(backup_container, snapshot["id"], "/tmp/restore-vaultwarden")
    code, output = backup_container.exec_run(
        [
            "python",
            "-c",
            "import sqlite3; c=sqlite3.connect('file:/tmp/restore-vaultwarden/vaultwarden/vaultwarden/db.sqlite3?mode=ro', uri=True); "
            "print(c.execute('PRAGMA integrity_check').fetchone()[0]); "
            "print(c.execute('SELECT count(*) FROM users').fetchone()[0]); c.close()",
        ]
    )
    assert code == 0, output.decode()
    assert output.decode().strip().splitlines() == ["ok", "1"]

    volume_snapshot = snapshot_with_path(backup_container, "/volumes")
    code, output = backup_container.exec_run(["restic", "ls", volume_snapshot["id"]])
    assert code == 0
    assert "/volumes/vaultwarden/data" not in output.decode()


def test_vaultwarden_consistent_mode_restarts_container(
    run_rcb_command,
    backup_container,
    vaultwarden_consistent_container,
):
    started_before = started_at(vaultwarden_consistent_container)

    exit_code, output = run_rcb_command("backup")
    assert exit_code == 0, output

    vaultwarden_consistent_container.reload()
    assert vaultwarden_consistent_container.status == "running"
    assert started_at(vaultwarden_consistent_container) != started_before, (
        "consistent mode should stop and restart Vaultwarden"
    )

    snapshot = snapshot_with_path(
        backup_container, "/vaultwarden/vaultwarden_consistent"
    )
    code, output = backup_container.exec_run(["restic", "ls", snapshot["id"]])
    assert code == 0, output.decode()
    listed = output.decode()
    assert "/vaultwarden/vaultwarden_consistent/db.sqlite3" in listed
    assert "/vaultwarden/vaultwarden_consistent/config.json" in listed
    assert "/vaultwarden/vaultwarden_consistent/rsa_key.pem" in listed
