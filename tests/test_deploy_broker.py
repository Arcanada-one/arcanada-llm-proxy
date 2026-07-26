from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "llm_proxy_deploy.py"


def load_broker():
    spec = importlib.util.spec_from_file_location("llm_proxy_deploy", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def healthy_runtime() -> dict:
    return {
        "Config": {
            "Env": [
                "LLM_PROXY_SHARED_SECRET=secret",
                "MC_BASE_URL=http://model-connector-model-connector-1:3900",
                "MC_API_KEY=secret",
                "LANGFUSE_OTLP_ENDPOINT=",
                "LANGFUSE_OTLP_HEADERS=",
                "PATH=/usr/local/bin:/usr/bin:/bin",
            ],
        },
        "HostConfig": {
            "Privileged": False,
            "NetworkMode": "code_default",
            "ReadonlyRootfs": False,
            "RestartPolicy": {"Name": "unless-stopped"},
            "PortBindings": {
                "4000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4000"}],
            },
        },
        "Mounts": [],
        "NetworkSettings": {
            "Networks": {
                "code_default": {},
                "model-connector_default": {},
            },
        },
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Image": "sha256:" + "1" * 64,
    }


def test_exact_release_and_digest_validation() -> None:
    broker = load_broker()

    assert broker.validate_release("a" * 40) == "a" * 40
    assert (
        broker.validate_image_digest("sha256:" + "b" * 64)
        == "ghcr.io/arcanada-one/arcanada-llm-proxy@sha256:" + "b" * 64
    )
    with pytest.raises(ValueError):
        broker.validate_release("main")
    with pytest.raises(ValueError):
        broker.validate_image_digest("latest")
    with pytest.raises(ValueError):
        broker.validate_image_digest("sha256:" + "B" * 64)


def test_deploy_capability_is_exact_stdin_value_and_constant_time_hash() -> None:
    broker = load_broker()
    capability = "a" * 64
    expected_hash = broker.hashlib.sha256(capability.encode()).hexdigest()

    broker.verify_deploy_capability(io.StringIO(f"{capability}\n"), expected_hash)

    for supplied in ("", "b" * 64 + "\n", capability, capability.upper() + "\n"):
        with pytest.raises(broker.DeployError, match="deploy capability rejected") as error:
            broker.verify_deploy_capability(io.StringIO(supplied), expected_hash)
        assert capability not in str(error.value)
        if supplied.strip():
            assert supplied.strip() not in str(error.value)


@pytest.mark.parametrize(
    ("command_name", "arguments"),
    [
        ("deploy", ["a" * 40, "sha256:" + "b" * 64, "123"]),
        ("commit", ["a" * 40]),
        ("rollback", ["a" * 40]),
    ],
)
def test_every_runner_invokable_mutator_requires_capability_before_action(
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    arguments: list[str],
) -> None:
    broker = load_broker()
    checked: list[str] = []

    def reject_capability() -> None:
        checked.append("capability")
        raise broker.DeployError("expected capability stop")

    monkeypatch.setattr(broker, "require_deploy_capability", reject_capability)
    monkeypatch.setattr(
        broker,
        "deploy_lock",
        lambda: pytest.fail("mutation started before capability validation"),
    )

    with pytest.raises(broker.DeployError, match="expected capability stop"):
        getattr(broker, command_name)(*arguments)

    assert checked == ["capability"]


def test_runtime_topology_is_fail_closed() -> None:
    broker = load_broker()

    broker.validate_runtime_topology(healthy_runtime())

    for mutate in (
        lambda value: value["HostConfig"].update(Privileged=True),
        lambda value: value["HostConfig"]["PortBindings"]["4000/tcp"][0].update(HostIp="0.0.0.0"),
        lambda value: value["NetworkSettings"]["Networks"].pop("model-connector_default"),
        lambda value: value["Mounts"].append(
            {"Source": "/run/docker.sock", "Destination": "/run/docker.sock"}
        ),
    ):
        document = healthy_runtime()
        mutate(document)
        with pytest.raises(broker.DeployError):
            broker.validate_runtime_topology(document)


def test_environment_projection_rejects_duplicate_or_unknown_names() -> None:
    broker = load_broker()

    projected = broker.project_runtime_environment(healthy_runtime()["Config"]["Env"])
    assert projected["MC_BASE_URL"].startswith("http://model-connector-")
    assert set(projected) == {
        "LANGFUSE_OTLP_ENDPOINT",
        "LANGFUSE_OTLP_HEADERS",
        "LLM_PROXY_FORCE_ROUTE",
        "LLM_PROXY_SHARED_SECRET",
        "MC_API_KEY",
        "MC_BASE_URL",
    }

    with pytest.raises(ValueError):
        broker.project_runtime_environment(["MC_API_KEY=a", "MC_API_KEY=b"])
    with pytest.raises(broker.DeployError):
        broker.project_runtime_environment(healthy_runtime()["Config"]["Env"] + ["UNREVIEWED_SECRET=value"])


def test_watchdog_decision_and_rollback_name_are_deterministic() -> None:
    broker = load_broker()

    assert broker.watchdog_decision(committed=False, healthy=True, now=99, deadline=100) == "wait"
    assert broker.watchdog_decision(committed=False, healthy=False, now=99, deadline=100) == "rollback"
    assert broker.watchdog_decision(committed=False, healthy=True, now=100, deadline=100) == "rollback"
    assert broker.watchdog_decision(committed=True, healthy=False, now=101, deadline=100) == "committed"
    assert (
        broker.rollback_container_name("0123456789abcdef" + "0" * 24)
        == "arcanada-llm-proxy-rollback-0123456789ab"
    )


def test_image_provenance_requires_both_release_labels() -> None:
    broker = load_broker()
    release = "c" * 40

    broker.validate_image_provenance(
        {
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": release,
                    "io.arcanada.release_sha": release,
                }
            }
        },
        release,
    )
    with pytest.raises(broker.DeployError):
        broker.validate_image_provenance(
            {
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": release,
                    }
                }
            },
            release,
        )


def test_failed_pre_cutover_never_removes_the_live_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "d" * 40
    state = {
        "release_sha": release,
        "rollback_container": broker.rollback_container_name(release),
        "status": "preparing",
    }
    writes: list[dict] = []
    stopped_timers: list[str] = []

    monkeypatch.setattr(broker, "container_exists", lambda name: name == broker.CONTAINER)
    monkeypatch.setattr(broker, "stop_timer", stopped_timers.append)
    monkeypatch.setattr(
        broker,
        "atomic_json",
        lambda _path, value: writes.append(dict(value)),
    )
    monkeypatch.setattr(
        broker,
        "rollback_locked",
        lambda *_args: pytest.fail("rollback must not run before rename succeeds"),
    )

    broker.recover_after_deploy_error(release, state)

    assert writes[-1]["status"] == "failed_pre_cutover"
    assert stopped_timers == [release]


def test_failed_post_rename_uses_the_retained_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "e" * 40
    backup = broker.rollback_container_name(release)
    state = {
        "release_sha": release,
        "rollback_container": backup,
        "status": "preparing",
    }
    calls: list[str] = []

    monkeypatch.setattr(broker, "container_exists", lambda name: name == backup)
    monkeypatch.setattr(
        broker,
        "rollback_locked",
        lambda _release, _state: calls.append("rollback"),
    )

    broker.recover_after_deploy_error(release, state)

    assert calls == ["rollback"]


def test_rollback_never_deletes_candidate_without_verified_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "f" * 40
    state = {
        "old_image_id": "sha256:" + "1" * 64,
        "release_sha": release,
        "rollback_container": broker.rollback_container_name(release),
        "status": "deployed",
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(broker, "container_exists", lambda name: name == broker.CONTAINER)
    monkeypatch.setattr(broker, "command", lambda args, **_kwargs: commands.append(list(args)))

    with pytest.raises(broker.DeployError, match="rollback container is unavailable"):
        broker.rollback_locked(release, state)

    assert commands == []


def test_rollback_rejects_backup_image_mismatch_before_candidate_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "f" * 40
    backup = broker.rollback_container_name(release)
    state = {
        "old_image_id": "sha256:" + "1" * 64,
        "release_sha": release,
        "rollback_container": backup,
        "status": "deployed",
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(broker, "container_exists", lambda _name: True)
    monkeypatch.setattr(
        broker,
        "inspect_container",
        lambda _name=broker.CONTAINER: {"Image": "sha256:" + "2" * 64},
    )
    monkeypatch.setattr(broker, "command", lambda args, **_kwargs: commands.append(list(args)))
    monkeypatch.setattr(broker, "wait_for_health", lambda: True)

    with pytest.raises(broker.DeployError, match="rollback image identity"):
        broker.rollback_locked(release, state)

    assert commands == []


def test_rollback_timer_stops_only_after_old_image_is_healthy_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "f" * 40
    backup = broker.rollback_container_name(release)
    old_image = "sha256:" + "1" * 64
    state = {
        "old_image_id": old_image,
        "release_sha": release,
        "rollback_container": backup,
        "status": "deployed",
    }
    events: list[str] = []

    monkeypatch.setattr(broker, "container_exists", lambda _name: True)

    def inspect(name: str = broker.CONTAINER) -> dict:
        if name == backup:
            return {"Image": old_image}
        restored = healthy_runtime()
        restored["Image"] = old_image
        return restored

    monkeypatch.setattr(broker, "inspect_container", inspect)
    monkeypatch.setattr(
        broker,
        "command",
        lambda args, **_kwargs: events.append(f"command:{' '.join(args)}"),
    )
    monkeypatch.setattr(
        broker,
        "wait_for_health",
        lambda: events.append("healthy") or True,
    )
    monkeypatch.setattr(
        broker,
        "atomic_json",
        lambda *_args: events.append(f"state:{state['status']}"),
    )
    monkeypatch.setattr(
        broker,
        "stop_timer",
        lambda _release: events.append("timer-stopped"),
    )

    broker.rollback_locked(release, state)

    assert state["status"] == "rolled_back"
    assert events.index("healthy") < events.index("state:rolled_back")
    assert events.index("state:rolled_back") < events.index("timer-stopped")


def test_failed_restore_keeps_watchdog_enabled_and_next_tick_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "f" * 40
    backup = broker.rollback_container_name(release)
    old_image = "sha256:" + "1" * 64
    state = {
        "old_image_id": old_image,
        "release_sha": release,
        "rollback_container": backup,
        "status": "deployed",
    }
    first_attempt = True
    timer_stops: list[str] = []

    def exists(name: str) -> bool:
        return name == broker.CONTAINER or (first_attempt and name == backup)

    def inspect(name: str = broker.CONTAINER) -> dict:
        if name == backup:
            return {"Image": old_image}
        restored = healthy_runtime()
        restored["Image"] = old_image
        return restored

    monkeypatch.setattr(broker, "container_exists", exists)
    monkeypatch.setattr(broker, "inspect_container", inspect)
    monkeypatch.setattr(broker, "command", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(broker, "atomic_json", lambda *_args: None)
    monkeypatch.setattr(broker, "stop_timer", timer_stops.append)
    monkeypatch.setattr(broker, "wait_for_health", lambda: not first_attempt)

    with pytest.raises(broker.DeployError, match="failed its health gate"):
        broker.rollback_locked(release, state)

    assert state["status"] == "restoring"
    assert timer_stops == []

    first_attempt = False
    broker.rollback_locked(release, state)

    assert state["status"] == "rolled_back"
    assert timer_stops == [release]


def test_watchdog_recovers_safely_when_cutover_never_renamed_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = load_broker()
    release = "a" * 40
    state = {
        "committed": False,
        "deadline": 1,
        "release_sha": release,
        "rollback_container": broker.rollback_container_name(release),
        "status": "preparing",
    }
    recovered: list[str] = []

    monkeypatch.setattr(broker, "deploy_lock", contextlib.nullcontext)
    monkeypatch.setattr(broker, "load_release", lambda _release: state)
    monkeypatch.setattr(broker, "local_health_ok", lambda: True)
    monkeypatch.setattr(broker.time, "time", lambda: 2)
    monkeypatch.setattr(
        broker,
        "recover_after_deploy_error",
        lambda requested, _state: recovered.append(requested),
    )
    monkeypatch.setattr(
        broker,
        "rollback_locked",
        lambda *_args: pytest.fail("watchdog must use phase-aware recovery"),
    )

    broker.watchdog(release)

    assert recovered == [release]


def test_deploy_arms_watchdog_before_destructive_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    broker = load_broker()
    release = "b" * 40
    digest = "sha256:" + "c" * 64
    calls: list[list[str]] = []

    monkeypatch.setattr(broker, "deploy_lock", contextlib.nullcontext)
    monkeypatch.setattr(broker, "require_deploy_capability", lambda: None)
    monkeypatch.setattr(broker, "preflight", lambda: None)
    monkeypatch.setattr(broker, "release_path", lambda _release: tmp_path / "release.json")
    monkeypatch.setattr(broker, "environment_path", lambda _release: tmp_path / "release.env")
    monkeypatch.setattr(broker, "container_exists", lambda _name: False)
    monkeypatch.setattr(broker, "inspect_container", healthy_runtime)
    monkeypatch.setattr(
        broker,
        "inspect_image",
        lambda _image: {
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": release,
                    "io.arcanada.release_sha": release,
                }
            }
        },
    )
    monkeypatch.setattr(broker, "atomic_environment", lambda *_args: None)
    monkeypatch.setattr(broker, "atomic_json", lambda *_args: None)
    monkeypatch.setattr(
        broker,
        "candidate_create_command",
        lambda *_args: ["/usr/bin/docker", "container", "create", "candidate"],
    )
    monkeypatch.setattr(broker, "wait_for_health", lambda: True)
    monkeypatch.setattr(broker, "command", lambda args, **_kwargs: calls.append(list(args)))

    broker.deploy(release, digest, "123")

    watchdog_start = [
        broker.SYSTEMCTL,
        "enable",
        "--now",
        broker.timer_unit(release),
    ]
    destructive_rename = [
        broker.DOCKER,
        "container",
        "rename",
        broker.CONTAINER,
        broker.rollback_container_name(release),
    ]
    assert calls.index(watchdog_start) < calls.index(destructive_rename)


def test_candidate_command_has_only_reviewed_runtime_surfaces() -> None:
    broker = load_broker()
    image = "ghcr.io/arcanada-one/arcanada-llm-proxy@sha256:" + "f" * 64
    env_path = pathlib.Path("/var/lib/arcanada-llm-proxy-deploy/releases/test.env")

    command = broker.candidate_create_command(image, env_path)

    assert command[:4] == [
        "/usr/bin/docker",
        "container",
        "create",
        "--name",
    ]
    assert command[-1] == image
    assert ["--publish", "127.0.0.1:4000:4000"] == command[
        command.index("--publish") : command.index("--publish") + 2
    ]
    assert ["--network", "code_default"] == command[
        command.index("--network") : command.index("--network") + 2
    ]
    assert "--volume" not in command
    assert "/run/docker.sock" not in command
