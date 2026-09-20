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
