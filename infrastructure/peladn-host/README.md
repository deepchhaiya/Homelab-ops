# Peladn host configuration (192.168.4.150)

Host-level config for the Peladn base station that is **not** managed by Flux/GitOps
(it lives on the bare-metal Proxmox host, outside Kubernetes). Kept here so a host
rebuild is reproducible. Apply these by hand after reinstalling the host.

## Files

| File | Installs to | Purpose |
|---|---|---|
| `fstab-external-drives.conf` | append to `/etc/fstab` | UUID mounts for the 3 USB externals + DAS |
| `beszel-agent.service` | `/etc/systemd/system/` | Beszel monitoring agent (secrets redacted) |
| `das-mount-watchdog.{sh,service,timer}` | `/usr/local/sbin/` + `/etc/systemd/system/` | Re-mounts the DAS + restarts nfs-server after a flap |
| `99-das-usb-no-suspend.rules` | `/etc/udev/rules.d/` | Disables USB autosuspend on the DAS chain |
| `99-dri.rules` | `/etc/udev/rules.d/` | Makes `/dev/dri/*` mode 0666 so the unprivileged media LXC (CT 202) can use the 780M for VAAPI |
| `nextcloud-das-recover.sh` | `/usr/local/sbin/` | Nextcloud recovery after a DAS flap |
| `backup-scripts/` | `/home/n8n-backup/` + timers | Talos/k8s/PBS backup jobs (see its README) |

## Drives: mount by UUID, reference by mountpoint

The USB `/dev/sdX` letters on this host **shuffle across reboots** (kernel probe
order). So:
- **fstab** mounts every external by **UUID** → fixed mountpoint (`fstab-external-drives.conf`).
- **Everything else** (e.g. Beszel `EXTRA_FILESYSTEMS`) references the **mountpoint**,
  never `sdX`. NVMe names are stable, so `/dev/nvme0n1` is fine as a device path.
- `/mnt/pvedas` (the DAS) must stay a **plain mount** — see the warning in
  `fstab-external-drives.conf`; `x-systemd.automount` breaks Beszel's I/O graph.

## Apply on a rebuild

```bash
# 1. Drives
cat fstab-external-drives.conf >> /etc/fstab      # (verify UUIDs first with lsblk)
mkdir -p /mnt/sg-ext-hdd /mnt/wd-ext-hdd /mnt/pvedas
systemctl daemon-reload && mount -a

# 2. Beszel agent  (fill KEY + TOKEN from the Beszel hub first!)
cp beszel-agent.service /etc/systemd/system/
$EDITOR /etc/systemd/system/beszel-agent.service   # set KEY= and TOKEN=
# (the beszel-agent binary itself installs to /opt/beszel-agent/ via the hub's install cmd)
systemctl daemon-reload && systemctl enable --now beszel-agent

# 3. DAS watchdog + udev (see the individual files)

# 4. GPU access for the unprivileged media LXC
cp 99-dri.rules /etc/udev/rules.d/
udevadm control --reload-rules && udevadm trigger
ls -l /dev/dri/                                     # card0 + renderD128 should be crw-rw-rw-
```
