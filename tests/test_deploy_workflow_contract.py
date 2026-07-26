from __future__ import annotations

import os
import pathlib
import re
import subprocess

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


def installer_function(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}$",
        INSTALLER.read_text(),
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def run_legacy_sudoers_preflight(path: pathlib.Path) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f"{installer_function('require_legacy_sudoers_absent')}\n"
        'require_legacy_sudoers_absent "$1"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "bash", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def run_installer_function(
    name: str,
    *args: str,
    preamble: str = "",
    function_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    function_text = function_text or installer_function(name)
    script = (
        "set -uo pipefail\n"
        f"{function_text}\n"
        f"{preamble}\n"
        f'{name} "$@"\n'
    )
    environment = os.environ.copy()
    environment.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def run_retained_docker_gid_check(
    status_path: pathlib.Path,
    docker_gid: int,
    *,
    function_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_installer_function(
        "process_has_group",
        str(status_path),
        str(docker_gid),
        function_text=function_text,
    )


def run_main_pid_check(
    main_pid: str, *, systemctl_status: int = 0
) -> subprocess.CompletedProcess[str]:
    return run_installer_function(
        "require_runner_main_pid",
        "actions.runner.Arcanada-one.arcana-prod-ci.service",
        preamble="""systemctl() {
  printf '%s\n' "$MOCK_MAIN_PID"
  return "$MOCK_SYSTEMCTL_STATUS"
}""",
        extra_env={
            "MOCK_MAIN_PID": main_pid,
            "MOCK_SYSTEMCTL_STATUS": str(systemctl_status),
        },
    )


def run_account_dockerless_check(
    groups: str,
    *,
    socket_accessible: bool,
    id_status: int = 0,
    function_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_installer_function(
        "require_runner_account_dockerless",
        "ci-runner-ci",
        function_text=function_text,
        preamble="""id() {
  if [ "${1:-}" = "-nG" ]; then
    printf '%s\n' "$MOCK_GROUPS"
    return "$MOCK_ID_STATUS"
  else
    return 0
  fi
}
gpasswd() { return 0; }
runuser() { return "$MOCK_SOCKET_STATUS"; }""",
        extra_env={
            "MOCK_GROUPS": groups,
            "MOCK_ID_STATUS": str(id_status),
            "MOCK_SOCKET_STATUS": "0" if socket_accessible else "1",
        },
    )


def write_process_status(
    proc_root: pathlib.Path, pid: str, groups: str
) -> pathlib.Path:
    process_root = proc_root / pid
    process_root.mkdir(parents=True)
    status = process_root / "status"
    status.write_text(f"Name:\trunner\nGroups:\t{groups}\n")
    return status


def write_cgroup_processes(
    cgroup_root: pathlib.Path,
    control_group: str,
    pids: str,
    *,
    child: str | None = None,
) -> pathlib.Path:
    cgroup = cgroup_root / control_group.removeprefix("/")
    if child is not None:
        cgroup /= child
    cgroup.mkdir(parents=True, exist_ok=True)
    procs = cgroup / "cgroup.procs"
    procs.write_text(pids)
    return procs


def run_runner_process_gate(
    proc_root: pathlib.Path,
    docker_gid: str,
    *,
    main_pid: str,
    pids: str | None,
    cgroup_root: pathlib.Path | None = None,
    control_group: str = (
        "/system.slice/actions.runner.Arcanada-one.arcana-prod-ci.service"
    ),
    control_group_status: int = 0,
    pgrep_status: int = 0,
    pgrep_pids: str | None = None,
    parser_text: str | None = None,
    gate_text: str | None = None,
    collector_text: str | None = None,
    capture_text: str | None = None,
    snapshots: tuple[str, str] | None = None,
    second_snapshot_status: int = 0,
) -> subprocess.CompletedProcess[str]:
    cgroup_root = cgroup_root or proc_root.parent / "cgroup"
    cgroup_root.mkdir(parents=True, exist_ok=True)
    if pids is not None:
        write_cgroup_processes(cgroup_root, control_group, pids)
    snapshot_counter = proc_root.parent / "snapshot-counter"
    snapshot_counter.write_text("0")
    if snapshots is not None:
        capture_text = """capture_runner_cgroup_snapshot() {
  local count
  count="$(cat "$MOCK_SNAPSHOT_COUNTER")"
  if [ "$count" = 0 ]; then
    printf '1\n' >"$MOCK_SNAPSHOT_COUNTER"
    printf '%s\n' "$MOCK_FIRST_SNAPSHOT"
    return 0
  fi
  printf '%s\n' "$MOCK_SECOND_SNAPSHOT"
  return "$MOCK_SECOND_SNAPSHOT_STATUS"
}"""
    functions = "\n".join(
        (
            parser_text or installer_function("process_has_group"),
            installer_function("require_runner_account_dockerless"),
            installer_function("require_runner_main_pid"),
            installer_function("require_runner_control_group"),
            collector_text
            or installer_function("collect_runner_cgroup_snapshot_records"),
            capture_text or installer_function("capture_runner_cgroup_snapshot"),
            gate_text or installer_function("require_runner_processes_dockerless"),
        )
    )
    return run_installer_function(
        "require_runner_processes_dockerless",
        docker_gid,
        str(proc_root),
        str(cgroup_root),
        function_text=functions,
        preamble="""id() {
  if [ "${1:-}" = "-nG" ]; then
    printf '%s\n' ci-runner-ci
    return 0
  fi
  [ "${1:-}" = ci-runner-ci ]
}
gpasswd() { return 0; }
runuser() { return 1; }
systemctl() {
  case "${3:-}" in
    MainPID) printf '%s\n' "$MOCK_MAIN_PID" ;;
    ControlGroup)
      printf '%s\n' "$MOCK_CONTROL_GROUP"
      return "$MOCK_CONTROL_GROUP_STATUS"
      ;;
    *) return 1 ;;
  esac
}
pgrep() {
  printf '%s' "$MOCK_PGREP_PIDS"
  return "$MOCK_PGREP_STATUS"
}""",
        extra_env={
            "MOCK_MAIN_PID": main_pid,
            "MOCK_CONTROL_GROUP": control_group,
            "MOCK_CONTROL_GROUP_STATUS": str(control_group_status),
            "MOCK_PGREP_PIDS": pgrep_pids if pgrep_pids is not None else (pids or ""),
            "MOCK_PGREP_STATUS": str(pgrep_status),
            "MOCK_SNAPSHOT_COUNTER": str(snapshot_counter),
            "MOCK_FIRST_SNAPSHOT": snapshots[0] if snapshots is not None else "",
            "MOCK_SECOND_SNAPSHOT": snapshots[1] if snapshots is not None else "",
            "MOCK_SECOND_SNAPSHOT_STATUS": str(second_snapshot_status),
        },
    )


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
    assert "'ci-runner:actions.runner.Arcanada-one.arcana-prod.service'" in text
    assert (
        "'ci-runner-ci:actions.runner.Arcanada-one.arcana-prod-ci.service'" in text
    )
    assert 'gpasswd --delete "$runner_user" docker' in text
    assert "legacy_deploy_root=/opt/arcanada-llm-proxy" in text
    assert 'legacy_deploy_tree="$legacy_deploy_root/code"' in text
    assert 'legacy_deploy_env="$legacy_deploy_tree/.env"' in text
    assert 'chmod 0700 "$protected_path"' in text
    assert 'chmod 0600 "$legacy_deploy_env"' in text
    assert 'runuser -u "$runner_user" -- test -r "$protected_path"' in text
    assert 'runuser -u "$runner_user" -- test -w "$protected_path"' in text
    assert "legacy_sudoers=/etc/sudoers.d/10-hermes-orch" in text
    assert 'require_legacy_sudoers_absent "$legacy_sudoers"' in text
    assert 'sudo -n -l -U "$runner_user"' in text
    assert "legacy broad sudo remains active" in text
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
    assert "ci-runner ALL=(root) NOPASSWD:NOSETENV:" not in text
    assert "ci-runner-ci ALL=(root) NOPASSWD:NOSETENV:" in text


def test_installer_host_awk_detects_retained_docker_gid(
    tmp_path: pathlib.Path,
) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\trunner\nGroups:\t1000 988 1001\n")

    result = run_retained_docker_gid_check(status, 988)

    assert result.returncode == 0
    assert result.stderr == ""


def test_installer_host_awk_accepts_absent_docker_gid(
    tmp_path: pathlib.Path,
) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\trunner\nGroups:\t1000 1001\n")

    result = run_retained_docker_gid_check(status, 988)

    assert result.returncode == 1
    assert result.stderr == ""


def test_installer_host_awk_matches_exact_numeric_group_token(
    tmp_path: pathlib.Path,
) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\trunner\nGroups:\t1000 988 1001\n")

    result = run_retained_docker_gid_check(status, 98)

    assert result.returncode == 1
    assert result.stderr == ""


def test_installer_host_awk_fails_closed_on_bad_status_documents(
    tmp_path: pathlib.Path,
) -> None:
    malformed_documents = (
        "",
        "Name:\trunner\n",
        "Name:\trunner\nGroups:\n",
        "Name:\trunner\nGroups:\t1000 invalid\n",
        "Name:\trunner\nGroups:\t1000\nGroups:\t1001\n",
        "Name:\trunner\nGroups:1000 1001\n",
    )
    for position, document in enumerate(malformed_documents):
        status = tmp_path / f"status-{position}"
        status.write_text(document)
        result = run_retained_docker_gid_check(status, 988)
        assert result.returncode == 2, (document, result)

    missing = run_retained_docker_gid_check(tmp_path / "vanished-status", 988)
    assert missing.returncode == 2
    unreadable = tmp_path / "unreadable-status"
    unreadable.mkdir()
    assert run_retained_docker_gid_check(unreadable, 988).returncode == 2


def test_installer_host_awk_contract_kills_known_silent_bypass_mutants(
    tmp_path: pathlib.Path,
) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\trunner\nGroups:\t1000 988 1001\n")
    malformed = tmp_path / "malformed"
    malformed.write_text("Name:\trunner\n")
    source = installer_function("process_has_group")
    mutants = (
        source.replace("field = 2", "index = 2").replace(
            "field <= NF; field += 1", "index <= NF; index += 1"
        ).replace("$field", "$index"),
        source.replace('("x" $field) == ("x" gid)', "index($field, gid) == 1"),
        source.replace("invalid || groups_seen != 1", "NR == 0"),
    )

    expected = ((status, 988, 0), (status, 98, 1), (malformed, 988, 2))
    for mutant in mutants:
        assert mutant != source
        outcomes = tuple(
            run_retained_docker_gid_check(
                path, gid, function_text=mutant
            ).returncode
            for path, gid, _ in expected
        )
        assert outcomes != tuple(code for _, _, code in expected)


def test_installer_main_pid_gate_accepts_only_live_positive_decimal_pid() -> None:
    live = run_main_pid_check(str(os.getpid()))
    assert live.returncode == 0
    assert live.stdout.strip() == str(os.getpid())

    for invalid in ("", "0", "-1", "12x", "12\n13", "999999999"):
        result = run_main_pid_check(invalid)
        assert result.returncode == 1, (invalid, result)

    systemctl_failure = run_main_pid_check(str(os.getpid()), systemctl_status=1)
    assert systemctl_failure.returncode == 1


def test_installer_account_gate_rejects_primary_docker_group_and_socket_access() -> None:
    primary_group = run_account_dockerless_check(
        "docker ci-runner-ci", socket_accessible=False
    )
    assert primary_group.returncode == 1
    assert "still belongs to docker group" in primary_group.stderr

    socket_access = run_account_dockerless_check(
        "ci-runner-ci", socket_accessible=True
    )
    assert socket_access.returncode == 1
    assert "can still access Docker socket" in socket_access.stderr

    clean = run_account_dockerless_check("ci-runner-ci", socket_accessible=False)
    assert clean.returncode == 0
    identity_error = run_account_dockerless_check(
        "ci-runner-ci", socket_accessible=False, id_status=1
    )
    assert identity_error.returncode == 1
    assert "group lookup failed" in identity_error.stderr


def test_installer_account_gate_kills_primary_group_and_socket_bypass_mutants() -> None:
    source = installer_function("require_runner_account_dockerless")
    group_check = """  if printf '%s\\n' "$runner_groups" | tr ' ' '\\n' | grep -Fxq docker; then
    printf 'install-llm-proxy-deploy: %s still belongs to docker group\\n' \\
      "$runner_user" >&2
    return 1
  fi
"""
    socket_check = """  if runuser -u "$runner_user" -- test -r /run/docker.sock ||
    runuser -u "$runner_user" -- test -w /run/docker.sock; then
    printf 'install-llm-proxy-deploy: %s can still access Docker socket\\n' \\
      "$runner_user" >&2
    return 1
  fi
"""
    group_mutant = source.replace(group_check, "")
    socket_mutant = source.replace(socket_check, "")
    assert group_mutant != source
    assert socket_mutant != source

    assert (
        run_account_dockerless_check(
            "docker ci-runner-ci",
            socket_accessible=False,
            function_text=group_mutant,
        ).returncode
        == 0
    )
    assert (
        run_account_dockerless_check(
            "ci-runner-ci",
            socket_accessible=True,
            function_text=socket_mutant,
        ).returncode
        == 0
    )


def test_installer_process_gate_has_no_conditional_silent_skip() -> None:
    text = INSTALLER.read_text()

    assert '[[ ! "$docker_gid" =~ ^[1-9][0-9]*$ ]]' in text
    assert (
        'runner_pid="$(require_runner_main_pid "$runner_unit" "$proc_root")"'
        in text
    )
    assert (
        'control_group="$(require_runner_control_group "$runner_unit")"' in text
    )
    assert (
        'first_snapshot="$(capture_runner_cgroup_snapshot "$cgroup_path")"'
        in text
    )
    assert (
        'second_snapshot="$(capture_runner_cgroup_snapshot "$cgroup_path")"'
        in text
    )
    assert 'if [ "$first_snapshot" != "$second_snapshot" ]; then' in text
    assert "pgrep" not in text
    assert (
        'require_runner_processes_dockerless "$docker_gid" /proc /sys/fs/cgroup'
        in text
    )
    assert (
        'process_has_group "$proc_root/$runner_pid/status" "$docker_gid"' in text
    )
    assert re.search(r'group_check_status="\$\?"', text)
    assert 'if [ "$group_check_status" -ne 1 ]; then' in text
    assert re.search(r"\]\]\s*&&\s*awk", text) is None
    parser = installer_function("process_has_group")
    assert parser.count("exit ") == 2
    end = parser.index("END {")
    assert "exit " not in parser[:end]


def test_installer_runner_process_gate_checks_main_pid_and_every_child(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    control_group = (
        "/system.slice/actions.runner.Arcanada-one.arcana-prod-ci.service"
    )
    write_process_status(proc_root, "2001", "1000 1001")
    write_process_status(proc_root, "2002", "1000 1001")
    write_cgroup_processes(cgroup_root, control_group, "2002\n", child="worker")
    clean = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids="2001\n",
        cgroup_root=cgroup_root,
    )
    assert clean.returncode == 0, clean

    (proc_root / "2002" / "status").write_text(
        "Name:\trunner\nGroups:\t1000 988 1001\n"
    )
    retained_child = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids="2001\n",
        cgroup_root=cgroup_root,
    )
    assert retained_child.returncode == 1
    assert "service retained Docker group" in retained_child.stderr


def test_installer_runner_process_gate_ignores_same_uid_process_outside_unit(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 1001")
    write_process_status(proc_root, "2999", "1000 988 1001")

    result = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids="2001\n",
        pgrep_pids="2001\n2999\n",
    )

    assert result.returncode == 0, result


def test_installer_runner_process_gate_requires_exact_systemd_control_group(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 1001")
    expected = "/system.slice/actions.runner.Arcanada-one.arcana-prod-ci.service"
    clean = run_runner_process_gate(
        proc_root, "988", main_pid="2001", pids="2001\n", control_group=expected
    )
    assert clean.returncode == 0, clean

    for control_group, status in (
        ("", 0),
        ("/user.slice/ci-runner-ci.scope", 0),
        ("/system.slice/actions.runner.other.service", 0),
        (expected, 1),
    ):
        result = run_runner_process_gate(
            proc_root,
            "988",
            main_pid="2001",
            pids="2001\n",
            control_group=control_group,
            control_group_status=status,
        )
        assert result.returncode == 2, (control_group, status, result)


def test_installer_runner_process_gate_fails_closed_on_enumeration_drift(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 1001")
    write_process_status(proc_root, "2002", "1000 1001")
    cases = (
        ("main-absent", "2001", "2002\n"),
        ("invalid-pid", "2001", "invalid\n"),
        ("empty-cgroup", "2001", ""),
        ("duplicate-pid", "2001", "2001\n2001\n"),
    )
    for name, main_pid, pids in cases:
        result = run_runner_process_gate(
            proc_root,
            "988",
            main_pid=main_pid,
            pids=pids,
        )
        assert result.returncode == 2, (name, result)

    missing_cgroup = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        cgroup_root=tmp_path / "missing-cgroup",
    )
    assert missing_cgroup.returncode == 2
    assert "cgroup enumeration failed" in missing_cgroup.stderr

    vanished_child = run_runner_process_gate(
        proc_root, "988", main_pid="2001", pids="2001\n2999\n"
    )
    assert vanished_child.returncode == 2
    assert "process groups are unreadable" in vanished_child.stderr

    unreadable_status = proc_root / "2003" / "status"
    unreadable_status.mkdir(parents=True)
    unreadable_child = run_runner_process_gate(
        proc_root, "988", main_pid="2001", pids="2001\n2003\n"
    )
    assert unreadable_child.returncode == 2
    assert "process groups are unreadable" in unreadable_child.stderr


def test_installer_runner_process_gate_rejects_symlink_or_incomplete_cgroup_tree(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 1001")
    control_group = (
        "/system.slice/actions.runner.Arcanada-one.arcana-prod-ci.service"
    )

    real_root = tmp_path / "real-cgroup"
    write_cgroup_processes(real_root, control_group, "2001\n")
    linked_root = tmp_path / "linked-cgroup"
    linked_root.symlink_to(real_root, target_is_directory=True)
    linked_root_result = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        cgroup_root=linked_root,
    )
    assert linked_root_result.returncode == 2
    assert "cgroup root is invalid" in linked_root_result.stderr

    linked_service_root = tmp_path / "linked-service-root"
    real_service = tmp_path / "real-service"
    write_cgroup_processes(real_service, "/", "2001\n")
    service_parent = linked_service_root / "system.slice"
    service_parent.mkdir(parents=True)
    (service_parent / control_group.rsplit("/", 1)[1]).symlink_to(
        real_service, target_is_directory=True
    )
    linked_service = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        cgroup_root=linked_service_root,
    )
    assert linked_service.returncode == 2
    assert "cgroup enumeration failed" in linked_service.stderr

    linked_procs_root = tmp_path / "linked-procs-root"
    service = linked_procs_root / control_group.removeprefix("/")
    service.mkdir(parents=True)
    external_procs = tmp_path / "external-cgroup.procs"
    external_procs.write_text("2001\n")
    (service / "cgroup.procs").symlink_to(external_procs)
    linked_procs = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        cgroup_root=linked_procs_root,
    )
    assert linked_procs.returncode == 2
    assert "cgroup enumeration failed" in linked_procs.stderr

    incomplete_nested_root = tmp_path / "incomplete-nested-root"
    write_cgroup_processes(incomplete_nested_root, control_group, "2001\n")
    (
        incomplete_nested_root
        / control_group.removeprefix("/")
        / "worker"
    ).mkdir()
    incomplete_nested = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        cgroup_root=incomplete_nested_root,
    )
    assert incomplete_nested.returncode == 2
    assert "cgroup enumeration failed" in incomplete_nested.stderr


def test_installer_runner_process_gate_rejects_snapshot_races_and_vanished_entry(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 1001")
    write_process_status(proc_root, "2002", "1000 1001")
    stable = "F|cgroup.procs\nP|cgroup.procs|2001"
    changed_snapshots = (
        stable + "\nP|cgroup.procs|2002",
        (
            "F|cgroup.procs\n"
            "F|worker/cgroup.procs\n"
            "P|cgroup.procs|2001\n"
            "P|worker/cgroup.procs|2002"
        ),
        "F|cgroup.procs\nP|cgroup.procs|2002",
    )
    for second_snapshot in changed_snapshots:
        result = run_runner_process_gate(
            proc_root,
            "988",
            main_pid="2001",
            pids=None,
            snapshots=(stable, second_snapshot),
        )
        assert result.returncode == 2, (second_snapshot, result)
        assert "cgroup changed during preflight" in result.stderr

    vanished_entry = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        snapshots=(stable, stable),
        second_snapshot_status=1,
    )
    assert vanished_entry.returncode == 2
    assert "cgroup recheck failed" in vanished_entry.stderr


def test_installer_runner_process_gate_validates_all_statuses_before_retained_result(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 988 1001")
    snapshot = (
        "F|cgroup.procs\n"
        "P|cgroup.procs|2001\n"
        "P|cgroup.procs|2002"
    )
    result = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        snapshots=(snapshot, snapshot),
    )
    assert result.returncode == 2
    assert "process groups are unreadable" in result.stderr

    source = installer_function("require_runner_processes_dockerless")
    early_return_mutant = source.replace(
        "        retained_group_seen=1",
        "        return 1",
    )
    assert early_return_mutant != source
    mutant = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids=None,
        snapshots=(snapshot, snapshot),
        gate_text=early_return_mutant,
    )
    assert mutant.returncode == 1


def test_installer_runner_process_gate_kills_main_child_and_pgrep_bypass_mutants(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    control_group = (
        "/system.slice/actions.runner.Arcanada-one.arcana-prod-ci.service"
    )
    write_process_status(proc_root, "2001", "1000 1001")
    write_process_status(proc_root, "2002", "1000 1001")
    write_process_status(proc_root, "2999", "1000 988 1001")
    source = installer_function("require_runner_processes_dockerless")
    main_presence_check = """    if [ "$main_pid_seen" -ne 1 ]; then
      printf 'install-llm-proxy-deploy: runner MainPID vanished during preflight\\n' >&2
      return 2
    fi
"""
    missing_main_mutant = source.replace(main_presence_check, "")
    pgrep_capture_mutant = """capture_runner_cgroup_snapshot() {
  local pid
  printf '%s\n' 'F|cgroup.procs'
  while IFS= read -r pid; do
    printf 'P|cgroup.procs|%s\n' "$pid"
  done < <(pgrep -u ci-runner-ci)
}"""
    collector = installer_function("collect_runner_cgroup_snapshot_records")
    recursive_call = """      collect_runner_cgroup_snapshot_records \\
        "$entry" "$snapshot_root" || return 1"""
    root_only_collector_mutant = collector.replace(recursive_call, "      :")
    assert missing_main_mutant != source
    assert root_only_collector_mutant != collector

    missing_main = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids="2002\n",
        gate_text=missing_main_mutant,
    )
    assert missing_main.returncode == 0
    write_cgroup_processes(cgroup_root, control_group, "2002\n", child="worker")
    (proc_root / "2002" / "status").write_text("Name:\trunner\nGroups:\t1000 988 1001\n")

    root_only = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids="2001\n",
        cgroup_root=cgroup_root,
        collector_text=root_only_collector_mutant,
    )
    assert root_only.returncode == 0

    pgrep = run_runner_process_gate(
        proc_root,
        "988",
        main_pid="2001",
        pids="2001\n",
        pgrep_pids="2001\n2999\n",
        capture_text=pgrep_capture_mutant,
    )
    assert pgrep.returncode == 1
    assert "runner service retained Docker group" in pgrep.stderr


def test_installer_runner_process_gate_rejects_invalid_docker_gid_and_mutant(
    tmp_path: pathlib.Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process_status(proc_root, "2001", "1000 1001")
    for docker_gid in ("", "0", "-1", "98x"):
        result = run_runner_process_gate(
            proc_root, docker_gid, main_pid="2001", pids="2001\n"
        )
        assert result.returncode == 2, (docker_gid, result)

    parser = installer_function("process_has_group")
    parser_mutant = parser.replace(
        "invalid = (gid !~ /^[1-9][0-9]*$/)", "invalid = 0"
    )
    gate = installer_function("require_runner_processes_dockerless")
    gid_check = """  if [[ ! "$docker_gid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'install-llm-proxy-deploy: Docker group GID is invalid\\n' >&2
    return 2
  fi
"""
    gate_mutant = gate.replace(gid_check, "")
    assert parser_mutant != parser
    assert gate_mutant != gate
    mutant = run_runner_process_gate(
        proc_root,
        "98x",
        main_pid="2001",
        pids="2001\n",
        parser_text=parser_mutant,
        gate_text=gate_mutant,
    )
    assert mutant.returncode == 0


def test_installer_fails_closed_on_any_legacy_sudoers_drift(
    tmp_path: pathlib.Path,
) -> None:
    legacy_sudoers = tmp_path / "10-hermes-orch"
    drift = "ci-runner ALL=(ALL) NOPASSWD: ALL\nunexpected extra rule\n"
    legacy_sudoers.write_text(drift)

    result = run_legacy_sudoers_preflight(legacy_sudoers)

    assert result.returncode == 1
    assert "must be removed by the global runner migration" in result.stderr
    assert legacy_sudoers.read_text() == drift


def test_installer_accepts_absent_legacy_sudoers_without_creating_rollback(
    tmp_path: pathlib.Path,
) -> None:
    legacy_sudoers = tmp_path / "10-hermes-orch"

    result = run_legacy_sudoers_preflight(legacy_sudoers)

    assert result.returncode == 0
    assert not legacy_sudoers.exists()
    assert list(tmp_path.iterdir()) == []


def test_installer_absence_preflight_is_idempotent(tmp_path: pathlib.Path) -> None:
    legacy_sudoers = tmp_path / "10-hermes-orch"

    first = run_legacy_sudoers_preflight(legacy_sudoers)
    second = run_legacy_sudoers_preflight(legacy_sudoers)

    assert first.returncode == 0
    assert second.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_installer_never_archives_or_removes_global_legacy_sudoers() -> None:
    text = INSTALLER.read_text()

    assert "disabled-sudoers" not in text
    assert 'mv -f -- "$legacy_sudoers"' not in text
    assert 'rm -f -- "$legacy_sudoers"' not in text


def test_legacy_sudoers_preflight_runs_before_any_host_mutation() -> None:
    text = INSTALLER.read_text()
    preflight = text.index('require_legacy_sudoers_absent "$legacy_sudoers"')

    assert preflight < text.index('gpasswd --delete "$runner_user" docker')
    assert preflight < text.index("install -d -o root -g root -m 0700")
    assert preflight < text.index('chown root:root "$protected_path"')


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
    assert "/opt/arcanada-llm-proxy/code/.env" in text
    assert "10-hermes-orch" in text
    assert "global runner migration" in text
    assert "/root/SUP-0016-runner-evacuation-prestate.Mh5MJaqY" in text
    assert "sha256sum -c" in text
    assert "privilege-surfaces.tar.gz" in text
    assert "tar -xOf" in text
    assert "cmp --silent" in text
    assert "installer never removes or archives this global sudoers file" in text
    assert "docker compose up" not in text
    assert "git reset --hard" not in text


def test_dependency_docs_match_the_audited_starlette_pin() -> None:
    for path in (README, ARCHITECTURE):
        text = path.read_text()
        assert "Starlette 1.3.1" in text
        assert "Starlette 1.0.0" not in text
