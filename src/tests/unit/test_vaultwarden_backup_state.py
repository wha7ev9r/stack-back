"""State-machine tests for the Vaultwarden backup handler."""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from restic_compose_backup.containers import RunningContainers
from restic_compose_backup import cli, restic, utils, vaultwarden
from . import fixtures
from .conftest import BaseTestCase

pytestmark = pytest.mark.unit

list_containers_func = "restic_compose_backup.utils.list_containers"


def _service(mode="online"):
    labels = {"stack-back.vaultwarden": "true"}
    if mode:
        labels["stack-back.vaultwarden.mode"] = mode
    return {
        "service": "vaultwarden",
        "image": "vaultwarden/server:latest",
        "labels": labels,
        "mounts": [
            {
                "Source": "vw_data",
                "Destination": "/data",
                "Type": "volume",
                "Name": "vw_data",
            }
        ],
    }


def test_recover_vaultwarden_intents(tmp_path):
    config = SimpleNamespace(vaultwarden_state_dir=str(tmp_path))
    intent_dir = tmp_path / "vaultwarden-stops"
    intent_dir.mkdir()
    intent = intent_dir / "abc.json"
    intent.write_text('{"id": "abc", "name": "vaultwarden"}')

    with (
        mock.patch.object(utils, "container_is_running", return_value=False),
        mock.patch.object(utils, "start_container", return_value=True) as start,
    ):
        cli.recover_vaultwarden_intents(config)

    start.assert_called_once_with("abc")
    assert not intent.exists()


class VaultwardenBackupStateTests(BaseTestCase):
    def _handler(self, mode="online"):
        containers = self.createContainers() + [_service(mode)]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
        return running.get_service("vaultwarden").backup_handlers()[0]

    def _prepare(self, handler, mounted_paths):
        handler._mounted_paths = lambda: mounted_paths
        handler._stage = mock.Mock(return_value=[])
        return handler

    def test_online_mode_does_not_stop(self):
        handler = self._prepare(
            self._handler("online"),
            SimpleNamespace(data_dir=Path("/vaultwarden-source/fake")),
        )
        with (
            mock.patch.object(
                utils, "container_is_running", return_value=True
            ) as running,
            mock.patch.object(utils, "stop_container") as stop,
            mock.patch.object(utils, "start_container") as start,
            mock.patch.object(vaultwarden, "preflight_staging_space", return_value=0),
            mock.patch.object(vaultwarden, "prepare_staging"),
            mock.patch.object(vaultwarden, "cleanup_staging"),
            mock.patch.object(restic, "backup_files", return_value=0) as backup,
        ):
            result = handler.backup()

        assert result == 0
        stop.assert_not_called()
        start.assert_not_called()
        assert running.call_count == 0
        backup.assert_called_once()

    def test_consistent_mode_stops_and_starts(self):
        handler = self._prepare(
            self._handler("consistent"),
            SimpleNamespace(data_dir=Path("/vaultwarden-source/fake")),
        )
        with (
            mock.patch.object(utils, "container_is_running", return_value=True),
            mock.patch.object(utils, "stop_container", return_value=True) as stop,
            mock.patch.object(handler, "_start_with_retry", return_value=True) as start,
            mock.patch.object(vaultwarden, "preflight_staging_space", return_value=0),
            mock.patch.object(vaultwarden, "prepare_staging"),
            mock.patch.object(vaultwarden, "cleanup_staging"),
            mock.patch.object(restic, "backup_files", return_value=0) as backup,
        ):
            result = handler.backup()

        assert result == 0
        stop.assert_called_once()
        start.assert_called_once()
        backup.assert_called_once()

    def test_consistent_stop_failure_aborts(self):
        handler = self._prepare(
            self._handler("consistent"),
            SimpleNamespace(data_dir=Path("/vaultwarden-source/fake")),
        )
        with (
            mock.patch.object(utils, "container_is_running", return_value=True),
            mock.patch.object(utils, "stop_container", return_value=False),
            mock.patch.object(vaultwarden, "cleanup_staging"),
            mock.patch.object(restic, "backup_files") as backup,
        ):
            result = handler.backup()

        assert result == 1
        backup.assert_not_called()

    def test_start_failure_still_uploads_staged_data(self):
        handler = self._prepare(
            self._handler("consistent"),
            SimpleNamespace(data_dir=Path("/vaultwarden-source/fake")),
        )
        with (
            mock.patch.object(utils, "container_is_running", return_value=True),
            mock.patch.object(utils, "stop_container", return_value=True),
            mock.patch.object(handler, "_start_with_retry", return_value=False),
            mock.patch.object(vaultwarden, "preflight_staging_space", return_value=0),
            mock.patch.object(vaultwarden, "prepare_staging"),
            mock.patch.object(vaultwarden, "cleanup_staging"),
            mock.patch.object(restic, "backup_files", return_value=0) as backup,
        ):
            result = handler.backup()

        assert result == 1
        backup.assert_called_once()

    def test_stage_failure_does_not_upload(self):
        handler = self._prepare(
            self._handler("consistent"),
            SimpleNamespace(data_dir=Path("/vaultwarden-source/fake")),
        )
        handler._stage = mock.Mock(side_effect=RuntimeError("copy failed"))
        with (
            mock.patch.object(utils, "container_is_running", return_value=True),
            mock.patch.object(utils, "stop_container", return_value=True),
            mock.patch.object(handler, "_start_with_retry", return_value=True),
            mock.patch.object(vaultwarden, "preflight_staging_space", return_value=0),
            mock.patch.object(vaultwarden, "prepare_staging"),
            mock.patch.object(vaultwarden, "cleanup_staging"),
            mock.patch.object(restic, "backup_files") as backup,
        ):
            result = handler.backup()

        assert result == 1
        backup.assert_not_called()
