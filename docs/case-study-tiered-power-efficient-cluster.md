# Case Study: A Power-Efficient Tiered Cluster on Mismatched Hardware

**Problem:** Six mismatched machines — mini PCs, a tower, an enterprise 1U, a Raspberry Pi — and a requirement to run ~30 self-hosted services plus local LLM inference without a server-grade power bill. A single always-on i9 drew ~110 W idle and was a single point of failure for everything.

**Solution:** One Kubernetes cluster spanning an always-on core and a Wake-on-LAN burst tier, with workload placement expressed as **node labels** rather than hostnames, so a machine can change role without touching a single manifest. Standing draw is ~40–60 W for everything that must never be down.

---

## Tiers as they run today

| Tier | Machine | Power | Role |
| ---- | ------- | ----- | ---- |
| 1 | Peladn — Ryzen 7 8845HS, 32 GB, Radeon 780M | 24×7, ~15–25 W | Talos control plane VM + two Docker LXCs (household services, media) + the 11 TB DAS |
| 1 | Evo-X2 — Ryzen AI Max+ 395, 96 GB UMA, Radeon 8060S | 24×7, ~20–30 W | Ollama on the host, PBS, observability LXC, K8s worker `tier=ai-worker` |
| 1 | Raspberry Pi 4 — 4 GB, ARM64 | 24×7, ~5 W | K8s worker `tier=always-on`, bare-metal Talos from USB SSD |
| 2 | Intel NUC i3-4010U · Dell R610 · i9-14900K + RTX 5070 | WOL, 0 W asleep | K8s workers `tier=on-demand`, batch and burst only |

Single cluster (`Ira-cluster`), mixed architecture (amd64 + arm64), one control plane, GitOps via Flux CD.

---

## The placement rules

Four rules decide where anything runs. They are the transferable part of this design; the hardware list is not.

### Rule 1 — State on local disk, bulk on shared disk

Databases and anything transactional live on node-local NVMe (`local-path` PVCs, or the LXC rootfs). Media and archives live on the DAS. This rule exists because violating it corrupted a database — see [ADR-004](../ADR/ADR-004-local-path-over-nfs-for-databases.md) and the [storage case study](case-study-storage-architecture.md).

The consequence for scheduling: a `local-path` PVC pins its pod to one node, so data gravity has to be an explicit decision rather than a surprise.

### Rule 2 — Monitor the box from a different box

Observability (VictoriaMetrics, Loki, Grafana, Alloy) was originally planned for the host that runs the Kubernetes control plane. That places the diagnostic tooling inside the failure domain it exists to diagnose. It moved to a separate always-on host the moment one existed — CT 405 on the Evo-X2, with agents (`vmagent`, `alloy-logs`) remaining in-cluster.

The same reasoning moved Proxmox Backup Server and the 26 TB archive drive off the machine being backed up.

### Rule 3 — Nothing a person waits on sits behind a WOL wake

A 60–90 second wake is acceptable for a nightly batch job and unacceptable for a chat UI, a bookmark manager or a dashboard. Interactive workloads are pinned to `tier=ai-worker` / `tier=always-on`; only unattended work targets `tier=on-demand`.

This is also why local inference moved off the WOL GPU box: waking a machine before every prompt is not a workflow anyone sustains.

### Rule 4 — Schedule by label, never by hostname

Labels are declared in the Talos machine config, so they survive a node rebuild:

```yaml
# kubernetes/talos-config/worker-evo-x2.yaml
machine:
  nodeLabels:
    node-role: worker
    tier: ai-worker
    arch: amd64
    gpu: "false"      # inference runs on the HOST iGPU, not inside the VM
```

| Label | Contract | Carried by |
| ----- | -------- | ---------- |
| `tier=always-on` | 24×7, low power, ARM64 | RPi4 |
| `tier=ai-worker` | interactive AI; anything a person waits on | Evo-X2 worker VM |
| `tier=on-demand` | may be asleep; batch only | NUC, R610, i9 |
| `arch=arm64` / `amd64` | multi-arch awareness | per node |

Workloads then express intent, not topology:

```yaml
spec:
  template:
    spec:
      nodeSelector:
        tier: ai-worker
```

**Payoff:** when AI workloads moved from the i9 to the Evo-X2, zero manifests changed — only which machine carried `tier=ai-worker`. Hostname pinning would have made that a pull request touching every AI service.

---

## Where workloads actually land

| Workload | Selector | Node |
| -------- | -------- | ---- |
| hermes-agent, hermes-family, open-webui, karakeep (+ Postgres, Meilisearch), hindsight, searxng | `tier=ai-worker` | Evo-X2 |
| n8n, miniflux (+ Postgres), beszel | `tier=always-on` | RPi4 |
| n8n Postgres, nut, homepage, vmagent, alloy-logs | `kubernetes.io/hostname` | RPi4 |
| Nextcloud, Immich, Jellyfin, Frigate | Docker LXC (CT 202) | Peladn — needs `/dev/dri` + DAS |
| Home Assistant, NPM, Vaultwarden, Mosquitto, Gotify, UpSnap | Docker LXC (CT 203) | Peladn — needs Zigbee USB, owns 80/443 |

Two patterns target the same node deliberately: `tier=always-on` is a capability promise, a hostname pin is an admission that a PVC ties the pod to one disk. Hostname pins are used only to mark data gravity.

### Why some services are not in Kubernetes at all

Anything bound to hardware on a specific machine — the Zigbee stick, the iGPU render node, the UPS USB cable — runs in a Docker LXC on that machine. Kubernetes gains nothing by scheduling a pod that can only ever run on one host, and loses the simplicity of a compose file next to the device.

---

## WOL orchestration

```text
n8n workflow  →  UpSnap API  →  WOL packet  →  Proxmox boots (--onboot 1)
              →  Talos worker VM auto-starts  →  node joins  →  Pending pods schedule
```

`node-waker` and `node-sleeper` workflows in n8n own node power; UpSnap runs in the home-ops LXC. Every WOL host's Talos VM is set `--onboot 1` so a wake needs no human step. Kubernetes marks a sleeping node `NotReady` after ~5 minutes, which is why nothing interactive is scheduled there.

n8n itself runs on the always-on tier — the orchestrator cannot live on a node it might have to wake.

---

## Results

| Metric | Before | After |
| ------ | ------ | ----- |
| Always-on idle draw | ~110 W (single i9) | ~40–60 W (three machines) |
| Single point of failure | one host ran everything | control plane, AI, observability, backups split across two hosts |
| Largest local model | capped by 24 GB ceiling | `qwen3.6:35b-a3b` at ~44 tok/s on an iGPU (96 GB UMA) |
| Machine role change cost | manifest edits per service | one label move, zero manifest edits |
| Monitoring blast radius | same host as control plane | independent always-on host |
| Backup location | on the machine being backed up | second always-on host, local restore |

---

## Lessons Learned

- **A second always-on host changes more decisions than a faster one.** Observability placement, backup placement and the failover model all improved simply because there was somewhere else to put things.
- **Unified memory beat a discrete GPU for inference.** 96 GB of shared memory on an iGPU runs a 35B model that a 12 GB discrete card cannot load at all. Worth benchmarking before buying VRAM.
- **Idle watts, not peak performance, decide what stays on.** An i3-4010U lost its always-on role to a Raspberry Pi doing the same work at ~5 W — several times less standing draw for the same result.
- **Labels are an abstraction worth paying for on day one.** Every hardware reshuffle since has been a label change, not a refactor.
- **A tier with one member is a single point of failure with a nicer name.** `tier=always-on` currently resolves to one node, so pods pinned to it have nowhere to go if the Pi is down — a conscious trade against running replicated storage, and one worth knowing explicitly.
- **Plan for the shape, not the assignment.** The always-on-core-plus-burst shape survived every revision; almost every specific machine assignment inside it changed at least once.
