# Homelab Build Guide — Build This, In Order

This is the sequence I would follow if I were building this lab again from nothing, written so somebody else can follow it on different hardware. Every stage links the actual configs in this repo, states how to verify it worked, and names the mistake I made there the first time.

It is a **build order**, not a tutorial for each tool — upstream docs do that better. What this adds is the order, the verification gates, and the decisions that are expensive to change later.

**What you end up with:** a single Talos Kubernetes cluster spanning an always-on core and a Wake-on-LAN burst tier, Docker LXCs for anything tied to physical hardware, all state reconciled from one public Git repo by Flux CD, secrets committed encrypted, backups on a host that is not the host being backed up, and ~40–60 W of standing power draw.

**Who this is for:** anyone with a pile of mismatched machines who wants a production-shaped platform rather than a pile of `docker run` commands. Roughly intermediate — you should be comfortable in a shell and have seen Kubernetes before.

---

## Translating this to your hardware

You almost certainly do not have my machines. What matters is the *roles*, not the models:

| Role in this build | Mine | Minimum sensible substitute |
| ------------------ | ---- | --------------------------- |
| **Primary always-on host** — hypervisor, control plane, hardware-bound services | Peladn mini PC, Ryzen 7 7840HS, 32 GB | any mini PC with 16 GB+ and an iGPU |
| **Second always-on host** — observability, backups, AI | GMKtek Evo-X2, 96 GB unified | a second mini PC; large unified memory only matters if you run local LLMs |
| **Low-power always-on worker** | Raspberry Pi 4, 4 GB, ARM64 | any SBC, or skip and use the primary host |
| **Burst workers** | Intel NUC, Dell R610, i9 + RTX 5070 | anything you already own that supports WOL — optional |
| **Bulk storage** | 2-bay USB DAS, 2× 12 TB in RAID 1 | any direct-attached storage on the primary host |
| **Archive target** | 26 TB external on the second host | any large disk, attached to a *different* machine |
| **Perimeter** | Lanner SED7551 running OPNsense | any router that lets you set static DHCP reservations |

**Bare minimum to follow this guide meaningfully:** one always-on host with a hypervisor and one disk. Stages 9–12 are additive and can wait months.

---

## Stage 0 — Decide four things before touching hardware

These are the decisions that are painful to reverse. Mine, with the reasoning written up:

| Decision | Choice | Why | Reference |
| -------- | ------ | --- | --------- |
| Kubernetes distribution | Talos Linux | immutable, API-only, no SSH, single-file machine config | [ADR-001](../ADR/ADR-001-talos-over-k3s.md) |
| GitOps engine | Flux CD | ~150 MB footprint, native SOPS, no UI to secure | [ADR-002](../ADR/ADR-002-flux-over-argocd.md) |
| Where databases live | node-local disk, never network storage | NFS locking broke SQLite and corrupted a database | [ADR-004](../ADR/ADR-004-local-path-over-nfs-for-databases.md) |
| Docker vs Kubernetes split | hardware-bound services in LXC, everything else in K8s | scheduling a pod that can only run on one host gains nothing | [tiering case study](case-study-tiered-power-efficient-cluster.md) |

**The rule worth internalising before anything else:** decide *where state lives* before deciding what runs where. Every placement rule downstream follows from it.

---

## Stage 1 — Hypervisor on every host

**Goal:** Proxmox VE on each machine that will run guests.

- Flash Proxmox VE 9.x with Rufus/balenaEtcher; install on each host.
- **Wired Ethernet only.** Proxmox does not handle Wi-Fi well and a 24×7 lab should not depend on it.
- Set a static DHCP reservation per host on your router, keyed to the MAC.
- Existing Proxmox 8 hosts upgrade in place with `apt` rather than reinstalling.

**Verify:** each host's web UI reachable at `https://<ip>:8006`, and pingable by hostname.

**Pitfall:** leave a machine that still hosts live services alone until the cluster can take those workloads over. I migrated my main i9 last, not first, and that was correct.

---

## Stage 2 — Repo and secrets, before the first config exists

**Goal:** a public Git repo where committing secrets is safe, and a workstation that never stores the private key on disk.

```bash
git clone https://github.com/<you>/Homelab-ops.git && cd Homelab-ops
```

```powershell
winget install -e --id Sidero.talosctl
winget install -e --id Kubernetes.kubectl
winget install FiloSottile.age
winget install Bitwarden.CLI
```

```bash
age-keygen -o dev-key.txt     # public key -> .sops.yaml (safe to commit)
                              # private key -> password manager only, then delete the file
```

Encrypting later, with the key injected into session memory and never written to disk:

```powershell
$env:BW_SESSION  = $(bw unlock --raw)
$env:SOPS_AGE_KEY = $(bw get notes "HOMELAB_SOPS_KEY")
sops --encrypt --in-place secret.yaml
```

**Repo refs:** [`.sops.yaml`](../.sops.yaml) · [`.gitignore`](../.gitignore) · [GitOps case study](case-study-gitops-talos-flux.md)

**Verify:** `sops --decrypt secret.enc.yaml` returns plaintext; `git status` shows no plaintext secret; `.gitignore` excludes `talos-config`, `controlplane.yaml`, `worker.yaml`, `kubeconfig`, `.env`.

**Pitfall:** set `.gitignore` up **before** generating Talos configs. Those files contain the cluster CA and join tokens, and a public repo does not forget.

---

## Stage 3 — Storage on the host, not in a guest

**Goal:** bulk storage mounted once, on the hypervisor, by UUID.

```bash
lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINT,SERIAL,MODEL
mkfs.xfs /dev/sdX                       # first time only — erases the disk
mkdir -p /mnt/pvedas
# /etc/fstab:
# UUID=<uuid>  /mnt/pvedas  xfs  defaults,nofail,nouuid,x-systemd.device-timeout=15  0 2
systemctl daemon-reload && mount -a
```

If the storage is USB-attached, harden the path before trusting it: disable autosuspend for the controller, force BOT over UAS, and add a mount watchdog.

**Repo refs:** [`infrastructure/peladn-host/`](../infrastructure/peladn-host/) — fstab, udev rule, `das-mount-watchdog.*` · [storage case study](case-study-storage-architecture.md)

**Verify:** `df -h /mnt/pvedas` after a **reboot** (not just after `mount -a`), and confirm the fstab entry uses a UUID.

**Pitfall — the biggest one in this build:** do not build a storage VM or container that re-exports the disk over NFS to guests on the same host. Kernel `nfsd` cannot export a bind-mounted path from inside an LXC (`exportfs: Failed to stat …`), and even if it could, you have added a network hop and a failure domain between a disk and a container sitting on the same machine. I built that container, ran it for three weeks, and deleted it.

---

## Stage 4 — Docker LXCs for hardware-bound services

**Goal:** two unprivileged Debian LXCs, with bulk storage bind-mounted in.

```bash
pveam update && pveam download local debian-13-standard_13.0-1_amd64.tar.zst
# create both CTs unprivileged, then in /etc/pve/lxc/<id>.conf:
#   features: nesting=1
#   mp0: /mnt/pvedas,mp=/mnt/das
#   lxc.cgroup2.devices.allow: c 226:* rwm          # only for the media/GPU container
#   lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
pct start 202 && pct exec 202 -- ls /mnt/das
```

Inside each: `apt install -y docker.io docker-compose curl git`, then clone the repo and bring the stack up from the committed compose file.

**Repo refs:** [`infrastructure/media-ops-lxc/`](../infrastructure/media-ops-lxc/) (CT 202 — Nextcloud, Immich, Jellyfin, Frigate) · [`infrastructure/home-ops-lxc/`](../infrastructure/home-ops-lxc/) (CT 203 — Home Assistant, NPM, Vaultwarden, Mosquitto, Gotify, UpSnap)

**Verify:** `docker ps` shows the stack; `docker exec jellyfin ls /dev/dri/` shows `renderD128`; every database path resolves to the container rootfs, **not** `/mnt/das`.

**Pitfall:** unprivileged LXCs map container UID 0 to host UID 100000. Run every `chown` for bind-mounted paths **on the host** with the mapped UID (container `1000:1000` → host `101000:101000`). Doing it inside the container looks successful and is not.

---

## Stage 5 — Talos control plane

**Goal:** one control-plane VM, managed entirely by API.

Build the image at [factory.talos.dev](https://factory.talos.dev) — nocloud platform, amd64, `qemu-guest-agent` extension. VM: q35 + OVMF (UEFI), VirtIO Block disk, VM-level firewall **off** (the perimeter firewall is the perimeter).

```powershell
talosctl gen config <cluster-name> https://<cp-ip>:6443
# patch controlplane.yaml: static IP, qemu-guest-agent extension,
#   cluster.allowSchedulingOnControlPlanes: true   (single-CP clusters)
talosctl apply-config --insecure --nodes <cp-ip> --file controlplane.yaml
talosctl config merge ./talosconfig
talosctl config endpoint <cp-ip>; talosctl config node <cp-ip>
talosctl health --wait-timeout 5m
talosctl bootstrap --nodes <cp-ip>        # ONCE, ever
talosctl kubeconfig ./kubeconfig --nodes <cp-ip>
kubectl get nodes
```

**Repo refs:** [`kubernetes/talos/`](../kubernetes/talos/) (encrypted machine configs) · [`kubernetes/talos/patches/`](../kubernetes/talos/patches/)

**Verify:** `kubectl get nodes` shows the CP `Ready`; `kubectl get pods -A` has no `CrashLoopBackOff`.

**Pitfall:** `talosctl bootstrap` is once per *cluster*, not once per node. If `talosctl health` hangs, the node is usually still booting — `talosctl dmesg --nodes <ip> | tail -20` is the useful command.

---

## Stage 6 — First always-on worker, with labels

**Goal:** a low-power worker whose labels are part of its machine config.

```yaml
machine:
  network: { hostname: talos-rpi4, interfaces: [ ... static IP ... ] }
  nodeLabels:
    node-role: worker
    tier: always-on
    arch: arm64
```

```powershell
talosctl apply-config --insecure --nodes <worker-ip> --file worker.yaml
kubectl get nodes --show-labels
```

**Verify:** node `Ready`, labels present, and still present after a reboot.

**Pitfall:** do not apply labels with `kubectl label node`. Put them in the machine config so they survive a rebuild — the labels are your scheduling contract, and re-adding them by hand at the wrong moment is how workloads silently end up in the wrong tier. On ARM boards, flash the **arm64** image and prefer a USB SSD over a microSD card.

**Pitfall — the static IP that silently doesn't apply.** Name the interface correctly or
your static address is ignored with no error at all. A Talos VM on Proxmox with a virtio
NIC comes up as **`ens18`**, not `eth0`; Talos skips an address block for an interface
that doesn't exist and falls back to DHCP, so the node joins on a lease and the config
*looks* right. Check the real name before applying:

```bash
talosctl get links --nodes <ip>      # in maintenance mode: add --insecure
```

Two of the workers in this repo carried that bug for months — the committed config said
one address while the node ran on another, which also makes the repo a misleading record
of the running cluster. When you clone an existing worker config for a new node, re-check
the interface name on the new hardware.

**Pitfall — never `talosctl gen config` for an additional worker.** It generates a *new*
cluster CA and join token, and the node will never join. Copy an existing worker config
and change only the hostname, address, and labels.

---

## Stage 7 — Flux CD and in-cluster decryption

**Goal:** the cluster pulls from Git; you stop running `kubectl apply`.

```powershell
flux bootstrap github --owner=<you> --repository=Homelab-ops `
  --branch=main --path=kubernetes/clusters/homelab --personal

kubectl create secret generic sops-age -n flux-system --from-file=age.agekey=age.key
```

**Repo refs:** [`kubernetes/clusters/homelab/`](../kubernetes/clusters/homelab/) — `apps.yaml` carries the `decryption` block that makes encrypted secrets in a public repo work.

**Verify:** `flux get kustomizations` shows `Applied`; a trivial commit appears in-cluster within the reconcile interval.

**Pitfall:** `prune: true` means deleting a manifest from Git deletes the resource from the cluster. Correct for GitOps, surprising the first time.

---

## Stage 8 — First applications and storage classes

**Goal:** real workloads, with state on local disk from day one.

Structure per app: `kubernetes/apps/<name>/` with `namespace.yaml`, manifests, `kustomization.yaml`; register it in [`kubernetes/apps/kustomization.yaml`](../kubernetes/apps/kustomization.yaml); encrypt any secret; push.

Use `local-path` for every PVC that holds a database. Add NFS **only** for a workload that genuinely needs RWX from another node — in this lab exactly one does, and the export is served by the host that owns the disk.

**Verify:** `kubectl get pvc -A` shows every database PVC on `local-path`; `kubectl get pods -A -o wide` shows pods where you intended.

**Pitfall:** a `local-path` PVC pins its pod to one node forever. That is fine, but pin it *on purpose* with a hostname selector so the constraint is documented rather than discovered.

---

## Stage 9 — Wake-on-LAN burst workers *(optional)*

**Goal:** extra compute that costs nothing while idle.

- Enable WOL in BIOS on each burst machine.
- Talos worker VM on each, with `--onboot 1` so a wake needs no human step.
- Label them `tier=on-demand`.
- UpSnap (in the home-ops LXC) sends the packets; n8n workflows (`node-waker`, `node-sleeper`) decide when.

```text
n8n → UpSnap API → WOL packet → Proxmox boots → Talos VM auto-starts → node joins
```

**Verify:** power the host down, trigger the waker, and watch `kubectl get nodes` go `NotReady → Ready` without intervention.

**Pitfall:** keep the orchestrator on the always-on tier — n8n cannot be the thing that wakes the node it runs on. And schedule nothing interactive here: Kubernetes takes ~5 minutes to mark a sleeping node `NotReady`.

---

## Stage 10 — Backups, on a different machine

**Goal:** Proxmox Backup Server on a host that is *not* the host being backed up.

- PBS in its own LXC on the second always-on host, with the archive disk attached locally.
- `vzdump` of the primary host's guests, scheduled by n8n over SSH.
- Exclude bulk media from `vzdump` (`backup=0` on those mount points) and back it up separately on a weekly cadence.

**Repo refs:** [`infrastructure/pbs-backup-lxc/`](../infrastructure/pbs-backup-lxc/) · [`infrastructure/peladn-host/backup-scripts/`](../infrastructure/peladn-host/backup-scripts/)

**Verify:** actually restore something. A backup job that has never been restored from is a hypothesis.

**Pitfall:** if PBS is privileged, it must be **restored** privileged — the chunk store is owned by a real UID and restoring unprivileged shifts ownership and breaks the datastore. Also: because databases sit in container rootfs (Stage 3's rule), `vzdump` captures them, so a restore yields working services rather than empty shells.

---

## Stage 11 — Observability, from outside the blast radius

**Goal:** metrics, logs and alerts that survive the failure they need to explain.

- VictoriaMetrics + Loki + Grafana + Alloy in an LXC on the **second** always-on host.
- `node-exporter` on every host including the firewall; `vmagent` and `alloy-logs` in-cluster, remote-writing outward.
- Alerts to Gotify.

**Repo refs:** [`infrastructure/observability-lxc/`](../infrastructure/observability-lxc/) · [`kubernetes/apps/monitoring/`](../kubernetes/apps/monitoring/)

**Verify:** stop the primary host and confirm Grafana is still reachable and alerting.

**Pitfall:** do not put this on the control-plane host. I planned to, and it would have meant losing the cluster and the diagnostic tooling in the same incident.

---

## Stage 12 — Local AI tier *(optional)*

**Goal:** local inference that is actually usable, plus the services around it.

- Ollama on the **host** (not in a container) where the iGPU and unified memory are, LAN-firewalled.
- A Talos worker VM on the same host labelled `tier=ai-worker` for the agent/UI/memory services.
- Keep a small model next to its small consumers — cheap tagging and embedding should not queue behind a 35B model.

**Repo refs:** [`infrastructure/ollama-host/`](../infrastructure/ollama-host/) · [`kubernetes/apps/hermes-agent/`](../kubernetes/apps/hermes-agent/) · [`kubernetes/apps/hindsight/`](../kubernetes/apps/hindsight/) · [`kubernetes/apps/open-webui/`](../kubernetes/apps/open-webui/) · [AI memory case study](case-study-ai-memory-layer.md) · [model selection case study](case-study-local-model-selection.md)

**Verify:** a prompt answered end to end with the burst nodes powered off.

**Pitfall:** benchmark memory capacity against model size before buying a GPU. 96 GB of unified memory runs a 35B model at ~44 tok/s here; a 12 GB discrete card cannot load it at all.

---

## Whole-lab verification

Work through this after a full power cycle, not after a clean run:

- [ ] Every host comes back without manual intervention
- [ ] `df -h` shows bulk storage mounted, by UUID
- [ ] LXC stacks up; GPU device visible inside the media container
- [ ] `kubectl get nodes` — control plane and always-on workers `Ready`
- [ ] `flux get kustomizations` — all `Applied`
- [ ] No plaintext secret anywhere in `git log -p` (check history, not just the working tree)
- [ ] Every database PVC on `local-path`; no database on network or USB storage
- [ ] A backup restored successfully at least once
- [ ] Grafana reachable with the primary host down
- [ ] A WOL node wakes, joins, and takes scheduled work
- [ ] Standing power draw measured, not estimated

---

## What I would do differently

In the order the pain arrived:

1. **Mount bulk storage on the host from day one.** The storage LXC cost three weeks and taught one lesson.
2. **Prove the storage hardware before designing around it.** My DAS disconnects were a missing heatsink on the USB bridge, not a driver bug. A design built to tolerate them would have made the fault permanent.
3. **Databases on local disk from the first deployment.** I learned this by restoring a corrupted n8n database.
4. **Put observability on the second host immediately.** I planned it on the control-plane host and only moved it when a second always-on machine appeared.
5. **Buy the second always-on host before the faster one.** Nearly every structural improvement here came from having *somewhere else* to put things, not from more speed.
6. **Label-based scheduling from the first worker.** Every hardware reshuffle since has been a label move instead of a refactor.
7. **Give the always-on tier two members.** One labelled node is a single point of failure with a nicer name.

---

## Power and cost

| Always-on | Approx. idle | Carries |
| --------- | ------------ | ------- |
| Primary host | ~15–25 W | control plane, household services, media, bulk storage |
| Second host | ~20–30 W | AI inference, observability, backups |
| SBC worker | ~6.5 W | always-on Kubernetes worker |
| **Total** | **~40–60 W** | everything that must never be down |

The burst tier adds nothing at idle. That is the entire argument for it — the Dell R610 alone would roughly triple the standing draw if it stayed awake.

---

## Using this repo for your own build

Everything here is a live, working system rather than a template — [`cluster.env.example`](../cluster.env.example) documents every environment-specific value you would need to change. A reasonable way to fork it:

1. Copy `kubernetes/apps/<app>/` for the applications you want and edit the `nodeSelector` to your own tier labels.
2. Take `infrastructure/peladn-host/` almost verbatim if you also have USB-attached storage — the watchdog and udev rules are hardware-generic.
3. Generate your **own** Talos configs and Age key. Never reuse mine, and never commit plaintext Talos configs.
4. Read the [ADRs](../ADR/) before changing a structural decision — each one records what the alternative cost.

Questions, corrections, or a better approach to any of this are genuinely welcome via issues on the repo.
