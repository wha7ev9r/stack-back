
![stack-back logo](./resources/stack-back_logo.svg)

[![docs](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://wha7ev9r.github.io/stack-back/)

[中文文档](https://wha7ev9r.github.io/stack-back/zh/)

Automated incremental backups using [restic] for any docker-compose setup.

* Backup docker volumes or host binds
* Backup postgres, mariadb, and mysql databases
* Backup SQLite databases online (WAL-safe) and Vaultwarden as a single snapshot
* Notifications over SMTP or Discord webhooks

# Usage

### Just add it to your services

```yaml
services:
  backup:
    image: ghcr.io/lawndoc/stack-back:<version>
    env_file:
      - stack-back.env
    environment:
      - AUTO_BACKUP_ALL: True
    volumes:
      - /var/run/docker.sock:/tmp/docker.sock:ro
      - cache:/cache # Persistent restic cache (greatly speeds up all restic operations)
```

### and it will back up all your volumes and databases

```yaml
  web:
    image: some_image  # Backs up the volumes below
    volumes:
      - media:/srv/media
      - /srv/files:/srv/files
  mysql:
    image: mysql:9  # Performs stateful backup using mysqldump
    volumes:
      - mysql:/var/lib/mysql  # Only SQL dump is backed up
```

[Documentation](https://wha7ev9r.github.io/stack-back/)

Please report issus on [github](https://github.com/lawndoc/stack-back/issues).

## Configuration (environment variables)

Minimum configuration

```bash
RESTIC_REPOSITORY
RESTIC_PASSWORD
```

All config options can be found in the [template env file](./stack-back.env.template)

Restic-specific environment variables can be found in the [restic documentation](https://restic.readthedocs.io/en/stable/040_backup.html#environment-variables)

### Example config: S3 bucket

stack-back.env

```bash
AUTO_BACKUP_ALL=True
RESTIC_REPOSITORY=s3:s3.us-east-1.amazonaws.com/bucket_name
RESTIC_PASSWORD=thisdecryptsyourbackupsdontloseit
AWS_ACCESS_KEY_ID=<your access key id>
AWS_SECRET_ACCESS_KEY=<your access key id>
CHECK_WITH_CACHE=true
# snapshot prune rules
RESTIC_KEEP_DAILY=7
RESTIC_KEEP_WEEKLY=4
RESTIC_KEEP_MONTHLY=12
RESTIC_KEEP_YEARLY=3
# Cron schedule. Run every day at 1am
CRON_SCHEDULE="0 1 * * *"
```

## Advanced configuration (container labels)

You can also use `stack-back` container labels for granular control over which volumes get backed up.

```yaml
  web:
    image: some_image
    labels:
      - stack-back.volumes.exclude: files  # host mount substring matching
    volumes:
      - media:/srv/media       # will be backed up
      - /srv/files:/srv/files  # will NOT be backed up
  mysql:
    image: mysql:9
    labels:
      - stack-back.mysql: False   # don't perform database dump backup
      - stack-back.volumes: False # don't back up any volumes for this container either
    volumes:
      - mysql:/var/lib/mysql
```

With the latest version and `AUTO_BACKUP_ALL=true`, SQLite databases are
detected automatically in the service's backed-up Docker Compose mounts and
exported online; **no labels are needed** for ordinary services. The scan is
limited to project data, uses read-only mounts, and does not scan the host
filesystem. Raw database files are excluded from the `/volumes` snapshot.

A live WAL database that cannot be opened by the read-only preflight is skipped
with a warning; use explicit paths for that database if an online snapshot is
required.

Explicit paths are available when you want to select specific databases:

```yaml
  app:
    image: some_image
    labels:
      stack-back.sqlite: "true"
      stack-back.sqlite.paths: "/data/app.db,/data/cache.db"
    volumes:
      - app_data:/data
```

Use `stack-back.sqlite: false` to opt out of SQLite backups for a service.
See the [SQLite guide](https://wha7ev9r.github.io/stack-back/guide/sqlite.html).

> **Vaultwarden is an explicit exception.** It is intentionally not
> auto-enabled by volume/SQLite discovery and still requires the dedicated
> labels below. Do not remove them when simplifying other services.

Vaultwarden gets a dedicated safe backup (default `online`, no restart; set
`stack-back.vaultwarden.mode: "consistent"` for a brief stop and a strictly
single-point snapshot):

```yaml
  vaultwarden:
    image: vaultwarden/server:latest
    labels:
      stack-back.vaultwarden: "true"
      # stack-back.vaultwarden.mode: "consistent"
      stack-back.volumes: "false"
    volumes:
      - vaultwarden_data:/data
```

If you prefer [ttionya/vaultwarden-backup](https://github.com/ttionya/vaultwarden-backup),
use that sidecar instead and set `stack-back.volumes: "false"` on the
Vaultwarden service. Do not enable both backup methods.

Detailed documentation on compose labels can be found in the [stack-back documentation](https://wha7ev9r.github.io/stack-back/guide/configuration.html#compose-labels)

See also the [Vaultwarden backup guide](https://wha7ev9r.github.io/stack-back/guide/vaultwarden.html)
and the [restore guide](https://wha7ev9r.github.io/stack-back/guide/restore.html).

## The `rcb` command

Everything is controlled using the [`rcb` command line tool](./src/) from this repo.

You can use the `rcb status` command to verify the configuration.

```bash
$ docker-compose exec -it backup rcb status
INFO: Status for compose project 'myproject'
INFO: Repository: '<restic repository>'
INFO: Backup currently running?: False
INFO: --------------- Detected Config ---------------
INFO: service: mysql
INFO:  - mysql (is_ready=True)
INFO: service: mariadb
INFO:  - mariadb (is_ready=True)
INFO: service: postgres
INFO:  - postgres (is_ready=True)
INFO: service: web
INFO:  - volume: media
INFO:  - volume: /srv/files
```

The `status` subcommand lists what will be backed up and even pings the database services checking their availability.

More `rcb` commands can be found in the [documentation].

# Contributing

Contributions are welcome regardless of experience level.

## Python environment

Use [`uv`](https://docs.astral.sh/uv/) within the repo root directory to manage your development environment.

```bash
git clone https://github.com/lawndoc/stack-back.git
cd stack-back
uv sync --directory src/
```

## Running unit tests

Make sure `uv` is already set up as shown above.

```bash
# Run only unit tests (fast)
uv run --directory src/ pytest -m unit

# Run only integration tests
uv run --directory src/ pytest -m integration

# Run all tests (unit + integration)
uv run --directory src/ pytest
```

**Note:** Integration tests require Docker and docker compose plugin, and will spin up real database containers. They take significantly longer than unit tests.

## Building Docs

```bash
pip install -r docs/requirements.txt
python src/setup.py build_sphinx
```

[restic]: https://restic.net/
[documentation]: https://wha7ev9r.github.io/stack-back/

---
This project is an actively maintained fork of [restic-compose-backup](https://github.com/ZettaIO/restic-compose-backup) by [Zetta.IO](https://www.zetta.io).

Huge thanks to them for creating this project.

[![Zetta.IO](https://raw.githubusercontent.com/lawndoc/stack-back/main/.github/logo.png)](https://www.zetta.io)
