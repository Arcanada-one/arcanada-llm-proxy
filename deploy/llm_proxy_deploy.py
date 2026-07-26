#!/usr/bin/python3 -I
"""Root-owned, exact-digest deployment boundary for Arcanada LLM Proxy."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

IMAGE_REPOSITORY = "ghcr.io/arcanada-one/arcanada-llm-proxy"
CONTAINER = "arcanada-llm-proxy"
DEFAULT_NETWORK = "code_default"
MODEL_CONNECTOR_NETWORK = "model-connector_default"
EXPECTED_NETWORKS = {DEFAULT_NETWORK, MODEL_CONNECTOR_NETWORK}
DOCKER = "/usr/bin/docker"
SYSTEMCTL = "/usr/bin/systemctl"
HELPER = pathlib.Path("/usr/local/sbin/arcanada-llm-proxy-deploy")
SERVICE_UNIT = pathlib.Path("/etc/systemd/system/arcanada-llm-proxy-rollback@.service")
TIMER_UNIT = pathlib.Path("/etc/systemd/system/arcanada-llm-proxy-rollback@.timer")
STATE_ROOT = pathlib.Path("/var/lib/arcanada-llm-proxy-deploy")
RELEASES_ROOT = STATE_ROOT / "releases"
LOCK_FILE = STATE_ROOT / "deploy.lock"
LOCAL_HEALTH_URL = "http://127.0.0.1:4000/health"
ORIGIN_ACL_UNIT = "sup0033-origin-acl.service"
WATCHDOG_DEADLINE_SECONDS = 580

RELEASE_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

APPLICATION_ENV_KEYS = {
    "LANGFUSE_OTLP_ENDPOINT",
    "LANGFUSE_OTLP_HEADERS",
    "LLM_PROXY_FORCE_ROUTE",
    "LLM_PROXY_SHARED_SECRET",
    "MC_API_KEY",
    "MC_BASE_URL",
}
REQUIRED_APPLICATION_ENV_KEYS = {
    "LLM_PROXY_SHARED_SECRET",
    "MC_API_KEY",
    "MC_BASE_URL",
}
IMAGE_ENV_KEYS = {
    "GPG_KEY",
    "LANG",
    "PATH",
    "PYTHON_PIP_VERSION",
    "PYTHON_SETUPTOOLS_VERSION",
    "PYTHON_GET_PIP_SHA256",
    "PYTHON_GET_PIP_URL",
    "PYTHON_SHA256",
    "PYTHON_VERSION",
}
SAFE_PROCESS_ENV = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


class DeployError(RuntimeError):
    """An operator-safe deployment failure that contains no secret values."""


def validate_release(value: str) -> str:
    if RELEASE_RE.fullmatch(value) is None:
        raise ValueError("release_sha must be exactly 40 lowercase hex characters")
    return value


def validate_image_digest(value: str) -> str:
    if DIGEST_RE.fullmatch(value) is None:
        raise ValueError("image_digest must be an exact lowercase sha256 digest")
    return f"{IMAGE_REPOSITORY}@{value}"


def validate_sha256(value: str) -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise ValueError("bundle identity must be exactly 64 lowercase hex characters")
    return value


def validate_run_id(value: str) -> str:
    if RUN_ID_RE.fullmatch(value) is None:
        raise ValueError("ci_run_id must be a positive decimal identifier")
    return value


def rollback_container_name(release_sha: str) -> str:
    return f"{CONTAINER}-rollback-{validate_release(release_sha)[:12]}"


def watchdog_decision(*, committed: bool, healthy: bool, now: int, deadline: int) -> str:
    if committed:
        return "committed"
    if not healthy or now >= deadline:
        return "rollback"
    return "wait"


def environment_from_list(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise ValueError("container environment contains an invalid entry")
        name, value = entry.split("=", 1)
        if ENV_NAME_RE.fullmatch(name) is None or name in result:
            raise ValueError("container environment contains an invalid or duplicate name")
        result[name] = value
    return result


def project_runtime_environment(values: Sequence[str]) -> dict[str, str]:
    current = environment_from_list(values)
    unknown = set(current) - APPLICATION_ENV_KEYS - IMAGE_ENV_KEYS
    if unknown:
        raise DeployError("current runtime contains unreviewed environment names")
    missing = [name for name in REQUIRED_APPLICATION_ENV_KEYS if not current.get(name)]
    if missing:
        raise DeployError("current runtime is missing required application settings")
    return {name: current.get(name, "") for name in sorted(APPLICATION_ENV_KEYS)}


def validate_runtime_topology(document: Mapping[str, Any]) -> None:
    config = document.get("Config")
    host = document.get("HostConfig")
    networks_document = document.get("NetworkSettings")
    state = document.get("State")
    if not all(isinstance(item, Mapping) for item in (config, host, networks_document, state)):
        raise DeployError("production container inspect returned an unexpected shape")
    assert isinstance(host, Mapping)
    assert isinstance(networks_document, Mapping)
    assert isinstance(state, Mapping)

    restart = host.get("RestartPolicy")
    port_bindings = host.get("PortBindings")
    networks = networks_document.get("Networks")
    health = state.get("Health")
    expected_ports = {
        "4000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4000"}],
    }
    if (
        host.get("Privileged") is not False
        or host.get("NetworkMode") != DEFAULT_NETWORK
        or not isinstance(restart, Mapping)
        or restart.get("Name") != "unless-stopped"
        or port_bindings != expected_ports
        or document.get("Mounts") != []
        or not isinstance(networks, Mapping)
        or set(networks) != EXPECTED_NETWORKS
        or state.get("Running") is not True
        or not isinstance(health, Mapping)
        or health.get("Status") != "healthy"
    ):
        raise DeployError("production container topology violates the reviewed contract")

    for key in ("Binds", "Devices", "CapAdd"):
        if host.get(key) not in (None, []):
            raise DeployError("production container has an unreviewed host capability")


def validate_image_provenance(document: Mapping[str, Any], release_sha: str) -> None:
    release = validate_release(release_sha)
    config = document.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if not isinstance(labels, Mapping) or any(
        labels.get(name) != release
        for name in (
            "org.opencontainers.image.revision",
            "io.arcanada.release_sha",
        )
    ):
        raise DeployError("published image is not bound to the requested release")


def safe_error(error: BaseException) -> str:
    return str(error) if isinstance(error, DeployError | ValueError) else type(error).__name__


def fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_directory(path: pathlib.Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or info.st_mode & 0o077
    ):
        raise DeployError("root-owned deploy state directory is insecure")


def atomic_bytes(path: pathlib.Path, body: bytes, mode: int = 0o600) -> None:
    secure_directory(path.parent)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = pathlib.Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_json(path: pathlib.Path, payload: Any) -> None:
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    atomic_bytes(path, body)


def atomic_environment(path: pathlib.Path, values: Mapping[str, str]) -> None:
    for name, value in values.items():
        if ENV_NAME_RE.fullmatch(name) is None or "\n" in value or "\r" in value:
            raise DeployError("runtime environment cannot be serialized safely")
    body = "".join(f"{name}={value}\n" for name, value in sorted(values.items()))
    atomic_bytes(path, body.encode())


def require_root() -> None:
    if os.geteuid() != 0:
        raise DeployError("root-owned deploy boundary required")


def require_root_file(path: pathlib.Path, *, executable: bool = False) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or info.st_mode & 0o022
        or (executable and not info.st_mode & stat.S_IXUSR)
    ):
        raise DeployError("root-owned deploy bundle is insecure")


def ensure_layout() -> None:
    require_root_file(HELPER, executable=True)
    require_root_file(SERVICE_UNIT)
    require_root_file(TIMER_UNIT)
    secure_directory(STATE_ROOT)
    secure_directory(RELEASES_ROOT)


def command(
    args: Sequence[str],
    *,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            env=SAFE_PROCESS_ENV,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DeployError(f"{pathlib.Path(args[0]).name} command timed out") from error
    if check and result.returncode != 0:
        raise DeployError(f"{pathlib.Path(args[0]).name} command failed")
    return result


def command_ok(args: Sequence[str], *, timeout: int = 30) -> bool:
    return command(args, timeout=timeout, check=False).returncode == 0


def command_json(args: Sequence[str]) -> Any:
    try:
        return json.loads(command(args).stdout)
    except json.JSONDecodeError as error:
        raise DeployError(f"{pathlib.Path(args[0]).name} returned invalid JSON") from error


def inspect_container(name: str = CONTAINER) -> dict[str, Any]:
    document = command_json([DOCKER, "container", "inspect", name])
    if not isinstance(document, list) or len(document) != 1:
        raise DeployError("production container inspect returned an unexpected shape")
    value = document[0]
    if not isinstance(value, dict):
        raise DeployError("production container inspect returned an unexpected shape")
    return value


def inspect_image(image: str) -> dict[str, Any]:
    document = command_json([DOCKER, "image", "inspect", image])
    if not isinstance(document, list) or len(document) != 1:
        raise DeployError("published image inspect returned an unexpected shape")
    value = document[0]
    if not isinstance(value, dict):
        raise DeployError("published image inspect returned an unexpected shape")
    return value


def container_exists(name: str) -> bool:
    return command_ok([DOCKER, "container", "inspect", name])


def local_health_ok() -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(LOCAL_HEALTH_URL, timeout=5) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read())
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except (OSError, TimeoutError, ValueError):
        return False


def wait_for_health(seconds: int = 75) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if local_health_ok():
            try:
                document = inspect_container()
                health = document.get("State", {}).get("Health", {})
                if health.get("Status") == "healthy":
                    return True
            except DeployError:
                pass
        time.sleep(2)
    return False


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(helper_sha: str, service_sha: str, timer_sha: str) -> None:
    expected = tuple(validate_sha256(item) for item in (helper_sha, service_sha, timer_sha))
    actual = tuple(sha256_file(path) for path in (HELPER, SERVICE_UNIT, TIMER_UNIT))
    if actual != expected:
        raise DeployError("installed root-owned deploy bundle differs from reviewed source")
    print("LLM_PROXY_DEPLOY_BUNDLE_PASS")


def release_path(release_sha: str) -> pathlib.Path:
    return RELEASES_ROOT / f"{validate_release(release_sha)}.json"


def environment_path(release_sha: str) -> pathlib.Path:
    return RELEASES_ROOT / f"{validate_release(release_sha)}.env"


def candidate_create_command(image: str, env_path: pathlib.Path) -> list[str]:
    if (
        not image.startswith(f"{IMAGE_REPOSITORY}@")
        or DIGEST_RE.fullmatch(image.removeprefix(f"{IMAGE_REPOSITORY}@")) is None
    ):
        raise ValueError("candidate image must use the exact service digest")
    if env_path.parent != RELEASES_ROOT or env_path.suffix != ".env":
        raise ValueError("candidate environment path is outside root-owned state")
    return [
        DOCKER,
        "container",
        "create",
        "--name",
        CONTAINER,
        "--restart",
        "unless-stopped",
        "--env-file",
        str(env_path),
        "--publish",
        "127.0.0.1:4000:4000",
        "--network",
        DEFAULT_NETWORK,
        "--network-alias",
        "proxy",
        image,
    ]


def load_release(release_sha: str) -> dict[str, Any]:
    path = release_path(release_sha)
    require_root_file(path)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise DeployError("release state is unreadable") from error
    if not isinstance(value, dict) or value.get("release_sha") != release_sha:
        raise DeployError("release state is invalid")
    return value


@contextlib.contextmanager
def deploy_lock() -> Iterator[None]:
    descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def timer_unit(release_sha: str) -> str:
    return f"arcanada-llm-proxy-rollback@{validate_release(release_sha)}.timer"


def stop_timer(release_sha: str) -> None:
    command([SYSTEMCTL, "stop", timer_unit(release_sha)], check=False)


def preflight() -> None:
    if shutil.disk_usage("/var/lib/docker").free < 2 * 1024**3:
        raise DeployError("less than 2 GB free in Docker storage")
    if not command_ok([SYSTEMCTL, "is-active", "--quiet", "docker.service"]):
        raise DeployError("Docker service is not active")
    if not command_ok([SYSTEMCTL, "is-enabled", "--quiet", ORIGIN_ACL_UNIT]):
        raise DeployError("origin ACL persistent service is not enabled")
    if not command_ok([SYSTEMCTL, "is-active", "--quiet", ORIGIN_ACL_UNIT]):
        raise DeployError("origin ACL persistent service is not active")
    document = inspect_container()
    validate_runtime_topology(document)
    project_runtime_environment(document["Config"]["Env"])
    if not local_health_ok():
        raise DeployError("current application health gate failed")
    print("LLM_PROXY_PREFLIGHT_PASS")


def rollback_locked(release_sha: str, state: dict[str, Any]) -> None:
    backup = state.get("rollback_container")
    if not isinstance(backup, str) or backup != rollback_container_name(release_sha):
        raise DeployError("rollback target is invalid")
    stop_timer(release_sha)
    if container_exists(CONTAINER):
        command([DOCKER, "container", "rm", "--force", CONTAINER])
    if not container_exists(backup):
        raise DeployError("rollback container is unavailable")
    command([DOCKER, "container", "rename", backup, CONTAINER])
    command([DOCKER, "container", "start", CONTAINER])
    if not wait_for_health():
        raise DeployError("rollback container failed its health gate")
    restored = inspect_container()
    validate_runtime_topology(restored)
    state.update(
        {
            "committed": False,
            "rolled_back_at": int(time.time()),
            "status": "rolled_back",
        }
    )
    atomic_json(release_path(release_sha), state)
    print(f"LLM_PROXY_ROLLBACK_PASS release_sha={release_sha}")


def recover_after_deploy_error(release_sha: str, state: dict[str, Any]) -> None:
    backup = state.get("rollback_container")
    if isinstance(backup, str) and container_exists(backup):
        rollback_locked(release_sha, state)
        return
    if not container_exists(CONTAINER):
        raise DeployError("pre-cutover failure left no production container")
    stop_timer(release_sha)
    state.update(
        {
            "committed": False,
            "failed_at": int(time.time()),
            "status": "failed_pre_cutover",
        }
    )
    atomic_json(release_path(release_sha), state)


def deploy(release_sha: str, image_digest: str, ci_run_id: str) -> None:
    release = validate_release(release_sha)
    image = validate_image_digest(image_digest)
    run_id = validate_run_id(ci_run_id)
    with deploy_lock():
        preflight()
        if release_path(release).exists():
            raise DeployError("release state already exists")
        backup = rollback_container_name(release)
        if container_exists(backup):
            raise DeployError("release rollback container already exists")

        current = inspect_container()
        validate_runtime_topology(current)
        runtime_env = project_runtime_environment(current["Config"]["Env"])
        old_image_id = current.get("Image")
        if not isinstance(old_image_id, str) or DIGEST_RE.fullmatch(old_image_id) is None:
            raise DeployError("current image identity is not an exact sha256 digest")

        command([DOCKER, "image", "pull", image], timeout=300)
        validate_image_provenance(inspect_image(image), release)
        env_path = environment_path(release)
        atomic_environment(env_path, runtime_env)
        state: dict[str, Any] = {
            "ci_run_id": run_id,
            "committed": False,
            "deadline": int(time.time()) + WATCHDOG_DEADLINE_SECONDS,
            "image": image,
            "old_image_id": old_image_id,
            "release_sha": release,
            "rollback_container": backup,
            "status": "preparing",
        }
        atomic_json(release_path(release), state)

        try:
            command([SYSTEMCTL, "start", timer_unit(release)])
            command([DOCKER, "container", "rename", CONTAINER, backup])
            command([DOCKER, "container", "stop", "--time", "15", backup])
            command(candidate_create_command(image, env_path))
            command(
                [
                    DOCKER,
                    "network",
                    "connect",
                    "--alias",
                    "proxy",
                    MODEL_CONNECTOR_NETWORK,
                    CONTAINER,
                ]
            )
            command([DOCKER, "container", "start", CONTAINER])
            if not wait_for_health():
                raise DeployError("new container failed its health gate")
            validate_runtime_topology(inspect_container())
            state.update({"deployed_at": int(time.time()), "status": "deployed"})
            atomic_json(release_path(release), state)
        except BaseException:
            recover_after_deploy_error(release, state)
            raise
        print(f"LLM_PROXY_DEPLOY_PASS release_sha={release} image_digest={image_digest} ci_run_id={run_id}")


def rollback(release_sha: str) -> None:
    release = validate_release(release_sha)
    with deploy_lock():
        state = load_release(release)
        if state.get("status") == "rolled_back":
            print(f"LLM_PROXY_ROLLBACK_ALREADY_APPLIED release_sha={release}")
            return
        rollback_locked(release, state)


def health(release_sha: str | None = None) -> None:
    document = inspect_container()
    validate_runtime_topology(document)
    if release_sha is not None:
        release = validate_release(release_sha)
        state = load_release(release)
        if document.get("Config", {}).get("Image") != state.get("image"):
            raise DeployError("running image differs from release state")
    if not local_health_ok():
        raise DeployError("application health gate failed")
    print("LLM_PROXY_HEALTH_PASS")


def commit(release_sha: str) -> None:
    release = validate_release(release_sha)
    with deploy_lock():
        state = load_release(release)
        if state.get("status") not in {"deployed", "committed"}:
            raise DeployError("only a deployed release can be committed")
        health(release)
        state.update(
            {
                "committed": True,
                "committed_at": int(time.time()),
                "status": "committed",
            }
        )
        atomic_json(release_path(release), state)
        stop_timer(release)
    print(f"LLM_PROXY_COMMIT_PASS release_sha={release}")


def watchdog(release_sha: str) -> None:
    release = validate_release(release_sha)
    with deploy_lock():
        state = load_release(release)
        decision = watchdog_decision(
            committed=state.get("committed") is True,
            healthy=local_health_ok(),
            now=int(time.time()),
            deadline=int(state.get("deadline", 0)),
        )
        if decision == "rollback":
            recover_after_deploy_error(release, state)
        elif decision == "committed":
            stop_timer(release)
            print(f"LLM_PROXY_WATCHDOG_COMMITTED release_sha={release}")
        else:
            print(f"LLM_PROXY_WATCHDOG_WAIT release_sha={release}")


def status(release_sha: str) -> None:
    release = validate_release(release_sha)
    state = load_release(release)
    public = {
        key: state.get(key)
        for key in (
            "ci_run_id",
            "committed",
            "committed_at",
            "deadline",
            "deployed_at",
            "image",
            "old_image_id",
            "release_sha",
            "rollback_container",
            "rolled_back_at",
            "status",
        )
    }
    print(json.dumps(public, sort_keys=True))


def usage() -> None:
    print(
        "usage: arcanada-llm-proxy-deploy "
        "{verify-bundle|preflight|deploy|health|commit|rollback|watchdog|status} ...",
        file=sys.stderr,
    )


def main(argv: Sequence[str]) -> int:
    try:
        require_root()
        ensure_layout()
        if not argv:
            usage()
            return 2
        command_name, *args = argv
        if command_name == "verify-bundle" and len(args) == 3:
            verify_bundle(*args)
        elif command_name == "preflight" and not args:
            preflight()
        elif command_name == "deploy" and len(args) == 3:
            deploy(*args)
        elif command_name == "health" and len(args) <= 1:
            health(*args)
        elif command_name == "commit" and len(args) == 1:
            commit(*args)
        elif command_name == "rollback" and len(args) == 1:
            rollback(*args)
        elif command_name == "watchdog" and len(args) == 1:
            watchdog(*args)
        elif command_name == "status" and len(args) == 1:
            status(*args)
        else:
            usage()
            return 2
        return 0
    except (DeployError, ValueError, OSError) as error:
        print(f"arcanada-llm-proxy-deploy: {safe_error(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
