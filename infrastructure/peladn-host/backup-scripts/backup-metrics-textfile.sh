#!/bin/bash
# backup-metrics-textfile.sh — emit backup freshness/size metrics for node-exporter.
# Run on the Peladn host as ROOT via a systemd timer (every ~15 min).
#
# Writes Prometheus textfile metrics to the node-exporter textfile collector dir.
# The Peladn node-exporter (192.168.4.150:9100) is scraped by VictoriaMetrics, so
# these land in VM with host="peladn" as backup-freshness metrics:
#   homelab_backup_age_seconds{name=...}   — seconds since the newest matching file
#   homelab_backup_size_bytes{name=...}    — size of that newest file
#   homelab_backup_count{name=...}         — number of matching files (retention check)
# A missing backup type emits age = +Inf (shows red on the dashboard).

set -u
OUTDIR=/var/lib/prometheus/node-exporter
OUT="$OUTDIR/homelab_backups.prom"
TMP="$OUT.$$"
NOW=$(date +%s)

# Backups to track:  <dir>|<glob>|<name-label>
#   talos-etcd: the Peladn-side staging dir of talos-backup.sh — freshness there
#   tracks the last successful `talosctl etcd snapshot` (mirrored to Evo-X2 every 12h).
ENTRIES=(
  "/mnt/pvedas/k8s-backups|miniflux-postgres-*|miniflux-postgres"
  "/mnt/pvedas/k8s-backups|n8n-postgres-*|n8n-postgres"
  "/mnt/pvedas/k8s-backups|karakeep-postgres-*|karakeep-postgres"
  "/mnt/pvedas/pbs-config-backups|pbs-ct200-config-*|pbs-ct200-config"
  "/home/n8n-backup/.cache/talos-etcd|etcd_*.db|talos-etcd"
)

{
  echo "# HELP homelab_backup_age_seconds Seconds since the newest backup file of this type."
  echo "# TYPE homelab_backup_age_seconds gauge"
  echo "# HELP homelab_backup_size_bytes Size in bytes of the newest backup file of this type."
  echo "# TYPE homelab_backup_size_bytes gauge"
  echo "# HELP homelab_backup_count Number of backup files of this type present."
  echo "# TYPE homelab_backup_count gauge"

  for e in "${ENTRIES[@]}"; do
    IFS='|' read -r dir glob name <<< "$e"
    # newest matching file by mtime
    newest=$(ls -1t "$dir"/$glob 2>/dev/null | head -1)
    if [[ -n "$newest" && -f "$newest" ]]; then
      mtime=$(stat -c%Y "$newest" 2>/dev/null || echo 0)
      size=$(stat -c%s "$newest" 2>/dev/null || echo 0)
      count=$(ls -1 "$dir"/$glob 2>/dev/null | wc -l)
      age=$(( NOW - mtime ))
      echo "homelab_backup_age_seconds{name=\"$name\"} $age"
      echo "homelab_backup_size_bytes{name=\"$name\"} $size"
      echo "homelab_backup_count{name=\"$name\"} $count"
    else
      # No file -> age +Inf so the panel goes red; size/count 0.
      echo "homelab_backup_age_seconds{name=\"$name\"} +Inf"
      echo "homelab_backup_size_bytes{name=\"$name\"} 0"
      echo "homelab_backup_count{name=\"$name\"} 0"
    fi
  done
} > "$TMP"

# Atomic replace (node-exporter best practice).
mv "$TMP" "$OUT"
chmod 0644 "$OUT"
