# Thingie

## Cluster-wide diagnostic access

See [Navi's cluster-wide diagnostic observer](k8s/deployments/configs/navi-observer/README.md)
for broad diagnostic reads (including Secret contents) and the one-time durable
kubeconfig bootstrap. Generated credentials must remain outside this repository.
The declared role excludes workload writes and active exec/proxy endpoints;
Secret contents may nevertheless expose more privileged credentials.

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
