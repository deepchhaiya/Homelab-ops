# Case Study: Storage Architecture — Retiring an NFS Tier and Keeping Databases Off It

**Problem:** A 2-bay USB DAS was made the primary storage for a homelab cluster and re-exported over NFS by a dedicated storage container. The enclosure dropped off the USB bus under load, the NFS export could not be served from inside a container at all, and a database running on the resulting share corrupted itself.

**Solution:** Fix the hardware fault first, then delete the storage tier instead of working around it. Mount the disk once on the hypervisor host, hand it to guests as bind mounts, keep exactly one NFS export for the one workload that genuinely needs RWX from another node, and move every database onto local disk as a written rule.

---

## Starting design (2026-03) and why it failed

| Component | As built | Outcome |
| --------- | -------- | ------- |
| CT 200 `hl-storage-lxc` | Privileged LXC, DAS passed in as a raw block device (`lxc.cgroup2.devices.allow: b 8:16 rwm`), XFS formatted in-container | Retired after ~3 weeks |
| NFS | `nfs-kernel-server` inside CT 200, exporting `/mnt/das` to the LAN | Moved to the host |
| Consumers | Two Docker LXCs on the same physical host + K8s PVCs | LXCs switched to bind mounts; K8s reduced to one export |

Three independent failures stacked up.

### 1. Thermal — the USB bridge had no heatsink

The enclosure uses an ASMedia ASM1352R USB-to-SATA RAID bridge (USB ID `174c:1352`). Under sustained I/O it disconnected and re-enumerated under a new block device name, breaking every bind mount that referenced it.

Diagnosis came from the disconnect *pattern* (load-correlated, not time-correlated) rather than from logs. The bridge chips on the backplane ship bare. Adding two salvaged heatsinks eliminated the disconnects; throughput settled at ~300 MB/s.

Two further mitigations were applied at the OS level, because a USB storage path deserves defence in depth:

```bash
# Disable USB autosuspend for this controller only
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="174c", ATTR{idProduct}=="1352", \
  TEST=="power/control", ATTR{power/control}="on"

# Force BOT instead of UAS — the UAS driver is unstable on ASMedia bridges
echo 'options usb-storage quirks=174c:1352:u' > /etc/modprobe.d/disable-uas-asmedia.conf
```

### 2. Architectural — kernel `nfsd` cannot export a bind-mounted path from inside an LXC

Once the DAS was mounted on the host and handed to CT 200 as a bind mount (`mp0: /mnt/pvedas,mp=/mnt/das`), NFS stopped working entirely:

```text
# inside CT 200
exportfs -ra
exportfs: Failed to stat /mnt/pvedas/n8n: No such file or directory

# client side
mount.nfs: mounting 192.168.4.11:/mnt/das/n8n/configuration failed,
reason given by server: No such file or directory
```

`nfsd` lives in the host kernel and LXCs share it. Asked to export a bind-mounted path, it resolves back to the **source inode path on the host** (`/mnt/pvedas/...`), which is not visible in the container's mount namespace. Workarounds exist (`features: nesting=1,nfs=1`, userspace nfs-ganesha) and were rejected as fragile.

The container's only job was serving NFS, so the container was the thing to remove — not the NFS configuration.

### 3. Data integrity — SQLite on a DAS-backed NFS PVC

n8n ran its default SQLite database on an NFS PVC backed by the DAS. On 2026-06-19 the enclosure flapped mid-write and n8n returned `SQLITE_CORRUPT: database disk image is malformed`.

NFS file locking is advisory across the network and does not provide the guarantees SQLite assumes. Layering that over a USB enclosure that intermittently disappears made corruption a matter of timing, not chance.

---

## Current design

```text
Peladn (192.168.4.150, Proxmox host) — DAS physically attached
│
├── /mnt/pvedas          10.9 TiB DAS, XFS, mounted by UUID in /etc/fstab
│   ├── bind mount ──►   CT 202  hl-media-ai-ops-lxc   as /mnt/das
│   ├── bind mount ──►   CT 203  hl-home-ops-lxc       as /mnt/das
│   └── nfsd on the HOST ──► K8s: /mnt/pvedas/karakeep/assets   (only export)
│
├── /mnt/sg-ext-hdd      2 TB Seagate external, host mount
├── /mnt/wd-ext-hdd      2 TB WD external, host mount
└── local NVMe           all databases and transactional state
```

### Mount by UUID, reference by mountpoint

USB `/dev/sdX` letters shuffle across reboots, so the host mounts by filesystem UUID and everything downstream (Beszel's `EXTRA_FILESYSTEMS`, scripts, bind mounts) references the mountpoint:

```bash
UUID=<uuid>  /mnt/pvedas  xfs  defaults,nofail,nouuid,x-systemd.device-timeout=15  0 2
```

| Option | Reason |
| ------ | ------ |
| `nofail` | a missing or late USB drive must never block boot |
| `x-systemd.device-timeout=15` | bounds the boot wait |
| `nouuid` | required for XFS when the filesystem UUID appears elsewhere |
| no `x-systemd.automount` | the automount registered `/mnt/pvedas` twice in `/proc/mounts` (an `autofs` entry plus the real device), making Beszel key on `systemd-1` and report capacity with no disk I/O |

Config: [`infrastructure/peladn-host/fstab-external-drives.conf`](../infrastructure/peladn-host/fstab-external-drives.conf)

### Bind mounts instead of NFS for same-host guests

```bash
pct set 202 -mp0 /mnt/pvedas,mp=/mnt/das
pct set 203 -mp0 /mnt/pvedas,mp=/mnt/das
```

Guests still see `/mnt/das`, so no Docker Compose volume path changed — the migration was invisible above the mount.

> **Unprivileged LXC ownership:** container UID 0 maps to host UID 100000. All `chown` fixes for bind-mounted paths must run on the Proxmox host with the mapped UID (container `1000:1000` → host `101000:101000`). Running `chown` inside the container appears to succeed and does not produce the intended ownership.

### One NFS export, on the host

Karakeep runs in Kubernetes on a different physical node and needs RWX access to its asset directory:

```bash
echo "/mnt/pvedas/karakeep 192.168.4.0/24(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports
systemctl enable --now nfs-server && exportfs -ra
```

```yaml
# kubernetes/apps/karakeep/karakeep-assets-pv.yaml
spec:
  accessModes: [ReadWriteMany]
  storageClassName: nfs-pvedas-karakeep
  mountOptions: [nfsvers=4.1, hard, timeo=600, retrans=2]
  nfs:
    server: 192.168.4.150
    path: /mnt/pvedas/karakeep/assets
```

### A flap stops the export permanently without a watchdog

`nfs-server` depends on the `/mnt/pvedas` mount unit. When the DAS drops, systemd stops the service and does **not** restart it when the mount returns. `das-mount-watchdog` (systemd timer, ~2 min tick) re-mounts the DAS, restarts `nfs-server`, re-runs `exportfs -ra`, and sends a Gotify notification.

Scripts: [`infrastructure/peladn-host/`](../infrastructure/peladn-host/)

---

## Placement rules that came out of it

| Data class | Location | Mechanism |
| ---------- | -------- | --------- |
| Media, photos, cloud files, camera clips | DAS | bind mount |
| Shared assets needed RWX from another node | DAS | NFS from the host (one export) |
| Databases — Home Assistant, Vaultwarden, NPM, Mosquitto, CouchDB, app DBs | local NVMe | local path |
| K8s stateful apps — n8n + Postgres, Miniflux, Hindsight | node-local disk | `local-path` storage class |
| Backups and archives | 26 TB HDD on a second host | PBS over the network |

The database rule is written down as [ADR-004](../ADR/ADR-004-local-path-over-nfs-for-databases.md) specifically so it does not get quietly reversed by a later convenience decision.

---

## Results

| Metric | Before | After |
| ------ | ------ | ----- |
| DAS disconnects under load | recurring | none since the heatsink fix |
| Sustained throughput | unstable | ~300 MB/s |
| Guests serving storage | 1 dedicated LXC | 0 |
| Hops between a container and a disk on the same host | 1 (NFS) | 0 (bind mount) |
| NFS exports in the lab | all bulk storage | 1 (`karakeep/assets`) |
| Database corruption incidents | 1 (n8n SQLite) | 0 since moving to `local-path` |
| Recovery from a DAS flap | manual re-mount | automatic, with notification |

A side effect worth noting: because the databases now sit in each container's rootfs rather than on bind-mounted bulk storage, a `vzdump` snapshot captures them. A restore brings back working services (vault, Home Assistant config, app DBs) rather than empty shells, and bulk media can be restored separately on a slower cadence.

---

## Lessons Learned

- **Diagnose the hardware before designing around it.** The disconnects looked like a driver or cabling problem and were a cooling problem. A design built to tolerate them would have shipped the fault permanently.
- **Never put a container between a disk and other containers on the same host.** The storage LXC added a failure domain, a network hop, and a guest to maintain, in exchange for nothing the hypervisor could not do directly.
- **`nfsd` is a kernel service, not a container workload.** If the export path arrives as a bind mount, `exportfs` cannot resolve it from inside the namespace. Serve from where the disk is mounted.
- **Databases do not belong on NFS, and absolutely not on USB-backed NFS.** SQLite over NFS is unsafe by design; the enclosure only decided the timing.
- **Mount removable storage by UUID.** Device letters are not stable identities on USB.
- **A service that depends on a mount needs a watchdog.** systemd will stop it on a flap and leave it stopped.
- **Write the rule down as an ADR.** "Databases on local disk" is easy to violate six months later when a shared path looks convenient.
