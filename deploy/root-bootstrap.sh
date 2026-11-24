#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
umask 077

readonly ssh_target='root@arcana-prd'
readonly remote_state='/root/.config/arcanada-llm-proxy'
readonly capability_file="${remote_state}/deploy-capability"
readonly remote_stage_prefix='/var/lib/arcanada-llm-proxy-deploy/bootstrap-source'
readonly repository='Arcanada-one/arcanada-llm-proxy'

repo_root="$(git rev-parse --show-toplevel)"
[[ "$(git -C "$repo_root" branch --show-current)" == main ]] || {
  printf 'bootstrap: run from a merged-main checkout\n' >&2
  exit 1
}
[[ -z "$(git -C "$repo_root" status --porcelain)" ]] || {
  printf 'bootstrap: main checkout must be clean\n' >&2
  exit 1
}
main_sha="$(git -C "$repo_root" rev-parse HEAD)"
[[ "$main_sha" =~ ^[0-9a-f]{40}$ ]]
remote_main="$(git -C "$repo_root" ls-remote --exit-code origin refs/heads/main | cut -f1)"
[[ "$remote_main" == "$main_sha" ]] || {
  printf 'bootstrap: local main is not the current origin/main\n' >&2
  exit 1
}

helper_sha="$(sha256sum "$repo_root/deploy/llm_proxy_deploy.py" | cut -d' ' -f1)"
service_sha="$(sha256sum "$repo_root/deploy/arcanada-llm-proxy-rollback@.service" | cut -d' ' -f1)"
timer_sha="$(sha256sum "$repo_root/deploy/arcanada-llm-proxy-rollback@.timer" | cut -d' ' -f1)"
for digest in "$helper_sha" "$service_sha" "$timer_sha"; do
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]]
done

# The reviewed helper creates or safely reuses the root-only capability. It
# never writes the value to stdout, argv, a local file, or a shell trace.
ssh -T "$ssh_target" "bash -s -- '$remote_state'" \
  < "$repo_root/deploy/root-bootstrap-capability.sh"

capability_sha="$(ssh -T "$ssh_target" "sha256sum '$capability_file' | cut -d' ' -f1")"
[[ "$capability_sha" =~ ^[0-9a-f]{64}$ ]]

stage="${remote_stage_prefix}-${main_sha}"
ssh -T "$ssh_target" \
  "test ! -e '$stage' && install -d -o root -g root -m 0700 '$stage'"
tar -C "$repo_root" -czf - \
  deploy/install-llm-proxy-deploy.sh \
  deploy/llm_proxy_deploy.py \
  'deploy/arcanada-llm-proxy-rollback@.service' \
  'deploy/arcanada-llm-proxy-rollback@.timer' |
  ssh -T "$ssh_target" "bash -c 'set -euo pipefail
stage=\"$stage\"
helper_sha=\"$helper_sha\"
service_sha=\"$service_sha\"
timer_sha=\"$timer_sha\"
capability_sha=\"$capability_sha\"
tar -xzf - -C \"\$stage\"
chown -R root:root \"\$stage\"
chmod 0700 \"\$stage\" \"\$stage/deploy\"
chmod 0755 \"\$stage/deploy/install-llm-proxy-deploy.sh\"
helper_actual=\"\$(sha256sum \"\$stage/deploy/llm_proxy_deploy.py\")\"
helper_actual=\"\${helper_actual%% *}\"
service_actual=\"\$(sha256sum \"\$stage/deploy/arcanada-llm-proxy-rollback@.service\")\"
service_actual=\"\${service_actual%% *}\"
timer_actual=\"\$(sha256sum \"\$stage/deploy/arcanada-llm-proxy-rollback@.timer\")\"
timer_actual=\"\${timer_actual%% *}\"
test \"\$helper_actual\" = \"\$helper_sha\"
test \"\$service_actual\" = \"\$service_sha\"
test \"\$timer_actual\" = \"\$timer_sha\"
\"\$stage/deploy/install-llm-proxy-deploy.sh\" \\
  \"\$helper_sha\" \"\$service_sha\" \"\$timer_sha\" \"\$capability_sha\"'"

# The only capability-value transfer is the SSH stdout pipe directly into gh.
# Neither command prints the pipe contents.
ssh -T "$ssh_target" "cat -- '$capability_file'" |
  gh secret set LLM_PROXY_DEPLOY_CAPABILITY \
    -e production -R "$repository"

printf 'LLM_PROXY_BOOTSTRAP_PASS main=%s helper=%s service=%s timer=%s capability_sha=%s\n' \
  "$main_sha" "$helper_sha" "$service_sha" "$timer_sha" "$capability_sha"
printf 'Root-only capability retained at %s on %s for recovery; do not read it to a terminal.\n' \
  "$capability_file" "$ssh_target"
