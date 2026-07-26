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

runner_users=(ci-runner ci-runner-ci)
docker_gid="$(getent group docker | cut -d: -f3)"
if ! id ci-runner-ci >/dev/null 2>&1; then
  printf 'install-llm-proxy-deploy: required runner user is absent\n' >&2
  exit 1
fi
for runner_user in "${runner_users[@]}"; do
  if ! id "$runner_user" >/dev/null 2>&1; then
    continue
  fi
  if id -nG "$runner_user" | tr ' ' '\n' | grep -Fxq docker; then
    gpasswd --delete "$runner_user" docker >/dev/null
  fi
  if id -nG "$runner_user" | tr ' ' '\n' | grep -Fxq docker; then
    printf 'install-llm-proxy-deploy: %s still belongs to docker group\n' \
      "$runner_user" >&2
    exit 1
  fi
  if runuser -u "$runner_user" -- test -r /run/docker.sock ||
    runuser -u "$runner_user" -- test -w /run/docker.sock; then
    printf 'install-llm-proxy-deploy: %s can still access Docker socket\n' \
      "$runner_user" >&2
    exit 1
  fi
  mapfile -t runner_pids < <(pgrep -u "$runner_user" || true)
  for runner_pid in "${runner_pids[@]}"; do
    if awk -v gid="$docker_gid" '
      /^Groups:/ {
        for (index = 2; index <= NF; index += 1) {
          if ($index == gid) exit 0
        }
        exit 1
      }
      END { if (NR == 0) exit 2 }
    ' "/proc/$runner_pid/status"; then
      printf 'install-llm-proxy-deploy: %s service retained Docker group\n' \
        "$runner_user" >&2
      exit 1
    fi
  done
done

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

legacy_sudoers=/etc/sudoers.d/10-hermes-orch
legacy_sudoers_backup=\
/var/lib/arcanada-llm-proxy-deploy/disabled-sudoers/10-hermes-orch
if [ -e "$legacy_sudoers" ]; then
  if [ -L "$legacy_sudoers" ] || [ ! -f "$legacy_sudoers" ] ||
    [ "$(stat -c '%U:%G' "$legacy_sudoers")" != root:root ] ||
    [ "$(stat -c '%a' "$legacy_sudoers")" != 440 ]; then
    printf 'install-llm-proxy-deploy: legacy sudoers file is unsafe\n' >&2
    exit 1
  fi
  if ! grep -Fqx \
    'ci-runner ALL=(root) NOPASSWD: HERMES_SVC, HERMES_LOG' \
    "$legacy_sudoers" ||
    ! grep -Eq 'systemctl (start|stop|restart) \*' "$legacy_sudoers"; then
    printf 'install-llm-proxy-deploy: legacy sudoers content is unexpected\n' >&2
    exit 1
  fi
  install -d -o root -g root -m 0700 \
    /var/lib/arcanada-llm-proxy-deploy/disabled-sudoers
  if [ -e "$legacy_sudoers_backup" ] &&
    ! cmp --silent "$legacy_sudoers" "$legacy_sudoers_backup"; then
    printf 'install-llm-proxy-deploy: legacy sudoers backup conflicts\n' >&2
    exit 1
  fi
  mv -f -- "$legacy_sudoers" "$legacy_sudoers_backup"
  chown root:root "$legacy_sudoers_backup"
  chmod 0600 "$legacy_sudoers_backup"
fi

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
