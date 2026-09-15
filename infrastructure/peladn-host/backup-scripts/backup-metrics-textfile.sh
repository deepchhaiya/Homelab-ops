#!/bin/bash
# backup-metrics-textfile.sh — emit backup freshness/size metrics for node-exporter.
# Run on the Peladn host as ROOT via a systemd timer (every ~15 min).
#
# Writes Prometheus textfile metrics to the node-exporter textfile collector dir.
# The Peladn node-exporter (192.168.4.150:9100) is scraped by VictoriaMetrics, so
# these land in VM with host="peladn" as backup-freshness metrics:
#   homelab_backup_age_seconds{name=,kind=}      — seconds since the newest matching file
#   homelab_backup_size_bytes{name=,kind=}       — size of that newest file
#   homelab_backup_count{name=,kind=}            — number of matching files (retention check)
#   homelab_backup_max_age_seconds{name=,kind=}  — allowed age before the job counts as overdue
# A missing backup type emits age = +Inf (shows red on the dashboard).
# The PBS snapshot side (vzdump + pxar jobs) is emitted on Evo-X2 by
# infrastructure/pbs-backup-lxc/pbs-snapshot-metrics.sh with the same metric names.

set -u
OUTDIR=/var/lib/prometheus/node-exporter
OUT="$OUTDIR/homelab_backups.prom"
TMP="$OUT.$$"
NOW=$(date +%s)
DAY=86400

# Backups to track:  <dir>|<glob>|<name-label>|<kind-label>|<max-age-seconds>
#   max-age = schedule interval + grace (weekly -> 8d, 12h -> 26h).
#   talos-etcd: the Peladn-side staging dir of talos-backup.sh — freshness there
#     tracks the last successful `talosctl etcd snapshot` (mirrored to Evo-X2 every 12h).
#   k8s-export-miniflux: k8s-export.sh (Talos Config and Data Backup workflow, 12h).
# pbs-ct200-config was dropped 2026-09-14: CT 200 (PBS) moved to Evo-X2 on
# 2026-05-23 and the n8n "PBS config backup (CT 200)" node is disabled.
ENTRIES=(
  "/mnt/pvedas/k8s-backups|miniflux-postgres-*|miniflux-postgres|db-dump|$((8 * DAY))"
  "/mnt/pvedas/k8s-backups|n8n-postgres-*|n8n-postgres|db-dump|$((8 * DAY))"
  "/mnt/pvedas/k8s-backups|n8n-files-*|n8n-files|db-dump|$((8 * DAY))"
  "/mnt/pvedas/k8s-backups|karakeep-postgres-*|karakeep-postgres|db-dump|$((8 * DAY))"
  "/mnt/pvedas/k8s-app-exports|*/miniflux_db.sql.gz|k8s-export-miniflux|db-dump|$((26 * 3600))"
  "/home/n8n-backup/.cache/talos-etcd|etcd_*.db|talos-etcd|etcd|$((26 * 3600))"
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

  for e in "${ENTRIES[@]}"; do
    IFS='|' read -r dir glob name kind maxage <<< "$e"
    labels="name=\"$name\",kind=\"$kind\""
    echo "homelab_backup_max_age_seconds{$labels} $maxage"
    # newest matching file by mtime
    newest=$(ls -1t "$dir"/$glob 2>/dev/null | head -1)
    if [[ -n "$newest" && -f "$newest" ]]; then
      mtime=$(stat -c%Y "$newest" 2>/dev/null || echo 0)
      size=$(stat -c%s "$newest" 2>/dev/null || echo 0)
      count=$(ls -1 "$dir"/$glob 2>/dev/null | wc -l)
      age=$(( NOW - mtime ))
      echo "homelab_backup_age_seconds{$labels} $age"
      echo "homelab_backup_size_bytes{$labels} $size"
      echo "homelab_backup_count{$labels} $count"
    else
      # No file -> age +Inf so the panel goes red; size/count 0.
      echo "homelab_backup_age_seconds{$labels} +Inf"
      echo "homelab_backup_size_bytes{$labels} 0"
      echo "homelab_backup_count{$labels} 0"
    fi
  done
} > "$TMP"

# Atomic replace (node-exporter best practice).
mv "$TMP" "$OUT"
chmod 0644 "$OUT"
