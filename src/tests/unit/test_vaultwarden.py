"""Tests for Vaultwarden path resolution and staging helpers."""

import json
import sqlite3
from pathlib import Path

import pytest

from restic_compose_backup import vaultwarden

pytestmark = pytest.mark.unit


def create_vaultwarden_data(root: Path) -> Path:
    data = root / "data"
    data.mkdir()
    database = data / "db.sqlite3"
    connection = sqlite3.connect(str(database))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE users (uuid TEXT PRIMARY KEY, email TEXT)")
    connection.execute(
        "CREATE TABLE __diesel_schema_migrations (version TEXT PRIMARY KEY)"
    )
    connection.execute("INSERT INTO users VALUES ('user-1', 'test@example.invalid')")
    connection.commit()
    connection.close()

    (data / "attachments" / "cipher-1").mkdir(parents=True)
    (data / "attachments" / "cipher-1" / "attachment-1").write_bytes(b"attachment")
    (data / "sends" / "send-1").mkdir(parents=True)
    (data / "sends" / "send-1" / "file-1").write_bytes(b"send")
    (data / "config.json").write_text(json.dumps({"domain": "https://vw.invalid"}))
    (data / "rsa_key.pem").write_text("private-key")
    (data / "rsa_key.pub.pem").write_text("public-key")
    (data / "icon_cache").mkdir()
    (data / "icon_cache" / "example.com.png").write_bytes(b"icon")
    (data / "tmp").mkdir()
    (data / "tmp" / "upload.tmp").write_text("temporary")
    (data / "vaultwarden.log").write_text("log line")
    return data


def resolve_paths(data: Path, **extra):
    environment = {
        "DATA_FOLDER": str(data),
        "DATABASE_URL": f"sqlite://{data}/db.sqlite3",
        "LOG_FILE": f"{data}/vaultwarden.log",
    }
    environment.update(extra)
    return vaultwarden.resolve_paths(environment)


def test_is_vaultwarden_image():
    assert vaultwarden.is_vaultwarden_image("vaultwarden/server:latest")
    assert vaultwarden.is_vaultwarden_image("ghcr.io/dani-garcia/vaultwarden:1.2.3")
    assert vaultwarden.is_vaultwarden_image("bitwardenrs/server:latest")
    assert not vaultwarden.is_vaultwarden_image("python:3.13-alpine")
    assert not vaultwarden.is_vaultwarden_image("my-vaultwarden/server")


def test_parse_mode():
    assert vaultwarden.parse_mode(None) == "online"
    assert vaultwarden.parse_mode("online") == "online"
    assert vaultwarden.parse_mode("CONSISTENT") == "consistent"
    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.parse_mode("stopped")


def test_parse_database_url():
    assert vaultwarden.parse_database_url(None, "/data") == "/data/db.sqlite3"
    assert (
        vaultwarden.parse_database_url("sqlite:///data/db.sqlite3", "/data")
        == "/data/db.sqlite3"
    )
    assert (
        vaultwarden.parse_database_url("sqlite://data/db.sqlite3", "/data")
        == "/data/db.sqlite3"
    )
    assert (
        vaultwarden.parse_database_url("/data/other.sqlite3", "/data")
        == "/data/other.sqlite3"
    )
    assert (
        vaultwarden.parse_database_url("sqlite://%DATA_FOLDER%/db.sqlite3", "/data")
        == "/data/db.sqlite3"
    )

    for invalid in (
        "mysql://user:pass@host/db",
        "postgresql://host/db",
        "sqlite:///data/db.sqlite3?mode=ro",
        "sqlite:///other/db.sqlite3",
        "sqlite://",
    ):
        with pytest.raises(vaultwarden.VaultwardenConfigError):
            vaultwarden.parse_database_url(invalid, "/data")


def test_resolve_paths_defaults():
    paths = vaultwarden.resolve_paths({})
    assert paths.data_dir == Path("/data")
    assert paths.db_path == Path("/data/db.sqlite3")
    assert paths.attachments == Path("/data/attachments")
    assert paths.sends == Path("/data/sends")
    assert paths.icon_cache == Path("/data/icon_cache")
    assert paths.rsa_key == Path("/data/rsa_key")
    assert paths.tmp == Path("/data/tmp")
    assert paths.db_rel == "db.sqlite3"


def test_resolve_paths_relative_data_folder():
    paths = vaultwarden.resolve_paths({"DATA_FOLDER": "data"})
    assert paths.data_dir == Path("/data")


def test_resolve_paths_rejects_outside_folders():
    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.resolve_paths(
            {"DATA_FOLDER": "/data", "ATTACHMENTS_FOLDER": "/other/attachments"}
        )


def test_resolve_paths_rejects_degenerate_paths():
    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.resolve_paths({"DATA_FOLDER": "/"})
    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.resolve_paths(
            {"DATA_FOLDER": "/data", "ATTACHMENTS_FOLDER": "/data"}
        )
    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.resolve_paths({"DATA_FOLDER": "/data", "LOG_DIR": "/data"})


def test_map_paths_to_source():
    paths = vaultwarden.resolve_paths({"DATA_FOLDER": "/data"})
    mapped = vaultwarden.map_paths_to_source(paths, Path("/vaultwarden-source/hash"))
    assert mapped.data_dir == Path("/vaultwarden-source/hash")
    assert mapped.db_path == Path("/vaultwarden-source/hash/db.sqlite3")
    assert mapped.attachments == Path("/vaultwarden-source/hash/attachments")
    assert mapped.db_rel == "db.sqlite3"


def test_staging_root():
    assert vaultwarden.staging_root("/vaultwarden", "", "vw") == Path("/vaultwarden/vw")
    assert vaultwarden.staging_root("/vaultwarden", "project", "vw") == Path(
        "/vaultwarden/project/vw"
    )


def test_prepare_and_cleanup_staging_refuses_foreign_directory(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "important.txt").write_text("do not delete")
    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.prepare_staging(foreign)
    assert (foreign / "important.txt").exists()

    staged = tmp_path / "staged"
    vaultwarden.prepare_staging(staged)
    assert staged.is_dir()
    assert not (staged / vaultwarden.STAGING_MARKER).exists()
    assert (staged.parent / (staged.name + vaultwarden.STAGING_MARKER)).is_file()
    vaultwarden.cleanup_staging(staged)
    assert not staged.exists()


def test_resolve_and_stage_data(tmp_path):
    data = create_vaultwarden_data(tmp_path)
    paths = resolve_paths(data)
    staging = tmp_path / "staging"
    vaultwarden.prepare_staging(staging)

    try:
        vaultwarden.copy_database(paths, staging, busy_timeout=5.0)
        vaultwarden.stage_data_tree(paths, staging, include_icon_cache=False)
        vaultwarden.validate_config_json(data / "config.json", staging / "config.json")
        warnings = vaultwarden.verify_staging(paths, staging, include_icon_cache=False)

        assert warnings == []
        assert (staging / "db.sqlite3").exists()
        assert (staging / "attachments" / "cipher-1" / "attachment-1").exists()
        assert (staging / "sends" / "send-1" / "file-1").exists()
        assert (staging / "config.json").exists()
        assert (staging / "rsa_key.pem").exists()
        assert (staging / "rsa_key.pub.pem").exists()
        assert not (staging / "tmp").exists()
        assert not (staging / "icon_cache").exists()
        assert not (staging / "vaultwarden.log").exists()
        assert not (staging / "db.sqlite3-wal").exists()
        assert not (staging / "db.sqlite3-shm").exists()
    finally:
        vaultwarden.cleanup_staging(staging)


def test_stage_includes_icon_cache_when_enabled(tmp_path):
    data = create_vaultwarden_data(tmp_path)
    paths = resolve_paths(data)
    staging = tmp_path / "staging"
    vaultwarden.prepare_staging(staging)
    try:
        vaultwarden.stage_data_tree(paths, staging, include_icon_cache=True)
        assert (staging / "icon_cache" / "example.com.png").exists()
    finally:
        vaultwarden.cleanup_staging(staging)


def test_config_json_retry_and_failure(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text("{not-json")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(vaultwarden.time, "sleep", lambda _seconds: None)

    with pytest.raises(vaultwarden.VaultwardenConfigError):
        vaultwarden.validate_config_json(
            data / "config.json", staging / "config.json", retries=1
        )


def test_estimate_data_size_and_preflight(tmp_path):
    data = create_vaultwarden_data(tmp_path)
    paths = resolve_paths(data)
    size = vaultwarden.estimate_data_size(paths, include_icon_cache=False)
    assert size > 0
    staging = tmp_path / "staging"
    required = vaultwarden.preflight_staging_space(
        paths, staging.parent, include_icon_cache=False
    )
    assert required == size
