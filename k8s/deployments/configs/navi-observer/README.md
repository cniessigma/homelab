# Navi's Obojima rollout observer

A **read-only Obojima observer**, not general cluster administration. Authentication
uses a dedicated **long-lived, revocable credential**, bootstrapped once. Navi's
Mac can then observe rollouts without recurring token issuance from a laptop.

## Permission boundary (unchanged)

Identity: `system:serviceaccount:navi-observer:navi-observer`.

- In `obojima-tools`, read/list/watch Pods and ReplicaSets. Dynamic names require
  namespace-wide reads; labels are not a Kubernetes RBAC boundary, so every Pod
  and ReplicaSet in this namespace is readable.
- Read/list/watch only the Deployment named `obojima-tools` in `obojima-tools`
  and the Application named `obojima-tools` in `argocd`.
- No Secrets/ConfigMaps, logs, exec, attach, port-forward, workload writes, node
  APIs/proxies, RBAC changes, or credential self-management permissions are
  granted. Ordinary authenticated discovery/self-access checks may be available
  through existing Kubernetes defaults; there is no blanket `view` binding.
- `automountServiceAccountToken: false` remains set. The observer cannot read,
  replace, or create the credential Secret; its one-time export requires admin
  access. A longer credential lifetime does not broaden its resource permissions.

Use `get <kind> obojima-tools` for the two name-restricted resources. Their
list/watch calls require `--field-selector=metadata.name=obojima-tools`.
Pod/ReplicaSet collection requests need no name selector.

This does not grant Cloudflare/browser login, Talos administration, SSH, access
to other applications, or HTTP proxying into pods. Kubernetes rollout inspection
does not need those permissions; protected UI checks need a separate session.

## GitOps-managed identity and credential object

The dedicated `navi-observer` Argo Application creates the identity namespace,
account, namespaced Roles/RoleBindings, and one **metadata-only** Secret:
`navi-observer/navi-observer-credential`, type
`kubernetes.io/service-account-token`. The Secret's ServiceAccount annotation
lets Kubernetes' token controller populate its credential in the live cluster.
Sync wave 1 creates it after the account. Never put `data` or `stringData` in the
manifest or commit a populated live Secret export.

Argo ignores only this exact Secret's generated `/data` and ServiceAccount UID
annotation. `RespectIgnoreDifferences=true` preserves them during normal syncs.
The Secret is deliberately not listed in `ServiceAccount.secrets`, which would
classify it as an automatically generated legacy credential subject to cleanup.

This long-lived credential has no scheduled expiry; if leaked, it remains useful
until revoked. It stays limited to the reads above. Treat its local kubeconfig
as a password, even on the private network. No credential values are stored in
Git or chat, and no renewal daemon is needed.

## One-time bootstrap on an administrator's machine

**PR preparation does not provision access.** After explicit merge approval,
let the existing app-of-apps discover and sync `navi-observer`. Confirm that
Argo is Synced/Healthy and allow the token controller to populate the Secret.
Do not bypass GitOps with `kubectl apply`. The Argo project/controller must be
allowed to reconcile namespaced RBAC and this Secret.

On a trusted workstation with Python 3, `kubectl`, and an existing authorized
admin context, run from this repository:

```sh
python3 scripts/mint-observer-kubeconfig.py \
  --context YOUR_HOMELAB_ADMIN_CONTEXT \
  --output "$HOME/.kube/homelab-observer.yaml"
```

If the admin context is in a separate file, add
`--admin-kubeconfig /path/to/admin-kubeconfig`.

Despite its retained filename, the helper now **exports** the existing durable
credential: it does not create a TokenRequest or write any cluster resource.
It reads only the specified admin context locally and the exact named Secret
through the authenticated API, checks the Secret's identity/type/controller
metadata, then writes a new mode-0600 observer kubeconfig. It copies only the
HTTPS endpoint, embedded trusted cluster CA, optional TLS name, and observer
token—not administrator authentication, private keys, client certificates,
exec plugins, or arbitrary Secret data. New output directories are private.

The helper refuses overwrites, symlinks, insecure TLS/proxy configurations,
default/admin input kubeconfig paths, and destinations inside Git. It never
prints credentials or passes them in command-line arguments. Its JSON output
file is a valid kubeconfig even with a `.yaml` extension. The admin kubeconfig
and default context stay untouched.

The configured API endpoint is `https://elder-brain.chardalyn.nies.io:6443`.
The cluster's private CA belongs in the kubeconfig; do not substitute
`--insecure-skip-tls-verify`. Transfer **only the observer file** privately to
Navi's Mac, outside this repository, and provide its path—not its contents.
The laptop is not needed again for routine use, only deliberate credential
rotation/recovery or later permission changes requiring admin access.

## Verify with the observer, not the admin context

Always select its file explicitly; leave the default context alone:

```sh
KCFG="$HOME/.kube/homelab-observer.yaml"
kubectl --kubeconfig "$KCFG" -n argocd get application obojima-tools \
  -o jsonpath='{.status.sync.status}{" "}{.status.health.status}{" "}{.status.sync.revisions}{"\n"}'
kubectl --kubeconfig "$KCFG" -n obojima-tools \
  rollout status deployment/obojima-tools --timeout=180s
kubectl --kubeconfig "$KCFG" -n obojima-tools get pods -l app=obojima-tools \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{" "}{.status.containerStatuses[*].ready}{" "}{.status.containerStatuses[*].imageID}{"\n"}{end}'
```

Compare the observed Argo revision and running image digest with the intended
Git commit and published image; a Git push alone proves neither.

Check the permission boundary without reading secret data:

```sh
# Expected yes:
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i get deployment/obojima-tools
kubectl --kubeconfig "$KCFG" -n argocd auth can-i get application/obojima-tools
# Expected no:
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i get secrets
kubectl --kubeconfig "$KCFG" -n argocd auth can-i get secrets
kubectl --kubeconfig "$KCFG" -n navi-observer auth can-i get secret/navi-observer-credential
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i patch deployment/obojima-tools
kubectl --kubeconfig "$KCFG" -n kube-system auth can-i get pods
kubectl --kubeconfig "$KCFG" auth can-i get nodes
```

Run these after provisioning: offline helper/manifest tests do not prove live
RBAC, since existing bindings can add permissions.

## Revocation and deliberate rotation

To revoke read access, remove the RoleBindings through GitOps and confirm
protected-resource denial with the old observer file. To invalidate the credential
itself, remove the credential Secret from GitOps and confirm it is pruned. Do
not merely delete a still-declared live object: Argo self-healing recreates it.

For deliberate rotation, provision a **new Secret name**, update the named
Secret in the Argo ignore rule and helper, export the new credential privately,
and prune the old Secret through GitOps. Verify the old credential no longer
works before removing its local file. The helper refuses output overwrites;
use a new path or deliberately remove the old observer file. Never distribute
the administrator kubeconfig as a shortcut.

## Local validation and upstream references

```sh
kubectl kustomize k8s/deployments/configs/navi-observer
python3 -m unittest discover -s scripts/tests -v
python3 scripts/mint-observer-kubeconfig.py --help
git diff --check
```

Helper tests use explicit fake `kubectl`/Secret responses and a disposable test
CA; they never read a live credential or use a production kubeconfig.

- [Kubernetes ServiceAccount credential lifecycle](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)
- [Kubernetes RBAC and name restrictions](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [Argo sync options and ignored differences](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/)
