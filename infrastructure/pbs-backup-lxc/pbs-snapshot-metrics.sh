#!/bin/bash
# pbs-snapshot-metrics.sh — emit PBS snapshot freshness metrics for node-exporter.
# Run on the Evo-X2 Proxmox HOST as ROOT via a systemd timer (every ~15 min).
# Deployed to /usr/local/sbin/pbs-snapshot-metrics.sh (+ .service/.timer in /etc/systemd/system).
#
# The 26TB backup HDD is mounted on the Evo-X2 host at /mnt/backup-hdd and bind-mounted
# into CT 200 (PBS). PBS stores each snapshot as <datastore>/<type>/<id>/<RFC3339-UTC>/,
# and writes index.json.blob only when the backup finishes — so reading the directory
# tree gives snapshot freshness with no PBS API token and no load on PBS.
#
# Same metric names as peladn-host/backup-scripts/backup-metrics-textfile.sh, so the
# Grafana "Homelab Backups" dashboard shows both. Scraped as job node-evox2 (host="evox2"):
#   homelab_backup_age_seconds{name=,kind=}      — seconds since the newest finished snapshot/file
#   homelab_backup_count{name=,kind=}            — snapshots (or files) retained
#   homelab_backup_size_bytes{name=,kind=}       — files only (PBS chunks are deduplicated)
#   homelab_backup_max_age_seconds{name=,kind=}  — allowed age before the job counts as overdue
# A group with no finished snapshot emits age = +Inf (shows red on the dashboard).

set -u
OUTDIR=/var/lib/prometheus/node-exporter
OUT="$OUTDIR/homelab_pbs_backups.prom"
TMP="$OUT.$$"
NOW=$(date +%s)
DAY=86400
HDD=/mnt/backup-hdd

# PBS groups written by n8n "DAS Backup to External HDD via PBS" (lZh1YZsfXwzb2PTq):
#   <datastore-dir>|<type>/<id>|<name-label>|<max-age-seconds>   (all weekly -> 8d)
# Groups not listed here (ct/401, host/prop, host/prop-critical) are retired leftovers.
PBS_GROUPS=(
  "pbs-backup|vm/201|pbs-vm201-talos-cp|$((8 * DAY))"          # Mon, Peladn vzdump
  "pbs-backup|ct/202|pbs-ct202-media-ai-ops|$((8 * DAY))"      # Mon, Peladn vzdump
  "pbs-backup|ct/203|pbs-ct203-home-ops|$((8 * DAY))"          # Mon, Peladn vzdump
  "external-hdds|host/wd-ext-hdd|pbs-wd-ext-hdd|$((8 * DAY))"  # Tue, pxar
  "pbs-backup|vm/402|pbs-vm402-talos-worker|$((8 * DAY))"      # Wed, Evo-X2 vzdump
  "pbs-backup|ct/405|pbs-ct405-observability|$((8 * DAY))"     # Wed, Evo-X2 vzdump
  "external-hdds|host/sg-ext-hdd|pbs-sg-ext-hdd|$((8 * DAY))"  # Thu, pxar
  "pbs-backup|host/das|pbs-das|$((8 * DAY))"                   # Fri, pxar of the 11TB DAS
)

# Plain files on the backup HDD:  <dir>|<glob>|<name-label>|<kind-label>|<max-age-seconds>
#   talos-etcd-evox2: the off-Peladn mirror pushed by talos-backup.sh (12h) — proves the
#   DR copy actually landed, not just that Peladn took a snapshot.
FILE_ENTRIES=(
  "$HDD/talos-etcd-snapshots|etcd_*.db|talos-etcd-evox2|etcd|$((26 * 3600))"
)

{
  echo "# HELP homelab_backup_age_seconds Seconds since the newest backup file of this type."
  echo "# TYPE homelab_backup_age_seconds gauge"
  echo "# HELP homelab_backup_size_bytes Size in bytes of the newest backup file of this type."
  echo "# TYPE homelab_backup_size_bytes gauge"
  echo "# HELP homelab_backup_count Number of backup files of this type present."
  echo "# TYPE homelab_backup_count gauge"
  echo "# HELP homelab_backup_max_age_seconds Allowed backup age before this job counts as overdue."
  echo "# TYPE homelab_backup_max_age_seconds gauge"

  for e in "${PBS_GROUPS[@]}"; do
    IFS='|' read -r ds group name maxage <<< "$e"
    labels="name=\"$name\",kind=\"pbs\""
    echo "homelab_backup_max_age_seconds{$labels} $maxage"
    # finished snapshots only; dir names are RFC3339 UTC so a lexical sort is chronological
    snaps=$(ls -1d "$HDD/$ds/$group"/*/index.json.blob 2>/dev/null | sort)
    if [[ -n "$snaps" ]]; then
      newest=$(basename "$(dirname "$(tail -1 <<< "$snaps")")")
      ts=$(date -u -d "$newest" +%s 2>/dev/null || echo 0)
      count=$(wc -l <<< "$snaps")
      echo "homelab_backup_age_seconds{$labels} $(( NOW - ts ))"
      echo "homelab_backup_count{$labels} $count"
    else
      echo "homelab_backup_age_seconds{$labels} +Inf"
      echo "homelab_backup_count{$labels} 0"
    fi
  done

  for e in "${FILE_ENTRIES[@]}"; do
    IFS='|' read -r dir glob name kind maxage <<< "$e"
    labels="name=\"$name\",kind=\"$kind\""
    echo "homelab_backup_max_age_seconds{$labels} $maxage"
    newest=$(ls -1t "$dir"/$glob 2>/dev/null | head -1)
    if [[ -n "$newest" && -f "$newest" ]]; then
      mtime=$(stat -c%Y "$newest" 2>/dev/null || echo 0)
      size=$(stat -c%s "$newest" 2>/dev/null || echo 0)
      count=$(ls -1 "$dir"/$glob 2>/dev/null | wc -l)
      echo "homelab_backup_age_seconds{$labels} $(( NOW - mtime ))"
      echo "homelab_backup_size_bytes{$labels} $size"
      echo "homelab_backup_count{$labels} $count"
    else
      echo "homelab_backup_age_seconds{$labels} +Inf"
      echo "homelab_backup_size_bytes{$labels} 0"
      echo "homelab_backup_count{$labels} 0"
    fi
  done
} > "$TMP"

# Atomic replace (node-exporter best practice).
mv "$TMP" "$OUT"
chmod 0644 "$OUT"
