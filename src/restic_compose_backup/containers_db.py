import logging
import posixpath
import shutil
import tempfile
import time
from pathlib import Path

from restic_compose_backup.containers import Container
from restic_compose_backup.config import config, Config
from restic_compose_backup import (
    commands,
    enums,
    restic,
    sqlite_backup,
    utils,
    vaultwarden,
)

logger = logging.getLogger(__name__)


def _config_float(name, default):
    try:
        return float(getattr(config, name))
    except (TypeError, ValueError):
        logger.warning("Invalid %s value, using %s", name, default)
        return default


class MariadbContainer(Container):
    container_type = "mariadb"

    def get_credentials(self) -> dict:
        """dict: get credentials for the service"""
        password = self.get_config_env("MARIADB_ROOT_PASSWORD")
        if password is not None:
            username = "root"
        else:
            username = self.get_config_env("MARIADB_USER")
            password = self.get_config_env("MARIADB_PASSWORD")
        return {
            "host": "127.0.0.1",
            "username": username,
            "password": password,
            "port": "3306",
        }

    def ping(self) -> bool:
        """Check the availability of the service"""
        creds = self.get_credentials()

        return (
            commands.ping_mariadb(
                self.id,
                creds["host"],
                creds["port"],
                creds["username"],
                creds["password"],
            )
            == 0
        )

    def dump_command(self) -> list:
        """list: create a dump command restic and use to send data through stdin"""
        creds = self.get_credentials()
        return [
            "mariadb-dump",
            f"--user={creds['username']}",
            "--all-databases",
            "--no-tablespaces",
            "--single-transaction",
            "--order-by-primary",
            "--compact",
            "--force",
        ]

    def backup(self):
        config = Config()
        creds = self.get_credentials()

        return restic.backup_from_stdin(
            config.repository,
            self.backup_destination_path(),
            self.id,
            self.dump_command(),
            environment={"MYSQL_PWD": creds["password"]},
        )

    def backup_destination_path(self) -> str:
        destination = Path("/databases")

        if utils.is_true(config.include_project_name):
            project_name = self.project_name
            if project_name != "":
                destination /= project_name

        destination /= self.service_name
        destination /= "all_databases.sql"

        return destination


class MysqlContainer(Container):
    container_type = "mysql"

    def get_credentials(self) -> dict:
        """dict: get credentials for the service"""
        password = self.get_config_env("MYSQL_ROOT_PASSWORD")
        if password is not None:
            username = "root"
        else:
            username = self.get_config_env("MYSQL_USER")
            password = self.get_config_env("MYSQL_PASSWORD")
        return {
            "host": "127.0.0.1",
            "username": username,
            "password": password,
            "port": "3306",
        }

    def ping(self) -> bool:
        """Check the availability of the service"""
        creds = self.get_credentials()

        return (
            commands.ping_mysql(
                self.id,
                creds["host"],
                creds["port"],
                creds["username"],
                creds["password"],
            )
            == 0
        )

    def dump_command(self) -> list:
        """list: create a dump command restic and use to send data through stdin"""
        creds = self.get_credentials()
        return [
            "mysqldump",
            f"--user={creds['username']}",
            "--all-databases",
            "--no-tablespaces",
            "--single-transaction",
            "--order-by-primary",
            "--compact",
            "--force",
        ]

    def backup(self):
        config = Config()
        creds = self.get_credentials()

        return restic.backup_from_stdin(
            config.repository,
            self.backup_destination_path(),
            self.id,
            self.dump_command(),
            environment={"MYSQL_PWD": creds["password"]},
        )

    def backup_destination_path(self) -> str:
        destination = Path("/databases")

        if utils.is_true(config.include_project_name):
            project_name = self.project_name
            if project_name != "":
                destination /= project_name

        destination /= self.service_name
        destination /= "all_databases.sql"

        return destination


class PostgresContainer(Container):
    container_type = "postgres"

    def get_credentials(self) -> dict:
        """dict: get credentials for the service"""
        return {
            "host": "127.0.0.1",
            "username": self.get_config_env("POSTGRES_USER"),
            "password": self.get_config_env("POSTGRES_PASSWORD"),
            "port": "5432",
            "database": self.get_config_env("POSTGRES_DB"),
        }

    def ping(self) -> bool:
        """Check the availability of the service"""
        creds = self.get_credentials()
        return (
            commands.ping_postgres(
                self.id,
                creds["host"],
                creds["port"],
                creds["username"],
                creds["password"],
            )
            == 0
        )

    def dump_command(self) -> list:
        """list: create a dump command restic and use to send data through stdin"""
        # NOTE: Backs up a single database from POSTGRES_DB env var
        creds = self.get_credentials()
        return [
            "pg_dump",
            f"--username={creds['username']}",
            creds["database"],
        ]

    def backup(self):
        config = Config()
        creds = self.get_credentials()

        return restic.backup_from_stdin(
            config.repository,
            self.backup_destination_path(),
            self.id,
            self.dump_command(),
        )

    def backup_destination_path(self) -> str:
        destination = Path("/databases")

        if utils.is_true(config.include_project_name):
            project_name = self.project_name
            if project_name != "":
                destination /= project_name

        destination /= self.service_name
        destination /= f"{self.get_credentials()['database']}.sql"

        return destination


class SqliteContainer(Container):
    container_type = "sqlite"

    _auto_paths = None

    def _auto_mounts(self):
        """Return ``(mount, read-only root)`` pairs for auto discovery."""
        roots = []
        exclude_bind = utils.is_true(config.exclude_bind_mounts)
        for mount in self._mounts:
            if mount.type not in ("volume", "bind"):
                continue
            if exclude_bind and mount.type == "bind":
                continue
            root = Path(sqlite_backup.sqlite_mount_target(mount.source))
            if not root.is_dir():
                continue
            roots.append((mount, root))
        return roots

    def discover(self) -> list[str]:
        """Find SQLite databases under this service's auto-mounted volumes."""
        if not self.sqlite_auto_enabled:
            return []
        if self._auto_paths is not None:
            return self._auto_paths

        paths = []
        roots = self._auto_mounts()
        if not roots:
            logger.error(
                "SQLite auto-detection for service %s has no readable mounts; "
                "check EXCLUDE_BIND_MOUNTS and that the service is running",
                self.service_name,
            )
        for mount, root in roots:
            for database in sqlite_backup.find_sqlite_databases(root):
                relative = database.relative_to(root).as_posix()
                path = posixpath.normpath(
                    posixpath.join(mount.destination, relative)
                )
                if path not in paths:
                    paths.append(path)

        self._auto_paths = paths
        if paths:
            logger.info(
                "Auto-detected %s SQLite database(s) in service %s: %s",
                len(paths),
                self.service_name,
                ", ".join(paths),
            )
        else:
            logger.error(
                "SQLite auto-detection found no databases in service %s",
                self.service_name,
            )
        return paths

    def _mount_and_rel(self, path: str):
        mount = self.mount_for_path(
            path,
            exclude_bind_mounts=utils.is_true(config.exclude_bind_mounts),
        )
        if mount is None:
            if utils.is_true(config.exclude_bind_mounts):
                logger.error(
                    "SQLite database %s is on a bind mount but EXCLUDE_BIND_MOUNTS is enabled",
                    path,
                )
            else:
                logger.error("SQLite database %s is not on a mounted volume/bind", path)
            return None, None

        if posixpath.normpath(path) == posixpath.normpath(mount.destination):
            logger.error(
                "SQLite database %s must be a file inside a directory mount; "
                "single-file bind mounts and mount roots are not supported",
                path,
            )
            return None, None

        relative = sqlite_backup.mount_relative_path(path, mount.destination)
        if relative is None:
            logger.error(
                "SQLite database %s is outside mount destination %s",
                path,
                mount.destination,
            )
            return None, None
        return mount, relative

    def _source_path(self, path: str) -> Path:
        mount, relative = self._mount_and_rel(path)
        if mount is None:
            raise sqlite_backup.SqliteBackupError(
                f"SQLite database {path} is not accessible from the backup container"
            )
        target = Path(sqlite_backup.sqlite_mount_target(mount.source))
        source = target / relative if relative else target
        if not source.is_file():
            raise sqlite_backup.SqliteBackupError(
                f"SQLite database {path} was not found at {source} inside the "
                "backup container"
            )
        if not source.resolve().is_relative_to(target.resolve()):
            raise sqlite_backup.SqliteBackupError(
                f"SQLite database {path} resolves outside its mounted volume"
            )
        return source

    def ping(self) -> bool:
        if self.sqlite_auto_enabled:
            exclude_bind = utils.is_true(config.exclude_bind_mounts)
            mounts = [
                mount
                for mount in self._mounts
                if mount.type in ("volume", "bind")
                and not (exclude_bind and mount.type == "bind")
            ]
            if not mounts:
                logger.error(
                    "SQLite auto-detection is enabled for service %s but it has "
                    "no usable volume/bind mounts",
                    self.service_name,
                )
                return False
            logger.info(
                "SQLite auto-detection is enabled for service %s; databases will "
                "be discovered with read-only mounts at backup time",
                self.service_name,
            )
            return True

        if self.sqlite_path_errors:
            for error in self.sqlite_path_errors:
                logger.error("Invalid stack-back.sqlite.paths entry: %s", error)
            return False
        if not self.sqlite_paths:
            logger.error(
                "stack-back.sqlite is enabled for service %s but no paths are configured",
                self.service_name,
            )
            return False

        ready = True
        for path in self.sqlite_paths:
            mount, _relative = self._mount_and_rel(path)
            if mount is None:
                ready = False
                continue
            if not commands.docker_path_is_file(self.id, path):
                logger.error(
                    "SQLite database %s was not found in service %s",
                    path,
                    self.service_name,
                )
                ready = False
        return ready

    def dedicated_mounts(self) -> list:
        specs = {}
        if self.sqlite_auto_enabled:
            exclude_bind = utils.is_true(config.exclude_bind_mounts)
            for mount in self._mounts:
                if mount.type not in ("volume", "bind"):
                    continue
                if exclude_bind and mount.type == "bind":
                    logger.error(
                        "SQLite auto-detection for service %s includes a bind "
                        "mount but EXCLUDE_BIND_MOUNTS is enabled",
                        self.service_name,
                    )
                    continue
                source = mount.name if mount.type == "volume" else mount.source
                if not source:
                    logger.error(
                        "Could not determine source for SQLite auto mount %s", mount
                    )
                    continue
                target = sqlite_backup.sqlite_mount_target(mount.source)
                specs[(mount.type, source, target)] = {
                    "type": mount.type,
                    "source": source,
                    "target": target,
                    "read_only": True,
                }
            return list(specs.values())

        for path in self.sqlite_paths:
            mount, _relative = self._mount_and_rel(path)
            if mount is None:
                continue
            source = mount.name if mount.type == "volume" else mount.source
            if not source:
                logger.error("Could not determine source for SQLite mount %s", mount)
                continue
            target = sqlite_backup.sqlite_mount_target(mount.source)
            specs[(mount.type, source, target)] = {
                "type": mount.type,
                "source": source,
                "target": target,
                "read_only": False,
            }
        return list(specs.values())

    def volume_exclude_targets(self) -> list:
        targets = {}
        paths = self._auto_paths if self.sqlite_auto_enabled else self.sqlite_paths
        if paths is None:
            return []
        for path in paths:
            mount, relative = self._mount_and_rel(path)
            if mount is None:
                continue
            targets[(mount.source, relative)] = {
                "source": mount.source,
                "rel": relative,
                "kind": "file",
            }
        return list(targets.values())

    def backup_destination_paths(self) -> list:
        destination = Path("/databases")
        if utils.is_true(config.include_project_name) and self.project_name:
            destination /= self.project_name
        destination /= self.service_name
        paths = self._auto_paths if self.sqlite_auto_enabled else self.sqlite_paths
        if paths is None:
            return []
        return [destination / utils.strip_root(path) for path in paths]

    def backup_destination_path(self) -> str:
        destinations = self.backup_destination_paths()
        if len(destinations) != 1:
            raise ValueError("SQLite service has multiple database paths")
        return destinations[0]

    def backup(self) -> int:
        local_config = Config()
        if self.sqlite_path_errors:
            for error in self.sqlite_path_errors:
                logger.error("Invalid stack-back.sqlite.paths entry: %s", error)
            return 1

        if self.sqlite_auto_enabled:
            paths = self.discover()
            if not paths:
                return 1
        else:
            paths = self.sqlite_paths
            if not paths:
                logger.error(
                    "stack-back.sqlite is enabled for service %s but no paths are configured",
                    self.service_name,
                )
                return 1

        temp_root = Path(config.sqlite_temp_dir)
        temp_root.mkdir(parents=True, exist_ok=True)
        result = 0
        workdir = Path(tempfile.mkdtemp(prefix="rcb-sqlite-", dir=temp_root))
        try:
            for index, path in enumerate(paths):
                destination = workdir / f"{index:03d}.db"
                try:
                    source = self._source_path(path)
                    sqlite_backup.ensure_free_space(source.stat().st_size, temp_root)
                    logger.info(
                        "Exporting SQLite database %s from service %s",
                        path,
                        self.service_name,
                    )
                    sqlite_backup.export_database(
                        source,
                        destination,
                        busy_timeout=_config_float("sqlite_busy_timeout", 30.0),
                        verify="quick",
                        read_only=self.sqlite_auto_enabled,
                    )
                    exit_code = restic.backup_stdin_file(
                        local_config.repository,
                        self.backup_destination_paths()[index],
                        destination,
                    )
                    if exit_code != 0:
                        logger.error(
                            "Restic exited with code %s for SQLite database %s",
                            exit_code,
                            path,
                        )
                        result = 1
                except Exception as ex:
                    logger.error("Failed to back up SQLite database %s: %s", path, ex)
                    logger.debug("SQLite backup failure", exc_info=True)
                    result = 1
                finally:
                    try:
                        destination.unlink()
                    except FileNotFoundError:
                        pass
                    sqlite_backup.remove_sidecars(destination)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return result


class VaultwardenContainer(Container):
    container_type = "vaultwarden"

    def _paths(self) -> vaultwarden.VaultwardenPaths:
        return vaultwarden.resolve_paths(
            self.environment,
            data_dir_override=self.vaultwarden_data_dir_override,
        )

    def resolved_paths(self) -> vaultwarden.VaultwardenPaths:
        """Public path details for ``rcb status``."""
        return self._paths()

    def rsa_key_present(self) -> bool:
        """bool: Whether any known Vaultwarden RSA key variant exists."""
        base = self._paths().rsa_key
        candidates = {base}
        for suffix in (".pem", ".der", ".pub.pem"):
            candidates.add(base.with_name(base.name + suffix))
        return any(
            commands.docker_path_is_file(self.id, str(path), log_missing=False)
            for path in candidates
        )

    def _data_mount(self):
        paths = self._paths()
        mount = self.mount_for_path(
            str(paths.data_dir),
            exclude_bind_mounts=utils.is_true(config.exclude_bind_mounts),
        )
        if mount is None:
            if utils.is_true(config.exclude_bind_mounts):
                raise vaultwarden.VaultwardenConfigError(
                    "Vaultwarden data directory is on a bind mount but "
                    "EXCLUDE_BIND_MOUNTS is enabled"
                )
            raise vaultwarden.VaultwardenConfigError(
                f"Vaultwarden data directory {paths.data_dir} is not on a "
                "mounted volume/bind"
            )
        relative = sqlite_backup.mount_relative_path(
            str(paths.data_dir), mount.destination
        )
        if relative is None:
            raise vaultwarden.VaultwardenConfigError(
                f"Vaultwarden data directory {paths.data_dir} is outside "
                f"mount destination {mount.destination}"
            )
        return paths, mount, relative

    def _mounted_paths(self) -> vaultwarden.VaultwardenPaths:
        paths, mount, relative = self._data_mount()
        target = Path(sqlite_backup.vaultwarden_source_target(mount.source))
        source_root = target / relative if relative else target
        if not source_root.is_dir():
            raise vaultwarden.VaultwardenConfigError(
                f"Vaultwarden data directory {paths.data_dir} was not found "
                f"at {source_root} inside the backup container"
            )

        resolved_root = source_root.resolve()
        target_root = target.resolve()
        if not resolved_root.is_relative_to(target_root):
            raise vaultwarden.VaultwardenConfigError(
                f"Vaultwarden data directory {paths.data_dir} resolves outside "
                "its mounted volume"
            )

        mounted = vaultwarden.map_paths_to_source(paths, source_root)
        if not mounted.db_path.resolve().is_relative_to(resolved_root):
            raise vaultwarden.VaultwardenConfigError(
                "Vaultwarden database resolves outside the data directory"
            )
        return mounted

    def dedicated_mounts(self) -> list:
        _paths, mount, _relative = self._data_mount()
        source = mount.name if mount.type == "volume" else mount.source
        if not source:
            raise vaultwarden.VaultwardenConfigError(
                "Could not determine the Vaultwarden data volume source"
            )
        return [
            {
                "type": mount.type,
                "source": source,
                "target": sqlite_backup.vaultwarden_source_target(mount.source),
            }
        ]

    def volume_exclude_targets(self) -> list:
        _paths, mount, relative = self._data_mount()
        return [
            {
                "source": mount.source,
                "rel": relative,
                "kind": "dir",
            }
        ]

    def backup_destination_paths(self) -> list:
        project_name = ""
        if utils.is_true(config.include_project_name):
            project_name = self.project_name
        return [
            vaultwarden.staging_root(
                config.vaultwarden_staging_dir,
                project_name,
                self.service_name,
            )
        ]

    def backup_destination_path(self) -> str:
        return self.backup_destination_paths()[0]

    def ping(self) -> bool:
        try:
            paths = self._paths()
            self._data_mount()
        except vaultwarden.VaultwardenConfigError as ex:
            logger.error(
                "Vaultwarden configuration error for service %s: %s",
                self.service_name,
                ex,
            )
            return False

        if not paths.db_path.is_absolute():
            logger.error(
                "Vaultwarden database path must be absolute: %s", paths.db_path
            )
            return False
        if not commands.docker_path_is_file(self.id, str(paths.db_path)):
            logger.error(
                "Vaultwarden database %s was not found in service %s",
                paths.db_path,
                self.service_name,
            )
            return False
        return True

    def _stop(self) -> bool:
        timeout = _config_float("vaultwarden_stop_timeout", 30.0)
        logger.info(
            "Stopping Vaultwarden container %s for a consistent backup",
            self.name,
        )
        if utils.stop_container(self.id, timeout=timeout):
            return True
        if not utils.container_is_running(self.id):
            # It stopped even though the API call reported an error.
            return True
        return False

    def _start_with_retry(self) -> bool:
        for attempt in range(3):
            if utils.container_is_running(self.id):
                return True
            logger.info(
                "Starting Vaultwarden container %s (attempt %s/3)",
                self.name,
                attempt + 1,
            )
            if utils.start_container(self.id):
                for _ in range(10):
                    if utils.container_is_running(self.id):
                        return True
                    time.sleep(0.5)
            time.sleep(2)
        return utils.container_is_running(self.id)

    def _stage(self, paths, staging: Path, include_icon_cache: bool) -> list:
        vaultwarden.copy_database(
            paths,
            staging,
            busy_timeout=_config_float("sqlite_busy_timeout", 30.0),
        )
        vaultwarden.stage_data_tree(
            paths,
            staging,
            include_icon_cache=include_icon_cache,
        )
        vaultwarden.validate_config_json(
            paths.data_dir / "config.json",
            staging / "config.json",
        )
        warnings = vaultwarden.verify_staging(
            paths,
            staging,
            include_icon_cache=include_icon_cache,
        )
        for warning in warnings:
            logger.warning("Vaultwarden backup warning: %s", warning)
        return warnings

    def backup(self) -> int:
        local_config = Config()
        try:
            if not vaultwarden.is_vaultwarden_image(self.image):
                logger.warning(
                    "Service %s is configured as Vaultwarden but image %s is not "
                    "a recognized Vaultwarden image; continuing because the label "
                    "is explicit",
                    self.service_name,
                    self.image,
                )
            mounted_paths = self._mounted_paths()
            staging = self.backup_destination_paths()[0]
            mode = vaultwarden.parse_mode(self.get_label(enums.LABEL_VAULTWARDEN_MODE))
            if mode == vaultwarden.VAULTWARDEN_MODE_CONSISTENT and utils.is_true(
                config.swarm_mode
            ):
                raise vaultwarden.VaultwardenConfigError(
                    "consistent Vaultwarden backups are not supported in swarm mode; "
                    "use stack-back.vaultwarden.mode: online"
                )

            data_dir = mounted_paths.data_dir.resolve()
            staging_parent = staging.parent.resolve()
            if staging_parent == data_dir or staging_parent.is_relative_to(data_dir):
                raise vaultwarden.VaultwardenConfigError(
                    "VAULTWARDEN_STAGING_DIR must not be inside Vaultwarden DATA_FOLDER"
                )
            resolved_staging = staging.resolve()
            for forbidden in ("/volumes", "/vaultwarden-source", "/sqlite"):
                forbidden_path = Path(forbidden)
                if (
                    resolved_staging == forbidden_path
                    or resolved_staging.is_relative_to(forbidden_path)
                ):
                    raise vaultwarden.VaultwardenConfigError(
                        f"VAULTWARDEN_STAGING_DIR must not be under {forbidden}"
                    )

            include_icon_cache = self.vaultwarden_icon_cache_enabled
            logger.info(
                "Backing up Vaultwarden service %s in %s mode",
                self.service_name,
                mode,
            )
            size = vaultwarden.preflight_staging_space(
                mounted_paths,
                staging.parent,
                include_icon_cache=include_icon_cache,
            )
            logger.info(
                "Vaultwarden staging requires about %.1f MiB in %s",
                size / (1024 * 1024),
                staging.parent,
            )
            vaultwarden.prepare_staging(staging)

            was_running = False
            stopped_by_us = False
            start_error = False
            staging_ok = False
            try:
                if mode == vaultwarden.VAULTWARDEN_MODE_CONSISTENT:
                    was_running = utils.container_is_running(self.id)
                    if was_running:
                        if not self._stop():
                            raise vaultwarden.VaultwardenConfigError(
                                "could not stop Vaultwarden for a consistent backup"
                            )
                        stopped_by_us = True

                self._stage(mounted_paths, staging, include_icon_cache)
                staging_ok = True
            finally:
                if stopped_by_us and was_running:
                    if not self._start_with_retry():
                        start_error = True
                        logger.error(
                            "Could not restart Vaultwarden container %s after backup",
                            self.name,
                        )

            result = 0
            if start_error:
                result = 1
            if not staging_ok:
                result = 1
                return result

            exit_code = restic.backup_files(
                local_config.repository,
                str(staging),
                options=["--ignore-ctime", "--ignore-inode"],
            )
            if exit_code != 0:
                logger.error(
                    "Restic exited with code %s for Vaultwarden service %s",
                    exit_code,
                    self.service_name,
                )
                result = 1
            return result
        except vaultwarden.VaultwardenConfigError as ex:
            logger.error(
                "Vaultwarden backup failed for service %s: %s",
                self.service_name,
                ex,
            )
            return 1
        except Exception as ex:
            logger.error(
                "Unexpected Vaultwarden backup failure for service %s: %s",
                self.service_name,
                ex,
            )
            logger.debug("Vaultwarden backup failure", exc_info=True)
            return 1
        finally:
            try:
                staging = self.backup_destination_paths()[0]
                vaultwarden.cleanup_staging(staging)
            except Exception as ex:
                logger.warning("Could not clean Vaultwarden staging: %s", ex)
