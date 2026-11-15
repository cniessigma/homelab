# Navi's cluster-wide diagnostic observer

A dedicated identity for **broad Kubernetes diagnostics across all namespaces**,
including backup investigation, logs, and **Secret contents explicitly approved
by Christopher**. Authentication remains a one-time, long-lived, revocable
credential; no recurring laptop-mediated renewal is needed.

## What the RBAC grants

Identity: `system:serviceaccount:navi-observer:navi-observer`.

A ClusterRoleBinding grants get/list/watch on explicitly listed top-level
resources across API groups, including:

- Workloads, Jobs/CronJobs, Pods, events, namespaces, ConfigMaps and Secrets.
- Nodes and metrics resources; storage classes, PVs/PVCs, CSI objects and
  volume/group snapshots.
- All Argo Applications/ApplicationSets/AppProjects, not just Obojima.
- VolSync ReplicationSources/ReplicationDestinations, MetalLB resources, and
  listed common backup, certificate, and monitoring operator resources when
  installed (including Velero).
- Pod logs through **get on `pods/log`** and the API's listed health/version
  endpoints through GET. A standard resource GET already includes its status;
  extra `*/status` grants are unnecessary for these diagnostic reads.

No workload/RBAC mutations, impersonation, token creation, exec, attach,
port-forward, or pod/service/node proxy subresources are granted by this role.
Node object reads do not grant Talos administration or kubelet proxy access.

**Important: Secret access is not a strict read-only security boundary.** Secrets
can contain service-account tokens, repository passwords, and external-system
credentials with stronger privileges. ConfigMaps, workload specs and logs can
also contain sensitive values. The user explicitly accepted that exposure for
this homelab. Do not dump these values into chat, tool output, Git, or PRs; prefer
metadata/status checks and process required credential values privately. Reading
credentials does not itself authorize unrequested writes, restores, or use of a
more privileged identity.

The resource list in `rbac.yaml` is intentionally explicit: RBAC cannot subtract
unsafe subresources from `resources: ['*']`, and even **get on `nodes/proxy` is
not read-only**. API groups are unrestricted, but a newly installed resource
plural not in the list remains denied until reviewed and added. Thus this is
broad diagnostic coverage, not an automatically unlimited future-API grant.

The previous Obojima-only Roles/RoleBindings are replaced by the ClusterRole and
ClusterRoleBinding. The ServiceAccount/credential identity is unchanged, so any
already-exported credential picks up the new permissions after GitOps sync.
`automountServiceAccountToken: false` and the lack of credential-management write
permissions remain unchanged. The kubeconfig's default namespace is still
`obojima-tools` for convenience; use `-A` or `-n` for other namespaces.

## GitOps-managed durable credential

The `navi-observer` Argo Application creates its namespace, ServiceAccount,
ClusterRole/ClusterRoleBinding, and one **metadata-only** Secret:
`navi-observer/navi-observer-credential`, type
`kubernetes.io/service-account-token`. Kubernetes' token controller fills the
credential in-cluster. Sync wave 1 creates the Secret after its account. Never
commit `data`, `stringData`, or a populated live Secret export.

Argo ignores only this exact Secret's generated `/data` and ServiceAccount UID
annotation, and `RespectIgnoreDifferences=true` preserves those fields during
sync. The Secret is not listed in `ServiceAccount.secrets`, avoiding automatic
legacy-token classification/cleanup.

The credential has no scheduled expiry; protect its local kubeconfig as a
password until revoked. No token values live in Git/chat and no renewal daemon
is needed. This Kubernetes identity does not grant Cloudflare/browser login,
SSH or Talos authentication. Reads outside Kubernetes (such as Restic object
storage) need separate authentication/authorization even if connection details
are available in a Secret.

## One-time bootstrap

**An open PR does not provision access.** After explicit merge approval, let
app-of-apps sync `navi-observer` and wait for its Secret to be populated. Do not
bypass GitOps with `kubectl apply`. The Argo project/controller must permit the
cluster-scoped RBAC resources as well as the namespaced account/Secret.

On an already-authorized admin workstation, from this repository:

```sh
python3 scripts/mint-observer-kubeconfig.py \
  --context YOUR_HOMELAB_ADMIN_CONTEXT \
  --output "$HOME/.kube/homelab-observer.yaml"
```

Add `--admin-kubeconfig /path/to/admin-kubeconfig` if needed. Despite its retained
filename, this helper only **exports** the existing credential: it reads the
exact Secret and writes no cluster resource or TokenRequest. It validates the
Secret identity/type/controller annotations and captures only trusted HTTPS
endpoint/CA settings plus observer authentication—not administrator users,
private keys, client certificates, exec plugins, or arbitrary Secret data.

The separate file is mode 0600, with private new directories. The helper refuses
overwrite/symlink/Git/default-admin-path destinations and insecure TLS/proxy
configuration; it never prints credentials or changes the default context.
JSON syntax is a valid kubeconfig even with a `.yaml` extension.

The configured endpoint is `https://elder-brain.chardalyn.nies.io:6443`; carry its
private CA in the kubeconfig rather than bypassing TLS. Transfer **only the
observer file** privately to Navi's Mac outside this repository and provide its
path, not its contents. Routine diagnostics then do not need the laptop.

## Diagnostic examples after provisioning

Always specify the observer file, leaving the default context alone:

```sh
KCFG="$HOME/.kube/homelab-observer.yaml"
kubectl --kubeconfig "$KCFG" get nodes
kubectl --kubeconfig "$KCFG" get pods,jobs,cronjobs,pvc -A
kubectl --kubeconfig "$KCFG" get pv,storageclasses
kubectl --kubeconfig "$KCFG" get replicationsources.volsync.backube -A
kubectl --kubeconfig "$KCFG" get applications.argoproj.io -A
kubectl --kubeconfig "$KCFG" -n smi-ost-bot logs smi-ost-bot-0 -c backup --tail=50
```

Treat logs as sensitive. For backups, inspect both the local SQLite snapshot
sidecar's verified timestamp and VolSync's last successful sync/status; Running
pods do not prove recent backups. Read access to status and logs does not prove
backup data is restorable. Testing a restore, mounting backup data in a new pod,
changing schedules, or modifying a repository still requires a separate approved
workflow, not a write grant hidden inside this observer role.

For Obojima, compare Argo's revision/health and the running image digest with
the intended commit/image. Check effective permissions without dumping values:

```sh
# Expected yes, across namespaces:
kubectl --kubeconfig "$KCFG" -n kube-system auth can-i get secrets
kubectl --kubeconfig "$KCFG" -n smi-ost-bot auth can-i get pods --subresource=log
kubectl --kubeconfig "$KCFG" auth can-i get nodes
# Expected no through this identity's declared RBAC:
kubectl --kubeconfig "$KCFG" -n obojima-tools auth can-i patch deployment/obojima-tools
kubectl --kubeconfig "$KCFG" -n kube-system auth can-i get pods --subresource=exec
kubectl --kubeconfig "$KCFG" -n kube-system auth can-i create pods --subresource=exec
kubectl --kubeconfig "$KCFG" auth can-i get nodes --subresource=proxy
kubectl --kubeconfig "$KCFG" -n navi-observer auth can-i create serviceaccounts --subresource=token
```

Run positive and negative checks after provisioning. Offline tests cannot prove
effective live authorization, because RBAC is additive and other bindings may
add rights; Secret-derived credentials are another separate escalation route.

## Revocation and deliberate rotation

Revoke these reads by removing the ClusterRoleBinding through GitOps and
confirming protected-resource denial. Invalidate the credential itself by
removing its Secret from GitOps and confirming pruning; direct deletion of a
still-declared live object lets Argo recreate it.

For rotation, use a **new Secret name**, update the exact Argo ignore rule and
helper, export privately, then prune the old Secret and verify old-credential
denial. Use a new local output path or deliberately remove the old observer
file; never copy an admin kubeconfig as a shortcut.

## Local validation and references

```sh
kubectl kustomize k8s/deployments/configs/navi-observer
python3 -m unittest discover -s scripts/tests -v
python3 scripts/mint-observer-kubeconfig.py --help
git diff --check
```

Tests are offline. Helper tests use synthetic Secret responses and a disposable
OpenSSL test CA. RBAC tests render with `kubectl kustomize` and parse YAML using
Ruby's standard library, so the full test suite requires Python 3, OpenSSL,
`kubectl`, and Ruby (no PyYAML installation). Neither suite contacts a cluster
or reads live credentials. These tests do not certify the actual Argo rollout,
effective permissions, or backup health.

- [Kubernetes RBAC escalation risks, including Secrets and nodes/proxy](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)
- [ServiceAccount credential lifecycle](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)
- [Argo sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/)
