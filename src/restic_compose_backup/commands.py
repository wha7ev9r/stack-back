import logging
from typing import List, Tuple, Union
import docker
from restic_compose_backup import utils
from subprocess import Popen, PIPE

logger = logging.getLogger(__name__)


def test():
    return run(["ls", "/volumes"])


def ping_mysql(container_id, host, port, username, password) -> int:
    """Check if the mysql is up and can be reached"""
    return docker_exec(
        container_id,
        [
            "mysqladmin",
            "ping",
            "--host",
            host,
            "--port",
            port,
            "--user",
            username,
        ],
        environment={"MYSQL_PWD": password},
    )


def ping_mariadb(container_id, host, port, username, password) -> int:
    """Check if the mariadb is up and can be reached"""
    return docker_exec(
        container_id,
        [
            "mariadb-admin",
            "ping",
            "--host",
            host,
            "--port",
            port,
            "--user",
            username,
        ],
        environment={"MYSQL_PWD": password},
    )


def ping_postgres(container_id, host, port, username, password) -> int:
    """Check if postgres can be reached"""
    return docker_exec(
        container_id,
        [
            "pg_isready",
            f"--host={host}",
            f"--port={port}",
            f"--username={username}",
        ],
    )


def docker_exec(
    container_id: str, cmd: List[str], environment: Union[dict, list] = []
) -> int:
    """Execute a command within the given container"""
    client = utils.docker_client()
    logger.debug("docker exec inside %s: %s", container_id, " ".join(cmd))
    exit_code, (stdout, stderr) = client.containers.get(container_id).exec_run(
        cmd, demux=True, environment=environment
    )

    if stdout:
        log_std(
            "stdout",
            stdout.decode(),
            logging.DEBUG if exit_code == 0 else logging.ERROR,
        )

    if stderr:
        log_std("stderr", stderr.decode(), logging.ERROR)

    return exit_code


def run(cmd: List[str], stdin=None) -> int:
    """Run a command with parameters"""
    logger.debug("cmd: %s", " ".join(map(str, cmd)))
    child = Popen(cmd, stdin=stdin, stdout=PIPE, stderr=PIPE)
    stdoutdata, stderrdata = child.communicate()

    if stdoutdata.strip():
        log_std(
            "stdout",
            stdoutdata.decode(),
            logging.DEBUG if child.returncode == 0 else logging.ERROR,
        )

    if stderrdata.strip():
        log_std("stderr", stderrdata.decode(), logging.ERROR)

    logger.debug("returncode %s", child.returncode)
    return child.returncode


def run_capture_std(cmd: List[str]) -> Tuple[str, str]:
    """Run a command with parameters and return stdout, stderr"""
    logger.debug("cmd: %s", " ".join(map(str, cmd)))
    child = Popen(cmd, stdout=PIPE, stderr=PIPE)
    return child.communicate()


def docker_path_is_file(
    container_id: str, path: str, *, log_missing: bool = True
) -> bool:
    """Check whether ``path`` is a regular file inside a container.

    Shell/exec is preferred because socket proxies commonly block the archive
    endpoint.  ``get_archive`` is used as a fallback for images without a
    shell.  If neither method works the path is treated as unverifiable and
    the backup itself will perform the strict check.  ``log_missing=False`` is
    used for probing optional path variants without noisy errors.
    """

    client = utils.docker_client()
    try:
        try:
            container = client.containers.get(container_id)
        except Exception as ex:
            if log_missing:
                logger.error("Could not inspect container %s: %s", container_id, ex)
            return False
        try:
            exit_code, _output = container.exec_run(
                ["/bin/sh", "-c", 'test -f "$1"', "sh", path],
                demux=True,
            )
            if exit_code == 0:
                return True
            if exit_code == 1:
                if log_missing:
                    logger.error(
                        "Path %s not found in container %s", path, container_id
                    )
                return False
        except Exception as ex:
            logger.debug("docker exec file check failed for %s: %s", path, ex)

        try:
            stream, stat = client.api.get_archive(container_id, path)
            try:
                next(stream, None)
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            mode = (stat or {}).get("mode", 0)
            if mode and (mode & 0o170000) != 0o100000:
                if log_missing:
                    logger.error(
                        "Path %s in container %s is not a regular file",
                        path,
                        container_id,
                    )
                return False
            return True
        except docker.errors.NotFound:
            if log_missing:
                logger.error("Path %s not found in container %s", path, container_id)
            return False
        except Exception as ex:
            if log_missing:
                logger.warning(
                    "Could not verify path %s in container %s: %s",
                    path,
                    container_id,
                    ex,
                )
            return True
    finally:
        client.close()


def log_std(source: str, data: str, level: int):
    if isinstance(data, bytes):
        data = data.decode()

    if not data.strip():
        return

    log_func = logger.debug if level == logging.DEBUG else logger.error
    log_func("%s %s %s", "-" * 10, source, "-" * 10)

    lines = data.split("\n")
    if lines[-1] == "":
        lines.pop()

    for line in lines:
        log_func(line)

    log_func("-" * 28)
