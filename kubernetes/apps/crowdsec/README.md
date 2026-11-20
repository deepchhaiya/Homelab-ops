# CrowdSec agent

Detects attacks in Nginx Proxy Manager traffic (secret-file probing, path
scanning, known CVE exploits) and gets the attacking IPs banned at the
OPNsense WAN.

## How it fits together

```
NPM (home-ops LXC) ── logs ──▶ Alloy ──▶ Loki (observability LXC, :3100)
                                              │  {job="npm_access"}
                                              ▼
                              crowdsec-agent (this app, RPi4 worker)
                                 parses lines, runs scenarios
                                              │  alert (IP + scenario)
                                              ▼
                        CrowdSec LAPI on OPNsense (192.168.4.1:8080)
                                              │  decision: ban 4h
                                              ▼
                     OPNsense firewall bouncer → pf table crowdsec_blacklists
```

- **Agent only.** `DISABLE_LOCAL_API=true`: no database, no decisions here.
  The `emptyDir` data volume and hub downloads are rebuilt on every start.
- **Reads Loki, not files.** NPM's log files live on the home-ops LXC, so
  the agent reads the copy Alloy already ships to Loki. The Loki datasource
  always starts at "now"; it never replays old lines.
- **Collections:** `crowdsecurity/nginx-proxy-manager` (parser + generic HTTP
  scenarios) and `crowdsecurity/http-cve`. Private-range client IPs (LAN,
  WireGuard) are whitelisted by the default `crowdsecurity/whitelists`.

## Prerequisites (OPNsense)

1. `os-crowdsec` plugin with LAPI enabled and listening on `192.168.4.1:8080`
   (`sockstat -4l | grep 8080` must show the LAN address, not 127.0.0.1).
2. Register this agent and keep the printed password:
   `cscli machines add rpi4-k8s --auto -f -`

## Secret

`secret.enc.yaml` holds `AGENT_USERNAME` / `AGENT_PASSWORD` in Secret
`crowdsec-agent`, encrypted with SOPS (age key from `.sops.yaml`).

## Operating it

```bash
kubectl -n crowdsec logs deploy/crowdsec-agent            # "performed '<scenario>'" lines = alerts
kubectl -n crowdsec exec deploy/crowdsec-agent -- cscli metrics show acquisition parsers
# Dry run first: alerts are recorded but nothing is banned
kubectl -n crowdsec exec deploy/crowdsec-agent -- cscli simulation enable --global
```

`cscli simulation` writes into the container filesystem, so it resets on pod
restart. On OPNsense: `cscli machines list`, `cscli alerts list`,
`cscli decisions list`.
