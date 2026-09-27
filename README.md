# Homelab-ops

Production-grade homelab platform built on **Talos Linux**, **Flux CD GitOps**, and **Proxmox** — engineered as a reference implementation for platform engineering, self-hosted AI infrastructure, and data resilience.

> This repo is a live, working system — not a template. See [`cluster.env.example`](cluster.env.example) for the environment-specific values you would need to adapt it to your own setup.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│  Tier 1 — Always-On (24/7)                                              │
│                                                                         │
│  Peladn  · Ryzen 7 8845HS · 32 GB · Radeon 780M iGPU                    │
│  ├── Talos K8s Control Plane VM (ira-peladn-talos-cp)                   │
│  ├── home-ops-lxc      · HA · NPM · Vaultwarden · ESPHome               │
│  └── media-ai-ops-lxc  · Nextcloud · Immich · Ollama (ROCm, qwen3:4b)   │
│                                                                         │
│  Evo-X2  · Ryzen AI Max+ 395 · 96 GB UMA · Radeon 8060S (gfx1151)       │
│  ├── PBS-lxc (CT200)               · Proxmox Backup Server              │
│  ├── observability-lxc (CT405)     · VictoriaMetrics · Loki · Grafana   │
│  ├── ollama-host  ← runs on host   · qwen3.6:35b-a3b · 44 tok/s         │
│  └── ira-evo-x2-talos-worker (VM)  · K8s worker (tier=ai-worker)        │
│       └── Hermes · Open WebUI · Karakeep · Hindsight (AI memory)        │
│                                                                         │
│  RPi4  · 4 GB · ARM64                                                   │
│  └── Talos K8s Worker · n8n · miniflux · beszel · nut · monitoring      │
├─────────────────────────────────────────────────────────────────────────┤
│  Tier 2 — On-Demand WOL                                                 │
│  Intel NUC  · Talos K8s Worker · general burst                          │
│  Dell R610  · Talos K8s Worker · heavy compute                          │
│  i9-14900K  · RTX 5070 · Talos Worker + Ollama CUDA                     │
└─────────────────────────────────────────────────────────────────────────┘
                  │ GitOps (Flux CD)
          ┌───────┴────────────────┐
          │  OPNsense (perimeter)  │
          └────────────────────────┘
```

**Key design decisions:** See [`ADR/`](ADR/) for full rationale.

---

## Stack

| Layer | Technology | Notes |
| ----- | ---------- | ----- |
| OS | [Talos Linux](https://www.talos.dev/) | Immutable, API-driven K8s OS |
| Orchestration | Kubernetes (K8s) | Mixed-arch cluster: 1 CP (Peladn) + 2 always-on workers (rpi4 ARM, Evo-X2 amd64) + WOL bursts |
| GitOps | [Flux CD v2](https://fluxcd.io/) | Push-based, native SOPS decryption at apply time |
| Secrets | [SOPS](https://github.com/getsops/sops) + Age | Encrypted secrets safe to commit |
| Hypervisor | [Proxmox VE 9.x](https://www.proxmox.com/) | LXC containers + VMs across Peladn + Evo-X2 |
| Backup | Proxmox Backup Server (PBS) on Evo-X2 | 26 TB Seagate external datastore, n8n-driven vzdump |
| Networking | OPNsense | Perimeter firewall, DNS, DHCP, NPM proxy frontends |
| Local LLM | [Ollama](https://ollama.com/) on Evo-X2 host (ROCm/gfx1151) | qwen3.6:35b-a3b @ ~44 tok/s on iGPU UMA |
| AI Agent | [Hermes Agent](https://hermes-agent.nousresearch.com/) | K8s pod, OpenAI-compat API, system-administrator skill |
| AI Memory | [Hindsight](https://github.com/vectorize-io/hindsight) | Self-hosted on Evo-X2 — native MCP + web UI, local embeddings + reranker, graph/temporal retrieval |
| Chat UI | [Open WebUI](https://github.com/open-webui/open-webui) | Browser front-end → both Ollama and Hermes models |
| Observability | Grafana + Loki + VictoriaMetrics + Alloy + Beszel | CT405 LXC on Evo-X2; 90d metrics / 30d logs retention |
| Notifications | Gotify | Grafana alerts + n8n workflows push here |

---

## Repository Structure

```text
Homelab-ops/
├── kubernetes/
│   ├── apps/                     # All K8s application manifests (Kustomize)
│   │   ├── beszel/               # At-a-glance host + container metrics
│   │   ├── hermes-agent/         # ⚕ Agent (skills + helper scripts + lab brain)
│   │   ├── karakeep/             # Bookmarks + AI auto-tag (assets on NFS DAS)
│   │   ├── hindsight/            # AI memory layer (Hindsight — native MCP + web UI)
│   │   ├── miniflux/             # RSS feed reader
│   │   ├── monitoring/           # vmagent + alloy-logs (K8s metrics + pod logs)
│   │   ├── n8n/                  # Workflow automation
│   │   ├── nut/                  # UPS monitoring
│   │   └── open-webui/           # Browser LLM chat — Ollama + Hermes backends
│   ├── talos/                    # Talos worker machine configs (SOPS-encrypted)
│   ├── talos-config/             # Plaintext talosconfig (for talosctl)
│   └── clusters/homelab/         # Flux CD cluster bootstrap + sync config
├── infrastructure/
│   ├── home-ops-lxc/             # CT203 — HA, NPM, Vaultwarden, etc.
│   ├── media-ops-lxc/            # CT202 — Nextcloud, Immich, Ollama (Peladn)
│   ├── observability-lxc/        # CT405 — VM + Loki + Grafana + Alloy
│   ├── ollama-host/              # Ollama systemd config (on Evo-X2 host)
│   └── failover/                 # Peladn→Evo-X2 failover runbook + n8n workflows
├── docs/                         # Solution guides and case studies
├── ADR/                          # Architectural Decision Records
└── cluster.env.example           # All environment-specific values documented
```

---

## Services

| Service | Namespace | Node tier | Purpose | Docs |
| ------- | --------- | --------- | ------- | ---- |
| **hermes-agent** | `hermes-agent` | `tier=ai-worker` (Evo-X2) | Multi-LLM agent (qwen3.6 local + Gemini/Anthropic fallback) with a custom `system-administrator` skill that knows the whole lab | [README](kubernetes/apps/hermes-agent/README.md) |
| **open-webui** | `open-webui` | `tier=ai-worker` (Evo-X2) | Browser chat — connects to both Ollama (direct chat with qwen3.6) and Hermes (agent mode with tools) | [README](kubernetes/apps/open-webui/README.md) |
| **karakeep** | `karakeep` | `tier=ai-worker` (Evo-X2) | Bookmark + read-later manager. Native iOS/Android/extensions. Auto-tags via Peladn Ollama. Assets on NFS DAS. Miniflux→Karakeep starred-entry sync runs every 15 min | [README](kubernetes/apps/karakeep/README.md) |
| **hindsight** | `hindsight` | `tier=ai-worker` (Evo-X2) | AI memory — native MCP + web UI + knowledge-graph retrieval (semantic/BM25/graph/temporal + cross-encoder rerank) | [case study](docs/case-study-ai-memory-layer.md) |
| n8n | `n8n` | `tier=always-on` (rpi4) | Workflow automation, WOL triggers, AI pipelines, Beszel→HA bridges | [README](kubernetes/apps/n8n/README.md) |
| miniflux | `miniflux` | `tier=always-on` (rpi4) | Lightweight RSS reader | [README](kubernetes/apps/miniflux/README.md) |
| beszel | `beszel` | `tier=always-on` (rpi4) + agents on every node | Host + container system metrics — parallel to VM/Loki, at-a-glance | [README](kubernetes/apps/beszel/README.md) |
| nut | `nut` | rpi4 (hostname pinned) | UPS power monitoring | [README](kubernetes/apps/nut/README.md) |
| monitoring | `monitoring` | rpi4 (hostname pinned) | `vmagent` (K8s metrics → VM) + `alloy-logs` (pod logs → Loki) | — |

---

## Key Patterns

### Secrets Management

All secrets are encrypted with SOPS + Age before committing. The Age public key is stored in `.sops.yaml`. Private key lives in Vaultwarden only.

```bash
# Encrypt a secret
sops --encrypt secret.yaml > secret.enc.yaml

# Flux decrypts automatically via the sops-age K8s secret
```

### Adding a New Application

1. Create `kubernetes/apps/<name>/` with `namespace.yaml`, manifests, `kustomization.yaml`
2. Add to `kubernetes/apps/kustomization.yaml`
3. Encrypt any secrets: `sops --encrypt secret.yaml > secret.enc.yaml`
4. Push — Flux reconciles within 1–10 minutes

### Node-tier scheduling

Apps are pinned to nodes via `nodeSelector` labels, not by hostname (where possible). The labels are stable contracts:

| Label | Means | Currently on |
|---|---|---|
| `tier=always-on` | 24/7 low-power | RPi4 |
| `tier=ai-worker` | GPU/agent workloads (Hermes, Open WebUI) | Evo-X2 K8s worker |
| `tier=on-demand` | Burst (WOL on-demand) | NUC, R610, i9 |

To migrate an app between tiers, edit one `nodeSelector` line in its deployment.yaml and push — Flux rolls it. Existing pods stay put until they restart.

### WOL (Wake-on-LAN) Automation

Burst nodes (NUC, i9) are powered off when idle. n8n workflows trigger WOL via UpSnap when rpi4 RAM exceeds 75% or GPU workloads are requested.

### Hermes Agent — the homelab brain

The `system-administrator` skill in [`kubernetes/apps/hermes-agent/skills/`](kubernetes/apps/hermes-agent/skills/system-administrator/) embeds a complete topology map plus 6 read-only helper scripts:

- `k8s_status.py` — pod state across the cluster (in-cluster ServiceAccount + RBAC)
- `service_health.py` — HTTP health of known services (Ollama, Hindsight, Grafana, …)
- `vm_query.py` — PromQL/MetricsQL against VictoriaMetrics
- `loki_query.py` — LogQL against Loki
- `proxmox_status.py` — Proxmox VE read-only API (PVEAuditor token)
- `beszel_query.py` — Beszel server API (PocketBase auth)
- `grafana_query.py` — Grafana API (dashboards, alerts, annotations) via service-account token

Open WebUI sits in front. Ask *"any K8s pods restarting in the last hour?"* or *"OPNsense WAN throughput last 30 min"* and Hermes routes to the right script.

---

## Docs & Case Studies

**Start here if you want to build something like this:**

- [**Homelab Build Guide**](docs/homelab-build-guide.md) — the whole thing in build order, with verification gates, a table for translating the roles onto your own hardware, and the mistake I made at each stage

**Case studies** — one problem each, with the failure, the decision, and the numbers:

- [A Power-Efficient Tiered Cluster on Mismatched Hardware](docs/case-study-tiered-power-efficient-cluster.md) — always-on core + WOL burst tier, and why workloads select labels rather than hostnames
- [Storage Architecture — Retiring an NFS Tier](docs/case-study-storage-architecture.md) — a thermal fault, why `nfsd` can't serve from inside an LXC, and the database corruption that put every DB on local disk
- [GitOps on Talos with Flux CD and SOPS](docs/case-study-gitops-talos-flux.md)
- [Self-hosted AI Memory Layer — mem0 → Hindsight](docs/case-study-ai-memory-layer.md)
- [Choosing Local Models for a Self-Hosted AI Pipeline](docs/case-study-local-model-selection.md)

**Decision records:** [`ADR/`](ADR/) — Talos over K3s, Flux over ArgoCD, local-path over NFS for databases, and more.

---

## Licence

MIT — fork, adapt, and deploy freely. Attribution appreciated but not required.
