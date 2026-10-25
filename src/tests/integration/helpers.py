"""Shared helpers for integration tests."""

import json


def snapshots(backup_container):
    exit_code, output = backup_container.exec_run("restic snapshots --json")
    assert exit_code == 0, output.decode()
    return json.loads(output.decode())


def snapshot_with_path(backup_container, path, *, prefix=False):
    for snapshot in snapshots(backup_container):
        if prefix:
            if any(
                path == item or path.startswith(item + "/")
                for item in snapshot["paths"]
            ):
                return snapshot
        elif path in snapshot["paths"]:
            return snapshot
    raise AssertionError(f"no snapshot containing {path}")


def restore(backup_container, snapshot_id, target):
    backup_container.exec_run(f"rm -rf {target}")
    exit_code, output = backup_container.exec_run(
        ["restic", "restore", snapshot_id, "--target", target]
    )
    assert exit_code == 0, output.decode()


def started_at(container):
    container.reload()
    return container.attrs["State"]["StartedAt"]
