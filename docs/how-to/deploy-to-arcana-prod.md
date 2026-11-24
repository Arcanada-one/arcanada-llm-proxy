# Deploy Arcanada LLM Proxy to arcana-prod

Merging or pushing `main` builds, tests, publishes and verifies an immutable
GHCR image. It does not change the production container. Production cutover is
available only through the `Production deploy` `workflow_dispatch`, protected
by the GitHub `production` environment.

## One-time root bootstrap

The dedicated broker runner is `ci-runner-ci`, used by
`actions.runner.Arcanada-one.arcana-prod-ci.service`. It must not belong to the
Docker group and its live runner processes must not retain Docker socket access.
The installer checks this account and every process in that runner's cgroup.
The separate `ci-runner` identity serves unrelated Docker-based fleet deploys;
keep its existing Docker-group membership. The installer grants the broker
only to `ci-runner-ci`, checks that `ci-runner` has no sudo rule naming the
broker, and continues to reject broad passwordless sudo for either identity.

Apply this as part of the global runner migration, before registering or
dispatching the Dockerless production workflow:

1. retain the Docker access required by unrelated `ci-runner` deployments;
2. confirm that no provisioner recreates `/etc/sudoers.d/10-hermes-orch`
   (the Hermes agent that owned it was retired on 2026-10-08, so on a current
   host the file is normally already absent and the installer check passes);
3. verify the root-owned rollback snapshot and byte-match its archived
   `etc/sudoers.d/10-hermes-orch` member against the live file;
4. remove the live legacy sudoers file as a host-migration action and validate
   the complete sudo policy;
5. remove Docker-group membership from `ci-runner-ci`, restart its runner
   service, and verify that its account and every retained process lack Docker
   socket access; do not remove Docker access from `ci-runner`;
6. run this installer and require its account, filesystem, socket, process,
   capability-state, and effective-sudo checks to pass;
7. only then enable the `arcana-prod-ci` runner for gated cutovers.

For the SUP-0016 migration, the verified rollback artifact is
`/root/SUP-0016-runner-evacuation-prestate.Mh5MJaqY`. It must remain owned by
root with mode 0700; its `SHA256SUMS` and payloads must remain mode 0600. Run
these gates before removing the live rule:

```bash
snapshot=/root/SUP-0016-runner-evacuation-prestate.Mh5MJaqY
sudo test "$(stat -c '%U:%G %a' "${snapshot}")" = "root:root 700"
sudo test "$(stat -c '%U:%G %a' "${snapshot}/SHA256SUMS")" = "root:root 600"
sudo sha256sum -c "${snapshot}/SHA256SUMS"
sudo tar -tzf "${snapshot}/privilege-surfaces.tar.gz" |
  grep -Fx 'etc/sudoers.d/10-hermes-orch'
sudo bash -eu -o pipefail -c \
  'tar -xOf "$1" etc/sudoers.d/10-hermes-orch | cmp --silent - "$2"' \
  _ "${snapshot}/privilege-surfaces.tar.gz" \
  /etc/sudoers.d/10-hermes-orch
sudo visudo -c
sudo rm -- /etc/sudoers.d/10-hermes-orch
sudo visudo -c
sudo test ! -e /etc/sudoers.d/10-hermes-orch
sudo test ! -L /etc/sudoers.d/10-hermes-orch
```

If the host migration must be rolled back, stop both runner services first,
extract only `etc/sudoers.d/10-hermes-orch` from the verified
`privilege-surfaces.tar.gz` into a root-owned temporary file, require
`visudo -cf` on that file, then atomically install it as root:root mode 0440
and run `visudo -c` again. Never restore the broad rule while either runner
is live.

The installer idempotently changes `/opt/arcanada-llm-proxy/code` to a
root-only deploy tree and makes
`/opt/arcanada-llm-proxy/code/.env` root-only mode 0600 when those legacy
paths exist. It verifies that neither runner can read, write, or traverse
them. Deployment state is root:root mode 0700 and its capability hash is
root:root mode 0600; the installer verifies that neither runner can read,
write, or traverse the state directory or hash file. Because `ci-runner-ci`
uses `ci-runner` as a supplementary/primary group on the production host,
review group-readable files under that runner's home and work directories as
part of host bootstrap. The deployment capability must remain only in the
protected workflow environment and root-only broker state; never save it in a
group-readable runner file or workspace. Group membership alone does not grant
access to the root-only state, but it does grant whatever access file mode bits
and ACLs grant to group `ci-runner`.

The installer never removes or archives this global sudoers file; it
fails closed unless the global runner migration has already removed it.
It installs only the `ci-runner-ci` broker rule and audits the effective sudo
policy for both identities. Any remaining wildcard service control fails the
install.

From an exact reviewed checkout, calculate the bundle identities and install
the root-owned broker. The fourth argument is the SHA-256 hash of the exact
64-lowercase-hex deployment capability, not the capability itself:

```bash
helper_sha="$(sha256sum deploy/llm_proxy_deploy.py | cut -d' ' -f1)"
service_sha="$(sha256sum deploy/arcanada-llm-proxy-rollback@.service | cut -d' ' -f1)"
timer_sha="$(sha256sum deploy/arcanada-llm-proxy-rollback@.timer | cut -d' ' -f1)"
sudo deploy/install-llm-proxy-deploy.sh \
  "${helper_sha}" "${service_sha}" "${timer_sha}" "<CAPABILITY_SHA256>"
```

Store the unhashed value only as the protected production environment secret
`LLM_PROXY_DEPLOY_CAPABILITY`. The installer stores only its hash in the
root-owned mode-0600 file
`/var/lib/arcanada-llm-proxy-deploy/deploy-capability.sha256`.

The root Docker identity must already be able to pull the private
`ghcr.io/arcanada-one/arcanada-llm-proxy` package. Use a GitHub package-reader
credential with only `read:packages` access to this package, provisioned through
the root bootstrap channel. Store it in root's Docker credential store (normally
`/root/.docker/config.json`, root:root mode 0600 under a root:root mode-0700
`/root/.docker` directory). Supply the token to `docker login` only through
`--password-stdin`; never pass it through workflow inputs, command arguments,
the runner environment, or a workspace file. This keeps the existing broker
design: root pulls the exact digest after validating the CI publication proof;
the runner never receives Docker socket access or package-pull credentials.

The installer creates:

- `/usr/local/sbin/arcanada-llm-proxy-deploy`;
- `arcanada-llm-proxy-rollback@.service` and `.timer`;
- root-only state below `/var/lib/arcanada-llm-proxy-deploy`;
- the root-only deployment-capability hash;
- a `NOSETENV` sudo rule limited to the broker grammar.

It does not restart the application.

## Dispatch a release

Wait for the main `CI` run to finish successfully. Read its
`llm-proxy-image-<release-sha>` artifact, then dispatch `Production deploy`
with:

- `release_sha`: the exact 40-character main commit;
- `image_digest`: the exact `sha256:<64 lowercase hex>` digest;
- `ci_run_id`: the successful main CI run that published the digest.

The guard binds all three values to the successful push run and its immutable
artifact. The Dockerless production job can call only the installed broker; it
does not read the Docker socket, the production environment file, or the
mutable deploy tree. The protected production jobs pipe the capability over
stdin for `deploy`, `commit`, and `rollback`; the secret never appears in
arguments or output. Read-only `verify-bundle`, `preflight`, `health`, and
`status` calls do not consume it.

## Cutover and health gate

Inside the root boundary the broker:

1. verifies free disk, Docker, origin ACL, current container health and the
   reviewed network/port topology;
2. pulls only the exact digest and checks both release-SHA image labels;
3. snapshots the current environment into a root-only file;
4. retains the old container and image as the rollback target;
5. enables and starts the recurring rollback watchdog before the first
   destructive rename, so the timer survives a host reboot;
6. starts the candidate on loopback port 4000 and both reviewed networks;
7. requires container health plus `GET /health = 200`.

The final workflow job performs another health check and writes the root-only
commit record. Until that commit exists, a failed health probe or the
ten-minute deadline restores the retained container automatically.

## Inspect or roll back

The workflow reports the sanitized broker status. For incident response, root
can inspect or restore the exact retained release:

```bash
sudo /usr/local/sbin/arcanada-llm-proxy-deploy status <RELEASE_SHA>
printf '%s\n' "${LLM_PROXY_DEPLOY_CAPABILITY}" |
  sudo /usr/local/sbin/arcanada-llm-proxy-deploy rollback <RELEASE_SHA>
```

Rollback verifies that the retained container uses `old_image_id` before it
removes the candidate. It then renames the retained container back to the
canonical name, starts it, checks the restored image identity plus the same
health and topology gates, records success, and only then disables the
watchdog. Failed restores leave the timer enabled for retry. No secret value
is printed or accepted as an argument.
