# monitoring (K8s side of the observability stack)

These components ship cluster metrics and logs to CT405 (`infrastructure/observability-lxc`):
VictoriaMetrics `:8428` and Loki `:3100`. Grafana runs there too.

| Component | Where | Ships |
|---|---|---|
| `vmagent` | rpi4 (always-on) | kubelet, cAdvisor, kube-state-metrics, node-exporter, apiserver (filtered), CoreDNS, annotated pods, alloy-logs → `remote_write` |
| `alloy-logs` | rpi4 | pod logs (`job="loki.source.kubernetes.pods"`) and **Kubernetes events** (`job="kubernetes-events"`, logfmt) |
| `kube-state-metrics` v2.19.1 | rpi4 | object state: pod phase/restarts/reasons, requests/limits, deployments, nodes, PVCs, CronJobs, **Flux CRs** (`gotk_resource_info`) |
| `node-exporter` (ns `node-exporter`, privileged) | every Talos node | `/var` usage, NICs, PSI pressure, load, temps |

`metrics-server` (kube-system) is separate. It was applied by hand, not through Flux, and
serves `kubectl top` and HPA only. Grafana doesn't need it: the same kubelet/cAdvisor
numbers reach VictoriaMetrics through vmagent.

Notes:
- **WOL nodes:** sleeping workers are cordoned. Dashboards and alerts exclude
  `kube_node_spec_unschedulable == 1` nodes and their parked DaemonSet pods. The
  node-exporter job keeps Running pods only, so a sleeping node is not a "down" target.
- **kube-state-metrics** has no read access to Secrets or ConfigMaps (see `--resources`).
- **Labels:** cAdvisor/kubelet identify the node as `instance`; KSM, node-exporter and
  CoreDNS use `node`. The dashboards join them with `label_replace`.
- **Not collected:** etcd, scheduler and controller-manager `/metrics` bind to localhost on
  Talos, so etcd latency comes from the API server's view. Talos service logs (kubelet,
  machined) are not shipped yet; that needs `machine.logging.destinations`.
