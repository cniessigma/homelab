# SMI OST bot

The single StatefulSet replica runs the pinned bot image with its database on
`smi-ost-db-local`. The local PV is bound to `jericho`, at
`/var/lib/kubelet/smi-ost-bot` on Talos's persistent EPHEMERAL partition. This
survives pod restarts and node reboots, but not a disk failure or a Talos reset
that wipes EPHEMERAL. A failed node requires restoring onto a replacement node;
the pod cannot automatically move its local database to another node.

SQLite uses WAL mode, so the live database must stay off NFS. The Python sidecar
uses SQLite's online backup API every hour, checks integrity, and publishes
completed `smi-ost-<UTC timestamp>.db` files atomically to the NFS claim
`smi-ost-backups`. Local snapshots are retained for 30 days. Only `.db` files are
restore candidates; `.tmp` files are incomplete and must never be restored.
A backup failure exits the sidecar so Kubernetes reports/restarts it.

VolSync backs up this snapshot claim at minute 45 of each hour to the separate
Restic repository `s3:https://s3.us-west-002.backblazeb2.com/sigma-homelab/smi-ost-bot`.
Retention is 24 hourly, 30 daily, 26 weekly, and 12 monthly snapshots. Credentials
and the unique repository password are stored in `restic-secret.enc.yaml` using
SOPS. Because the local and offsite schedules are independent, the newest offsite
database can be approximately two hours old.

## Health checks

```sh
kubectl -n smi-ost-bot get pods,pvc,replicationsources
kubectl -n smi-ost-bot logs smi-ost-bot-0 -c smi-ost-bot --tail=30
kubectl -n smi-ost-bot logs smi-ost-bot-0 -c backup --tail=30
kubectl -n smi-ost-bot get replicationsource smi-ost-backup -o yaml
```

Check the sidecar's latest verified timestamp and VolSync's `status.lastSyncTime`;
a Running pod alone does not prove recent offsite backups. No alert delivery is
configured by these manifests.

## Recovery

1. Pause Argo reconciliation with the application's
   `argocd.argoproj.io/skip-reconcile=true` annotation, then scale the StatefulSet
   to zero. Ensure its pod is gone before modifying the database.
2. Mount `smi-ost-backups` and `smi-ost-db-local` in a maintenance pod on `jericho`.
   If recovering from Backblaze, use Restic with `smi-ost-restic-secret` to restore
   a snapshot into an empty directory first. Select a completed `.db` file.
3. Run SQLite `PRAGMA integrity_check` against the selected copy. Preserve the
   current database plus any `-wal` and `-shm` files together for rollback.
4. Install the selected backup as `smi_ost.db` in the local volume. Remove old
   `smi_ost.db-wal` and `smi_ost.db-shm` from the active directory only after the
   old set is preserved and all writers are stopped. Do not mix old WAL files
   with a restored database.
5. Scale to one and verify the bot connects and the backup sidecar succeeds.
   Remove the skip-reconcile annotation and verify Argo is Synced/Healthy.

For replacement-node recovery, create the directory on persistent node storage,
update the PV path/node affinity in Git, and rebind a replacement claim/PV before
restoring. The PV has Retain reclaim policy. The NFS snapshot claim opts out of
Argo pruning; deleting its PVC manually may still delete its backing NFS data.

## Migration record

On 2026-09-20 the source was `/home/ec2-user/smi_ost_bot/db/smi_ost.db` on
`54.68.50.167`. Docker was stopped and disabled with no bot process running.
SQLite's backup API produced an integrity-checked copy containing 16 submissions,
82 votes, and 37 tracked messages. The source database is retained. The original
homelab NFS claim `smi-ost-db-smi-ost-bot-0` and immutable claim template are kept
for rollback; that volume is no longer mounted by the bot. A trailing whitespace
character was removed from the Discord token and the corrected secret encrypted.

## Admin console

The bot image embeds a private console at `https://ost-admin.nies.io`, served
on port 8080 in the existing bot container. `admin.yaml` defines a ClusterIP
Service; `kustomization.yaml` generates the two non-secret Cloudflare Access
identifiers. The configured application admits the account in the existing
Homelab policy. Clearing both values locks the console with HTTP 503 while
the Discord bot continues operating. Changes to these generated
ConfigMap values automatically trigger a pod rollout via Kustomize’s name hash.

Cloudflare Tunnel is remotely managed, so its public hostname and Access
application must be configured in Zero Trust (the repository's cloudflared
config does not control routes):

1. Create a self-hosted Access application for **ost-admin.nies.io**, with an
   Allow policy restricted to the admin account. Do not use a Bypass policy.
2. Copy its Audience (AUD) and team domain into `ADMIN_CF_AUDIENCE` and
   `ADMIN_CF_ISSUER` in `kustomization.yaml`. The issuer is
   `https://<team>.cloudflareaccess.com`, without a path.
3. Add a tunnel public hostname for **ost-admin.nies.io** pointing to
   `http://smi-ost-bot.smi-ost-bot.svc.cluster.local:8080`.
4. Build the bot's linux/arm64 image, push to `docker.nies.io:5000`, and pin its
   digest in `deployment.yaml`. Commit/push the manifests and let Argo reconcile.
5. Verify Argo is Synced/Healthy at the intended revision, `/healthz` responds,
   requests without an Access JWT are rejected, and the authenticated console
   loads. Verify the next SQLite snapshot and VolSync sync after the rollout.

The application independently validates the Access JWT and prevents cross-site
mutations. Only users admitted by this dedicated application have admin rights.
A future public site should have its own hostname and Access audience; never
reuse the admin audience. Keep this Service private to the cluster.

Admin deletions cascade to votes and create an `admin_events` audit record with
the authenticated email and full pre-change submission/vote JSON in the same
transaction. The existing backup sidecar automatically includes this table in
snapshots. The UI shows the most recent 50 actions per server; audit records are
retained indefinitely. Removing a vote does not ban the voter, and recovery is
an explicit database maintenance operation, not an undo button.

Cloudflare setup completed on 2026-09-20: Access application
`f1ca0bfe-6802-4898-a43e-cf9546921660` uses the existing admin-only Homelab policy.
The Homelab tunnel also requires a valid token for this application's audience
before forwarding requests to the bot. The hostname is a proxied CNAME to that
tunnel. Changes to Cloudflare routing and Access are managed through its API,
while Kubernetes deployments remain GitOps-managed.
