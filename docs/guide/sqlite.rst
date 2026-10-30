SQLite
======

stack-back uses SQLite's official online backup API
(``sqlite3.Connection.backup``) instead of copying ``.db``, ``-wal`` or
``-shm`` files.  The exported database is converted to rollback-journal mode,
validated, and stored as its own restic snapshot.

Automatic mode (default with ``AUTO_BACKUP_ALL``)
------------------------------------------------

Set ``AUTO_BACKUP_ALL=true`` and enable volume backup for the service.  No
SQLite labels are needed:

.. code:: yaml

    app:
      image: some_image
      volumes:
        - app_data:/data

stack-back will:

* scan only the service's backed-up Docker volume/bind mounts; it never scans
  the host filesystem or unrelated directories;
* mount those volumes read-only in the backup process container;
* find files with a SQLite header, including files without a ``.db`` suffix;
* export each database with the online backup API using a read-only source
  connection;
* store each database as a separate snapshot:
  ``/databases/<service>/<container path>`` (with a project segment when
  ``INCLUDE_PROJECT_NAME=true``);
* remove the raw database and its ``-wal``/``-shm``/``-journal`` files from
  the ``/volumes`` snapshot, including copies exposed through the backup
  service's own parent bind mounts.

The file scan is header-only for candidate files and runs over the service's
project data; it does not perform a host disk scan or read entire files just
to detect SQLite.  The actual database read is the online backup API export.

Explicit paths
--------------

Explicit paths are still useful when a volume contains many SQLite files
(application caches, historical backups, browser databases) and you only want
specific databases:

.. code:: yaml

    app:
      image: some_image
      labels:
        stack-back.sqlite: "true"
        stack-back.sqlite.paths: "/data/app.db,/data/cache.db"
      volumes:
        - app_data:/data

Paths must be absolute, comma-separated and inside a directory volume/bind
mount.  Glob patterns, single-file bind mounts and mount roots are not
supported.  Explicit paths use a read-write mount so SQLite can create/remove
WAL sidecars and perform crash recovery.

``stack-back.sqlite: false`` opts a service out of both automatic and explicit
SQLite backups.  This is useful for a container whose data is handled by
another tool.

Automatic detection without ``AUTO_BACKUP_ALL``
------------------------------------------------

To scan one service while ``AUTO_BACKUP_ALL`` is off, use:

.. code:: yaml

    labels:
      stack-back.sqlite: "auto"

``auto`` has no path list.  It scans the service's mounts read-only.  If the
service has no volume backup enabled, it still scans its connected
volume/bind mounts for this explicit request.

Vaultwarden and other dedicated handlers
----------------------------------------

Vaultwarden is intentionally excluded from generic automatic detection.
A Vaultwarden database needs the complete ``/data`` set (attachments, sends,
config and RSA keys), not just ``db.sqlite3``.  Use either:

* stack-back's dedicated Vaultwarden handler (see :doc:`vaultwarden`), or
* `ttionya/vaultwarden-backup <https://github.com/ttionya/vaultwarden-backup>`_
  as a separate Vaultwarden backup tool.

If you use ttionya, do not also enable ``stack-back.vaultwarden``.  Disable
stack-back's raw volume copy for the Vaultwarden service to avoid a second
inconsistent copy of ``/data``:

.. code:: yaml

    vaultwarden:
      image: vaultwarden/server:latest
      labels:
        stack-back.volumes: "false"
      volumes:
        - vaultwarden_data:/data

Configure the ttionya container according to its upstream README (data volume,
schedule, destination, encryption and restore procedure).  Automatic SQLite
detection skips both Vaultwarden images and
``ttionya/vaultwarden-backup`` images.

opt-outs and limitations
------------------------

* Automatic mode requires ``AUTO_BACKUP_ALL=true`` unless the service has
  ``stack-back.sqlite: auto``.
* Automatic mode only scans mounts included in that service's volume backup
  (``stack-back.volumes`` filters, include/exclude patterns and
  ``EXCLUDE_BIND_MOUNTS`` are honored).  Explicit paths still fail hard when
  a bind mount is excluded or a configured file is missing.
* A SQLite header identifies a database, not necessarily application data.
  Services that keep browser/cache/history SQLite files may produce extra
  snapshots.  Use explicit ``stack-back.sqlite.paths`` to narrow the scope.
* Automatic mode preflights each detected database with a read-only
  connection.  A database that cannot be opened read-only (for example a WAL
  database whose ``-shm`` is not readable) is skipped with a warning, and its
  raw file remains in ``/volumes``.  Use explicit paths (which use a
  read-write mount) or the stop-during-backup fallback for that database if
  its online export is required.
* The service itself and a running backup-process container are never backup
  targets, even if the image name does not contain ``stack-back``.
* SQLite WAL requires a local POSIX filesystem and a single host.  NFS/SMB,
  SQLCipher and custom SQLite VFS implementations are not supported.
* The online export holds a read transaction while it runs.  In
  rollback-journal mode writers may block; in WAL mode a long read can delay
  checkpointing and grow the ``-wal`` file.  Schedule large/busy databases
  off-peak.
* If read-only automatic export fails because of an application lock, use
  explicit paths plus ``stack-back.volumes.stop-during-backup: true`` as a
  fallback.

Restore
-------

Stop the service before restoring a database.  Restore the database snapshot,
delete any existing ``-wal``/``-shm``/``-journal`` files next to the target,
fix ownership/permissions, and start the service.  Restoring only
``/volumes`` is not sufficient when SQLite backup is enabled: the authoritative
database is the separately stored ``/databases/...`` snapshot.
