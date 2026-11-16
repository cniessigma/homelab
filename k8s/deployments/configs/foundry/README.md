# Foundry backups: repository isolation and cutover

Foundry's existing PVCs and nested mounts are unchanged. Each PVC needs a separate
Restic repository: VolSync does not support sharing a repository across sources.
The legacy sources saved some snapshots but failed in `forget` on repository locks;
shared hostname/path metadata also makes retention and automatic restore selection
ambiguous. A saved snapshot is not proof of a recoverable, complete Foundry instance.

## Resources

- `backup.yaml`: original `foundry-{worlds,base,modules}-backup` sources, permanently
  paused, still referencing the unchanged `foundry-restic-secret` archive.
- `backup-isolated.yaml`: new `foundry-{worlds,base,modules}-isolated-backup` sources,
  each referencing its own `foundry-{worlds,base,modules}-restic-secret`.
- `restic-{worlds,base,modules}-secret.enc.yaml`: SOPS-encrypted configurations using
  the existing project age recipient and object-storage access credentials. Each
  has its own repository prefix and independently generated encryption password.
  Preserve these encrypted files and the age recovery key: losing a password makes
  that repository unrecoverable.
- New repository prefixes are siblings of, not children of, the legacy repository:
  the existing prefix plus `-isolated-v1/{worlds,base,modules}`. Prefix values and
  credentials remain encrypted. No existing snapshots are moved or deleted.
- Existing hourly starts remain worlds `:00`, base `:15`, modules `:30`. Retention,
  Direct copy, cache storage class, and seven-day pruning policy are unchanged.

## Stage 1: approved merge pauses the old writers

**All six sources are intentionally paused in this PR. Merging it alone does not
resume working backups.** This creates an explicit cutover gate rather than relying
on Argo resource ordering to prove old movers have stopped. The application itself
stays running; its Deployment, PVCs and mounts are not modified. New source names
also keep their Jobs, caches, and status separate from the retired sources.

After an approved merge, verify Argo `foundry` is Synced at the intended commit.
Then, with the explicit homelab observer kubeconfig:

```sh
KCFG="$HOME/.kube/homelab-observer.yaml"
kubectl --kubeconfig "$KCFG" --request-timeout=10s -n foundry \
  get replicationsources.volsync.backube \
  -o 'custom-columns=NAME:.metadata.name,PAUSED:.spec.paused,PVC:.spec.sourcePVC,SECRET:.spec.restic.repository'
kubectl --kubeconfig "$KCFG" --request-timeout=10s -n foundry \
  get jobs -o 'custom-columns=NAME:.metadata.name,PARALLELISM:.spec.parallelism,ACTIVE:.status.active,SUCCEEDED:.status.succeeded,FAILED:.status.failed'
kubectl --kubeconfig "$KCFG" --request-timeout=10s -n foundry \
  get pods -l app.kubernetes.io/created-by=volsync \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,DELETING:.metadata.deletionTimestamp'
```

Confirm all legacy sources are paused, legacy Jobs have zero parallelism/no active
movers, and no legacy mover container is running or terminating. Completed/failed
historical pods can remain. In VolSync 0.15, pause changes Job parallelism to zero;
it is **not** a Restic graceful-pause command, so observe actual pod termination.
If this gate fails, stop and diagnose; do not delete Jobs, directly patch Argo-managed
objects, stop Foundry, or bypass GitOps without separate approval. Do not leave this
staged pause unattended: proceed to approved activation after verifying the gate.

## Stage 2: activate only the isolated sources through Git

After the Stage 1 inactivity check, make and approve a follow-up Git change that
sets `spec.paused: false` on only the three objects in `backup-isolated.yaml`.
**Leave all original sources in `backup.yaml` paused.** Update the phase-one tests
in `test_foundry_backup_isolation.py` to expect only the isolated sources active;
retain the legacy-pause and repository-isolation assertions. Do not change the old Secret
or enable unlock/retention against its archive. Push/merge through the normal GitOps
path, then verify Argo at that follow-up commit. No direct `kubectl apply`/patch,
reconciliation pause, automated hooks, alerts, or force-unlock are part of this fix.

VolSync initializes each new repository on its first scheduled backup; existing
locks in the old repository do not need to be removed to start these new ones.
New movers may provision separate cache PVCs. Keep legacy caches and backup archive
until new backups have been validated; cleanup is a separate optional operation.
Initial backups may take longer and require new uploads because deduplication does
not carry across repository boundaries. Confirm actual first writes succeed: an
empty-prefix listing is read-only discovery, not proof of Put/Delete authorization.

## Verification and recovery

- For each new source, require an advancing `status.lastSyncTime`,
  `status.latestMoverStatus.result: Successful`, and a fully successful mover Job,
  not merely `Synchronizing`, a Running pod, Argo Healthy, or `snapshot saved`.
- Read source status and bounded mover logs with the observer credential. Never
  print Secret values. Confirm cleanup/maintenance also succeeds; first-cycle prune
  may run because the new sources have no prior prune status.
- Test a restore of each isolated repository into a **new empty scratch PVC/target**
  with an authorized operator; never restore over the live Foundry PVCs. Restore
  mapping is base -> `/data`, worlds -> `/data/Data/worlds`, and the modules PVC's
  `modules`/`systems` subdirectories -> the corresponding Foundry child mounts.
- Preserve all three repositories for complete coverage. The base mover does not
  inherit Foundry's child mounts. Restore approval must cover any temporary restore
  resources and their cleanup; observer access alone cannot create those resources.
- This PR fixes repository isolation, not application consistency: Direct mode reads
  live files. Independently timed copies are not an atomic Foundry backup. A separate
  approved quiesced/application-aware backup procedure is needed for that guarantee.
- On a failed new backup, pause the affected **new** source through Git and diagnose
  without modifying source PVC data. Do not blindly revert to enabled legacy writers:
  reverting this PR wholesale would re-enable the unsupported shared-repository setup.
- For emergency recovery from the legacy mixed archive, first inventory snapshot
  contents and identify the source dataset. Do not trust an automatic `latest` restore
  or run `forget`/`prune` against it. Any stale-lock repair needs a separately approved
  maintenance window with all repository users verified inactive.

## Offline validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -v
```

Tests use local `kubectl kustomize` plus Ruby stdlib YAML/JSON. Foundry backup tests
render the actual **non-secret** resources with `KUBECONFIG=/dev/null`, and separately
check KSOPS file references, encrypted credential fields, and the expected public
age recipient. They do not decrypt SOPS files, contact Kubernetes/object storage,
or prove a backup/restore. Full KSOPS rendering requires the existing age identity
in the operator/Argo environment; no private key is copied to the agent.

Upstream contracts:
- VolSync 0.15 per-PVC repository isolation and unlock documentation:
  https://github.com/backube/volsync/blob/v0.15.0/docs/usage/restic/index.rst
- Mover backup/forget/restore selection:
  https://github.com/backube/volsync/blob/v0.15.0/mover-restic/entry.sh
- Foundry manual-copy backup guidance:
  https://foundryvtt.com/article/user-data-backup/
