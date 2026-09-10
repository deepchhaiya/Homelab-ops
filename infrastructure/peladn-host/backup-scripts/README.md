# Peladn host backup scripts

These scripts run **on the Peladn Proxmox host** (`prop`, 192.168.4.150) and are
invoked by scheduled **n8n workflows** (which SSH in, run the script, and send a
Gotify notification). They are version-controlled here for DR — the live copies
live at `/opt/backup-scripts/` and, for `talos-backup.sh`, also `/home/n8n-backup/`
(the path its workflow SSHes to).

> ⚠️ Keep the repo copy and the host copy in sync. After editing here, deploy with:
> `tr -d '\r' < <script> | ssh root@192.168.4.150 "cat > /opt/backup-scripts/<script> && chmod 0755 /opt/backup-scripts/<script>"`

## Scripts

| Script | Run as | n8n workflow | Schedule | Backs up |
|---|---|---|---|---|
| `rpi4-pvc-backup.sh` | `n8n-backup` | rpi4 PVC Dumps (`l8DHMWUaHEoAaQkg`) | Sat 1 AM | Logical dumps of DBs on the **rpi4 Talos node's local-path** (never captured by vzdump): miniflux pg + **n8n pg** + karakeep pg, **plus n8n filesystem extras** (community-node manifest + `/home/data` user CSVs — not in the pg dump) → `/mnt/pvedas/k8s-backups` |
| `talos-backup.sh` | `n8n-backup` | Talos Config and Data Backup (`ggPHd7ROI3GVsLOL`) | every 12 h | Talos **etcd snapshot** only → mirrored to **Evo-X2** `/mnt/backup-hdd/talos-etcd-snapshots/`. Talos YAML configs live in git (`kubernetes/talos/`). |
| `k8s-export.sh` | root | Talos Config and Data Backup (`ggPHd7ROI3GVsLOL`) | Sun 1 AM | k8s manifest/data export |
| `pbs-config-backup.sh` | root | *(to wire — Sat 3 AM in the PBS workflow)* | weekly | CT 200 (PBS LXC) **config only**: `pct config 200` + `/etc/proxmox-backup/` (datastore.cfg/repos, acl, remote, prune, domains, keys) + host `storage.cfg`. **NOT** the datastore chunk data. → `/mnt/pvedas/pbs-config-backups` |

Everything **except `talos-backup.sh`** lands under `/mnt/pvedas` (the DAS), which
is swept into the **Friday DAS PBS backup** (`DAS Backup to External HDD via PBS`,
`lZh1YZsfXwzb2PTq`). `talos-backup.sh` is deliberately the exception — its whole
point is that the etcd snapshot must survive Peladn, so it pushes straight to the
24 TB backup-hdd on **Evo-X2** and never touches `/mnt/pvedas`.

## Why each exists / notable design points

- **rpi4-pvc-backup.sh** — the rpi4 is a *physical* Talos node, so its local-path
  PVCs are never in any vzdump. This takes logical dumps instead. **n8n was added
  2026-06-21** after it migrated off SQLite-on-NFS to Postgres-on-local-path (it
  used to ride along in the NFS DAS backup; now nothing else captures it). karakeep
  pg is currently empty (its real data is SQLite on the NFS assets PVC) — dumped
  anyway to catch future use. Uses a shared `dump_pg()` helper.
- **pbs-config-backup.sh** — CT 200 is **not** captured by any vzdump (the Monday
  job only does 201/202/203). This backs up just enough to rebuild PBS and re-point
  it at the existing datastores. Handles CT 200 being **stopped** (its normal
  state) via `pct mount` (mounts the rootfs without starting the container), falls
  back to `pct exec` if running. Captures config only — the actual backup chunks
  stay in the datastores.
- **talos-backup.sh** — the backup-hdd it used to write to *moved from Peladn to
  Evo-X2* around 2026-05; the mount vanished here, `set -e` killed the job, and the
  Talos etcd backup silently stopped for ~3.5 months. Rewritten 2026-09-10: runs as
  the unprivileged `n8n-backup` user, takes `talosctl etcd snapshot` locally into
  `~/.cache/talos-etcd`, keeps the newest `$KEEP` (default 72), then
  `rsync --delete` mirrors that dir to `talossnap@192.168.4.84:/mnt/backup-hdd/talos-etcd-snapshots/`.
  The staging dir **is** the retention policy — both ends always match. The push
  key on Evo-X2 is locked with `command="rrsync -wo …"` so a compromise of
  `n8n-backup` can only rsync into that one dir, not get a shell. Safe to run hourly
  or 12 h. Restore: see `infrastructure/failover/CONTROL-PLANE-RECOVERY.md`.

## Restore notes

- **n8n pg**: `gunzip -c n8n-postgres-DATE.sql.gz | kubectl exec -i -n n8n n8n-postgres-0 -- psql -U n8n -d n8n`
  (the `N8N_ENCRYPTION_KEY` needed to decrypt restored credentials lives in `kubernetes/apps/n8n/secret.enc.yaml`, SOPS-encrypted — keep it unchanged.)
- **n8n files** (`/home/data` CSVs + community-node manifest): `kubectl exec -i -n n8n deploy/n8n -- tar xzf - -C / < n8n-files-DATE.tar.gz`.
  Community nodes aren't shipped in the tar (only the manifest) — reinstall the packages listed in `nodes/package.json` (e.g. `n8n-nodes-proxmox`, `n8n-nodes-wake-on-lan`) via **Settings → Community Nodes**.
- **PBS config**: extract the tarball, drop `/etc/proxmox-backup/*` back into a fresh
  CT 200, recreate the datastores from `datastore.cfg` pointing at the existing
  chunk dirs (the data was never deleted), then `proxmox-backup-manager` verify.
