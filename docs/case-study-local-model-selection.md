# Case Study: Choosing Local Models for a Self-Hosted AI Pipeline

**Problem:** A self-hosted RSS curation pipeline started putting shopping deals — aluminium foil, a dog life jacket, a $0.77 packet of gravy mix — into a daily technical briefing. The obvious diagnosis was "the local model is too small". That diagnosis was wrong, and acting on it would have cost hardware and fixed nothing.

**Solution:** Treat model selection as an engineering decision with evidence: find the real failure, move anything that *must* be correct out of the prompt and into code, then benchmark candidate models on **real production inputs** rather than synthetic ones. The outcome was a better pipeline on the *same* hardware, and three models deliberately left unchanged.

Stack: [n8n](https://n8n.io) workflows on a Talos K8s cluster, [Ollama](https://ollama.com) on an AMD Ryzen AI Max ("Evo-X2") node and a always-on LXC, [Miniflux](https://miniflux.app) as the feed reader, Gotify for push.

---

## The failure

The curator scores every unread article 0–10 with `qwen3:4b-instruct`; anything ≥ 5 is written to a table that feeds the morning brief. Deals scored well above 5 — and the model's stated reasons showed exactly what had gone wrong:

| Article | Score | Bucket | Model's justification |
| ------- | ----- | ------ | --------------------- |
| McCormick Brown Gravy Mix, $0.77 | **10** | noise | "Covers security, CVEs, breaches, exploits, incident writeups, hardening" |
| adidas VL Court trainers, $17 | **9** | homelab | "self-hosting, Proxmox, Slickdeals" |
| Nike ACG backpack, $62.99 | **9** | AI | "LLMs, agents, AI shifts" |
| RXBAR protein bars, $14.95 | 5 | homelab | post angle: *"Proxmox Self-Hosting: 12g Protein"* |

The model was not reasoning badly about the articles. It was **pasting the prompt's own bucket definitions into the justification field** — the classic small-model failure when a long taxonomy meets an input that fits none of it. The system prompt was ~2,400 characters of scoring bands and category definitions, and it never mentioned that retail content existed at all.

## Two structural bugs the model was masking

Reading the workflow graph rather than the model output found the actual defects:

1. **The insert had no category filter.** Anything scoring ≥ 5 was written to the brief table, deal or not.
2. **Deal delivery depended on a *low* score.** Deals have their own notification channel, but that branch hung off the switch's `skip` output. A deal scoring ≥ 5 took the `star`/`notify` path instead — so scoring a deal highly simultaneously put it in the brief *and* stopped it reaching the person who wanted it.

The deal routing had only ever worked because the model usually scored deals low. That is a latent bug in any switch-based pipeline: **a branch attached to one output silently stops firing when classification changes.**

---

## Key implementation decisions

### 1. Anything that must be correct belongs in code, not in a prompt

Commerce detection moved out of the model entirely — category and feed regex, plus a price-and-retail-cue fallback for deals arriving through a non-deal feed:

```js
if (COMMERCE_SOURCE.test(category) || COMMERCE_SOURCE.test(feed)) return true;
return PRICE.test(title) && RETAIL_CUE.test(title);
```

Validated against the last 100 real rows before deployment: **0 missed deals, 0 false positives**, with "Minisforum MS-01 for $599" and "S3 Express One Zone drops prices 40% off list" correctly left alone. A prompt rule cannot offer that guarantee; a regex tested against history can.

### 2. Short prompts with worked examples beat long rubrics

The replacement prompt is roughly half the length and ships three **worked examples as real user/assistant turns** — a Slickdeals listing scored 0, a Kubernetes release scored 8, an entertainment story scored 1. Small models copy a demonstrated answer far more reliably than they follow a described one.

Same 4B model, same articles, new prompt: every deal scored **0**, no invalid buckets, no rubric echo.

**The prompt was the bug.** The model upgrade that followed was a refinement, not the fix.

### 3. Guard the output anyway

Cheap, deterministic post-checks catch what prompting cannot:

- **Enum allowlist** — the model had been inventing buckets (`CAREER`, `pathlite`, `7-8: Clearly数`, and a compound `"security infra"`). Anything off-list becomes `noise`.
- **Echo detector** — a regex for the prompt's own phrasing appearing in the justification. A match caps the score and marks the verdict `[unverified]`, because a model that parroted the instructions did not read the article.

### 4. Benchmark on real inputs, not synthetic ones

The first comparison run sent **titles only** and concluded the newer models were worse — they scored a Kubernetes release a 2, reasoning "title alone is insufficient for technical assessment". They were right to refuse; production sends up to 1,400 characters of article text. The eval was punishing models for being honest about missing evidence.

The decisive run replayed **24 real messages with genuine excerpts, extracted from past n8n execution data**. Execution history is the best evaluation corpus available for an existing pipeline — it is exactly the distribution the model sees in production, and it is free.

### 5. Check whether a candidate is a reasoning model before measuring it

Newer Qwen releases emit hidden chain-of-thought unless the request sets `"think": false`:

| Model | thinking on | `think: false` |
| ----- | ----------- | -------------- |
| `qwen3.5:9b` | 56.2 s/item | **2.2 s/item** |
| `qwen3.5:4b` | 39.4 s/item | 1.4 s/item |

A 25× latency difference that has nothing to do with hardware. **A benchmark result that looks absurd — a 9B slower than a 35B MoE — usually means a wrong flag, not slow silicon.**

### 6. Judge structured output on field validity, not the headline number

Scores were broadly similar across candidates. The bucket field decided it:

| Model | Invalid buckets | Bucket spread over 24 real items | Speed |
| ----- | --------------- | -------------------------------- | ----- |
| `qwen3:4b-instruct` (incumbent) | 0 | **`infra`×21, `noise`×3** — collapsed | 1.3 s/item |
| `qwen3.5:4b` | **2** (`"security infra"`) | ai×16, infra×4, security×2, storage×1, news×1 | 1.4 s/item |
| **`qwen3.5:9b`** (chosen) | **0** | ai×17, infra×4, storage×1, security×1, career×1 | 2.2 s/item |
| `qwen3.6:35b-a3b` | 0 | — (not pursued for this job) | 16.9 s/item |

The incumbent filed every AI paper, storage post and inference-engine release under `infra`, which made the bucket-driven notification grouping meaningless. That — not the score — was the reason to upgrade.

In production the difference is visible on near-identical inputs: consecutive `llama.cpp` releases now score **9** ("critical Vulkan fix for PowerVR GPUs") and **5** ("minor release with backend cleanup"). The 4B gave all of them an identical 8.

---

## Three models deliberately *not* changed

Upgrading everything to the newest model would have broken two services and slowed a third.

| Service | Model kept | Why |
| ------- | ---------- | --- |
| Memory layer (fact extraction) | `qwen3:4b-instruct` | Reasoning models and structured extraction don't mix. Retain latency was ~35 s with a thinking model vs ~1–5 s here — a small **non-thinking instruct** model is the correct tool, documented in [the memory-layer case study](case-study-ai-memory-layer.md). |
| Bookmark tagging | `qwen3:4b-instruct` | Runs on a 16 GB always-on host shared with other services; a 9B there is a poor trade. |
| Family chat assistant | `gemma4:e4b` | Benchmarked at **53 tok/s** vs 35 tok/s for `qwen3.5:9b` (dense, not MoE) and 69–75 tok/s for `qwen3.6:35b-a3b`. The MoE was faster *and* stronger at no extra RAM — but only `gemma4:e4b` accepts **audio** input, and that capability cannot be recovered by tuning. |

"Newer" is not a property that transfers across jobs. A 4B instruct model is *better* than a 9B reasoning model for strict-schema extraction, and a multimodal 8B is better than a faster MoE when voice input matters.

---

## Results

- Deals in the daily brief: **16 of 68 entries → 0**.
- Deal notifications reaching their intended recipient: restored (they had been silently broken whenever the model scored a deal well).
- Invalid category values: **eliminated** by the allowlist.
- Curator run time: **2 m 47 s → 2 m 07 s**, despite a larger model, because `think: false` removed wasted reasoning tokens.
- Duplicate articles in the brief (same URL via a digest feed and its source): **collapsed** by URL normalisation.
- Hardware bought: **none**.

## Lessons learned

1. **Suspect the prompt before the model.** A long rubric handed to a small model gets pattern-matched and echoed back. Halving the prompt and adding three examples fixed the headline failure on the original model.
2. **Move guarantees into code.** "Never publish shopping deals" is a business rule, not a prompting preference. Regex validated against historical data gives an auditable guarantee.
3. **Read the graph, not just the output.** The most damaging bug — deals never reaching their channel — was invisible in model output and obvious in the workflow wiring.
4. **Benchmark with production data.** Synthetic titles produced a conclusion that was exactly backwards.
5. **Know your flags.** `think: false` was worth 25× on latency; no model change came close to mattering as much.
6. **Right tool per job.** Four models now run across the stack — a 4B instruct for extraction, a 9B dense for judgement, a 35B MoE for long-form synthesis, and a multimodal 8B for family chat — because those jobs have genuinely different requirements.
