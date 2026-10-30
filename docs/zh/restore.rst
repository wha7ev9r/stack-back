恢复数据
========

stack-back 没有单独的 ``rcb restore`` 命令；备份是 restic 快照，恢复时用
restic，再把文件放回正确的 volume 或 bind mount。

始终先恢复到临时目录，确认内容后再替换线上数据。不要在运行中的主机上直接
把整个 ``/volumes`` 覆盖回去。

找到快照
--------

在 stack-back 容器里执行：

.. code:: bash

    docker exec -it stack-back restic snapshots
    docker exec -it stack-back restic ls <snapshot-id>

常见路径：

.. code:: text

    /volumes/<project>/<service>/<容器内路径>
    /databases/<project>/<service>/<容器内路径>
    /vaultwarden/<project>/<service>/
    /mysql/<project>/<service>/
    /postgres/<project>/<service>/

项目名前缀取决于 ``INCLUDE_PROJECT_NAME``；以 ``restic ls`` 的实际输出为准。

把指定路径恢复到临时目录：

.. code:: bash

    docker exec -it stack-back restic restore <snapshot-id> \
        --target /restore \
        --include /volumes/myproject/myapp/data

``/restore`` 是备份容器内的目录；如果不存在，用 ``/tmp/restore`` 之类的
可写目录，确认后再复制出来。

恢复 bind mount
---------------

1. 停止使用这些文件的服务。
2. 用 restic 恢复到临时目录。
3. 复制回宿主机路径，保留权限和硬链接：

   .. code:: bash

       rsync -aHAX --delete /restore/volumes/myproject/myapp/data/ \
           /opt/1panel/docker/compose/myapp/data/

4. 如果容器以非 root 用户运行，修正 owner/group。
5. 启动服务并检查日志和数据。

恢复 named volume
-----------------

用临时容器恢复，避免权限被宿主用户改变：

.. code:: bash

    docker run --rm \
      -v myapp_data:/target \
      -v /restore:/restore:ro \
      alpine sh -c 'cp -a /restore/volumes/myproject/myapp/data/. /target/'

然后启动服务；必要时再 ``chown``。

恢复 SQLite
-----------

SQLite 的权威副本是单独的 ``/databases/...`` 快照。只恢复 ``/volumes``
可能拿到的是跳过导出时的原始文件，不保证一致性。

1. 停止服务。
2. 恢复数据库到临时目录：

   .. code:: bash

       docker exec -it stack-back restic restore <snapshot-id> \
           --target /restore \
           --include /databases/myproject/myapp/var/lib/app/app.db

3. 复制到线上路径，并设置服务用户的 owner/mode。
4. 删除目标位置旧的 sidecar：

   .. code:: bash

       rm -f /path/to/app.db-wal /path/to/app.db-shm /path/to/app.db-journal

5. 启动服务，确认能正常读写数据库。

一个 volume 里有多个 SQLite 时，只恢复需要的库；不要整卷覆盖，以免把
较新的非数据库文件回退。

恢复 Vaultwarden
----------------

Vaultwarden 需要完整数据：``db.sqlite3``、attachments、sends、
``config.json`` 和 RSA key，不能只恢复数据库。

1. 停止 Vaultwarden。
2. 恢复整个 Vaultwarden 快照：

   .. code:: bash

       docker exec -it stack-back restic restore <snapshot-id> \
           --target /restore \
           --include /vaultwarden/myproject/vaultwarden

3. 先备份当前数据目录。
4. 把恢复目录的 **内容** 复制到 Vaultwarden 的 data 目录，不要多套一层：

   .. code:: bash

       rsync -aHAX /restore/vaultwarden/myproject/vaultwarden/ \
           /opt/1panel/docker/compose/vaultwarden/data/

5. 删除旧的 ``db.sqlite3-wal`` / ``db.sqlite3-shm``。
6. 修正 owner/group。
7. 启动 Vaultwarden，验证登录、附件和 sends。

实际路径可能包含项目名和 service 名；先用 ``restic ls`` 确认。使用
ttionya 的话，用 ttionya 自己的恢复流程，不要混用两套备份。

恢复 MySQL / MariaDB / PostgreSQL
---------------------------------

数据库备份是逻辑 dump，用 ``restic ls`` 找到 ``.sql`` / ``.dump`` 文件，
再用 ``restic dump`` 流式导入。

MySQL / MariaDB：

.. code:: bash

    docker exec stack-back restic dump <snapshot-id> <dump-path> \
      | docker exec -i db sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'

PostgreSQL：

.. code:: bash

    docker exec stack-back restic dump <snapshot-id> <dump-path> \
      | docker exec -i db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

建议在维护窗口执行；恢复成功后立刻重新备份一次。

恢复 1Panel 网站、证书和 Compose 文件
-------------------------------------

1Panel 的网站文件和证书通常位于 ``/opt/1panel/www/sites``，会通过
OpenResty 的挂载进入快照，例如：

.. code:: text

    /volumes/openresty/openresty/www/sites/<domain>/index
    /volumes/openresty/openresty/www/sites/<domain>/proxy/
    /volumes/openresty/openresty/www/sites/<domain>/ssl/fullchain.pem
    /volumes/openresty/openresty/www/sites/<domain>/ssl/privkey.pem

Compose 文件和 1Panel 数据：

.. code:: text

    /volumes/_1panel/compose/<project>/docker-compose.yml
    /volumes/_1panel/apps/...
    /volumes/_1panel/db/...
    /volumes/_1panel/secret/...

恢复示例：

.. code:: bash

    docker exec -it stack-back restic restore <snapshot-id> \
        --target /restore \
        --include /volumes/openresty/openresty/www/sites/example.com
    rsync -aHAX /restore/volumes/openresty/openresty/www/sites/example.com/ \
        /opt/1panel/www/sites/example.com/

证书私钥保持 ``600`` 和 root owner；恢复后检查并重载 OpenResty：

.. code:: bash

    docker exec <openresty-container> openresty -t
    docker exec <openresty-container> openresty -s reload

如果恢复整个 Compose 项目，YAML 和数据一起恢复，然后校验：

.. code:: bash

    cd /opt/1panel/docker/compose/<project>
    docker compose config
    docker compose -p <project> up -d

完整灾难恢复顺序
----------------

1. 安装 stack-back，配置同一个 restic 仓库和密码。
2. 恢复 ``/volumes`` 到临时目录。
3. 恢复 1Panel 网站、证书、Compose 文件和 apps/db/secret。
4. 恢复 ``/databases`` 下需要的 SQLite。
5. 恢复 Vaultwarden、MySQL/PostgreSQL 等专用快照。
6. 按依赖顺序启动服务并验证。
7. 重新执行 ``rcb backup``，确认有新快照。

**restic 密码必须单独保存。** 只有 S3/远端存储而丢失密码，仓库无法恢复。

验证
----

- ``restic snapshots`` 能看到新快照；
- ``rcb status`` 能发现服务和卷；
- 业务日志无报错；
- 登录、数据库读写正常；
- 再跑一次备份，退出码为 0。
