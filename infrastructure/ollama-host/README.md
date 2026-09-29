# Ollama on Evo-X2 host — Phase 22a

**Why on the host, not in an LXC:**
Ollama's bundled ROCm libs reliably detect the gfx1151 iGPU on the **Proxmox host**, but **silently fall back to CPU inside an LXC** (privileged or unprivileged, with system ROCm 7.2.3 installed, every documented env var combination). After ~2.5 hours of trying every documented workaround, the conclusion was that Strix Halo (RDNA 3.5 / gfx1151) + Ollama + LXC has an ecosystem gap as of 2026-05-26. CT401 LXC remains around for future re-use (Open WebUI, agent companions, etc.) but Ollama itself runs on the host.

**What's confirmed working:**
- Backend: ROCm0 on gfx1150 (via `HSA_OVERRIDE_GFX_VERSION=11.5.0`)
- GPU memory: **71.3 GiB** usable (UMA 48 GB + GTT carveout)
- Generation rate: **44.3 tok/s** on `qwen3.6:35b-a3b` (Q4 MoE, 3.8 B active params/token)
- Prompt eval: 289.7 tok/s
- Cold model load: ~2.6 s (after that, `OLLAMA_KEEP_ALIVE=3h` keeps it hot)

---

## Installation (reproducible)

```bash
# On Evo-X2 host (192.168.4.84) — root
apt-get install -y zstd curl ca-certificates
curl -fsSL https://ollama.com/install.sh | sh
```

The install script picks the `-rocm` package automatically when it detects the AMD GPU, prints `>>> AMD GPU ready.` when done.

### Systemd override

Drop the contents of `ollama-override.conf` into `/etc/systemd/system/ollama.service.d/override.conf`, then:

```bash
systemctl daemon-reload && systemctl restart ollama
```

### Verify GPU is active

```bash
journalctl -u ollama --no-pager --since="-30 seconds" | grep -E "inference compute|library=ROCm"
# expect: library=ROCm compute=gfx1150 total="71.3 GiB" available="71.2 GiB"
```

If you see `library=cpu` instead, the override didn't apply — re-check the env vars below.

### Security — nftables (LAN-only access)

```bash
apt-get install -y nftables
mkdir -p /etc/nftables.d
# drop contents of nftables-ollama.conf into /etc/nftables.d/ollama.conf
nft -f /etc/nftables.d/ollama.conf
grep -q 'nftables.d/ollama' /etc/nftables.conf || echo 'include "/etc/nftables.d/ollama.conf"' >> /etc/nftables.conf
systemctl enable --now nftables
```

This adds **one** targeted rule: `tcp dport 11434` accepted only from `192.168.4.0/24`, dropped from anywhere else. Doesn't touch any other ports.

---

## Models pulled

| Model | Size on disk | Context | VRAM (loaded) | Use |
|---|---|---|---|---|
| `qwen3.6:35b-a3b` | 22 GB | 262,144 (server default) | 22.5 GB | Hermes admin + family, Hindsight reflect/mental models, curator brief, Open WebUI |
| `qwen3.5:9b` | 6.6 GB | 8,192 (Modelfile pin) | ~8 GB | Curator scoring, email summary, weekly digest (sends 16,384) |
| `qwen3:4b-instruct` | 2.5 GB | 32,768 (Modelfile pin) | ~7.5 GB | Hindsight retain + consolidation (largest prompt seen: 17.7k tokens) |
| `qwen3-embedding:0.6b` | 639 MB | - | ~1.5 GB | No current consumer (was mem0's embedder; Hindsight embeds in-pod) |

Removed 2026-09-29: `gemma4:e4b` (family Hermes moved to qwen3.6) and `gpt-oss:20b`
(benchmarked for the curator: 70% action agreement with production, 0/13 brief
format compliance).

### Per-model context

Ollama keys a loaded model by its context size: a request with a different
`num_ctx` unloads and reloads the model. The server default (262,144) suits
qwen3.6, whose KV cache is tiny, but would be expensive for the dense models,
where `OLLAMA_NUM_PARALLEL` also multiplies it. Those are pinned to what their
callers send, so a caller that omits `num_ctx` gets the same loaded instance:

```bash
pin() { printf 'FROM %s\nPARAMETER num_ctx %s\n' "$1" "$2" > /tmp/mf && ollama create "$1" -f /tmp/mf; }
pin qwen3:4b-instruct 32768
pin qwen3.5:9b 8192
ollama show qwen3:4b-instruct --parameters   # expect num_ctx 32768
```

Re-run after any `ollama pull` of these models; a pull replaces the manifest and
drops the pin. Check what is loaded with `curl -s localhost:11434/api/ps` (the
`context_length` field).

Hermes Agent needs >=64K (at 32K its responses were cut off with
`finish_reason='length'`), and agentic coding tools need a large window too, see
[`docs/vscode-ai-agent-setup.md`](../../docs/vscode-ai-agent-setup.md) for using
`qwen3.6:35b-a3b` as a Claude-Code-style agent inside VS Code (via the Cline
extension to `http://192.168.4.84:11434`).

Pull command:
```bash
ollama pull qwen3.6:35b-a3b
ollama pull qwen3.5:9b
ollama pull qwen3:4b-instruct
# then re-apply the context pins above
```

---

## Operational notes

| Want | How |
|---|---|
| Check loaded models + VRAM | `ollama ps` |
| Reach from another host on LAN | `curl http://192.168.4.84:11434/api/generate -d '{"model":"qwen3.6:35b-a3b","prompt":"..."}'` |
| Tail Ollama logs | `journalctl -u ollama -f` |
| Restart service after env change | `systemctl restart ollama` |
| Free GPU memory now | `ollama stop <model>` (or wait for KEEP_ALIVE to expire) |
| Verify the firewall rule | `nft list table inet ollama_fw` |
| Drop ALL firewall (debug only) | `nft delete table inet ollama_fw` |

---

## Why `HSA_OVERRIDE_GFX_VERSION=11.5.0` (not 11.5.1)

ROCm's bundled compute kernels exist for gfx1150 and gfx1151, but Ollama's bundled HIP runtime more reliably picks up the gfx1150 kernels when the override claims 11.5.0. With 11.5.1, the runtime tries to load gfx1151-specific kernels that exist but trigger a different code path that doesn't enumerate the device in some Ollama versions. The Proxmox community forum's Strix Halo guide pins 11.5.0 for this reason.

The actual hardware is gfx1151. The override is a "use kernels labeled gfx1150" hint, not a hardware downgrade.

---

## Reference — sources

- AMD Strix Halo working guide on Ollama GitHub: [issue #14855](https://github.com/ollama/ollama/issues/14855)
- Proxmox 9.x + Strix Halo passthrough: [forum thread](https://forum.proxmox.com/threads/proxmox-9-x-strix-halo-gpu-passthrough.181331/)
- Known-good llama.cpp + Strix Halo: [llama.cpp discussion #20856](https://github.com/ggml-org/llama.cpp/discussions/20856)
