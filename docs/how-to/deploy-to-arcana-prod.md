# Deploy Arcanada LLM Proxy to arcana-prod

Merging or pushing `main` builds, tests, publishes and verifies an immutable
GHCR image. It does not change the production container. Production cutover is
available only through the `Production deploy` `workflow_dispatch`, protected
by the GitHub `production` environment.

## One-time root bootstrap

The production runner identity is `ci-runner-ci`. Before installing the
service broker, remove that account from the Docker group, restart
`actions.runner.Arcanada-one.arcana-prod-ci.service`, and verify that the
account and the live runner process can no longer access the Docker socket.
The installer repeats all three checks and fails closed.

From an exact reviewed checkout, calculate the bundle identities and install
the root-owned broker:

```bash
helper_sha="$(sha256sum deploy/llm_proxy_deploy.py | cut -d' ' -f1)"
service_sha="$(sha256sum deploy/arcanada-llm-proxy-rollback@.service | cut -d' ' -f1)"
timer_sha="$(sha256sum deploy/arcanada-llm-proxy-rollback@.timer | cut -d' ' -f1)"
sudo deploy/install-llm-proxy-deploy.sh \
  "${helper_sha}" "${service_sha}" "${timer_sha}"
```

The root Docker identity must already be able to pull the private
`ghcr.io/arcanada-one/arcanada-llm-proxy` package. Provision that credential
through the normal root bootstrap channel; never pass it through workflow
inputs or command arguments.

The installer creates:

- `/usr/local/sbin/arcanada-llm-proxy-deploy`;
- `arcanada-llm-proxy-rollback@.service` and `.timer`;
- root-only state below `/var/lib/arcanada-llm-proxy-deploy`;
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
mutable deploy tree.

## Cutover and health gate

Inside the root boundary the broker:

1. verifies free disk, Docker, origin ACL, current container health and the
   reviewed network/port topology;
2. pulls only the exact digest and checks both release-SHA image labels;
3. snapshots the current environment into a root-only file;
4. retains the old container and image as the rollback target;
5. arms the recurring rollback watchdog before the first destructive rename;
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
sudo /usr/local/sbin/arcanada-llm-proxy-deploy rollback <RELEASE_SHA>
```

Rollback removes the candidate, renames the retained container back to the
canonical name, starts it, and requires the same health and topology gates.
No secret value is printed or accepted as an argument.
