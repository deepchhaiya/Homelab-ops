# Control-plane recovery — Peladn down → K8s back on Evo-X2

The Talos K8s control plane is a **single node**: `ira-peladn-talos-cp` = VM **201**
on the Peladn PVE host, `192.168.4.172`, 1-member etcd, endpoint
`https://192.168.4.172:6443` (no VIP). If Peladn dies, this runbook brings the
control plane back on **Evo-X2** with the *same* identity and IP, so kubeconfig /
talosconfig / worker trust keep working unchanged.

Two paths:

| | Path A — restore VM 201 image | Path B — etcd-snapshot recover |
|---|---|---|
| Source | `pbs-local` weekly vzdump of VM 201 (on Evo-X2) | `evox2:/mnt/backup-hdd/talos-etcd-snapshots/etcd_*.db` (every 12 h) |
| RPO | up to ~7 days | ≤ 12 h |
| RTO | ~15–30 min (43 GB restore) | ~20–40 min (build node + bootstrap) |
| Use when | default | VM 201 backup missing/corrupt, or you need the lower RPO |
| Mechanism | `peladn-failover.sh` (scripted) | manual `talosctl` |

Path A is the default. Path B is the fallback / RPO-tightener.

---

## 0. What backs this (all already in place)

- **VM 201 vzdump** → `pbs-local` on Evo-X2, weekly (`DAS Backup to External HDD via PBS`, Monday). Contains a full point-in-time etcd.
- **etcd snapshots** → `talossnap@192.168.4.84:/mnt/backup-hdd/talos-etcd-snapshots/` every 12 h (`talos-backup.sh` via the `Talos Config and Data Backup` workflow). ~11 MB each, newest-72 retained.
- **Machine secrets / cluster identity** → `kubernetes/talos/controlplane.enc.yaml` (SOPS, Age key in Vaultwarden). This is what makes a rebuilt node *the same cluster*. **Keep a decrypted copy offline** (`controlplane.yaml`, mode 0400, on Evo-X2 or a USB) — Vaultwarden runs on Peladn and is down exactly when you need it.
- **Restore tooling on Evo-X2** — `pbs-local` storage, `/usr/local/bin/peladn-failover.sh`, stub bind-mount dirs (all from `evox2-readiness.sh`). Path B additionally needs `talosctl` on the Evo-X2 host — **install it now**, don't wait: `curl -sL https://github.com/siderolabs/talos/releases/download/v1.12.6/talosctl-linux-amd64 -o /usr/local/bin/talosctl && chmod +x /usr/local/bin/talosctl` (match the cluster's Talos version).

---

## 1. Detect

The `peladn-failover` watchdog emails **"⚠️ Peladn DOWN ~30 min (3 failed checks)"**
after 3 consecutive misses of `https://192.168.4.150:8006/api2/json/version`
(~30 min at the 10-min poll).

## 2. Decide — is it real?

Do **not** fail over on a blip. Confirm Peladn is genuinely gone:

```bash
ping -c3 192.168.4.150
curl -k -m5 https://192.168.4.150:8006/api2/json/version      # PVE API
ssh root@192.168.4.150 uptime                                  # host
kubectl get --raw='/readyz'                                    # API server (via .172)
```

If Peladn is reachable but only VM 201 is stuck: `ssh root@192.168.4.150 'qm stop 201'`
and see if it recovers on its own before failing over.

## 3. Fence

Before anything comes up on Evo-X2 claiming `192.168.4.172`, make sure VM 201 on
Peladn is **down and stays down**:

```bash
ssh root@192.168.4.150 'qm stop 201 && qm set 201 --onboot 0'   # if Peladn is reachable at all
```

If Peladn is fully dead this is moot — just never boot its VM 201 again (step 6).

---

## Path A — restore VM 201 image (default)

### A1. Trigger

From the alert email:

```bash
curl -sS -u 'n8n:<PASSWORD>' -X POST https://n8n.dkghar.duckdns.org/webhook/peladn-failover
```

or run it directly on the Evo-X2 host:

```bash
ssh root@192.168.4.84
curl -fsSL https://raw.githubusercontent.com/DeepAchut/Homelab-ops/main/infrastructure/failover/peladn-failover.sh \
  -o /usr/local/bin/peladn-failover.sh && chmod +x /usr/local/bin/peladn-failover.sh
bash /usr/local/bin/peladn-failover.sh          # add FORCE=1 ONLY if Peladn's PVE API still answers
```

The script: split-brain guard → `qmrestore` VM 201 + `pct restore` CT 203, CT 202
from `pbs-local` (`--force`, into `local-lvm`) → `qm set 201 --cpu x86-64-v2-AES` →
start 201, 203, 202.

### A2. Verify

```bash
ssh root@192.168.4.84 'qm status 201; pct status 203; pct status 202'
talosctl -n 192.168.4.172 -e 192.168.4.172 health --wait-timeout 5m
kubectl get nodes            # ira-peladn-talos-cp -> Ready in a few min
kubectl -n kube-system get pods | grep -E 'apiserver|etcd|controller|scheduler'
flux get kustomizations      # should reconcile once the API is back
```

Workers (`ira-evo-x2-talos-worker`, `ira-rpi4-talos-worker`) reconnect on their own
once the API answers at `.172`. Bulk media on CT 202 rehydrates on demand — see
`Phase-21-Part-2 - failover-runbook-evox2.md` §5.

### A3. If the restored etcd is too old

Optionally fast-forward to the latest 12 h snapshot — this is Path B step B3
run against the just-restored VM 201 (stop etcd first: `talosctl -n .172 service etcd stop`),
or just accept the RPO. Usually not worth it.

---

## Path B — etcd-snapshot recover (fallback / low-RPO)

Use when the VM 201 vzdump is missing/corrupt, or you want ≤12 h RPO.

### B1. Provide a node

Either the VM 201 you restored in Path A (wipe its etcd), **or** a fresh Talos VM on
Evo-X2: 2 vCPU / 6 GB / 40 GB, q35 + OVMF, Talos **v1.12.6** ISO, NIC on `vmbr0`,
`onboot=0`. Boot to maintenance mode. It must be able to take `192.168.4.172` — the
`controlplane.yaml` pins that IP, and Peladn's 201 is fenced (step 3).

### B2. Apply the control-plane config (same identity)

```bash
# on the Evo-X2 host, with the OFFLINE decrypted copy:
talosctl apply-config --insecure -n <new-node-maintenance-ip> --file /root/dr/controlplane.yaml
# node installs to disk, reboots, comes up as 192.168.4.172 (not yet bootstrapped)
```

### B3. Bootstrap FROM the snapshot

```bash
SNAP=$(ssh talossnap@192.168.4.84 true 2>/dev/null; \
       ls -1t /mnt/backup-hdd/talos-etcd-snapshots/etcd_*.db | head -1)   # newest
talosctl bootstrap -n 192.168.4.172 -e 192.168.4.172 --recover-from "$SNAP"
```

`--recover-from` rebuilds a single-member etcd from the snapshot. Then:

```bash
talosctl -n 192.168.4.172 -e 192.168.4.172 health --wait-timeout 10m
kubectl get nodes
```

### B4. Re-point clients (only if the IP changed)

If for any reason the recovered CP is **not** on `.172`, update
`~/.kube/config` server, `~/.talos/config` endpoints/nodes, and
`cluster.controlPlane.endpoint`. Certs already include the identity from
`controlplane.yaml`, so no regen — just the endpoint. Keeping `.172` avoids all of this.

---

## 6. Post-recovery

- **Never boot the old Peladn VM 201 again.** Its etcd is stale and it would fight
  for `192.168.4.172`. `qm set 201 --onboot 0` is already done (step 3); leave it
  stopped, then destroy it once the Evo-X2 CP is proven.
- Flux resumes reconciling on its own.
- `talos-backup.sh` keeps snapshotting (it targets `.172`, wherever that now lives).
- **Watch etcd**: `talosctl -n 192.168.4.172 etcd status` — single member, healthy.

## 7. Fail back to Peladn (once it's healthy)

1. Rebuild / repair the Peladn host.
2. Take a fresh backup of the **now-authoritative** CP running on Evo-X2
   (`qm stop` it briefly + vzdump, or `talosctl etcd snapshot`).
3. Restore that onto Peladn as VM 201, `onboot=1`.
4. Stop + destroy the Evo-X2 CP VM. Re-enable `onboot` only on the Peladn one.
5. Never have both running — same IP, same etcd identity = split brain.
6. Re-add the OPNsense reservation / confirm `.172` lands on the Peladn VM.

---

## Drill (do this on a maintenance window)

**Quick validation** (proves the backup is restorable, zero prod impact):

```bash
ssh root@192.168.4.84
LATEST=$(pvesm list pbs-local | awk '$1 ~ "backup/vm/201/" {print $1}' | sort | tail -1)
qmrestore "$LATEST" 251 --storage local-lvm         # throwaway VMID, do NOT start on the real NIC/IP
qm config 251 ; qm destroy 251
```

**Full drill** (exercises `peladn-failover.sh` end to end):

1. `ssh root@192.168.4.150 'qm stop 201'` — leave it stopped.
2. On Evo-X2: `FORCE=1 bash /usr/local/bin/peladn-failover.sh` (FORCE=1 because Peladn's
   API is still up during a drill).
3. Time it. Verify `kubectl get nodes` / `talosctl -n .172 health`.
4. **Cleanup, in order:** `qm stop 201 && qm destroy 201` **on Evo-X2** (the restored
   copy) and `pct stop/destroy 203 202` on Evo-X2 → then `ssh root@192.168.4.150 'qm start 201'`.
   Confirm the original CP rejoins and etcd is healthy.
5. Re-run `talos-backup.sh` once and confirm a fresh snapshot lands.

> During the full drill VM 201 briefly exists on **both** hosts. The Peladn one is
> stopped the whole time — never start it until the Evo-X2 copy is destroyed.
