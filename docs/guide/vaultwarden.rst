Vaultwarden
===========

stack-back can back up Vaultwarden with an online SQLite backup and a single
restic snapshot containing the database and all important data files.

The default is the same approach used by the community backup projects:

* `ttionya/vaultwarden-backup <https://github.com/ttionya/vaultwarden-backup>`_
* `Bruceforce/vaultwarden-backup <https://github.com/Bruceforce/vaultwarden-backup>`_
* `jjlin/vaultwarden-backup <https://github.com/jjlin/vaultwarden-backup>`_

They keep Vaultwarden running, use SQLite's ``.backup`` command (the online
backup API) for ``db.sqlite3`` and copy ``attachments``, ``sends``,
``config.json`` and ``rsa_key.*``.  stack-back does the same, but puts
everything into one restic snapshot and verifies the database and staging
directory before uploading.

Alternative: ttionya/vaultwarden-backup
---------------------------------------

If you prefer a dedicated Vaultwarden backup tool, use
`ttionya/vaultwarden-backup <https://github.com/ttionya/vaultwarden-backup>`_
instead of stack-back's Vaultwarden handler.  Do not enable both.

stack-back's automatic SQLite detection skips Vaultwarden and
``ttionya/vaultwarden-backup`` images.  To prevent stack-back's AUTO volume
backup from copying a second, potentially inconsistent copy of ``/data``,
disable volume backup on the Vaultwarden service:

.. code:: yaml

    vaultwarden:
      image: vaultwarden/server:latest
      labels:
        stack-back.volumes: "false"
      volumes:
        - vaultwarden_data:/data

Configure the ttionya sidecar according to its upstream README.  It is
responsible for its own schedule, destination, encryption and restore
procedure; those are separate from the stack-back restic repository.

Configuration
-------------

Add the following labels to the Vaultwarden service:

.. code:: yaml

    vaultwarden:
      image: vaultwarden/server:latest
      labels:
        stack-back.vaultwarden: "true"
        # optional, default: online
        # stack-back.vaultwarden.mode: "online"
        # optional, default: false (icon_cache can be regenerated)
        # stack-back.vaultwarden.icon-cache: "true"
      volumes:
        - vaultwarden_data:/data

``stack-back.vaultwarden`` must be explicitly enabled.  Unlike the MySQL,
MariaDB and PostgreSQL integrations, ``AUTO_BACKUP_ALL`` does not enable
Vaultwarden automatically, because the online/consistent choice changes
service availability.

Modes
-----

``online`` (default)
    Vaultwarden keeps running.  stack-back exports ``db.sqlite3`` through
    SQLite's online backup API, then copies the remaining files into a
    temporary staging directory and uploads it as one restic snapshot.

    The database snapshot is consistent.  The files are copied after the
    database snapshot, so there is a small window in which an attachment or
    Send deleted at the same moment could be missing from the file copy.
    ``config.json`` is re-read and JSON-validated with retries.  This is the
    same trade-off as the community tools above.

``consistent`` (optional)
    stack-back stops Vaultwarden, exports the database and copies the data
    directory while the service is stopped, starts Vaultwarden again, and then
    uploads the staged data to restic.  The service outage only covers
    ``stop + local copy + start``, not the restic upload.  Use this mode when
    the database and attachments must represent exactly the same point in
    time.

    A stopped Vaultwarden can also be a problem if the backup process is
    killed before it restarts the service.  stack-back writes a stop intent
    file and attempts to restart the service from the parent process and on a
    later ``rcb status``/``rcb backup`` run, but a SIGKILL of the whole backup
    stack or a host crash cannot be handled automatically.  Monitor
    Vaultwarden after unusual backup failures.

    ``consistent`` mode is not supported in Swarm mode, because Swarm may
    recreate a stopped task.  Use ``online`` there.

What is backed up
-----------------

The snapshot path is:

.. code:: text

    /vaultwarden/<service>/
    ├── db.sqlite3
    ├── attachments/...
    ├── sends/...
    ├── config.json
    ├── rsa_key.der
    ├── rsa_key.pem
    ├── rsa_key.pub.der
    ├── rsa_key.pub.pem
    └── icon_cache/...      only when explicitly enabled

``db.sqlite3`` is written from SQLite's online backup API, converted to
rollback-journal mode and contains no ``-wal``/``-shm``/``-journal`` files.
``tmp``, ``LOG_FILE``/``LOG_DIR`` and (by default) ``icon_cache`` are skipped.

The Vaultwarden data directory is excluded from the ordinary ``/volumes``
snapshot.  Restore Vaultwarden from the ``/vaultwarden`` snapshot, not from
``/volumes``.

When ``INCLUDE_PROJECT_NAME`` is enabled the project name is added to the path:
``/vaultwarden/<project>/<service>``.

Environment variables
---------------------

The module reads Vaultwarden's own environment variables:

``DATA_FOLDER``
    Default ``/data``.  Relative paths are resolved against the image's
    working directory (``/``).

``DATABASE_URL``
    Default ``sqlite://%DATA_FOLDER%/db.sqlite3``.  SQLite URLs and bare paths
    are supported; MySQL/PostgreSQL URLs are rejected by this module.

``ATTACHMENTS_FOLDER``, ``SENDS_FOLDER``, ``ICON_CACHE_FOLDER``,
``RSA_KEY_FILENAME``, ``TMP_FOLDER``, ``LOG_FILE``, ``LOG_DIR``
    Defaults follow the official Vaultwarden defaults under ``DATA_FOLDER``.
    They must be inside ``DATA_FOLDER`` in this version.

``stack-back.vaultwarden.data-dir``
    Optional label to override ``DATA_FOLDER`` for unusual layouts.

stack-back also supports:

``VAULTWARDEN_STAGING_DIR``
    Default ``/vaultwarden``.  Temporary staging area.  For large attachment
    collections, mount a volume at this path in the backup container.  It must
    not be inside the Vaultwarden data directory or under ``/volumes``.

``VAULTWARDEN_STOP_TIMEOUT``
    Default ``30`` seconds.  Stop timeout for ``consistent`` mode.

``VAULTWARDEN_STATE_DIR``
    Optional directory for crash-recovery intent files.  Defaults to
    ``/cache/.stack-back`` when available, otherwise ``/tmp/.stack-back``.

``SQLITE_BUSY_TIMEOUT``
    Default ``30`` seconds.  SQLite busy timeout for the database export.

Restore
-------

Always test a restore before relying on a backup.

1. Stop Vaultwarden.
2. Find the snapshot and restore it to a temporary directory:

   .. code:: bash

       restic snapshots --path /vaultwarden/vaultwarden
       restic restore <snapshot-id> --target /tmp/vw-restore

3. Restore into a **clean** data directory, not on top of the old one:

   .. code:: text

       /tmp/vw-restore/vaultwarden/<service>/*  ->  DATA_FOLDER

4. Delete stale SQLite sidecars in the target (a ``.backup`` database is
   self-contained; a stale ``-wal`` can corrupt recovery):

   .. code:: bash

       rm -f DATA_FOLDER/db.sqlite3-wal DATA_FOLDER/db.sqlite3-shm DATA_FOLDER/db.sqlite3-journal

5. Restore the four ``rsa_key.*`` files, ``config.json``, ``attachments`` and
   ``sends`` together with the database from the same snapshot.
6. Set ownership and permissions to match the Vaultwarden container user
   (for example ``user: 1000``).  ``config.json`` and the private RSA key must
   not be world-readable.
7. Restore with the same or a newer Vaultwarden image.  Vaultwarden does not
   support downgrading a database; a newer image may migrate it.
8. Start Vaultwarden and verify login, attachment download, Send and the admin
   page.

External configuration such as the compose file, ``.env``, ``ADMIN_TOKEN``,
SMTP credentials, ``DATABASE_URL`` and ``DATA_FOLDER`` is usually **not**
inside the Vaultwarden data directory.  Save it separately and include it in
the recovery checklist.

Security
--------

``config.json`` can contain admin tokens and SMTP credentials.
``rsa_key.pem`` is a private key; the attachments and the database contain
sensitive vault data.  restic encrypts all of it, so the restic repository
password and repository access are the critical secrets.  Keep the staging
directory private, clean it after every run, and do not put the restic
password in the same place as the backup repository.

Limitations
-----------

* Only the SQLite Vaultwarden backend is supported by this module.  For
  MySQL/PostgreSQL, use ``stack-back.mysql``/``stack-back.postgres`` for the
  database and a separate volume backup for the data files.
* Paths outside ``DATA_FOLDER`` (custom attachments/sends/rsa locations) are
  rejected in this version.
* If ``EXCLUDE_BIND_MOUNTS`` is enabled and Vaultwarden's data directory is a
  bind mount, the backup fails instead of silently skipping the vault.
* ``consistent`` mode is not supported in Swarm.
* SQLite WAL requires a local POSIX filesystem.  Network filesystems
  (NFS/SMB) are not supported for WAL mode.
* Encrypted SQLCipher databases and custom SQLite VFS implementations are not
  supported.
* Large attachment collections require staging space roughly equal to the
  data directory.

Status
------

``rcb status`` shows the mode, the resolved paths, the snapshot destination and
whether the database file can be reached.  It also warns when no
``rsa_key*`` variant is found.  For ``consistent`` mode it warns that the
service will be stopped during the staging copy.
