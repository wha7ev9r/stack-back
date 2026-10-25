"""Unit tests for SQLite/Vaultwarden container configuration."""

from unittest import mock

import pytest

from restic_compose_backup.containers import RunningContainers
from restic_compose_backup.containers_db import SqliteContainer, VaultwardenContainer
from . import fixtures
from .conftest import BaseTestCase

pytestmark = pytest.mark.unit

list_containers_func = "restic_compose_backup.utils.list_containers"


class SqliteVaultwardenConfigurationTests(BaseTestCase):
    def test_sqlite_handler_configuration(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "app",
                "image": "python:3.13-alpine",
                "labels": {
                    "stack-back.sqlite": "true",
                    "stack-back.sqlite.paths": "/data/app.db,/data/cache.db",
                },
                "mounts": [
                    {
                        "Source": "app_data",
                        "Destination": "/data",
                        "Type": "volume",
                        "Name": "app_data",
                    }
                ],
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
        service = running.get_service("app")
        assert service is not None
        assert service.sqlite_backup_enabled
        assert service.sqlite_paths == ["/data/app.db", "/data/cache.db"]
        assert service.backup_enabled
        handlers = service.backup_handlers()
        assert len(handlers) == 1
        assert isinstance(handlers[0], SqliteContainer)
        assert service.stop_during_backup is False
        assert handlers[0].backup_destination_paths()[0].as_posix() == (
            "/databases/app/data/app.db"
        )

    def test_sqlite_invalid_paths(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "app",
                "labels": {
                    "stack-back.sqlite": "true",
                    "stack-back.sqlite.paths": "relative.db, /data/../etc/passwd",
                },
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
        service = running.get_service("app")
        assert service.sqlite_paths == []
        assert len(service.sqlite_path_errors) == 2

    def test_sqlite_dedicated_mount_and_excludes(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "app",
                "labels": {
                    "stack-back.sqlite": "true",
                    "stack-back.sqlite.paths": "/data/app.db",
                    "stack-back.volumes": "true",
                },
                "mounts": [
                    {
                        "Source": "app_data",
                        "Destination": "/data",
                        "Type": "volume",
                        "Name": "app_data",
                    }
                ],
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
            dedicated = running.generate_dedicated_mounts()
            excludes = running.generate_volume_snapshot_excludes()

        assert len(dedicated) == 1
        assert len(excludes) >= 5
        assert "/volumes/app/data/app.db" in excludes
        assert "/volumes/app/data/app.db-wal" in excludes
        assert "/volumes/app/data/app.db-shm" in excludes

    def test_ancestor_volume_exclude(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "app",
                "labels": {
                    "stack-back.sqlite": "true",
                    "stack-back.sqlite.paths": "/data/app.db",
                },
                "mounts": [
                    {
                        "Source": "/srv/app",
                        "Destination": "/data",
                        "Type": "bind",
                    }
                ],
            },
            {
                "service": "parent",
                "labels": {"stack-back.volumes": "true"},
                "mounts": [
                    {
                        "Source": "/srv",
                        "Destination": "/srv",
                        "Type": "bind",
                    }
                ],
            },
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
            excludes = running.generate_volume_snapshot_excludes()

        assert "/volumes/parent/srv/app/app.db" in excludes
        assert "/volumes/parent/srv/app/app.db-wal" in excludes

    def test_sqlite_stop_during_backup_is_allowed(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "app",
                "labels": {
                    "stack-back.sqlite": "true",
                    "stack-back.sqlite.paths": "/data/app.db",
                    "stack-back.volumes.stop-during-backup": "true",
                },
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
        service = running.get_service("app")
        assert service.stop_during_backup is True

    def test_sqlite_file_bind_and_tmpfs_are_rejected(self):
        for mount in (
            {
                "Source": "/srv/app.db",
                "Destination": "/data/app.db",
                "Type": "bind",
            },
            {
                "Source": "tmpfs",
                "Destination": "/data",
                "Type": "tmpfs",
            },
        ):
            containers = self.createContainers()
            containers += [
                {
                    "service": "app",
                    "labels": {
                        "stack-back.sqlite": "true",
                        "stack-back.sqlite.paths": "/data/app.db",
                    },
                    "mounts": [mount],
                }
            ]
            with mock.patch(
                list_containers_func, fixtures.containers(containers=containers)
            ):
                running = RunningContainers()
            handler = running.get_service("app").backup_handlers()[0]
            assert handler._mount_and_rel("/data/app.db") == (None, None)

    def test_vaultwarden_handler_configuration(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "vaultwarden",
                "image": "vaultwarden/server:latest",
                "labels": {
                    "stack-back.vaultwarden": "true",
                    "stack-back.vaultwarden.mode": "online",
                },
                "mounts": [
                    {
                        "Source": "vw_data",
                        "Destination": "/data",
                        "Type": "volume",
                        "Name": "vw_data",
                    }
                ],
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
        service = running.get_service("vaultwarden")
        assert service is not None
        assert service.vaultwarden_backup_enabled
        assert service.vaultwarden_mode == "online"
        assert service.stop_during_backup is False
        handlers = service.backup_handlers()
        assert len(handlers) == 1
        assert isinstance(handlers[0], VaultwardenContainer)

    def test_vaultwarden_default_mode_and_icon_cache(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "vaultwarden",
                "labels": {"stack-back.vaultwarden": "true"},
                "mounts": [
                    {
                        "Source": "vw_data",
                        "Destination": "/data",
                        "Type": "volume",
                        "Name": "vw_data",
                    }
                ],
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
        service = running.get_service("vaultwarden")
        assert service.vaultwarden_mode == "online"
        assert service.vaultwarden_icon_cache_enabled is False
        excludes = running.generate_volume_snapshot_excludes()
        assert excludes == []

    def test_vaultwarden_excludes_whole_data_directory(self):
        containers = self.createContainers()
        containers += [
            {
                "service": "vaultwarden",
                "labels": {
                    "stack-back.vaultwarden": "true",
                    "stack-back.volumes": "true",
                },
                "mounts": [
                    {
                        "Source": "vw_data",
                        "Destination": "/data",
                        "Type": "volume",
                        "Name": "vw_data",
                    }
                ],
            }
        ]
        with mock.patch(
            list_containers_func, fixtures.containers(containers=containers)
        ):
            running = RunningContainers()
            excludes = running.generate_volume_snapshot_excludes()
        assert "/volumes/vaultwarden/data" in excludes
