# Navi's Obojima rollout observer

This is a **read-only Obojima observer**, not general cluster administration.
The dedicated Argo CD Application manages the ServiceAccount and RoleBindings
through GitOps. No credentials, token Secrets, pods, or ClusterRoleBindings are
created by these manifests.

## Permission boundary

Identity: `system:serviceaccount:navi-observer:navi-observer`.

- In `obojima-tools`, read/list/watch Pods and ReplicaSets. These dynamic names
  are namespace-scoped; Kubernetes RBAC does not restrict this access by an
  `app` label, so all Pods and ReplicaSets in this namespace are readable.
- In `obojima-tools`, read/list/watch only the Deployment named `obojima-tools`.
- In `argocd`, read/list/watch only the Application named `obojima-tools`.
- No Secrets, ConfigMaps, logs, exec, attach, port-forward, workload writes,
  node resources/proxies, RBAC changes, or token minting permissions are granted.
  Ordinary authenticated API discovery/self-access checks may be available
  through Kubernetes' existing default bindings; there is no blanket `view` role.
- The ServiceAccount has `automountServiceAccountToken: false`. It has no
  self-renewal permission. An administrator must issue each expiring token.

For the two name-restricted resource kinds, use `get <kind> obojima-tools`.
A list/watch must include `--field-selector=metadata.name=obojima-tools`; listing
all Applications or Deployments is intentionally denied. Listing Pods and
ReplicaSets needs no name selector.

This does not grant Cloudflare Access/browser login, HTTP proxying into pods,
Talos API access, SSH, or access to other applications. Inspecting rollout and
image IDs through Kubernetes does not require those permissions. Live protected
UI checks still need a separately authorized application session.

## Bootstrap: merge/sync, then mint on an administrator's machine

Preparing the PR does not provision access. Once its merge is explicitly
approved, the existing `app-of-apps` discovers `navi-observer`. Argo creates its
namespace and reconciles the account plus Roles/RoleBindings in the existing
`obojima-tools` and `argocd` namespaces. Do not bypass GitOps with `kubectl apply`.
Confirm `navi-observer` is Synced/Healthy and its resources exist before minting.
The normal Argo project/controller must be allowed to reconcile namespaced RBAC.

On a workstation with Python 3, `kubectl` supporting `create token`, and an
existing authorized admin context, run from this repository:

```sh
python3 scripts/mint-observer-kubeconfig.py \
  --context YOUR_HOMELAB_ADMIN_CONTEXT \
  --output "$HOME/.kube/homelab-observer.yaml"
```

If the admin context is in a separate file, also provide
`--admin-kubeconfig /path/to/admin-kubeconfig`.

The helper:

- Uses the specified context explicitly, without changing the current context
  or writing to the admin kubeconfig.
- Carries over only the HTTPS server, embedded cluster CA, and optional TLS
  server name. It refuses insecure TLS/proxy configuration and never copies
  administrator users, private keys, client certificates, or exec plugins.
- Requests a **one-hour** TokenRequest for this ServiceAccount. Kubernetes may
  adjust that lifetime; use the actual expiration reported by the helper.
- Writes only the CA/endpoint plus the observer token to a new mode-0600 file.
  It refuses overwrites, symlinks, the default kubeconfig, input kubeconfig paths,
  and destinations inside a Git working tree. The generated JSON is a valid
  kubeconfig even when the filename ends in `.yaml`.
- Captures credentials internally; neither the token nor admin configuration is
  printed or passed as a command-line argument. The only cluster write is the
  administrator's TokenRequest; it does not apply RBAC or create a legacy token.

The configured endpoint is `https://elder-brain.chardalyn.nies.io:6443`.
Network reachability alone is not enough: the generated file must contain its
trusted cluster CA and a valid observer token. Do not use
`--insecure-skip-tls-verify` as a substitute for the CA. The API server's CA may
be private and absent from the Mac's system trust store; embedding the CA in
this kubeconfig is the intended solution.

If the helper is run on another machine, transfer **only the observer file** to
Navi's Mac using a private channel, outside this repository. Tell Navi the file
path; never paste its contents or the admin kubeconfig into chat.

## Verify with the observer, not the admin context

Always select the observer file explicitly; leave the default context alone:

```sh
KCFG="$HOME/.kube/homelab-observer.yaml"
kubectl --kubeconfig "$KCFG" -n argocd get application obojima-tools \
  -o jsonpath='{.status.sync.status}{" "}{.status.health.status}{" "}{.status.sync.revisions}{"\n"}'
kubectl --kubeconfig "$KCFG" -n obojima-tools \
  rollout status deployment/obojima-tools --timeout=180s
kubectl --kubeconfig "$KCFG" -n obojima-tools get pods -l app=obojima-tools \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{" "}{.status.containerStatuses[*].ready}{" "}{.status.containerStatuses[*].imageID}{"\n"}{end}'
```

Compare Argo's observed revision with the intended homelab commit and compare
the running image digest with the published manifest. A Git push by itself does
not prove either condition.

Check the permission boundary without reading secret data:

```sh
# Expected yes:
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i get deployment/obojima-tools
kubectl --kubeconfig "$KCFG" -n argocd auth can-i get application/obojima-tools
# Expected no:
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i get secrets
kubectl --kubeconfig "$KCFG" -n argocd auth can-i get secrets
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i patch deployment/obojima-tools
kubectl --kubeconfig "$KCFG" -n kube-system auth can-i get pods
kubectl --kubeconfig "$KCFG" auth can-i get nodes
```

These must be exercised against the live cluster after provisioning. Offline
manifest/helper tests are not evidence of effective live RBAC: other existing
bindings can add permissions.

## Expiry and revocation

After expiry, mint a fresh file on the admin workstation. The helper intentionally
refuses to overwrite the old one; use a fresh output path or deliberately remove
the expired observer file first. Transfer only the new observer file, never the
admin configuration. There is no renewal daemon or long-lived token in Git.

To revoke through GitOps, remove these RoleBindings (and the account when it is
no longer needed) from this application's manifests and let Argo reconcile.
Confirm denial using the former observer credential; do not merely wait for an
old token's requested expiry. Deleting resources directly while they remain in
Git can cause Argo self-healing to recreate them.

## Local validation and upstream references

```sh
kubectl kustomize k8s/deployments/configs/navi-observer
python3 -m unittest discover -s scripts/tests -v
python3 scripts/mint-observer-kubeconfig.py --help
git diff --check
```

The helper tests use explicit fake `kubectl` responses and a disposable test CA;
they never request a live token or use a production kubeconfig.

- [Kubernetes RBAC and name restrictions](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [TokenRequest through kubectl create token](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_create/kubectl_create_token/)
- [Kubeconfig organization and cluster trust](https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/)
