from __future__ import annotations

import contextlib
import importlib.util
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
        "start",
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
