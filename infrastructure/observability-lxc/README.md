# Observability LXC (CT405, Evo-X2)

Tier-1 monitoring stack for the homelab, running on **CT405 `hl-observability-lxc`** on the
Evo-X2 (Debian 13, Docker Compose). Part of Phase 21 Part 3.

- **VictoriaMetrics** (`:8428`) — metrics store; scrapes node_exporters and receives
  `remote_write` from the K8s **vmagent** (`kubernetes/apps/monitoring`).
- **Loki** (`:3100`) — log store (+ Grafana Alloy shipper).
- **Grafana** (`:3000`) — dashboards; VictoriaMetrics + Loki datasources auto-provisioned.
- **node-exporter** (`:9100`) — this LXC's OS metrics.

**Beszel stays** as the lightweight at-a-glance view; this stack adds history, logs,
dashboards, and real alerting (alerting/SMART/speedtest come in Tier 2).

## Host

- CT405 on Evo-X2, DHCP (reserve MAC `BC:24:11:56:BC:A2` in OPNsense for a stable IP).
- 4 cores / 4 GB / 40 GB, unprivileged, `nesting=1`.

## Deploy

```bash
# inside CT405
git clone https://github.com/DeepAchut/Homelab-ops.git
cd Homelab-ops/infrastructure/observability-lxc
cp .env.example .env            # optionally set GRAFANA_ADMIN_PASSWORD (else admin/admin, change on first login)
docker compose up -d
docker compose ps
```

> Data lives in Docker named volumes inside the LXC rootfs (captured by the CT405 vzdump backup). Metrics retention 90d, logs 30d.

## Scrape targets

node_exporter on: this LXC, Evo-X2 host, Peladn host, CT202, CT203 (see `scrape.yaml`).
K8s pod/node metrics arrive via the vmagent `remote_write`.

## Access

Put Grafana behind NPM (e.g. `grafana.dkghar.duckdns.org` → `192.168.4.66:3000`).
VictoriaMetrics/Loki stay internal-only.

## Secrets

No secrets committed. `GRAFANA_ADMIN_PASSWORD` lives in a gitignored `.env` (or SOPS
`.env.enc.yaml`). Everything else is non-sensitive config.

## Dashboards (provisioned from `grafana/provisioning/dashboards/json`)

| Dashboard | Source | Shows |
|---|---|---|
| OPNsense Network | Telegraf | WAN/LAN throughput, CPU/mem/disk, speedtest |
| **OPNsense Firewall** | filterlog (Loki) + Alloy counters + opnsense-exporter | block/NAT rates, **rule matches** (rule # + tracker), top blocked sources/ports/destinations, top NAT talkers, firewall log, new LAN devices (hostwatch) |
| **OPNsense Health** | opnsense-exporter + Telegraf Unbound + DNS log | gateway status/RTT/loss, WireGuard peers, services, firmware/reboot, pf states, mbufs, DNS rates/rcodes/top names/clients, TCP states, ARP, temps, auth & system logs |
| OPNsense Suricata IDS | Suricata EVE (Loki) | alerts, signatures, sources |
| **Kubernetes Cluster** | kubelet/cAdvisor + kube-state-metrics + node-exporter + apiserver + CoreDNS + events | `kubectl top nodes/pods` equivalents, requests vs allocatable, unhealthy pods, restarts, OOM reasons, PVCs, control plane, Flux, warning events, error-log rates |
| **Kubernetes Workloads** | same + pod logs | per namespace/pod: CPU/mem vs request/limit, throttling, restarts, events, logs with search |
| Homelab Backups | textfile metrics | backup freshness |

`kubectl top` reads metrics-server, which keeps only the latest sample in memory. The
Grafana "top" panels read the same kubelet/cAdvisor data from VictoriaMetrics, with 90 d
of history.

## Log-derived metrics (`config.alloy` → `alloy:12345/metrics`, job `alloy-observability`)

| Metric | Labels |
|---|---|
| `opnsense_filterlog_entries_total` | action, iface, dir, proto, ipversion |
| `opnsense_dns_replies_total` | rcode, cached |
| `opnsense_suricata_alerts_total` | alert_severity |
| `opnsense_hostwatch_new_stations_total` | iface |

The counters are cheap to query over long ranges, so stat and rate panels use them. IPs,
ports and rule ids stay as Loki **structured metadata** (`src_ip`, `dst_ip`, `src_port`,
`dst_port`, `proto`, `rule`, `tracker`), never labels. The counters start at zero when this
config is deployed. The Loki-backed tables only get the new fields for lines ingested after
the rollout.

## OPNsense exporter (one-time setup)

1. **System › Access › Users → add** `svc-exporter` with a random password, no shell, and
   these privileges only: *Diagnostics: ARP Table, Diagnostics: Firewall statistics,
   Diagnostics: Netstat, Lobby: Dashboard, Reporting: Traffic, Services: Unbound (MVC),
   Status: DHCP leases, Status: DNS Overview, Status: Services, System: Firmware,
   System: Gateways, System: Settings: Cron, System: Status, VPN: WireGuard*.
   (No Kea privilege: DHCP is dnsmasq, and the Kea collectors are disabled.)
2. On that user, under **API keys**, click **+** to download `apikey.txt`.
3. Put `key=` into `OPNSENSE_API_KEY` and `secret=` into `OPNSENSE_API_SECRET` with
   `sops edit .env.enc.yaml` (on the Mac), then delete the downloaded file.
   CT405 has no Age key, so decrypt on the Mac and pipe the plaintext in:
   ```bash
   git show origin/main:infrastructure/observability-lxc/.env.enc.yaml \
     | sops -d --input-type yaml --output-type dotenv /dev/stdin \
     | ssh evox2 'pct exec 405 -- sh -c "umask 077; cat > /root/Homelab-ops/infrastructure/observability-lxc/.env"'
   ```
   (`.env.enc.yaml` is a YAML map, so plain `sops -d` gives YAML that compose can't read.)
4. Unbound DNS › Advanced: enable **Extended Statistics**.

## OPNsense remote logging (System › Settings › Logging › Remote)

Over the last 30 days only `filterlog`, `suricata`, `hostwatch` and `wireguard` reached
Loki. To fill the **auth / system log** panels, add these applications to the 1514 TCP
target: `audit` (GUI logins, config changes), `sshd`, `lighttpd`, `dpinger` (gateway
alarms), `configd`, `dnsmasq` (DHCP leases: parsed into `opnsense_dhcp_messages_total`
and the DHCP row on OPNsense Health; also enable DHCP logging in the dnsmasq settings). Only `block` and `nat` show up in filterlog. To count
**pass** matches for a rule, tick *Log packets that are handled by this rule* on that rule.
Do this only on rules you care about: pass logging is high volume.

## Rollout / verify

```bash
# on CT405, after git pull
docker compose run --rm --no-deps alloy fmt /etc/alloy/config.alloy   # syntax check before restart
docker compose up -d                       # (re)creates services whose compose definition changed
# Single-file bind mounts (scrape.yaml, loki.yaml, config.alloy) keep pointing at the OLD
# file after `git pull` (git replaces the file, the mount holds the old inode), and
# VictoriaMetrics' configCheckInterval rereads that stale copy. Restart whatever changed:
docker compose restart victoriametrics alloy grafana   # + loki if loki.yaml changed
curl -s localhost:8428/api/v1/targets | grep -o '"job":"[a-z-]*","health":"[a-z]*"' | sort -u
```

## Next (Tier 2)

smartctl_exporter (drive temps incl. the 26 TB/2 TB externals, `-d sat,16`),
vmalert → Gotify (temp/host alerts), speedtest-exporter. Tier 3: n8n LAN-device watcher.
