# Open WebUI ↔ Hindsight memory integration

Two complementary pieces. Open WebUI stores functions in its **database**, not on
disk, so these are version-controlled here and **pasted into the UI** — there is no
Flux/kubectl path for them.

| File | Install under | What it does |
|---|---|---|
| `hindsight_memory_filter.py` | Workspace → **Functions** | Automatic. Recalls relevant memories before every reply and saves the exchange afterwards. |
| `hindsight_tool.py` | Workspace → **Tools** | Deliberate. Gives the model `recall_memory`, `save_memory` and `reflect_on_memory` to call when it chooses. |

Use both: the filter gives you memory that "just works", the tool lets the model
search or write on purpose (and lets you say "remember that…").

## Install

1. Open WebUI → **Workspace** → **Functions** → **+** → paste
   `hindsight_memory_filter.py` → **Save**. Toggle it **on** (globally, or per model).
2. Open WebUI → **Workspace** → **Tools** → **+** → paste `hindsight_tool.py` →
   **Save**. Enable it on the models you want.

No credentials to type. Both read `HINDSIGHT_URL`, `HINDSIGHT_BANK` and
`HINDSIGHT_API_KEY` from the pod environment, which `../deployment.yaml` supplies
from the SOPS secret `open-webui-hindsight`. The Valves are overrides only.

## How it talks to Hindsight

In-cluster service, no NodePort:

```
POST {HINDSIGHT_URL}/v1/default/banks/{bank}/memories/recall   {"query", "max_tokens"}
POST {HINDSIGHT_URL}/v1/default/banks/{bank}/memories          {"items":[{"content","context","tags"}]}
POST {HINDSIGHT_URL}/v1/default/banks/{bank}/reflect           {"query","budget"}
```

Auth is a **raw** `Authorization: <key>` header — **no `Bearer` prefix** (the
Hindsight MCP endpoint does take `Bearer`; the HTTP API does not require it).

`recall` returns `{results:[{id,text,type,context,mentioned_at,tags,scores,…}]}`.
`reflect` returns `{text, based_on, structured_output, usage, trace}`.

## Design notes

- **Memory never breaks chat.** Every Hindsight call in the filter is wrapped; a
  Hindsight outage silently degrades to a normal conversation.
- **Retain is not pre-summarised.** The filter hands Hindsight the raw exchange and
  lets its own extraction pipeline decide what is fact-worthy — that is what it is for.
- **`async: true` on retain** so saving never adds latency to the reply.
- Messages shorter than `min_chars` (default 12) skip both recall and retain, to keep
  "ok", "thanks" and similar out of the bank.
- Recalled memories are injected as a system block that explicitly tells the model they
  are background, may be stale, and should not be parroted back.

## Verifying

From inside the pod:

```bash
kubectl -n open-webui exec deploy/open-webui -- sh -c '
curl -s -X POST "$HINDSIGHT_URL/v1/default/banks/$HINDSIGHT_BANK/memories/recall" \
  -H "Content-Type: application/json" -H "Authorization: $HINDSIGHT_API_KEY" \
  -d "{\"query\":\"test\",\"max_tokens\":300}" | head -c 300'
```

Expect HTTP 200 and a `results` array. In the UI you should see a
"Recalling memories…" status line above replies once the filter is enabled.
