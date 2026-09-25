# AGENTS.md

## Purpose
This repo manages a Talos-based Raspberry Pi Kubernetes homelab using GitOps (ArgoCD + Kustomize + Helm + SOPS/KSOPS).

## High-Level Layout
- `k8s/`: Kubernetes manifests and app configs reconciled by ArgoCD.
- `talos/`: Talos cluster config, secrets, generated machine configs, and helper scripts.
- `k8s/deployments/configs/registry/`: In-cluster Docker registry Helm chart config.
- `k8s/deployments/configs/smi-ost-bot/`: Example workload pulling from local registry.

## Source of Truth
- Treat Git as source of truth.
- Deploy Argo-managed resources through Git: commit and push manifests/config, then let Argo reconcile.
- Do not directly apply or patch Argo-managed resources, or pause reconciliation, without explicit user approval. Authorization to deploy alone does not authorize bypassing GitOps.
- Temporary database-transfer and maintenance operations are separate from declarative deployment changes. Preserve rollback data, remove temporary resources when finished, and restore reconciliation if it was explicitly paused.
- Do not hand-edit generated files in `talos/.rendered/`; regenerate instead.

## Talos Workflow
- Generate machine configs:
  - `talhelper genconfig --config-file talos/talconfig.yaml --no-gitignore --out-dir talos/.rendered --secret-file talos/talsecret.enc.yaml`
- Apply configs to all nodes:
  - `./talos/apply-all.sh`
- `talos/apply-all.sh` resolves node FQDNs (`<node>.chardalyn.nies.io`) with `dig` and falls back to `ipAddress` in `talos/talconfig.yaml`.

## Registry Behavior (Important)
- Talos registry access is configured in `talos/talconfig.yaml`.
- Use `machine.registries.mirrors` with explicit `http://` endpoints for local non-TLS registry access.
- Do not rely on `tls.insecureSkipVerify` alone for plain-HTTP registries.
- Preferred image host is `docker.nies.io:5000` (DNS points to local registry IP).

## Secrets and Encryption
- Encrypted secrets use SOPS (`*.enc.yaml`).
- KSOPS generator is used in Kustomizations.
- Never commit decrypted secret files (for example `.decrypted~secret.enc.yaml`).
- Keep plaintext secret values out of tool output, logs, and chat. Process them locally without printing values, and persist repository changes only as SOPS-encrypted files.

## SMI OST Bot Storage and Backups
- Application code and product context live in `cniessigma/smi_ost_bot` (`~/smi_ost_bot` locally). See that repo's `AGENTS.md` and [project context](https://github.com/cniessigma/smi_ost_bot/blob/main/docs/PROJECT_CONTEXT.md) when working on bot or ranking UI behavior.
- The bot uses SQLite WAL mode. Keep the live database on local storage, not NFS.
- The current database PVC is `smi-ost-db-local`, backed by a local PV on `jericho` at `/var/lib/kubelet/smi-ost-bot`. This survives reboots but depends on that node's disk and is lost if its Talos EPHEMERAL partition is wiped. Node replacement requires a restore; there is no automatic storage failover.
- The `backup.py` sidecar uses SQLite's online backup API, verifies integrity, and publishes hourly timestamped snapshots to the NFS PVC `smi-ost-backups`, retaining 30 days.
- VolSync replicates the snapshot PVC to the dedicated Backblaze Restic repository `sigma-homelab/smi-ost-bot`. Keep it separate from the Foundry repository.
- Do not copy live SQLite database files as a backup. Restore only completed `.db` snapshots, never `.tmp` files, and stop all writers before replacing a database.
- See `k8s/deployments/configs/smi-ost-bot/README.md` for recovery instructions.

## Editing Rules
- Keep changes minimal and targeted.
- Preserve existing YAML style and indentation.
- Avoid unrelated refactors.
- If changing Talos registry settings, update workload image references consistently.

## Validation Checklist Before Finishing
- `bash -n talos/apply-all.sh` for script syntax.
- Confirm manifest references are consistent (registry host, paths, names).
- If Talos config changed, regenerate `.rendered` artifacts before apply.
- After deployment, verify Argo is Synced/Healthy at the intended Git commit and the workload starts successfully.
- When database or backup settings change, verify a snapshot can be restored and passes SQLite integrity checks, and confirm a successful VolSync sync rather than relying on pod readiness alone.
