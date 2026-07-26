from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
INSTALLER = ROOT / "deploy" / "install-llm-proxy-deploy.sh"
SERVICE = ROOT / "deploy" / "arcanada-llm-proxy-rollback@.service"
TIMER = ROOT / "deploy" / "arcanada-llm-proxy-rollback@.timer"
RUNBOOK = ROOT / "docs" / "how-to" / "deploy-to-arcana-prod.md"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "explanation" / "architecture.md"


def action_refs(text: str) -> list[str]:
    return re.findall(r"^\s*uses:\s*([^#\s]+)", text, flags=re.MULTILINE)


def test_main_ci_builds_tests_and_publishes_off_prod() -> None:
    text = CI.read_text()

    assert "[self-hosted, linux, arcana-ai, docker, ci-general]" in text
    assert "ghcr.io/arcanada-one/arcanada-llm-proxy:${{ github.sha }}" in text
    assert "org.opencontainers.image.revision=${{ github.sha }}" in text
    assert "io.arcanada.release_sha=${{ github.sha }}" in text
    assert "provenance: true" in text
    assert "sbom: true" in text
    assert "published-image.json" in text
    assert "verify-published-image:" in text
    assert "deploy:" not in text
    assert "arcana-prod" not in text
    assert "ubuntu-latest" not in text


def test_production_cutover_is_manual_gated_and_dockerless() -> None:
    text = DEPLOY.read_text()

    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "workflow_run:" not in text
    assert "environment: production" in text
    assert "runs-on: [self-hosted, linux, arcana-prod-ci]" in text
    assert "release_sha:" in text
    assert "image_digest:" in text
    assert "ci_run_id:" in text
    assert "await-authorization" not in text
    assert "sudo -n /usr/local/sbin/arcanada-llm-proxy-deploy deploy" in text
    assert "sudo -n /usr/local/sbin/arcanada-llm-proxy-deploy commit" in text
    assert "DEPLOY_CAPABILITY: ${{ secrets.LLM_PROXY_DEPLOY_CAPABILITY }}" in text
    assert text.count("secrets.LLM_PROXY_DEPLOY_CAPABILITY") == 2
    assert "printf '%s\\n' \"${DEPLOY_CAPABILITY}\" |" in text
    assert "secrets.LLM_PROXY_DEPLOY_CAPABILITY" not in text.split("  cutover:", 1)[0]
    for command in ("deploy", "commit", "rollback"):
        assert re.search(
            rf'printf \'%s\\n\' "\$\{{DEPLOY_CAPABILITY\}}" \|\s+'
            rf"sudo -n /usr/local/sbin/arcanada-llm-proxy-deploy (?:\\\s+)?{command}",
            text,
        )
    assert "--capability" not in text

    forbidden = (
        "docker ",
        "/run/docker.sock",
        "/opt/arcanada-llm-proxy",
        ".env",
        "git reset",
        "git pull",
    )
    for token in forbidden:
        assert token not in text


def test_all_touched_workflow_actions_are_commit_pinned() -> None:
    for path in (CI, DEPLOY):
        refs = action_refs(path.read_text())
        assert refs
        for ref in refs:
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref), (path, ref)


def test_installer_enforces_dockerless_prod_ci_runner_and_narrow_sudo() -> None:
    text = INSTALLER.read_text()

    assert "ci-runner" in text
    assert "ci-runner-ci" in text
    assert "runner_users=(ci-runner ci-runner-ci)" in text
    assert 'gpasswd --delete "$runner_user" docker' in text
    assert "still belongs to docker group" in text
    assert "can still access Docker socket" in text
    assert "service retained Docker group" in text
    assert "NOPASSWD:NOSETENV:" in text
    assert "/usr/local/sbin/arcanada-llm-proxy-deploy" in text
    assert "deploy-capability.sha256" in text
    assert "chmod 0600" in text
    assert "capability-sha256" in text
    assert "printf '%s\\n' \"$4\"" in text
    assert "ALL=(ALL) NOPASSWD: ALL" not in text


def test_watchdog_is_root_owned_hardened_and_recurring() -> None:
    service = SERVICE.read_text()
    timer = TIMER.read_text()

    assert "User=root" in service
    assert "ExecStart=/usr/local/sbin/arcanada-llm-proxy-deploy watchdog %i" in service
    assert "ProtectSystem=strict" in service
    assert "ReadWritePaths=/var/lib/arcanada-llm-proxy-deploy /run/docker.sock" in service
    assert "OnUnitActiveSec=10s" in timer
    assert "arcanada-llm-proxy-rollback@%i.service" in timer
    assert '"enable", "--now"' in (ROOT / "deploy" / "llm_proxy_deploy.py").read_text()
    assert '"disable", "--now"' in (ROOT / "deploy" / "llm_proxy_deploy.py").read_text()


def test_runbook_exposes_only_the_reviewed_operator_flow() -> None:
    text = RUNBOOK.read_text()

    assert "install-llm-proxy-deploy.sh" in text
    assert "workflow_dispatch" in text
    assert "release_sha" in text
    assert "image_digest" in text
    assert "ci_run_id" in text
    assert "root-only" in text
    assert "LLM_PROXY_DEPLOY_CAPABILITY" in text
    assert "stdin" in text
    assert re.search(r"both `ci-runner` and\s+`ci-runner-ci`", text)
    assert "docker compose up" not in text
    assert "git reset --hard" not in text


def test_dependency_docs_match_the_audited_starlette_pin() -> None:
    for path in (README, ARCHITECTURE):
        text = path.read_text()
        assert "Starlette 1.3.1" in text
        assert "Starlette 1.0.0" not in text
