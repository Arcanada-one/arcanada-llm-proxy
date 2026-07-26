# Deploy Arcanada LLM Proxy to arcana-prod

Merging or pushing `main` builds, tests, publishes and verifies an immutable
GHCR image. It does not change the production container. Production cutover is
available only through the `Production deploy` `workflow_dispatch`, protected
by the GitHub `production` environment.

## One-time root bootstrap

The current and legacy production runner identities are `ci-runner-ci` and
`ci-runner`. Before installing the service broker, remove both `ci-runner` and
`ci-runner-ci` from the Docker group. Restart every Actions runner service
whose process runs as either account, including
`actions.runner.Arcanada-one.arcana-prod-ci.service`, then verify that neither
the accounts nor any retained live process can access the Docker socket. The
installer removes any lingering Docker-group entries, then repeats the
account, socket, and live-process checks for both identities and fails closed.

Apply this as part of the global runner migration, before registering or
dispatching the Dockerless production workflow:

1. stop routing production work to the legacy `ci-runner` identity;
2. disable the global provisioner that recreates
   `/etc/sudoers.d/10-hermes-orch`;
3. remove Docker-group membership from both runner identities and restart
   every retained runner process;
4. run this installer and require its account, filesystem, socket, process,
   and effective-sudo checks to pass;
5. only then enable the `arcana-prod-ci` runner for gated cutovers.

The installer idempotently changes `/opt/arcanada-llm-proxy/code` to a
root-only deploy tree and makes
`/opt/arcanada-llm-proxy/code/.env` root-only mode 0600 when those legacy
paths exist. It verifies that neither runner can read, write, or traverse
them. It also moves the known broad
`/etc/sudoers.d/10-hermes-orch` rule to the root-only rollback artifact
`/var/lib/arcanada-llm-proxy-deploy/disabled-sudoers/10-hermes-orch`, installs
only the `ci-runner-ci` broker rule, and audits the effective sudo policy for
both identities. Any remaining wildcard service control fails the install.

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
`ghcr.io/arcanada-one/arcanada-llm-proxy` package. Provision that credential
through the normal root bootstrap channel; never pass it through workflow
inputs or command arguments.

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
