Restore
=======

stack-back stores backups as restic snapshots.  There is no separate
``rcb restore`` command: restore with restic, then put the files back into the
right container volume or bind mount.

Always restore to a temporary directory first.  Do not restore a whole
``/volumes`` tree over a running host.

Find the snapshot
-----------------

Run restic inside the backup service:

.. code:: bash

    docker compose exec backup restic snapshots
    docker compose exec backup restic ls <snapshot-id>

Snapshot paths follow the service layout:

.. code:: text

    /volumes/<project>/<service>/<path-inside-container>
    /databases/<project>/<service>/<path-inside-container>
    /vaultwarden/<project>/<service>/
    /mysql/<project>/<service>/
    /postgres/<project>/<service>/

The exact project prefix depends on ``INCLUDE_PROJECT_NAME``.  Use
``restic ls`` rather than guessing.

Restore one path to a staging directory:

.. code:: bash

    docker compose exec backup restic restore <snapshot-id> \
        --target /restore \
        --include /volumes/myproject/myapp/data

``/restore`` is a normal directory inside the backup container.  If it is not
mounted, use a writable path such as ``/tmp/restore`` inside the container and
copy the result out before removing the container.

Restore a bind mount
--------------------

1. Stop the service that owns the files.
2. Restore the snapshot path to staging.
3. Copy it back, preserving ownership and hard links:

   .. code:: bash

       rsync -aHAX --delete /restore/volumes/myproject/myapp/data/ \
           /opt/1panel/docker/compose/myapp/data/

4. Fix ownership if the container runs as a non-root user.
5. Start the service and check its logs/data.

Restore a named volume
----------------------

Use a temporary container so ownership and permissions are preserved:

.. code:: bash

    docker run --rm \
      -v myapp_data:/target \
      -v /restore:/restore:ro \
      alpine sh -c 'cp -a /restore/volumes/myproject/myapp/data/. /target/'

Then start the service.  Named volumes are usually owned by the image's user;
copy as root and then ``chown`` if necessary.

Restore SQLite
--------------

The authoritative SQLite copy is the separate ``/databases/...`` snapshot.
The raw file may also exist in ``/volumes`` when automatic detection skipped it,
but the online backup snapshot is the consistent one.

1. Stop the service.
2. Restore the database to staging:

   .. code:: bash

       docker compose exec backup restic restore <snapshot-id> \
           --target /restore \
           --include /databases/myproject/myapp/var/lib/app/app.db

3. Copy the file to the live path and set the service's owner/mode.
4. Remove stale sidecars next to the target:

   .. code:: bash

       rm -f /path/to/app.db-wal /path/to/app.db-shm /path/to/app.db-journal

5. Start the service and verify it can read/write the database.

If the volume contains several SQLite files, restore only the databases you
need; restoring the whole volume is usually slower and can overwrite newer
files.

Restore Vaultwarden
-------------------

Vaultwarden needs the complete data set, not only ``db.sqlite3``: database,
attachments, sends, config and RSA keys.

1. Stop Vaultwarden.
2. Restore the whole Vaultwarden snapshot to staging:

   .. code:: bash

       docker compose exec backup restic restore <snapshot-id> \
           --target /restore \
           --include /vaultwarden/myproject/vaultwarden

3. Back up the current data directory before replacing it.
4. Copy the **contents** of the restored directory into the Vaultwarden data
   directory (do not copy the extra directory level):

   .. code:: bash

       rsync -aHAX /restore/vaultwarden/myproject/vaultwarden/ \
           /opt/1panel/docker/compose/vaultwarden/data/

5. Remove stale ``db.sqlite3-wal`` / ``db.sqlite3-shm`` files.
6. Set ownership to the UID/GID used by the Vaultwarden image.
7. Start Vaultwarden and test login, attachments and sends.

The exact snapshot path can differ because the project prefix and service name
are included.  Use ``restic ls`` to confirm it.  If you use ttionya instead of
stack-back's Vaultwarden handler, restore with ttionya's own procedure; do not
mix the two backup sets.

Restore MySQL / MariaDB / PostgreSQL
------------------------------------

stack-back stores a logical dump for these services.  Find it with
``restic ls``; it is usually a ``.sql`` or ``.dump`` file under
``/databases``/``/mysql``/``/postgres``.

MySQL/MariaDB:

.. code:: bash

    docker compose exec backup restic dump <snapshot-id> <dump-path> \
      | docker compose exec -T db sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'

PostgreSQL:

.. code:: bash

    docker compose exec backup restic dump <snapshot-id> <dump-path> \
      | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

Prefer a maintenance window, and take a fresh backup immediately after a
successful restore.

Restore 1Panel websites, certificates and Compose files
-------------------------------------------------------

For a 1Panel installation, the public website data and certificates are
normally under ``/opt/1panel/www/sites``.  They appear in the restic snapshot
through the OpenResty service mounts, for example:

.. code:: text

    /volumes/openresty/openresty/www/sites/<domain>/index
    /volumes/openresty/openresty/www/sites/<domain>/proxy/
    /volumes/openresty/openresty/www/sites/<domain>/ssl/fullchain.pem
    /volumes/openresty/openresty/www/sites/<domain>/ssl/privkey.pem

Compose files are backed up from the stack-back service's parent mount:

.. code:: text

    /volumes/_1panel/compose/<project>/docker-compose.yml
    /volumes/_1panel/apps/...        # 1Panel application files
    /volumes/_1panel/db/...          # 1Panel database
    /volumes/_1panel/secret/...      # 1Panel secrets, if mounted

Restore the selected paths to staging, copy them back, keep
``privkey.pem`` mode ``600`` and root ownership, then reload OpenResty:

.. code:: bash

    docker compose exec backup restic restore <snapshot-id> \
        --target /restore \
        --include /volumes/openresty/openresty/www/sites/example.com
    rsync -aHAX /restore/volumes/openresty/openresty/www/sites/example.com/ \
        /opt/1panel/www/sites/example.com/
    docker exec 1Panel-openresty-... openresty -t
    docker exec 1Panel-openresty-... openresty -s reload

If you restore a complete Compose project, restore its YAML and data together,
then validate with:

.. code:: bash

    cd /opt/1panel/docker/compose/<project>
    docker compose config
    docker compose -p <project> up -d

Full disaster recovery
----------------------

1. Install stack-back and configure the same restic repository and password.
2. Restore ``/volumes`` to staging.
3. Copy back the 1Panel paths, Compose files and application data.
4. Restore the selected ``/databases`` SQLite snapshots and database dumps.
5. Restore Vaultwarden and any other dedicated handler snapshots.
6. Start services in dependency order and verify.
7. Run a new ``rcb backup`` and confirm a fresh snapshot exists.

Do not forget the restic password.  Without it the repository cannot be
restored, even if the S3/remote storage is intact.

Verify before declaring success
-------------------------------

- ``restic snapshots`` shows the expected new snapshot.
- ``rcb status`` discovers the services and volumes.
- Application logs are clean.
- Login/database reads work.
- A new backup completes with exit code 0.
