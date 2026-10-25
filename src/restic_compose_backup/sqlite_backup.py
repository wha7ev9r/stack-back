"""Portable SQLite online-backup helpers.

The functions in this module only use the Python standard library.  The same
exporting code is used for generic SQLite databases and for the Vaultwarden
``db.sqlite3`` database.

SQLite's online backup API (``sqlite3.Connection.backup``) is used instead of
copying database files directly.  After the copy the destination is converted
to rollback-journal mode so the resulting file is a self-contained database
without ``-wal``/``-shm`` sidecars.
"""

import hashlib
import logging
import os
import posixpath
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

SQLITE_MAGIC = b"SQLite format 3\x00"
MOUNT_HASH_LENGTH = 16
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
TEMP_SPACE_MARGIN_BYTES = 64 * 1024 * 1024
VERIFY_MODES = {"none", "quick", "full"}


class SqliteBackupError(Exception):
    """Raised when a SQLite database cannot be exported safely."""


def normalize_sqlite_paths(raw_value) -> tuple[list[str], list[str]]:
    """Parse the ``stack-back.sqlite.paths`` label.

    Returns ``(paths, errors)``.  Invalid entries are reported instead of
    being silently ignored.
    """

    if raw_value is None or raw_value == "":
        return [], []

    if not isinstance(raw_value, str):
        return [], ["stack-back.sqlite.paths must be a comma-separated string"]

    paths: list[str] = []
    errors: list[str] = []
    for entry in raw_value.split(","):
        value = entry.strip()
        if not value:
            continue
        if not value.startswith("/"):
            errors.append(f"path is not absolute: {value}")
            continue
        if any(char in value for char in "*?["):
            errors.append(f"glob patterns are not supported: {value}")
            continue

        normalized = posixpath.normpath(value)
        if normalized != value:
            errors.append(f"path must be normalized (no '.' or '..'): {value}")
            continue
        if normalized == "/":
            errors.append("path cannot be /")
            continue
        if normalized not in paths:
            paths.append(normalized)

    return paths, errors


def mount_relative_path(path: str, destination: str) -> str | None:
    """Return ``path`` relative to a mount destination, or None.

    Both arguments are container paths.  A file or directory equal to the
    mount destination yields an empty string.
    """

    normalized_path = posixpath.normpath(path)
    normalized_destination = posixpath.normpath(destination)

    if normalized_destination == "/":
        return normalized_path.lstrip("/")
    if normalized_path == normalized_destination:
        return ""
    prefix = normalized_destination.rstrip("/") + "/"
    if normalized_path.startswith(prefix):
        return normalized_path[len(prefix) :]
    return None


def find_mount_for_path(
    path: str,
    mounts,
    *,
    exclude_bind_mounts: bool = False,
    allowed_types=("volume", "bind"),
):
    """Find the most specific mount containing ``path``.

    ``mounts`` only needs objects with ``source``, ``destination`` and ``type``
    attributes; this keeps the helper testable without Docker.
    """

    if not path.startswith("/"):
        return None

    best = None
    best_length = -1
    for mount in mounts:
        if getattr(mount, "type", None) not in allowed_types:
            continue
        if exclude_bind_mounts and getattr(mount, "type", None) == "bind":
            continue
        relative = mount_relative_path(path, mount.destination)
        if relative is None:
            continue
        destination = posixpath.normpath(mount.destination)
        if len(destination) > best_length:
            best = mount
            best_length = len(destination)
    return best


def mount_target(prefix: str, source: str) -> str:
    """Return a deterministic in-container target for a mounted source."""

    if not source:
        raise SqliteBackupError("cannot create a mount target for an empty source")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:MOUNT_HASH_LENGTH]
    return f"{prefix.rstrip('/')}/{digest}"


def sqlite_mount_target(source: str) -> str:
    return mount_target("/sqlite", source)


def vaultwarden_source_target(source: str) -> str:
    return mount_target("/vaultwarden-source", source)


def escape_restic_pattern(path: str) -> str:
    """Escape ``filepath.Match`` metacharacters used by restic."""

    result = []
    for char in path:
        if char in "\\*?[":
            result.append("\\")
        result.append(char)
    return "".join(result)


def sidecar_paths(database: Path) -> list[Path]:
    return [Path(str(database) + suffix) for suffix in SIDECAR_SUFFIXES]


def remove_sidecars(database: Path) -> None:
    for sidecar in sidecar_paths(database):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass


def read_sqlite_header(database: Path) -> bytes:
    with open(database, "rb") as handle:
        return handle.read(len(SQLITE_MAGIC))


def is_sqlite_file(path: Path) -> bool:
    """Return True when ``path`` is a regular file with a SQLite header."""
    path = Path(path)
    try:
        if not path.is_file() or path.is_symlink():
            return False
        if path.stat().st_size < len(SQLITE_MAGIC):
            return False
        return read_sqlite_header(path) == SQLITE_MAGIC
    except OSError:
        return False


def find_sqlite_databases(root: Path) -> list[Path]:
    """Find SQLite database files under a mounted directory.

    A header check is used rather than a filename extension so files such as
    ``data.db`` and extensionless databases are found.  Sidecars and symlinks
    are skipped.  This is intentionally only used for the explicit
    ``stack-back.sqlite: auto`` opt-in.
    """

    root = Path(root)
    if not root.is_dir():
        return []

    databases = []
    for dirpath, dirnames, filenames in os.walk(
        root,
        followlinks=False,
        onerror=lambda ex: logger.debug("Could not scan %s: %s", root, ex),
    ):
        dirnames[:] = [
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            if name.endswith(SIDECAR_SUFFIXES):
                continue
            path = Path(dirpath) / name
            if is_sqlite_file(path):
                databases.append(path)

    return sorted(databases)


def validate_sqlite_file(database: Path) -> None:
    """Ensure ``database`` is a non-empty SQLite database file."""

    if not database.is_file():
        raise SqliteBackupError(f"SQLite database is not a regular file: {database}")

    try:
        size = database.stat().st_size
    except OSError as ex:
        raise SqliteBackupError(f"cannot stat SQLite database {database}: {ex}") from ex

    if size <= 0:
        raise SqliteBackupError(f"SQLite database is empty: {database}")

    try:
        header = read_sqlite_header(database)
    except OSError as ex:
        raise SqliteBackupError(f"cannot read SQLite database {database}: {ex}") from ex

    if header != SQLITE_MAGIC:
        raise SqliteBackupError(
            f"{database} is not a standard SQLite database "
            "(wrong path, empty volume directory, SQLCipher or a custom VFS)"
        )


def ensure_free_space(source_size: int, directory: Path) -> None:
    """Fail before a copy if the temporary directory cannot hold it."""

    try:
        usage = shutil.disk_usage(str(directory))
    except OSError as ex:
        raise SqliteBackupError(
            f"cannot determine free space for {directory}: {ex}"
        ) from ex

    required = int(source_size * 1.10) + TEMP_SPACE_MARGIN_BYTES
    if usage.free < required:
        raise SqliteBackupError(
            f"not enough free space in {directory}: need about "
            f"{required // (1024 * 1024)} MiB, have "
            f"{usage.free // (1024 * 1024)} MiB"
        )


def check_database(
    database: Path,
    *,
    verify: str = "quick",
    required_tables=(),
) -> None:
    """Validate an existing database file (magic, schema, integrity)."""

    database = Path(database)
    validate_sqlite_file(database)
    _check_database(
        database,
        verify=verify,
        required_tables=required_tables,
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def _check_database(
    database: Path,
    *,
    verify: str,
    required_tables,
) -> None:
    if verify not in VERIFY_MODES:
        raise SqliteBackupError(f"invalid SQLite verify mode: {verify}")

    with closing(sqlite3.connect(str(database))) as connection:
        if required_tables:
            names = _table_names(connection)
            missing = [name for name in required_tables if name not in names]
            if missing:
                raise SqliteBackupError(
                    f"{database} is not the expected database "
                    f"(missing tables: {', '.join(missing)})"
                )

        if verify == "none":
            return

        pragma = "integrity_check" if verify == "full" else "quick_check"
        try:
            rows = connection.execute(f"PRAGMA {pragma}").fetchall()
        except sqlite3.Error as ex:
            raise SqliteBackupError(
                f"SQLite {pragma} failed for {database}: {ex}"
            ) from ex

        if not rows or any(row[0] != "ok" for row in rows):
            details = "; ".join(str(row[0]) for row in rows)
            raise SqliteBackupError(
                f"SQLite {pragma} reported errors for {database}: {details}"
            )


def _open_source(
    database: Path, busy_timeout: float, *, read_only: bool = False
) -> sqlite3.Connection:
    """Open an existing database without creating it."""

    mode = "ro" if read_only else "rw"
    uri = f"{database.as_uri()}?mode={mode}"
    connection = sqlite3.connect(uri, uri=True, timeout=busy_timeout)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
        connection.execute("PRAGMA query_only = ON")
    except Exception:
        connection.close()
        raise
    return connection


def export_database(
    source: Path,
    destination: Path,
    *,
    busy_timeout: float = 30.0,
    verify: str = "quick",
    required_tables=(),
    read_only: bool = False,
) -> None:
    """Copy ``source`` into ``destination`` using the online backup API.

    The source is never created implicitly.  ``read_only=True`` opens the
    source with ``mode=ro`` for the auto-detection path.  The destination is
    normalized to rollback-journal mode and validated before returning.
    """

    source = Path(source)
    destination = Path(destination)

    validate_sqlite_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ensure_free_space(source.stat().st_size, destination.parent)

    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    remove_sidecars(destination)

    try:
        with closing(
            _open_source(source, busy_timeout, read_only=read_only)
        ) as source_connection:
            with closing(sqlite3.connect(str(destination))) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.commit()

        # The online backup API copies the source header, including its
        # journal mode.  Convert the result to a standalone rollback-journal
        # database so the file can be restored/opened without sidecars.
        with closing(sqlite3.connect(str(destination))) as connection:
            mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
            if not mode or str(mode[0]).lower() != "delete":
                raise SqliteBackupError(
                    f"could not convert {destination} to rollback-journal mode"
                )

        remove_sidecars(destination)
        header = read_sqlite_header(destination)
        if header != SQLITE_MAGIC:
            raise SqliteBackupError(
                f"exported database {destination} is not a valid SQLite file"
            )

        _check_database(
            destination,
            verify=verify,
            required_tables=required_tables,
        )
    except SqliteBackupError:
        _cleanup_destination(destination)
        raise
    except sqlite3.Error as ex:
        _cleanup_destination(destination)
        raise SqliteBackupError(
            f"SQLite online backup failed for {source}: {ex}. "
            "If the database is locked exclusively, stop the service during "
            "backup; if it is encrypted (SQLCipher), it is not supported."
        ) from ex
    except Exception:
        _cleanup_destination(destination)
        raise


def _cleanup_destination(destination: Path) -> None:
    try:
        destination.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove incomplete SQLite export %s", destination)
    remove_sidecars(destination)
