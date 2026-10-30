stack-back 中文文档
====================

英文文档：`English <../>`_

stack-back 用 restic 为 Docker Compose 项目做增量备份。它既可以备份
volume / bind mount，也可以备份 MySQL、MariaDB、PostgreSQL，以及
SQLite 和 Vaultwarden。

.. contents:: 本页目录
   :depth: 2
   :local:

快速开始
--------

在需要备份的 Compose 项目中加入一个 backup 服务：

.. code:: yaml

    services:
      backup:
        image: ghcr.io/lawndoc/stack-back:<version>
        restart: unless-stopped
        environment:
          - DOCKER_HOST=unix:///tmp/docker.sock
          - RESTIC_REPOSITORY=/restic_data
          - RESTIC_PASSWORD=请替换为强密码
          - AUTO_BACKUP_ALL=true
          - CRON_SCHEDULE=0 2 * * *
        volumes:
          - /var/run/docker.sock:/tmp/docker.sock:ro
          - ./restic:/restic_data
          - ./cache:/cache

需要备份的服务加上标签，或者设置 ``AUTO_BACKUP_ALL=true`` 自动备份
它们的 volume。backup 服务自身需要能访问 Docker socket，并且
``RESTIC_PASSWORD`` 必须安全保存；丢失它就无法恢复仓库。

详细安装方式见 `安装文档 <../guide/install.html>`_。

核心环境变量
------------

``RESTIC_REPOSITORY``
    仓库位置，例如本地路径 ``/restic_data`` 或 S3 地址。

``RESTIC_PASSWORD``
    restic 仓库密码，必须长期保存。

``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``
    使用 S3 仓库时的凭据。

``AUTO_BACKUP_ALL``
    设为 ``true`` 时，自动备份所有服务的 volume，并默认启用 SQLite
    自动识别。设为 ``false`` 时只处理显式标签。

``INCLUDE_ALL_COMPOSE_PROJECTS``
    是否把同一 Docker 主机上其它 Compose 项目也纳入备份。

``INCLUDE_PROJECT_NAME``
    是否在快照路径中加 Compose 项目名。

``EXCLUDE_BIND_MOUNTS``
    是否完全忽略 bind mount；启用后 bind 上的 SQLite 不会自动扫描。

``CRON_SCHEDULE``
    备份计划，默认每天 02:00。

``RESTIC_KEEP_DAILY`` / ``WEEKLY`` / ``MONTHLY`` / ``YEARLY``
    保留策略。

常用标签
--------

.. list-table::
   :header-rows: 1
   :widths: 38 62

   * - 标签
     - 作用
   * - ``stack-back.volumes: true``
     - 备份该服务的 volume / bind mount。
   * - ``stack-back.volumes.include`` / ``exclude``
     - 只备份或排除匹配的挂载路径。
   * - ``stack-back.volumes.stop-during-backup: true``
     - 备份前停止服务，结束后启动；适合独占锁场景。
   * - ``stack-back.mysql`` / ``mariadb`` / ``postgres``
     - 使用对应数据库的在线 dump。
   * - ``stack-back.sqlite: "true"``
     - 显式 SQLite 模式，必须同时写 ``stack-back.sqlite.paths``。
   * - ``stack-back.sqlite: "auto"``
     - 仅对该服务自动扫描 SQLite，不写路径。
   * - ``stack-back.sqlite: "false"``
     - 关闭该服务的 SQLite 备份；常用于 WAF、监控等日志库。
   * - ``stack-back.vaultwarden: "true"``
     - 启用 stack-back 的 Vaultwarden 专用备份（可选）。

SQLite 备份
-----------

当 ``AUTO_BACKUP_ALL=true`` 且服务启用 volume 备份时，stack-back 会
自动识别该服务 **实际备份的 Docker Compose 挂载目录** 中的 SQLite：

- 只扫描项目数据，不扫描宿主机磁盘或无关目录；
- 只读挂载并用 SQLite online backup API 导出；
- 读取文件头判断 SQLite，扩展名不是唯一依据；
- 自动生成的原始 DB 文件会从 ``/volumes`` 快照中排除；
- 每个数据库生成独立快照：
  ``/databases/<service>/<容器内路径>``；开启
  ``INCLUDE_PROJECT_NAME`` 时会多一段项目名。

自动模式会先做只读预检。打不开的 DB（例如 WAL 的 ``-shm`` 在只读
挂载下不可用）会被跳过并记录 warning，原始 ``/volumes`` 副本保留，
不会让整次备份失败。

显式路径
~~~~~~~~

如果 volume 里有很多缓存、浏览器 SQLite 或历史备份，只想备份指定文件：

.. code:: yaml

    labels:
      stack-back.sqlite: "true"
      stack-back.sqlite.paths: "/data/app.db,/data/cache.db"

显式路径需要绝对路径、位于目录 volume / bind 中；不支持 glob 和
单文件 bind。显式模式使用可写挂载，适合需要 SQLite 管理 WAL sidecar
的场景。

自动模式
~~~~~~~~

即使 ``AUTO_BACKUP_ALL=false``，也可以只让某个服务扫描：

.. code:: yaml

    labels:
      stack-back.sqlite: "auto"

关闭某个服务
~~~~~~~~~~~~

.. code:: yaml

    labels:
      stack-back.sqlite: "false"

例如 OpenResty / 1Panel WAF 的监控库通常不需要单独导出，可以加这个
标签；它的原始数据仍会随 ``stack-back.volumes`` 一起备份。

恢复 SQLite
~~~~~~~~~~~~

1. 停止使用该数据库的服务；
2. 恢复 ``/databases/...`` 下的数据库快照；
3. 删除目标位置旧的 ``-wal`` / ``-shm`` / ``-journal``；
4. 修正 owner / mode；
5. 启动服务。

只恢复 ``/volumes`` 不够：启用 SQLite 备份后，权威数据库是
``/databases/...`` 快照。

限制：

- WAL 需要本地 POSIX 文件系统，NFS/SMB 不支持；
- SQLCipher / 自定义 SQLite VFS 不支持；
- 在线备份会持有读事务，rollback-journal 模式可能阻塞写入，
  WAL 模式长期读可能推迟 checkpoint、使 WAL 增长；大库避开高峰；
- 自动模式是“尽力而为”：无法只读打开的库会跳过并保留原始副本。

更完整的 SQLite 说明见
`英文 SQLite 文档 <../guide/sqlite.html>`_。

Vaultwarden
-----------

Vaultwarden 建议使用
`ttionya/vaultwarden-backup <https://github.com/ttionya/vaultwarden-backup>`_
作为独立备份方案：

- 不要同时启用 ``stack-back.vaultwarden``；
- 给 Vaultwarden 服务加 ``stack-back.volumes: "false"``，避免
  stack-back 再备份一份原始 ``/data``；
- ttionya 自己负责数据库、attachments、sends、config、RSA key 的
  备份和恢复。

示例：

.. code:: yaml

    vaultwarden:
      image: vaultwarden/server:latest
      labels:
        stack-back.volumes: "false"
      volumes:
        - vaultwarden_data:/data

如果希望统一放在 restic 仓库里，也可以使用 stack-back 的专用模式：

.. code:: yaml

    labels:
      stack-back.vaultwarden: "true"
      # 默认 online，不停止服务
      # stack-back.vaultwarden.mode: "consistent"

详见 `Vaultwarden 文档 <../guide/vaultwarden.html>`_。

日常操作
--------

.. code:: bash

    # 查看状态
    docker compose exec backup rcb status

    # 手动备份
    docker compose exec backup rcb backup

    # 查看快照
    docker compose exec backup rcb snapshots

    # 清理旧快照 / 维护
    docker compose exec backup rcb maintenance

恢复时建议先停止业务服务，再把 restic 快照恢复到临时目录，确认数据库
完整性后再替换。恢复数据库后要删除旧的 WAL/SHM/journal 文件。

排错
----

**SQLite 显示 auto，但没有 DB 快照**

查看 ``rcb backup`` 日志中的 ``Skipping SQLite``。这通常表示只读预检
失败（常见于 WAL ``-shm`` 不可写）。原始 ``/volumes`` 副本仍在；需要
在线导出时改用显式路径（可写挂载）或 ``stop-during-backup``。

**bind mount 上的 SQLite**

``EXCLUDE_BIND_MOUNTS=true`` 时自动模式不会扫描 bind。显式路径遇到
被排除的 bind 会直接失败，不会静默跳过。

**Docker socket 权限**

backup 服务必须挂载 ``/var/run/docker.sock`` 到
``/tmp/docker.sock``，并设置 ``DOCKER_HOST=unix:///tmp/docker.sock``。

**Swarm**

Vaultwarden ``consistent`` 模式不支持 Swarm；使用 ``online``，或改用
ttionya。

更多英文文档
------------

- `安装 <../guide/install.html>`_
- `完整配置参考 <../guide/configuration.html>`_
- `SQLite 详细说明 <../guide/sqlite.html>`_
- `Vaultwarden <../guide/vaultwarden.html>`_
- `rcb 命令 <../guide/rcb.html>`_
- `工作原理 <../guide/advanced.html>`_
