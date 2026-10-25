# Integration Tests

This directory contains integration tests for stack-back that use real Docker containers and docker-compose.

## Test Coverage

### Volume Backups (`test_volume_backups.py`)
- Backing up bind mounts
- Backing up named Docker volumes
- Restoring data from backups
- Multiple backup snapshots
- Excluded services

### Database Backups (`test_database_backups.py`)
- MySQL backup and restore
- MariaDB backup and restore
- PostgreSQL backup and restore
- Multiple databases in a single backup
- Incremental backups after changes
- Database health checks

### SQLite Backups (`test_sqlite_backup.py`)
- Live WAL database backup through the online backup API
- Idle WAL database with no `-wal`/`-shm` sidecars
- Raw database/sidecar exclusion from the `/volumes` snapshot
- Restoring and opening the exported database read-only

### Vaultwarden Backups (`test_vaultwarden_backup.py`)
- Default online mode does not restart Vaultwarden
- Single snapshot containing database, attachments, sends, config and RSA keys
- Temporary files and icon cache excluded
- Optional consistent mode stops and restarts the service

### Label Configuration (`test_label_configuration.py`)
- Volume include patterns (`stack-back.volumes.include`)
- Volume exclude patterns (`stack-back.volumes.exclude`)
- Service exclusion (`stack-back.volumes=false`)
- Database-specific labels
- Multiple mount filtering

## Requirements

- Docker daemon running
- Docker compose plugin installed
- Sufficient disk space for test containers and volumes
