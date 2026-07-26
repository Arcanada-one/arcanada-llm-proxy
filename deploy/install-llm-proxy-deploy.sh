#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

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

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
helper="$root/deploy/llm_proxy_deploy.py"
service_unit="$root/deploy/arcanada-llm-proxy-rollback@.service"
timer_unit="$root/deploy/arcanada-llm-proxy-rollback@.timer"
test "$(sha256sum "$helper" | cut -d' ' -f1)" = "$1"
test "$(sha256sum "$service_unit" | cut -d' ' -f1)" = "$2"
test "$(sha256sum "$timer_unit" | cut -d' ' -f1)" = "$3"

require_legacy_sudoers_absent() {
  local legacy_sudoers_path="$1"
  if [ -e "$legacy_sudoers_path" ] || [ -L "$legacy_sudoers_path" ]; then
    printf '%s\n' \
      'install-llm-proxy-deploy: legacy sudoers must be removed by the global runner migration' \
      >&2
    return 1
  fi
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

require_runner_main_pid() {
  local runner_unit="$1"
  local proc_root="${2:-/proc}"
  local runner_pid
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
require_legacy_sudoers_absent "$legacy_sudoers"

require_runner_account_dockerless() {
  local runner_user="$1"
  local runner_groups
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
  if runuser -u "$runner_user" -- test -r /run/docker.sock ||
    runuser -u "$runner_user" -- test -w /run/docker.sock; then
    printf 'install-llm-proxy-deploy: %s can still access Docker socket\n' \
      "$runner_user" >&2
    return 1
  fi
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
  for runner in \
    'ci-runner:actions.runner.Arcanada-one.arcana-prod.service' \
    'ci-runner-ci:actions.runner.Arcanada-one.arcana-prod-ci.service'; do
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
    if runuser -u "$runner_user" -- test -r "$protected_path" ||
      runuser -u "$runner_user" -- test -w "$protected_path" ||
      runuser -u "$runner_user" -- test -x "$protected_path"; then
      printf 'install-llm-proxy-deploy: %s retains legacy deploy-tree access\n' \
        "$runner_user" >&2
      exit 1
    fi
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
  /var/lib/arcanada-llm-proxy-deploy/deploy-capability.sha256

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
  if ! sudo_report="$(sudo -n -l -U "$runner_user" 2>&1)"; then
    printf 'install-llm-proxy-deploy: cannot audit %s sudo rules\n' \
      "$runner_user" >&2
    exit 1
  fi
  if grep -Eq \
    'NOPASSWD:[[:space:]]+ALL|systemctl (start|stop|restart|reload) \*|journalctl --no-pager \*' \
    <<<"$sudo_report"; then
    printf 'install-llm-proxy-deploy: %s legacy broad sudo remains active\n' \
      "$runner_user" >&2
    exit 1
  fi
done
systemctl daemon-reload

/usr/local/sbin/arcanada-llm-proxy-deploy verify-bundle "$1" "$2" "$3"
/usr/local/sbin/arcanada-llm-proxy-deploy preflight
printf 'LLM_PROXY_DEPLOY_INSTALL_PASS helper_sha=%s service_sha=%s timer_sha=%s\n' \
  "$1" "$2" "$3"
