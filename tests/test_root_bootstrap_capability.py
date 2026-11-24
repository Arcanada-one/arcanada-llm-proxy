from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "root-bootstrap-capability.sh"
BOOTSTRAP = ROOT / "deploy" / "root-bootstrap.sh"


def capability_env(tmp_path: pathlib.Path, metadata: str = "0:0:600") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrappers = {
        "install": "#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = -d ]; then shift; continue; fi\n  if [ \"$1\" = -o ] || [ \"$1\" = -g ] || [ \"$1\" = -m ]; then shift 2; continue; fi\n  mkdir -p -- \"$1\"\n  chmod 700 -- \"$1\"\n  exit 0\ndone\nexit 2\n",
        "stat": "#!/bin/sh\nprintf '%s\\n' \"${MOCK_METADATA:?}\"\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "openssl": "#!/bin/sh\nprintf '%s\\n' \"called\" >\"$OPENSSL_MARKER\"\nprintf '%064d\\n' 0\n",
    }
    for name, body in wrappers.items():
        wrapper = bin_dir / name
        wrapper.write_text(body)
        wrapper.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        MOCK_METADATA=metadata,
        OPENSSL_MARKER=str(tmp_path / "openssl-called"),
    )
    return env


def run_capability(state: pathlib.Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(HELPER), str(state)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_root_bootstrap_reuses_only_valid_capability_without_printing_it(tmp_path: pathlib.Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    secret = "a" * 64 + "\n"
    capability = state / "deploy-capability"
    capability.write_text(secret)
    capability.chmod(0o600)

    env = capability_env(tmp_path)
    result = run_capability(state, env)

    assert result.returncode == 0, result.stderr
    assert "ready (value withheld)" in result.stdout
    assert secret not in result.stdout + result.stderr
    assert capability.read_text() == secret
    assert not pathlib.Path(env["OPENSSL_MARKER"]).exists()


@pytest.mark.parametrize(
    ("state_kind", "content", "metadata"),
    (
        ("file", "a" * 64 + "\n", "1000:1000:600"),
        ("file", "a" * 64 + "\n", "0:0:644"),
        ("file", "a" * 63 + "\n", "0:0:600"),
        ("file", "z" * 64 + "\n", "0:0:600"),
        ("directory", "", "0:0:600"),
        ("symlink", "", "0:0:600"),
    ),
)
def test_root_bootstrap_refuses_unsafe_existing_capability(
    tmp_path: pathlib.Path, state_kind: str, content: str, metadata: str
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    capability = state / "deploy-capability"
    if state_kind == "file":
        capability.write_text(content)
        capability.chmod(0o600)
    elif state_kind == "directory":
        capability.mkdir()
    else:
        capability.symlink_to(state / "missing")

    env = capability_env(tmp_path, metadata)
    result = run_capability(state, env)

    assert result.returncode != 0
    assert "unsafe type, owner, mode, size, or format" in result.stderr
    assert not pathlib.Path(env["OPENSSL_MARKER"]).exists()


def test_root_bootstrap_creates_capability_atomically_and_quietly(tmp_path: pathlib.Path) -> None:
    state = tmp_path / "state"
    env = capability_env(tmp_path)

    result = run_capability(state, env)

    capability = state / "deploy-capability"
    assert result.returncode == 0, result.stderr
    assert capability.read_bytes() == b"0" * 64 + b"\n"
    assert capability.stat().st_mode & 0o777 == 0o600
    assert "0" * 64 not in result.stdout + result.stderr
    assert not list(state.glob(".deploy-capability.*"))


def test_root_bootstrap_runs_the_reviewed_repository_helper() -> None:
    source = BOOTSTRAP.read_text()
    assert '"$repo_root/deploy/root-bootstrap-capability.sh"' in source
    assert "test ! -e \"$capability_file\"" not in source
    assert "gh secret set LLM_PROXY_DEPLOY_CAPABILITY" in source
    assert 'ssh -T "$ssh_target" "cat --' in source
