# Thingie

## Read-only deployment observation

See [Navi's Obojima observer](k8s/deployments/configs/navi-observer/README.md)
for the scoped GitOps RBAC and expiring-kubeconfig bootstrap. Credentials must
remain outside this repository; this does not grant general cluster access.

## Talos configuration

Ensure sops is installed, and that you have `secrets/age.key` present

```
source .env
talhelper genconfig \
    --config-file talos/talconfig.yaml \
    --no-gitignore \
    --out-dir talos/.rendered \
    --secret-file talos/talsecret.enc.yaml
```
