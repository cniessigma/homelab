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
- Prefer updating manifests/config in repo over imperative `kubectl` edits.
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

## Editing Rules
- Keep changes minimal and targeted.
- Preserve existing YAML style and indentation.
- Avoid unrelated refactors.
- If changing Talos registry settings, update workload image references consistently.

## Validation Checklist Before Finishing
- `bash -n talos/apply-all.sh` for script syntax.
- Confirm manifest references are consistent (registry host, paths, names).
- If Talos config changed, regenerate `.rendered` artifacts before apply.
