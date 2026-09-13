"""
title: Hindsight Memory
author: deep
version: 0.1.0
required_open_webui_version: 0.5.0
description: Long-term memory for Open WebUI backed by the self-hosted Hindsight service. Recalls relevant memories before each reply and saves new facts afterwards.
"""

# Install: Open WebUI -> Workspace -> Functions -> + -> paste this file -> Save,
# then toggle it on globally (or per-model). Credentials come from the pod env
# (HINDSIGHT_URL / HINDSIGHT_BANK / HINDSIGHT_API_KEY), set by the deployment, so
# nothing secret needs to be typed into a Valve.
#
# Hindsight accepts the tenant key as a RAW `Authorization: <key>` header (no
# "Bearer " prefix) on the HTTP API.
#
# Failure policy: memory is an enhancement, never a dependency. Every network
# path is wrapped so that a Hindsight outage degrades to a normal chat rather
# than breaking the conversation.

import os
from typing import Any, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(
            default=True, description="Master switch for this filter."
        )
        base_url: str = Field(
            default=os.getenv(
                "HINDSIGHT_URL", "http://hindsight.hindsight.svc.cluster.local:8888"
            ),
            description="Hindsight base URL (in-cluster service by default).",
        )
        bank: str = Field(
            default=os.getenv("HINDSIGHT_BANK", "deep"),
            description="Hindsight memory bank to read/write.",
        )
        api_key: str = Field(
            default=os.getenv("HINDSIGHT_API_KEY", ""),
            description="Raw Authorization header value. Leave blank to use the pod env.",
        )
        recall_enabled: bool = Field(
            default=True, description="Inject relevant memories before replying."
        )
        retain_enabled: bool = Field(
            default=True, description="Save new facts from the exchange afterwards."
        )
        max_memories: int = Field(
            default=6, description="Most memories to inject into one turn."
        )
        recall_max_tokens: int = Field(
            default=800, description="Token budget asked of Hindsight recall."
        )
        min_chars: int = Field(
            default=12,
            description="Skip recall/retain for messages shorter than this.",
        )
        timeout_s: int = Field(default=25, description="Per-request timeout.")
        show_status: bool = Field(
            default=True, description="Show a status line while recalling."
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---------- helpers -------------------------------------------------

    def _headers(self) -> dict:
        key = self.valves.api_key or os.getenv("HINDSIGHT_API_KEY", "")
        return {"Content-Type": "application/json", "Authorization": key}

    def _url(self, suffix: str) -> str:
        base = self.valves.base_url.rstrip("/")
        return f"{base}/v1/default/banks/{self.valves.bank}{suffix}"

    @staticmethod
    def _last_role(messages: list, role: str) -> str:
        for m in reversed(messages or []):
            if m.get("role") == role:
                c = m.get("content")
                if isinstance(c, str):
                    return c
                # multimodal: keep only the text parts
                if isinstance(c, list):
                    return " ".join(
                        p.get("text", "") for p in c if isinstance(p, dict)
                    ).strip()
        return ""

    async def _post(self, url: str, payload: dict) -> Optional[dict]:
        try:
            timeout = aiohttp.ClientTimeout(total=self.valves.timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, json=payload, headers=self._headers()) as r:
                    if r.status >= 400:
                        return None
                    return await r.json()
        except Exception:
            # Never let a memory failure surface as a chat failure.
            return None

    # ---------- inlet: recall ------------------------------------------

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Optional[Callable[[Any], Any]] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not (self.valves.enabled and self.valves.recall_enabled):
            return body

        messages = body.get("messages") or []
        query = self._last_role(messages, "user")
        if len(query.strip()) < self.valves.min_chars:
            return body

        if self.valves.show_status and __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": "Recalling memories…", "done": False},
                }
            )

        data = await self._post(
            self._url("/memories/recall"),
            {"query": query, "max_tokens": self.valves.recall_max_tokens},
        )
        results = (data or {}).get("results") or []
        results = results[: self.valves.max_memories]

        if results:
            lines = []
            for m in results:
                text = (m.get("text") or "").strip()
                if not text:
                    continue
                when = (m.get("mentioned_at") or "")[:10]
                lines.append(f"- {text}" + (f"  ({when})" if when else ""))

            if lines:
                block = (
                    "## Long-term memory (Hindsight)\n"
                    "Relevant facts recalled for this conversation. Treat them as background "
                    "context about the user and their systems. They may be out of date — prefer "
                    "what the user says now, and do not repeat them back verbatim unless asked.\n\n"
                    + "\n".join(lines)
                )
                # Merge into an existing system message if present, else prepend one.
                for m in messages:
                    if m.get("role") == "system":
                        m["content"] = f"{m.get('content','')}\n\n{block}".strip()
                        break
                else:
                    messages.insert(0, {"role": "system", "content": block})
                body["messages"] = messages

        if self.valves.show_status and __event_emitter__:
            n = len(results)
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": (
                            f"Recalled {n} memor{'y' if n == 1 else 'ies'}"
                            if n
                            else "No relevant memories"
                        ),
                        "done": True,
                    },
                }
            )
        return body

    # ---------- outlet: retain -----------------------------------------

    async def outlet(
        self,
        body: dict,
        __event_emitter__: Optional[Callable[[Any], Any]] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not (self.valves.enabled and self.valves.retain_enabled):
            return body

        messages = body.get("messages") or []
        user_msg = self._last_role(messages, "user")
        assistant_msg = self._last_role(messages, "assistant")
        if len(user_msg.strip()) < self.valves.min_chars or not assistant_msg.strip():
            return body

        # Hand Hindsight the exchange; its own extraction pipeline decides what is
        # actually fact-worthy, so we do not try to pre-summarise here.
        content = f"User: {user_msg.strip()}\n\nAssistant: {assistant_msg.strip()}"
        who = (__user__ or {}).get("email") or (__user__ or {}).get("name") or "unknown"

        await self._post(
            self._url("/memories"),
            {
                "items": [
                    {
                        "content": content,
                        "context": f"open-webui chat ({who})",
                        "tags": ["open-webui"],
                    }
                ],
                "async": True,
            },
        )
        return body
