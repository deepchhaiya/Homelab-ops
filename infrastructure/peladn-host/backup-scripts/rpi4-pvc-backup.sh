#!/bin/bash
# rpi4 PVC dumps -> /mnt/pvedas/k8s-backups/
# Run on Peladn as the n8n-backup user (invoked by the n8n "rpi4 PVC Dumps" workflow, Sat 1AM).
#
# WHY this script exists: the rpi4 Talos worker is a physical node, NOT a Proxmox
# VM, so its local-path PVCs are never captured by vzdump. This takes logical
# dumps of the databases that live on rpi4 local-path and writes them to the DAS
# (/mnt/pvedas/k8s-backups), which is then swept into the Friday DAS PBS backup.
#
# What it backs up:
#   * miniflux postgres — logical pg_dump.
#   * n8n postgres — logical pg_dump. ADDED 2026-06-21 after n8n migrated off
#     SQLite-on-NFS to Postgres-on-local-path (it was previously swept in via the
#     NFS DAS backup; now it lives on rpi4 local-path and nothing else captures it).
#   * karakeep postgres — logical pg_dump (currently EMPTY; karakeep's real data is
#     SQLite on the NFS assets PVC, already in the DAS backup. Kept to catch future use).
#   * n8n filesystem extras — the pg dump covers workflows/credentials/executions,
#     but two things live ONLY on n8n's PVCs: the community-node manifest
#     (/home/node/.n8n/nodes/package*.json — tells a restore which nodes to
#     reinstall, e.g. n8n-nodes-proxmox, n8n-nodes-wake-on-lan) and user files
#     under /home/data (e.g. csvs/). Tarred together. Intentionally EXCLUDED:
#     the encryption key (config) — it's in secret.enc.yaml (SOPS/git); the legacy
#     database.sqlite (obsolete post-Postgres migration); nodes/node_modules
#     (reinstallable from the manifest); binaryData (empty / DB-mode).
#
# NOTE (2026-08-10): mem0 was retired — its qdrant/postgres backup sections were
# removed. The AI memory is now Hindsight, on the Evo-X2 K8s WORKER (VM 402) —
# and unlike the physical rpi4, that VM IS captured by vzdump, so Hindsight's
# local-path PVC is already backed up at the VM level. No separate logical dump
# is done here (its data also reconstructs from the exported-memories JSON).
#
# Retention: keep last 14 per prefix.

set -u  # don't -e; per-section errors handled explicitly
DATE=$(date +%Y%m%d-%H%M%S)
DEST=/mnt/pvedas/k8s-backups
KEEP=14
EXIT=0

TMP=$(mktemp -d -t rpi4-pvc.XXXXXX)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

log() { echo "[$(date +%H:%M:%S)] $*"; }

# Logical pg_dump of a postgres pod that exposes POSTGRES_USER/POSTGRES_DB in its env.
# Args: <namespace> <pod> <output-prefix> [min-size-bytes]
dump_pg() {
  local ns="$1" pod="$2" prefix="$3" minsize="${4:-0}"
  local out="$DEST/${prefix}-$DATE.sql.gz"
  log "dumping ${prefix} (${ns}/${pod})..."
  if kubectl exec -n "$ns" "$pod" -- \
       bash -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
       2>"$TMP/${prefix}.err" | gzip > "$out"; then
    local size; size=$(stat -c%s "$out")
    if [[ "$size" -lt "$minsize" ]]; then
      log "  X ${prefix} dump suspiciously small ($size bytes)"
      cat "$TMP/${prefix}.err"
      EXIT=1
    else
      log "  OK ${prefix}: $size bytes"
    fi
  else
    log "  X ${prefix} dump failed"
    cat "$TMP/${prefix}.err"
    EXIT=1
  fi
}

mkdir -p "$DEST"
cd "$TMP" || exit 1

# ---------- 1. miniflux postgres ----------
log "dumping miniflux postgres..."
MF_POD=$(kubectl -n miniflux get pod -l app=miniflux-db -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "$MF_POD" ]]; then
  MF_POD=$(kubectl -n miniflux get pods -o name | grep -m1 miniflux-db | sed 's|pod/||')
fi
if [[ -z "$MF_POD" ]]; then
  log "  X could not find miniflux-db pod"
  EXIT=1
else
  dump_pg miniflux "$MF_POD" miniflux-postgres 1000
fi

# ---------- 2. n8n postgres (the workflow DB — local-path on rpi4, nothing else backs it up) ----------
dump_pg n8n n8n-postgres-0 n8n-postgres 1000

# ---------- 2b. n8n filesystem extras (NOT in the pg dump): community-node manifest + /home/data ----------
log "archiving n8n filesystem extras (community-node manifest + /home/data)..."
N8N_FILES_OUT="$DEST/n8n-files-$DATE.tar.gz"
# -C / + relative paths so the archive restores cleanly with `tar xzf - -C /`.
if kubectl exec -n n8n deploy/n8n -- tar czf - -C / \
     home/data \
     home/node/.n8n/nodes/package.json \
     home/node/.n8n/nodes/package-lock.json \
     2>"$TMP/n8n-files.err" > "$N8N_FILES_OUT"; then
  NF_SIZE=$(stat -c%s "$N8N_FILES_OUT")
  if [[ "$NF_SIZE" -lt 200 ]]; then
    log "  X n8n-files archive suspiciously small ($NF_SIZE bytes)"
    cat "$TMP/n8n-files.err"
    EXIT=1
  else
    log "  OK n8n-files: $NF_SIZE bytes"
  fi
else
  log "  X n8n-files archive failed"
  cat "$TMP/n8n-files.err"
  EXIT=1
fi

# ---------- 3. karakeep postgres (currently empty; real data is SQLite on NFS assets PVC) ----------
dump_pg karakeep postgres-0 karakeep-postgres 0

# ---------- 4. retention ----------
log "applying retention (keep last $KEEP per prefix)..."
for PREFIX in miniflux-postgres n8n-postgres n8n-files karakeep-postgres; do
  ls -1t "$DEST"/${PREFIX}-* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r OLD; do
    log "  removing old: $(basename "$OLD")"
    rm -f "$OLD"
  done
done

# ---------- 5. summary ----------
log "summary (this run's files):"
ls -lh "$DEST"/*-$DATE.* 2>/dev/null | sed 's/^/  /'
log "free space:"
df -h "$DEST" | sed 's/^/  /'

exit $EXIT
