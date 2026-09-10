#!/bin/bash
# /opt/backup-scripts/talos-backup.sh   (staged copy the workflow runs: /home/n8n-backup/talos-backup.sh)
#
# Takes an etcd snapshot of the Talos control plane and mirrors it to the 24 TB
# backup-hdd, which is physically on EVO-X2 (192.168.4.84) — so the snapshot
# survives Peladn (VM 201 / the whole control plane) going down.
#
# History: until ~2026-05 this wrote to a LOCAL /mnt/backup-hdd on Peladn; that
# drive was moved to Evo-X2, the mount vanished here, `set -e` killed the job,
# and the etcd backup silently stopped for ~3.5 months. This version snapshots
# locally then rsyncs to Evo-X2 and is safe to run often (hourly / 12 h).
#
# Invoked by n8n "Talos Config and Data Backup" (ggPHd7ROI3GVsLOL):
#   ssh (cred "n8n-backup Proxmox Peladn SSH key")  ->  bash /home/n8n-backup/talos-backup.sh
# i.e. it runs as the UNPRIVILEGED n8n-backup user. Every default path below is
# writable by n8n-backup; the push uses an rsync-only (rrsync-locked) key so a
# compromise of n8n-backup cannot get a shell on Evo-X2.
#
# The local staging dir is the source of truth for retention: it is pruned to
# $KEEP snapshots and then `rsync --delete` mirrors it to Evo-X2, so the two
# ends always match. Exit 0 on success, non-zero on any failure (the n8n Gotify
# node keys off the exit code).
#
# Env overrides (all optional):
#   CP_NODE      control-plane node IP        (default 192.168.4.172)
#   TALOSCONFIG  talosconfig path             (default /home/n8n-backup/.talos/config)
#   EVOX2_HOST   rsync target host            (default 192.168.4.84)
#   EVOX2_USER   ssh user on Evo-X2           (default talossnap)
#   SSH_KEY      private key for that user    (default /home/n8n-backup/.ssh/id_talos_snap)
#   KEEP         snapshots to retain both ends (default 72 -> 3 d hourly / 36 d at 12 h)
#   STAGE_DIR    local staging / retention dir (default /home/n8n-backup/.cache/talos-etcd)
#   GOTIFY_URL / GOTIFY_TOKEN  optional direct failure push (n8n also alerts on non-zero exit)
#
# Evo-X2 side (one-time, see infrastructure/failover/README or the recovery runbook):
#   user 'talossnap', dir /mnt/backup-hdd/talos-etcd-snapshots owned by it, and in
#   ~talossnap/.ssh/authorized_keys:
#     from="192.168.4.150",restrict,command="/usr/bin/rrsync -wo /mnt/backup-hdd/talos-etcd-snapshots" ssh-ed25519 AAAA... n8n-backup@peladn->evox2 talos-etcd
set -euo pipefail

CP_NODE="${CP_NODE:-192.168.4.172}"
TALOSCONFIG_PATH="${TALOSCONFIG:-/home/n8n-backup/.talos/config}"
EVOX2_HOST="${EVOX2_HOST:-192.168.4.84}"
EVOX2_USER="${EVOX2_USER:-talossnap}"
SSH_KEY="${SSH_KEY:-/home/n8n-backup/.ssh/id_talos_snap}"
KEEP="${KEEP:-72}"
STAGE_DIR="${STAGE_DIR:-/home/n8n-backup/.cache/talos-etcd}"
TALOSCTL="${TALOSCTL:-/usr/local/bin/talosctl}"

TS="$(date +%Y%m%d_%H%M%S)"
SNAP="etcd_${TS}.db"
TARGET="${EVOX2_USER}@${EVOX2_HOST}"
SSH_CMD="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10"

log()  { echo "[$(date +%H:%M:%S)] $*"; }
fail() {
  echo "[$(date +%H:%M:%S)] ERROR: $*" >&2
  if [ -n "${GOTIFY_URL:-}" ] && [ -n "${GOTIFY_TOKEN:-}" ]; then
    curl -fsS -m 10 -X POST "${GOTIFY_URL%/}/message?token=${GOTIFY_TOKEN}" \
      -F "title=talos-backup.sh FAILED on Peladn" -F "message=$*" -F "priority=8" >/dev/null 2>&1 || true
  fi
  exit 1
}

mkdir -p "$STAGE_DIR"

log "etcd snapshot: ${CP_NODE} -> ${STAGE_DIR}/${SNAP}"
"$TALOSCTL" --talosconfig="$TALOSCONFIG_PATH" -n "$CP_NODE" etcd snapshot "${STAGE_DIR}/${SNAP}" \
  || fail "talosctl etcd snapshot failed"

# sanity: a real snapshot of this cluster is ~11 MB; treat anything under 1 MB as broken
SZ="$(stat -c%s "${STAGE_DIR}/${SNAP}")"
[ "$SZ" -ge 1048576 ] || { rm -f "${STAGE_DIR}/${SNAP}"; fail "snapshot only ${SZ} bytes — not trusting it"; }
log "snapshot ok (${SZ} bytes)"

# retention: keep newest $KEEP in the staging dir (this dir IS the retention policy)
mapfile -t OLD < <(ls -1t "${STAGE_DIR}"/etcd_*.db 2>/dev/null | tail -n +"$((KEEP+1))")
[ "${#OLD[@]}" -gt 0 ] && { printf '%s\n' "${OLD[@]}" | xargs -r rm -f; log "pruned ${#OLD[@]} old local snapshot(s)"; }

# guard against a bug wiping the remote: never mirror an empty staging dir
CNT="$(ls -1 "${STAGE_DIR}"/etcd_*.db 2>/dev/null | wc -l)"
[ "$CNT" -ge 1 ] || fail "staging dir empty after snapshot — refusing to rsync --delete"

log "mirror ${CNT} snapshot(s) -> ${TARGET}:/mnt/backup-hdd/talos-etcd-snapshots/"
rsync -rt --delete-after --timeout=60 --no-owner --no-group \
  -e "$SSH_CMD" "${STAGE_DIR}/" "${TARGET}:./" \
  || fail "rsync to Evo-X2 failed"

log "done — ${CNT} snapshot(s), newest ${SNAP} (${SZ} bytes), on Evo-X2 backup-hdd"
