#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

if [ "$(id -u)" -ne 0 ]; then
  printf 'install-llm-proxy-deploy: root required\n' >&2
  exit 1
fi
if [ "$#" -ne 4 ]; then
  printf 'usage: %s <helper-sha256> <service-sha256> <timer-sha256> <capability-sha256>\n' \
    "$0" >&2
  exit 2
fi
for expected in "$@"; do
  if ! [[ "$expected" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'install-llm-proxy-deploy: invalid bundle digest\n' >&2
    exit 2
  fi
done

require_access_probe_binaries() {
  local probe_binary
  for probe_binary in sudo runuser hostname; do
    if ! command -v "$probe_binary" >/dev/null 2>&1; then
      printf 'install-llm-proxy-deploy: required access probe command is missing: %s\n' \
        "$probe_binary" >&2
      return 1
    fi
  done
}
require_access_probe_binaries || exit 1

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
helper="$root/deploy/llm_proxy_deploy.py"
service_unit="$root/deploy/arcanada-llm-proxy-rollback@.service"
timer_unit="$root/deploy/arcanada-llm-proxy-rollback@.timer"
test "$(sha256sum "$helper" | cut -d' ' -f1)" = "$1"
test "$(sha256sum "$service_unit" | cut -d' ' -f1)" = "$2"
test "$(sha256sum "$timer_unit" | cut -d' ' -f1)" = "$3"

# INFRA-0417: the check is on WHO the legacy rule grants to, not on whether the
# file exists anywhere on the machine.
#
# What it defends against is real: 10-hermes-orch grants passwordless
# `systemctl start|stop|restart *` and `journalctl *`, which is a way around
# every narrow verb this broker exposes — but only for the accounts it actually
# names. The original check assumed one machine, one tenant, so "file present"
# and "our runner is over-privileged" were the same statement.
#
# They are not on a consolidated host. arcana-prd is shared with another
# project's production stand, whose own runner account (`actions`) is what that
# rule grants to. That account does not run any runner that can reach this
# broker — the sudoers this installer writes names ci-runner-ci and nobody else
# — and the file belongs to a tenant this installer has no business editing.
# Refusing on its mere presence would demand that a neighbour's access be
# removed to install our service, which is not a boundary we get to cross.
#
# So the refusal now requires both: the rule exists AND it grants to an account
# that runs a runner this broker is reachable from. That is the condition the
# check was always trying to express.
#
# 2026-10-08: the Hermes agent that owned this rule is retired and its global
# provisioner no longer exists. The file is expected to be absent everywhere;
# absence passes this check. The guard stays as a fail-closed tripwire in case
# a stale copy survives on some host or is restored from an old snapshot.
require_legacy_sudoers_absent() {
  local legacy_sudoers_path="$1"
  shift
  local guarded_user
  if [ ! -e "$legacy_sudoers_path" ] && [ ! -L "$legacy_sudoers_path" ]; then
    return 0
  fi
  if [ -L "$legacy_sudoers_path" ]; then
    printf '%s\n' \
      'install-llm-proxy-deploy: legacy sudoers path is a symlink' >&2
    return 1
  fi
  # Fail closed when the caller names nobody: a check with an empty subject
  # list must refuse, not pass. The contract test calls the function with no
  # accounts precisely to pin that down, and it is the right default — a
  # future caller that forgets the argument gets the strict behaviour.
  if [ "$#" -eq 0 ]; then
    printf '%s\n' \
      'install-llm-proxy-deploy: legacy sudoers must be removed by the global runner migration' \
      >&2
    return 1
  fi
  for guarded_user in "$@"; do
    # Deliberately NOT gated on `id "$guarded_user"`. Whether the account
    # currently exists is a weaker fact than whether the rule names it: an
    # account can be created later, and the grant would then be live with no
    # installer run in between. Matching the file is the check.
    #
    # Word-boundary match: `actions` must not satisfy a check for `ci-runner`.
    if grep -Eq "(^|[[:space:],])${guarded_user}([[:space:],]|$)" \
      "$legacy_sudoers_path"; then
      printf '%s\n' \
        "install-llm-proxy-deploy: legacy sudoers still grants to ${guarded_user}" \
        >&2
      return 1
    fi
  done
  return 0
}

process_has_group() {
  local status_path="$1"
  local expected_gid="$2"
  awk -v gid="$expected_gid" '
    BEGIN {
      invalid = (gid !~ /^[1-9][0-9]*$/)
      groups_seen = 0
      found = 0
    }
    /^Groups:/ {
      groups_seen += 1
      if ($1 != "Groups:" || NF < 2) invalid = 1
      for (field = 2; field <= NF; field += 1) {
        if ($field !~ /^[0-9][0-9]*$/) invalid = 1
        if (("x" $field) == ("x" gid)) found = 1
      }
    }
    END {
      if (invalid || groups_seen != 1) exit 2
      exit found ? 0 : 1
    }
  ' "$status_path"
}

require_runner_unit_exists() {
  local runner_unit="$1"
  local load_state
  if ! load_state="$(systemctl show --property LoadState --value "$runner_unit")"; then
    printf 'install-llm-proxy-deploy: runner unit lookup failed\n' >&2
    return 1
  fi
  if [ "$load_state" != loaded ]; then
    printf 'install-llm-proxy-deploy: runner unit does not exist or is not loaded\n' >&2
    return 1
  fi
}

require_runner_main_pid() {
  local runner_unit="$1"
  local proc_root="${2:-/proc}"
  local runner_pid
  if ! require_runner_unit_exists "$runner_unit"; then
    return 1
  fi
  if ! runner_pid="$(systemctl show --property MainPID --value "$runner_unit")"; then
    printf 'install-llm-proxy-deploy: runner MainPID lookup failed\n' >&2
    return 1
  fi
  if [[ ! "$runner_pid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'install-llm-proxy-deploy: runner MainPID is invalid\n' >&2
    return 1
  fi
  if [ ! -r "$proc_root/$runner_pid/status" ]; then
    printf 'install-llm-proxy-deploy: runner MainPID is not live\n' >&2
    return 1
  fi
  printf '%s\n' "$runner_pid"
}

require_runner_control_group() {
  local runner_unit="$1"
  local control_group
  local expected_control_group="/system.slice/$runner_unit"
  if ! control_group="$(
    systemctl show --property ControlGroup --value "$runner_unit"
  )"; then
    printf 'install-llm-proxy-deploy: runner ControlGroup lookup failed\n' >&2
    return 1
  fi
  if [ "$control_group" != "$expected_control_group" ]; then
    printf 'install-llm-proxy-deploy: runner ControlGroup is invalid\n' >&2
    return 1
  fi
  printf '%s\n' "$control_group"
}

collect_runner_cgroup_snapshot_records() {
  local cgroup_path="$1"
  local snapshot_root="$2"
  local procs_path="$cgroup_path/cgroup.procs"
  local entry
  local relative_path
  local runner_pid
  local -a local_pids

  if [ -L "$cgroup_path" ] || [ ! -d "$cgroup_path" ]; then
    printf 'install-llm-proxy-deploy: runner cgroup is unavailable\n' >&2
    return 1
  fi
  if [ -L "$procs_path" ] || [ ! -f "$procs_path" ] ||
    [ ! -r "$procs_path" ]; then
    printf 'install-llm-proxy-deploy: runner cgroup.procs is unreadable\n' >&2
    return 1
  fi
  if ! mapfile -t local_pids <"$procs_path"; then
    printf 'install-llm-proxy-deploy: runner cgroup.procs read failed\n' >&2
    return 1
  fi
  relative_path="${procs_path#"$snapshot_root"/}"
  if [ "$relative_path" = "$procs_path" ] || [ -z "$relative_path" ] ||
    [[ "$relative_path" == *"|"* ]] ||
    [[ "$relative_path" == *$'\n'* ]] ||
    [[ "$relative_path" == *$'\r'* ]]; then
    printf 'install-llm-proxy-deploy: runner cgroup path is invalid\n' >&2
    return 1
  fi
  printf 'F|%s\n' "$relative_path"
  for runner_pid in "${local_pids[@]}"; do
    if [[ ! "$runner_pid" =~ ^[1-9][0-9]*$ ]]; then
      printf 'install-llm-proxy-deploy: runner cgroup process ID is invalid\n' >&2
      return 1
    fi
    printf 'P|%s|%s\n' "$relative_path" "$runner_pid"
  done

  shopt -s dotglob nullglob
  for entry in "$cgroup_path"/*; do
    if [ -L "$entry" ]; then
      printf 'install-llm-proxy-deploy: runner cgroup contains a symlink\n' >&2
      return 1
    elif [ -d "$entry" ]; then
      collect_runner_cgroup_snapshot_records \
        "$entry" "$snapshot_root" || return 1
    elif [ -f "$entry" ]; then
      :
    else
      printf 'install-llm-proxy-deploy: runner cgroup entry vanished or is invalid\n' >&2
      return 1
    fi
  done
}

capture_runner_cgroup_snapshot() {
  local cgroup_path="$1"
  local snapshot
  if ! snapshot="$(
    collect_runner_cgroup_snapshot_records "$cgroup_path" "$cgroup_path"
  )"; then
    return 1
  fi
  if ! snapshot="$(printf '%s\n' "$snapshot" | LC_ALL=C sort)"; then
    printf 'install-llm-proxy-deploy: runner cgroup snapshot sort failed\n' >&2
    return 1
  fi
  printf '%s\n' "$snapshot"
}

legacy_sudoers=/etc/sudoers.d/10-hermes-orch
require_legacy_sudoers_absent "$legacy_sudoers" ci-runner ci-runner-ci

require_runner_account_dockerless() {
  local runner_user="$1"
  local runner_groups
  local access_mode
  local probe_status
  if ! runner_groups="$(id -nG "$runner_user")"; then
    printf 'install-llm-proxy-deploy: %s group lookup failed\n' \
      "$runner_user" >&2
    return 1
  fi
  if printf '%s\n' "$runner_groups" | tr ' ' '\n' | grep -Fxq docker; then
    gpasswd --delete "$runner_user" docker >/dev/null
  fi
  if ! runner_groups="$(id -nG "$runner_user")"; then
    printf 'install-llm-proxy-deploy: %s group lookup failed\n' \
      "$runner_user" >&2
    return 1
  fi
  if printf '%s\n' "$runner_groups" | tr ' ' '\n' | grep -Fxq docker; then
    printf 'install-llm-proxy-deploy: %s still belongs to docker group\n' \
      "$runner_user" >&2
    return 1
  fi
  for access_mode in -r -w; do
    if probe_runuser_access "$runner_user" "$access_mode" /run/docker.sock; then
      printf 'install-llm-proxy-deploy: %s can still access Docker socket\n' \
        "$runner_user" >&2
      return 1
    else
      probe_status=$?
      if [ "$probe_status" -ne 1 ]; then
        return 2
      fi
    fi
  done
}

probe_runuser_access() {
  local runner_user="$1"
  local access_mode="$2"
  local protected_path="$3"
  local output
  local probe_status
  if ! command -v runuser >/dev/null 2>&1; then
    printf 'install-llm-proxy-deploy: runuser access probe command is missing\n' >&2
    return 2
  fi
  if output="$(runuser -u "$runner_user" -- test "$access_mode" \
    "$protected_path" 2>&1)"; then
    probe_status=0
  else
    probe_status=$?
  fi
  case "$probe_status" in
    0)
      if [ -n "$output" ]; then
        printf 'install-llm-proxy-deploy: runuser access probe returned unexpected output\n' >&2
        return 2
      fi
      return 0
      ;;
    1)
      if [ -z "$output" ] ||
        [ "$output" = 'runuser: failed to execute test: Permission denied' ]; then
        return 1
      fi
      printf 'install-llm-proxy-deploy: runuser access probe failed unexpectedly\n' >&2
      return 2
      ;;
    *)
      printf 'install-llm-proxy-deploy: runuser access probe exited unexpectedly (%s)\n' \
        "$probe_status" >&2
      return 2
      ;;
  esac
}

sudo_denial_output_is_expected() {
  local runner_user="$1"
  local output="$2"
  local host
  if ! host="$(hostname 2>/dev/null)" || [ -z "$host" ] || [[ "$host" == *$'\n'* ]]; then
    printf 'install-llm-proxy-deploy: unable to identify host for sudo denial check\n' >&2
    return 2
  fi
  [ "$output" = "User $runner_user is not allowed to run sudo on $host." ]
}

sudo_policy_output_is_expected() {
  local runner_user="$1"
  local output="$2"
  local expected_command="${3:-}"
  local line
  local saw_user_header=0
  local saw_command=0
  local saw_matching_defaults=0
  local saw_runas_defaults=0
  local saw_runas_entry=0
  local in_defaults=0
  local in_runas_defaults=0
  local command_line_pattern='^[[:space:]]+\([^)]*\)[[:space:]].+$'
  local runas_defaults_entry_pattern='^[[:space:]]+Defaults!/usr/local/sbin/[[:alnum:]][[:alnum:]_.-]*([[:space:]]+[[:alnum:]_./*:+-]+)*(,[[:space:]]*/usr/local/sbin/[[:alnum:]][[:alnum:]_.-]*([[:space:]]+[[:alnum:]_./*:+-]+)*)*[[:space:]]+env_keep\+="[A-Z0-9_]+(,[A-Z0-9_]+)*"$'
  while IFS= read -r line; do
    case "$line" in
      "User $runner_user may run the following commands on "*)
        [[ "$line" =~ ^User[[:space:]]+${runner_user}[[:space:]]+may[[:space:]]+run[[:space:]]+the[[:space:]]+following[[:space:]]+commands[[:space:]]+on[[:space:]][[:alnum:]_.-]+:$ ]] || return 1
        [ "$saw_user_header" -eq 0 ] || return 1
        [ "$saw_runas_defaults" -eq 0 ] || [ "$saw_runas_entry" -eq 1 ] || return 1
        saw_user_header=1
        in_defaults=0
        in_runas_defaults=0
        ;;
      "Runas and Command-specific defaults for $runner_user:")
        [ "$saw_matching_defaults" -eq 1 ] || return 1
        [ "$saw_runas_defaults" -eq 0 ] || return 1
        [ "$saw_user_header" -eq 0 ] || return 1
        saw_runas_defaults=1
        in_defaults=0
        in_runas_defaults=1
        ;;
      "Matching Defaults entries for $runner_user on "*)
        [[ "$line" =~ ^Matching[[:space:]]Defaults[[:space:]]entries[[:space:]]for[[:space:]]${runner_user}[[:space:]]on[[:space:]][[:alnum:]_.-]+:$ ]] || return 1
        [ "$in_defaults" -eq 0 ] || return 1
        [ "$saw_matching_defaults" -eq 0 ] || return 1
        [ "$saw_runas_defaults" -eq 0 ] || return 1
        [ "$saw_user_header" -eq 0 ] || return 1
        saw_matching_defaults=1
        in_defaults=1
        ;;
      '')
        ;;
      *)
        if [ "$saw_user_header" -eq 1 ]; then
          if [[ "$line" =~ $command_line_pattern ]]; then
            if [ -z "$expected_command" ] || [[ "$line" == *"$expected_command"* ]]; then
              saw_command=1
            fi
          else
            return 1
          fi
        elif [ "$in_runas_defaults" -eq 1 ] && [[ "$line" =~ $runas_defaults_entry_pattern ]]; then
          saw_runas_entry=1
        elif [ "$in_defaults" -eq 1 ] && [[ "$line" == [[:space:]]* ]]; then
          :
        else
          return 1
        fi
        ;;
    esac
  done <<<"$output"
  [ "$saw_runas_defaults" -eq 0 ] || [ "$saw_runas_entry" -eq 1 ] || return 1
  [ "$saw_user_header" -eq 1 ] && [ "$saw_command" -eq 1 ]
}

probe_sudo_command() {
  # Command-specific `sudo -l` prints the qualified command on allow and may
  # return exit 1 with no output when that command is denied.
  local runner_user="$1"
  shift
  if [ "$#" -eq 0 ]; then
    printf 'install-llm-proxy-deploy: sudo access probe requires a command\n' >&2
    return 2
  fi
  local expected_command="$*"
  local output
  local probe_status
  if ! command -v sudo >/dev/null 2>&1; then
    printf 'install-llm-proxy-deploy: sudo access probe command is missing\n' >&2
    return 2
  fi
  if output="$(sudo -n -l -U "$runner_user" "$@" 2>&1)"; then
    probe_status=0
  else
    probe_status=$?
  fi
  case "$probe_status" in
    0)
      if [ "$output" != "$expected_command" ]; then
        printf 'install-llm-proxy-deploy: sudo access probe returned unexpected output\n' >&2
        return 2
      fi
      return 1
      ;;
    1)
      if [ -z "$output" ] || sudo_denial_output_is_expected "$runner_user" "$output"; then
        return 0
      fi
      printf 'install-llm-proxy-deploy: sudo access probe returned unexpected denial output\n' >&2
      return 2
      ;;
    *)
      printf 'install-llm-proxy-deploy: sudo access probe exited unexpectedly (%s)\n' \
        "$probe_status" >&2
      return 2
      ;;
  esac
}

probe_sudo_policy() {
  local runner_user="$1"
  local output
  local probe_status
  if ! command -v sudo >/dev/null 2>&1; then
    printf 'install-llm-proxy-deploy: sudo access probe command is missing\n' >&2
    return 2
  fi
  if output="$(sudo -n -l -U "$runner_user" 2>&1)"; then
    probe_status=0
  else
    probe_status=$?
  fi
  case "$probe_status" in
    0)
      if sudo_policy_output_is_expected "$runner_user" "$output"; then
        printf '%s\n' "$output"
        return 0
      fi
      printf 'install-llm-proxy-deploy: sudo policy probe returned unexpected output\n' >&2
      return 2
      ;;
    1)
      if sudo_denial_output_is_expected "$runner_user" "$output"; then
        return 1
      fi
      printf 'install-llm-proxy-deploy: sudo policy probe returned unexpected denial output\n' >&2
      return 2
      ;;
    *)
      printf 'install-llm-proxy-deploy: sudo policy probe exited unexpectedly (%s)\n' \
        "$probe_status" >&2
      return 2
      ;;
  esac
}

require_sudo_command_denied() {
  local probe_status
  if probe_sudo_command "$@"; then
    return 0
  else
    probe_status=$?
  fi
  case "$probe_status" in
    1)
      printf 'install-llm-proxy-deploy: %s can invoke the deploy broker through sudo\n' \
        "$1" >&2
      return 1
      ;;
    *)
      return 2
      ;;
  esac
}

require_runner_cannot_invoke_broker() {
  local runner_user="$1"
  local broker=/usr/local/sbin/arcanada-llm-proxy-deploy
  local sample_release=0000000000000000000000000000000000000000
  local sample_digest=0000000000000000000000000000000000000000000000000000000000000000
  local sample_run=1
  local probe_status
  local -a actions=(verify-bundle preflight deploy health commit rollback status)
  local action
  local -a command_args

  # The exact sudo denial is safe; every other nonzero or malformed response
  # is an unmeasured access boundary and aborts installation.
  for action in "${actions[@]}"; do
    case "$action" in
      verify-bundle)
        command_args=("$broker" "$action" "$sample_digest" "$sample_digest" "$sample_digest")
        ;;
      preflight)
        command_args=("$broker" "$action")
        ;;
      deploy)
        command_args=("$broker" "$action" "$sample_release" "sha256:$sample_digest" "$sample_run")
        ;;
      health|commit|rollback)
        command_args=("$broker" "$action" "$sample_release")
        ;;
      status)
        command_args=("$broker" "$action" "$sample_release")
        ;;
    esac
    if require_sudo_command_denied "$runner_user" "${command_args[@]}"; then
      :
    else
      probe_status=$?
      return "$probe_status"
    fi
  done
}

require_runner_cannot_access_deploy_state() {
  local runner_user="$1"
  shift
  local protected_path
  local access_mode
  local probe_status
  for protected_path in "$@"; do
    for access_mode in -r -w -x; do
      if probe_runuser_access "$runner_user" "$access_mode" "$protected_path"; then
        printf 'install-llm-proxy-deploy: %s can access deployment capability state\n' \
          "$runner_user" >&2
        return 1
      else
        probe_status=$?
        if [ "$probe_status" -ne 1 ]; then
          return 2
        fi
      fi
    done
  done
}

require_runner_processes_dockerless() {
  local docker_gid="$1"
  local proc_root="$2"
  local cgroup_root="$3"
  local runner
  local runner_user
  local runner_unit
  local control_group
  local cgroup_path
  local runner_pid
  local main_pid
  local first_snapshot
  local second_snapshot
  local snapshot_kind
  local snapshot_path
  local snapshot_pid
  local snapshot_extra
  local main_pid_seen
  local group_check_status
  local retained_group_seen
  local invalid_status_seen
  local -A seen_pids

  if [[ ! "$docker_gid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'install-llm-proxy-deploy: Docker group GID is invalid\n' >&2
    return 2
  fi
  if [[ "$proc_root" != /* ]] || [ -L "$proc_root" ] ||
    [ ! -d "$proc_root" ]; then
    printf 'install-llm-proxy-deploy: proc root is invalid\n' >&2
    return 2
  fi
  if [[ "$cgroup_root" != /* ]] || [ -L "$cgroup_root" ] ||
    [ ! -d "$cgroup_root" ]; then
    printf 'install-llm-proxy-deploy: cgroup root is invalid\n' >&2
    return 2
  fi
  retained_group_seen=0
  # Only arcana-prd-ci can invoke this broker through its sudoers grant.
  # ci-runner serves unrelated Docker-based deploys and keeps Docker access.
  local -a broker_runners=(
    'ci-runner-ci:actions.runner.Arcanada-one.arcana-prd-ci.service'
  )
  for runner in "${broker_runners[@]}"; do
    runner_user="${runner%%:*}"
    runner_unit="${runner#*:}"
    if ! id "$runner_user" >/dev/null 2>&1; then
      continue
    fi
    require_runner_account_dockerless "$runner_user" || return 2
    if ! runner_pid="$(require_runner_main_pid "$runner_unit" "$proc_root")"; then
      return 2
    fi
    main_pid="$runner_pid"
    if ! control_group="$(require_runner_control_group "$runner_unit")"; then
      return 2
    fi
    cgroup_path="${cgroup_root}${control_group}"
    if ! first_snapshot="$(capture_runner_cgroup_snapshot "$cgroup_path")"; then
      printf 'install-llm-proxy-deploy: %s cgroup enumeration failed\n' \
        "$runner_user" >&2
      return 2
    fi
    main_pid_seen=0
    invalid_status_seen=0
    seen_pids=()
    while IFS='|' read -r \
      snapshot_kind snapshot_path snapshot_pid snapshot_extra; do
      if [ "$snapshot_kind" = F ]; then
        if [ -z "$snapshot_path" ] || [ -n "$snapshot_pid" ] ||
          [ -n "$snapshot_extra" ]; then
          invalid_status_seen=1
        fi
        continue
      fi
      if [ "$snapshot_kind" != P ] || [ -z "$snapshot_path" ] ||
        [[ ! "$snapshot_pid" =~ ^[1-9][0-9]*$ ]] ||
        [ -n "$snapshot_extra" ]; then
        invalid_status_seen=1
        continue
      fi
      runner_pid="$snapshot_pid"
      if [[ -n "${seen_pids[$runner_pid]:-}" ]]; then
        invalid_status_seen=1
        continue
      fi
      seen_pids["$runner_pid"]=1
      if [ "$runner_pid" = "$main_pid" ]; then
        main_pid_seen=1
      fi
      if process_has_group "$proc_root/$runner_pid/status" "$docker_gid"; then
        retained_group_seen=1
      else
        group_check_status="$?"
        if [ "$group_check_status" -ne 1 ]; then
          invalid_status_seen=1
        fi
      fi
    done <<<"$first_snapshot"
    if ! second_snapshot="$(capture_runner_cgroup_snapshot "$cgroup_path")"; then
      printf 'install-llm-proxy-deploy: %s cgroup recheck failed\n' \
        "$runner_user" >&2
      return 2
    fi
    if [ "$first_snapshot" != "$second_snapshot" ]; then
      printf 'install-llm-proxy-deploy: runner cgroup changed during preflight\n' >&2
      return 2
    fi
    if [ "${#seen_pids[@]}" -eq 0 ]; then
      printf 'install-llm-proxy-deploy: %s cgroup has no live processes\n' \
        "$runner_user" >&2
      return 2
    fi
    if [ "$main_pid_seen" -ne 1 ]; then
      printf 'install-llm-proxy-deploy: runner MainPID vanished during preflight\n' >&2
      return 2
    fi
    if [ "$invalid_status_seen" -ne 0 ]; then
      printf 'install-llm-proxy-deploy: %s process groups are unreadable\n' \
        "$runner_user" >&2
      return 2
    fi
  done
  if [ "$retained_group_seen" -ne 0 ]; then
    printf 'install-llm-proxy-deploy: runner service retained Docker group\n' >&2
    return 1
  fi
  return 0
}

runner_users=(ci-runner ci-runner-ci)
capability_file=/var/lib/arcanada-llm-proxy-deploy/deploy-capability.sha256
docker_gid="$(getent group docker | cut -d: -f3)"
if ! id ci-runner-ci >/dev/null 2>&1; then
  printf 'install-llm-proxy-deploy: required runner user is absent\n' >&2
  exit 1
fi
if require_runner_processes_dockerless "$docker_gid" /proc /sys/fs/cgroup; then
  :
else
  runner_group_status=$?
  if [ "$runner_group_status" -eq 1 ]; then
    printf 'install-llm-proxy-deploy: runner process retained Docker group\n' >&2
  else
    printf 'install-llm-proxy-deploy: runner process group validation failed\n' >&2
  fi
  exit 1
fi

for source in "$helper" "$service_unit" "$timer_unit"; do
  test -f "$source"
  test ! -L "$source"
done
/usr/bin/python3 -I -c \
  'import pathlib,sys; compile(pathlib.Path(sys.argv[1]).read_text(), sys.argv[1], "exec")' \
  "$helper"

install -d -o root -g root -m 0700 \
  /var/lib/arcanada-llm-proxy-deploy \
  /var/lib/arcanada-llm-proxy-deploy/releases
exec 9>/var/lib/arcanada-llm-proxy-deploy/deploy.lock
chmod 0600 /var/lib/arcanada-llm-proxy-deploy/deploy.lock
flock -n 9

legacy_deploy_root=/opt/arcanada-llm-proxy
legacy_deploy_tree="$legacy_deploy_root/code"
legacy_deploy_env="$legacy_deploy_tree/.env"
for protected_path in "$legacy_deploy_root" "$legacy_deploy_tree"; do
  if [ ! -e "$protected_path" ]; then
    continue
  fi
  if [ -L "$protected_path" ] || [ ! -d "$protected_path" ]; then
    printf 'install-llm-proxy-deploy: legacy deploy directory is unsafe\n' >&2
    exit 1
  fi
  if ! command -v setfacl >/dev/null 2>&1; then
    printf 'install-llm-proxy-deploy: setfacl is required for deploy-tree hardening\n' >&2
    exit 1
  fi
  setfacl --remove-all "$protected_path"
  chown root:root "$protected_path"
  chmod 0700 "$protected_path"
done
if [ -e "$legacy_deploy_env" ]; then
  if [ -L "$legacy_deploy_env" ] || [ ! -f "$legacy_deploy_env" ]; then
    printf 'install-llm-proxy-deploy: legacy environment file is unsafe\n' >&2
    exit 1
  fi
  setfacl --remove-all "$legacy_deploy_env"
  chown root:root "$legacy_deploy_env"
  chmod 0600 "$legacy_deploy_env"
fi
for runner_user in "${runner_users[@]}"; do
  if ! id "$runner_user" >/dev/null 2>&1; then
    continue
  fi
  for protected_path in \
    "$legacy_deploy_root" \
    "$legacy_deploy_tree" \
    "$legacy_deploy_env"; do
    if [ ! -e "$protected_path" ]; then
      continue
    fi
    for access_mode in -r -w -x; do
      if probe_runuser_access "$runner_user" "$access_mode" "$protected_path"; then
        printf 'install-llm-proxy-deploy: %s retains legacy deploy-tree access\n' \
          "$runner_user" >&2
        exit 1
      else
        probe_status=$?
        if [ "$probe_status" -ne 1 ]; then
          exit 1
        fi
      fi
    done
  done
done

helper_tmp="$(mktemp /usr/local/sbin/.arcanada-llm-proxy-deploy.XXXXXX)"
service_tmp="$(mktemp /etc/systemd/system/.arcanada-llm-proxy-rollback.service.XXXXXX)"
timer_tmp="$(mktemp /etc/systemd/system/.arcanada-llm-proxy-rollback.timer.XXXXXX)"
sudoers_tmp="$(mktemp /etc/sudoers.d/.arcanada-llm-proxy-deploy.XXXXXX)"
capability_tmp="$(
  mktemp /var/lib/arcanada-llm-proxy-deploy/.deploy-capability.sha256.XXXXXX
)"
trap 'rm -f -- "$helper_tmp" "$service_tmp" "$timer_tmp" "$sudoers_tmp" "$capability_tmp"' EXIT

install -o root -g root -m 0755 "$helper" "$helper_tmp"
install -o root -g root -m 0644 "$service_unit" "$service_tmp"
install -o root -g root -m 0644 "$timer_unit" "$timer_tmp"
test "$(sha256sum "$helper_tmp" | cut -d' ' -f1)" = "$1"
test "$(sha256sum "$service_tmp" | cut -d' ' -f1)" = "$2"
test "$(sha256sum "$timer_tmp" | cut -d' ' -f1)" = "$3"
mv -f -- "$helper_tmp" /usr/local/sbin/arcanada-llm-proxy-deploy
mv -f -- "$service_tmp" \
  /etc/systemd/system/arcanada-llm-proxy-rollback@.service
mv -f -- "$timer_tmp" \
  /etc/systemd/system/arcanada-llm-proxy-rollback@.timer
printf '%s\n' "$4" >"$capability_tmp"
chown root:root "$capability_tmp"
chmod 0600 "$capability_tmp"
mv -f -- "$capability_tmp" \
  "$capability_file"

systemd-analyze verify \
  /etc/systemd/system/arcanada-llm-proxy-rollback@.service \
  /etc/systemd/system/arcanada-llm-proxy-rollback@.timer

{
  printf '%s\n' \
    'Cmnd_Alias ARCANADA_LLM_PROXY_DEPLOY = /usr/local/sbin/arcanada-llm-proxy-deploy verify-bundle *, /usr/local/sbin/arcanada-llm-proxy-deploy preflight, /usr/local/sbin/arcanada-llm-proxy-deploy deploy *, /usr/local/sbin/arcanada-llm-proxy-deploy health *, /usr/local/sbin/arcanada-llm-proxy-deploy commit *, /usr/local/sbin/arcanada-llm-proxy-deploy rollback *, /usr/local/sbin/arcanada-llm-proxy-deploy status *'
  printf '%s\n' \
    'ci-runner-ci ALL=(root) NOPASSWD:NOSETENV: ARCANADA_LLM_PROXY_DEPLOY'
} >"$sudoers_tmp"
chmod 0440 "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
install -o root -g root -m 0440 "$sudoers_tmp" \
  /etc/sudoers.d/arcanada-llm-proxy-deploy
visudo -cf /etc/sudoers.d/arcanada-llm-proxy-deploy >/dev/null
visudo -c >/dev/null
for runner_user in "${runner_users[@]}"; do
  if ! id "$runner_user" >/dev/null 2>&1; then
    continue
  fi
  if sudo_report="$(probe_sudo_policy "$runner_user")"; then
    :
  else
    probe_status=$?
    if [ "$probe_status" -eq 1 ]; then
      sudo_report=''
    else
      exit 1
    fi
  fi
  if [ "$runner_user" = ci-runner ] &&
    ! require_runner_cannot_invoke_broker "$runner_user"; then
    exit 1
  fi
  if grep -Eq \
    'NOPASSWD:[[:space:]]+ALL|systemctl (start|stop|restart|reload) \*|journalctl --no-pager \*' \
    <<<"$sudo_report"; then
    printf 'install-llm-proxy-deploy: %s legacy broad sudo remains active\n' \
      "$runner_user" >&2
    exit 1
  fi
  if ! require_runner_cannot_access_deploy_state \
    "$runner_user" /var/lib/arcanada-llm-proxy-deploy "$capability_file"; then
    exit 1
  fi
done
systemctl daemon-reload

/usr/local/sbin/arcanada-llm-proxy-deploy verify-bundle "$1" "$2" "$3"
/usr/local/sbin/arcanada-llm-proxy-deploy preflight
printf 'LLM_PROXY_DEPLOY_INSTALL_PASS helper_sha=%s service_sha=%s timer_sha=%s\n' \
  "$1" "$2" "$3"
