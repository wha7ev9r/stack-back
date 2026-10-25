"""Vaultwarden-specific helpers.

The functions here are intentionally independent of the Docker SDK so path
resolution, staging and validation can be unit-tested on their own.
"""

import json
import logging
import os
import posixpath
import shutil
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path

from restic_compose_backup import sqlite_backup

logger = logging.getLogger(__name__)

VAULTWARDEN_MODE_ONLINE = "online"
VAULTWARDEN_MODE_CONSISTENT = "consistent"
VAULTWARDEN_MODES = (VAULTWARDEN_MODE_ONLINE, VAULTWARDEN_MODE_CONSISTENT)

VAULTWARDEN_IMAGE_REPOS = (
    "vaultwarden/server",
    "ghcr.io/dani-garcia/vaultwarden",
    "dani-garcia/vaultwarden",
    "bitwardenrs/server",
)

STAGING_MARKER = ".stack-back-staging"
REQUIRED_DATABASE_TABLES = ("users", "__diesel_schema_migrations")

_TRUE_VALUES = ["1", "true", "True", "TRUE", True, 1]
_FALSE_VALUES = ["0", "false", "False", "FALSE", False, 0]


class VaultwardenConfigError(Exception):
    """Raised when a Vaultwarden container cannot be backed up safely."""


@dataclass(frozen=True)
class VaultwardenPaths:
    data_dir: Path
    db_path: Path
    attachments: Path
    sends: Path
    icon_cache: Path
    rsa_key: Path
    tmp: Path
    log_file: Path | None
    log_dir: Path | None
    db_rel: str


def is_true(value) -> bool:
    return value in _TRUE_VALUES


def is_false(value) -> bool:
    return value in _FALSE_VALUES


def environment_to_dict(environment) -> dict:
    """Convert Docker's ``Config.Env`` list/dict to a dict."""

    if not environment:
        return {}
    if isinstance(environment, dict):
        return {str(key): value for key, value in environment.items()}

    result = {}
    for entry in environment:
        if "=" in entry:
            key, value = entry.split("=", 1)
            result[key] = value
    return result


def is_vaultwarden_image(image: str) -> bool:
    """Best-effort recognition of official/legacy Vaultwarden images."""

    if not image:
        return False

    name = image.strip()
    if "@" in name:
        name = name.split("@", 1)[0]
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]

    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]

    return any(
        name == repo or name.startswith(repo + "/") for repo in VAULTWARDEN_IMAGE_REPOS
    )


def parse_mode(value) -> str:
    if value is None or value == "":
        return VAULTWARDEN_MODE_ONLINE
    if not isinstance(value, str):
        raise VaultwardenConfigError("stack-back.vaultwarden.mode must be a string")
    normalized = value.strip().lower()
    if normalized not in VAULTWARDEN_MODES:
        raise VaultwardenConfigError(
            "stack-back.vaultwarden.mode must be 'online' or 'consistent'"
        )
    return normalized


def _normalize_container_path(value: str, *, data_dir: str) -> str:
    value = value.strip()
    if value.startswith("%DATA_FOLDER%"):
        value = data_dir + value[len("%DATA_FOLDER%") :]
    if not value.startswith("/"):
        value = "/" + value
    return posixpath.normpath(value)


def _is_under(path: str, root: str) -> bool:
    if path == root:
        return True
    return path.startswith(root.rstrip("/") + "/")


def parse_database_url(url, data_dir: str) -> str:
    """Resolve a Vaultwarden DATABASE_URL to an absolute container path.

    Vaultwarden strips the literal ``sqlite://`` prefix; using urlparse here
    would misinterpret ``sqlite://data/db.sqlite3`` (``data`` as netloc).
    Only SQLite is supported by this module.
    """

    if url is not None and not isinstance(url, str):
        raise VaultwardenConfigError("DATABASE_URL must be a string")

    if not url:
        return posixpath.normpath(posixpath.join(data_dir, "db.sqlite3"))

    value = url.strip()
    value = value.replace("%DATA_FOLDER%", data_dir)

    lowered = value.lower()
    if lowered.startswith(("mysql://", "mariadb://", "postgres://", "postgresql://")):
        raise VaultwardenConfigError(
            "only the SQLite Vaultwarden backend is supported by this module; "
            "back up the MySQL/PostgreSQL database with stack-back.mysql/"
            "stack-back.postgres and the data files with a volume backup"
        )

    if value.startswith("sqlite://"):
        path = value[len("sqlite://") :]
    elif value.startswith("sqlite:"):
        raise VaultwardenConfigError(
            "unsupported DATABASE_URL form; use sqlite:///absolute/path"
        )
    else:
        # Bare paths are supported by Vaultwarden for backwards compatibility.
        path = value

    if "?" in path or "#" in path:
        raise VaultwardenConfigError(
            "DATABASE_URL query strings/fragments are not supported"
        )
    if not path.strip():
        raise VaultwardenConfigError("DATABASE_URL does not contain a SQLite path")

    if not path.startswith("/"):
        path = "/" + path

    normalized = posixpath.normpath(path)
    if not _is_under(normalized, data_dir):
        raise VaultwardenConfigError(
            f"DATABASE_URL must point inside DATA_FOLDER ({data_dir}): {normalized}"
        )
    return normalized


def _optional_path(
    name: str,
    value,
    default: str,
    data_dir: str,
) -> str | None:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise VaultwardenConfigError(f"{name} must be a string")
    normalized = _normalize_container_path(value, data_dir=data_dir)
    if not _is_under(normalized, data_dir):
        raise VaultwardenConfigError(
            f"{name} must point inside DATA_FOLDER ({data_dir}): {normalized}"
        )
    return normalized


def resolve_paths(environment, data_dir_override=None) -> VaultwardenPaths:
    """Resolve Vaultwarden's data paths from container environment variables."""

    env = environment_to_dict(environment)

    raw_data_dir = data_dir_override or env.get("DATA_FOLDER") or "/data"
    if not isinstance(raw_data_dir, str) or not raw_data_dir.strip():
        raise VaultwardenConfigError("DATA_FOLDER must be a non-empty string")
    data_dir = _normalize_container_path(raw_data_dir, data_dir="/")

    db_path = parse_database_url(env.get("DATABASE_URL"), data_dir)

    attachments = _optional_path(
        "ATTACHMENTS_FOLDER",
        env.get("ATTACHMENTS_FOLDER"),
        posixpath.join(data_dir, "attachments"),
        data_dir,
    )
    sends = _optional_path(
        "SENDS_FOLDER",
        env.get("SENDS_FOLDER"),
        posixpath.join(data_dir, "sends"),
        data_dir,
    )
    icon_cache = _optional_path(
        "ICON_CACHE_FOLDER",
        env.get("ICON_CACHE_FOLDER"),
        posixpath.join(data_dir, "icon_cache"),
        data_dir,
    )
    rsa_key = _optional_path(
        "RSA_KEY_FILENAME",
        env.get("RSA_KEY_FILENAME"),
        posixpath.join(data_dir, "rsa_key"),
        data_dir,
    )
    tmp = _optional_path(
        "TMP_FOLDER",
        env.get("TMP_FOLDER"),
        posixpath.join(data_dir, "tmp"),
        data_dir,
    )
    log_file = _optional_path(
        "LOG_FILE",
        env.get("LOG_FILE"),
        "",
        data_dir,
    )
    log_dir = _optional_path(
        "LOG_DIR",
        env.get("LOG_DIR"),
        "",
        data_dir,
    )

    if data_dir == "/":
        raise VaultwardenConfigError("DATA_FOLDER cannot be /")

    db_rel = posixpath.relpath(db_path, data_dir)
    if db_rel.startswith(".."):
        raise VaultwardenConfigError(f"database path is outside DATA_FOLDER: {db_path}")

    for name, value in (
        ("DATABASE_URL", db_path),
        ("ATTACHMENTS_FOLDER", attachments),
        ("SENDS_FOLDER", sends),
        ("ICON_CACHE_FOLDER", icon_cache),
        ("RSA_KEY_FILENAME", rsa_key),
        ("TMP_FOLDER", tmp),
    ):
        if posixpath.normpath(value) == data_dir:
            raise VaultwardenConfigError(f"{name} cannot be identical to DATA_FOLDER")
    for name, value in (("LOG_FILE", log_file), ("LOG_DIR", log_dir)):
        if value and posixpath.normpath(value) == data_dir:
            raise VaultwardenConfigError(f"{name} cannot be identical to DATA_FOLDER")

    return VaultwardenPaths(
        data_dir=Path(data_dir),
        db_path=Path(db_path),
        attachments=Path(attachments),
        sends=Path(sends),
        icon_cache=Path(icon_cache),
        rsa_key=Path(rsa_key),
        tmp=Path(tmp),
        log_file=Path(log_file) if log_file else None,
        log_dir=Path(log_dir) if log_dir else None,
        db_rel=db_rel,
    )


def map_paths_to_source(paths: VaultwardenPaths, source_root: Path) -> VaultwardenPaths:
    """Map container paths onto the root where DATA_FOLDER is mounted.

    The backup process container sees the data directory at
    ``/vaultwarden-source/<hash>`` rather than at its original path (for
    example ``/data``).
    """

    source_root = Path(source_root)

    def mapped(path: Path) -> Path:
        return source_root / Path(path).relative_to(paths.data_dir)

    return replace(
        paths,
        data_dir=source_root,
        db_path=mapped(paths.db_path),
        attachments=mapped(paths.attachments),
        sends=mapped(paths.sends),
        icon_cache=mapped(paths.icon_cache),
        rsa_key=mapped(paths.rsa_key),
        tmp=mapped(paths.tmp),
        log_file=mapped(paths.log_file) if paths.log_file else None,
        log_dir=mapped(paths.log_dir) if paths.log_dir else None,
    )


def staging_root(staging_dir: str, project_name: str, service_name: str) -> Path:
    path = Path(staging_dir)
    if not path.is_absolute():
        raise VaultwardenConfigError("VAULTWARDEN_STAGING_DIR must be absolute")
    if project_name:
        path = path / project_name
    if not service_name:
        raise VaultwardenConfigError("cannot determine Vaultwarden service name")
    return path / service_name


def _skipped_paths(paths: VaultwardenPaths, *, include_icon_cache: bool) -> set[str]:
    skipped = {
        str(paths.db_path),
        str(paths.tmp),
    }
    skipped.update(str(path) for path in sqlite_backup.sidecar_paths(paths.db_path))
    if not include_icon_cache:
        skipped.add(str(paths.icon_cache))
    if paths.log_file is not None:
        skipped.add(str(paths.log_file))
    if paths.log_dir is not None:
        skipped.add(str(paths.log_dir))
    return skipped


def _is_skipped(path: str, skipped: set[str]) -> bool:
    normalized = posixpath.normpath(path)
    for candidate in skipped:
        if normalized == candidate or normalized.startswith(
            candidate.rstrip("/") + "/"
        ):
            return True
    return False


def _copy_with_owner(source, destination, *, follow_symlinks=True):
    shutil.copy2(source, destination, follow_symlinks=follow_symlinks)
    try:
        source_stat = os.lstat(source)
        os.chown(
            destination,
            source_stat.st_uid,
            source_stat.st_gid,
            follow_symlinks=follow_symlinks,
        )
    except (OSError, NotImplementedError) as ex:
        logger.warning("Could not preserve ownership for %s: %s", destination, ex)
    return destination


def estimate_data_size(paths: VaultwardenPaths, *, include_icon_cache: bool) -> int:
    """Estimate the bytes that need to be staged."""

    skipped = _skipped_paths(paths, include_icon_cache=include_icon_cache)
    total = 0
    for root, dirs, files in os.walk(paths.data_dir, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not _is_skipped(posixpath.join(root, name), skipped)
        ]
        for name in files:
            full_path = posixpath.join(root, name)
            if _is_skipped(full_path, skipped):
                continue
            try:
                total += os.lstat(full_path).st_size
            except OSError:
                logger.warning("Could not stat %s while estimating size", full_path)

    try:
        total += os.lstat(paths.db_path).st_size
    except OSError:
        pass
    return total


def preflight_staging_space(
    paths: VaultwardenPaths,
    staging_dir: Path,
    *,
    include_icon_cache: bool,
) -> int:
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    size = estimate_data_size(paths, include_icon_cache=include_icon_cache)
    sqlite_backup.ensure_free_space(size, staging_dir.parent)
    return size


def _staging_marker(path: Path) -> Path:
    # Keep the marker outside the snapshot so restic does not back it up.
    return path.with_name(path.name + STAGING_MARKER)


def prepare_staging(path: Path) -> None:
    """Create a staging directory, removing only a previous stack-back one."""

    marker = _staging_marker(path)
    if path.exists():
        if not marker.is_file():
            raise VaultwardenConfigError(
                f"staging directory {path} exists but was not created by stack-back; "
                "refusing to delete it"
            )
        shutil.rmtree(path)

    path.mkdir(parents=True, mode=0o700)
    marker.write_text("temporary Vaultwarden staging directory\n")


def cleanup_staging(path: Path) -> None:
    marker = _staging_marker(path)
    if path.exists() and not marker.is_file():
        logger.warning("Refusing to remove unexpected staging directory %s", path)
        return
    if path.exists():
        shutil.rmtree(path)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def stage_data_tree(
    paths: VaultwardenPaths,
    staging_dir: Path,
    *,
    include_icon_cache: bool,
) -> None:
    """Copy everything from DATA_FOLDER except the DB and transient data."""

    skipped = _skipped_paths(paths, include_icon_cache=include_icon_cache)

    def ignore(directory, names):
        ignored = []
        for name in names:
            full_path = posixpath.join(directory, name)
            if _is_skipped(full_path, skipped):
                ignored.append(name)
                continue
            try:
                file_stat = os.lstat(full_path)
            except OSError as ex:
                logger.warning(
                    "Skipping unstattable Vaultwarden entry %s: %s", full_path, ex
                )
                ignored.append(name)
                continue
            if not (
                stat.S_ISREG(file_stat.st_mode)
                or stat.S_ISDIR(file_stat.st_mode)
                or stat.S_ISLNK(file_stat.st_mode)
            ):
                logger.warning("Skipping special Vaultwarden file %s", full_path)
                ignored.append(name)
        return ignored

    shutil.copytree(
        paths.data_dir,
        staging_dir,
        dirs_exist_ok=True,
        symlinks=True,
        copy_function=_copy_with_owner,
        ignore=ignore,
    )

    # copytree copies file metadata but not directory ownership.  Preserve
    # metadata for directories that were copied so restore keeps working for
    # non-root Vaultwarden users.
    for root, dirs, _files in os.walk(paths.data_dir, followlinks=False):
        relative = posixpath.relpath(root, paths.data_dir)
        if relative == ".":
            # Keep the staging root private; it contains vault data.
            continue
        target = staging_dir / relative
        if not target.is_dir():
            continue
        try:
            shutil.copystat(root, target)
            source_stat = os.stat(root, follow_symlinks=False)
            os.chown(
                target,
                source_stat.st_uid,
                source_stat.st_gid,
                follow_symlinks=False,
            )
        except (OSError, NotImplementedError) as ex:
            logger.warning(
                "Could not preserve directory metadata for %s: %s", target, ex
            )

    try:
        os.chmod(staging_dir, 0o700)
    except OSError as ex:
        logger.warning("Could not set staging directory mode: %s", ex)


def copy_database(
    paths: VaultwardenPaths, staging_dir: Path, *, busy_timeout: float
) -> None:
    destination = staging_dir / paths.db_rel
    sqlite_backup.export_database(
        paths.db_path,
        destination,
        busy_timeout=busy_timeout,
        verify="full",
        required_tables=REQUIRED_DATABASE_TABLES,
    )
    try:
        source_stat = os.stat(paths.db_path)
        os.chmod(destination, stat.S_IMODE(source_stat.st_mode))
        os.chown(destination, source_stat.st_uid, source_stat.st_gid)
    except (OSError, NotImplementedError) as ex:
        logger.warning("Could not preserve database ownership/mode: %s", ex)


def config_json_path(paths: VaultwardenPaths) -> Path:
    return paths.data_dir / "config.json"


def validate_config_json(source: Path, staging: Path, *, retries: int = 3) -> None:
    """Copy config.json if present and make sure it is complete JSON."""

    if not source.is_file():
        return
    last_error = None
    for attempt in range(retries + 1):
        try:
            shutil.copy2(source, staging)
            with open(staging, "r", encoding="utf-8") as handle:
                json.load(handle)
            return
        except (OSError, ValueError) as ex:
            last_error = ex
            if attempt < retries:
                time.sleep(0.5)
    raise VaultwardenConfigError(f"config.json is not valid JSON: {last_error}")


def rsa_key_present(paths: VaultwardenPaths, staging_dir: Path) -> bool:
    parent = paths.rsa_key.parent
    try:
        relative = parent.relative_to(paths.data_dir)
    except ValueError:
        return False
    target_dir = staging_dir / relative
    if not target_dir.is_dir():
        return False
    prefix = paths.rsa_key.name
    return any(
        child.is_file() and child.name.startswith(prefix)
        for child in target_dir.iterdir()
    )


def verify_staging(
    paths: VaultwardenPaths,
    staging_dir: Path,
    *,
    include_icon_cache: bool,
) -> list[str]:
    """Validate a staged Vaultwarden backup.  Returns warnings."""

    database = staging_dir / paths.db_rel
    sqlite_backup.check_database(
        database,
        verify="full",
        required_tables=REQUIRED_DATABASE_TABLES,
    )

    for sidecar in sqlite_backup.sidecar_paths(database):
        if sidecar.exists():
            raise VaultwardenConfigError(
                f"staged database has an unexpected sidecar: {sidecar}"
            )

    warnings = []
    if not rsa_key_present(paths, staging_dir):
        warnings.append("rsa_key.pem was not found in the staged data")
    icon_relative = posixpath.relpath(str(paths.icon_cache), str(paths.data_dir))
    if not include_icon_cache and (staging_dir / icon_relative).exists():
        warnings.append("icon_cache is present although it was disabled")

    return warnings
