# Ollama on Evo-X2 host — Phase 22a

**Why on the host, not in an LXC:**
Ollama's bundled ROCm libs reliably detect the gfx1151 iGPU on the **Proxmox host**, but **silently fall back to CPU inside an LXC** (privileged or unprivileged, with system ROCm 7.2.3 installed, every documented env var combination). After ~2.5 hours of trying every documented workaround, the conclusion was that Strix Halo (RDNA 3.5 / gfx1151) + Ollama + LXC has an ecosystem gap as of 2026-05-26. CT401 LXC remains around for future re-use (Open WebUI, agent companions, etc.) but Ollama itself runs on the host.

**What's confirmed working:**
- Backend: ROCm0 on gfx1150 (via `HSA_OVERRIDE_GFX_VERSION=11.5.0`)
- GPU memory: **71.3 GiB** usable (UMA 48 GB + GTT carveout)
- Generation rate: **44.3 tok/s** on `qwen3.6:35b-a3b` (Q4 MoE, 3.8 B active params/token)
- Prompt eval: 289.7 tok/s
- Cold model load: ~2.6 s (after that, `KEEP_ALIVE=24h` keeps it hot)

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

| Model | Size on disk | VRAM (loaded) | Use |
|---|---|---|---|
| `qwen3.6:35b-a3b` | 23 GB | ~26 GB | Primary — chat, tool calling, code (3.8 B active params) |
| `qwen3-embedding:0.6b` | 639 MB | ~1.5 GB | Embeddings (was mem0's embedder; Hindsight uses its own in-pod bge-small) |

**Context window:** the override sets `OLLAMA_CONTEXT_LENGTH=65536` (default is ~4K,
which truncates large prompts). Hermes Agent needs ≥64K — at 32K its responses were
cut off with `finish_reason='length'` (raised 2026-09-13). This is required for agentic coding tools — see
[`docs/vscode-ai-agent-setup.md`](../../docs/vscode-ai-agent-setup.md) for using
`qwen3.6:35b-a3b` as a **Claude-Code-style agent inside VS Code** (via the Cline
extension → `http://192.168.4.84:11434`). Verified: OpenAI-compatible chat + tool
calling both work.

Pull command:
```bash
ollama pull qwen3.6:35b-a3b
ollama pull qwen3-embedding:0.6b
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
