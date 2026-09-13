"""
title: Hindsight Memory Tools
author: deep
version: 0.1.0
required_open_webui_version: 0.5.0
description: Lets the model explicitly search, save and reason over long-term memory in the self-hosted Hindsight service.
"""

# Install: Open WebUI -> Workspace -> Tools -> + -> paste this file -> Save,
# then enable it on the models you want. Complements the "Hindsight Memory"
# FILTER: the filter recalls automatically every turn, this tool lets the model
# deliberately search or write when it decides to.
#
# Credentials come from the pod env (HINDSIGHT_URL / HINDSIGHT_BANK /
# HINDSIGHT_API_KEY) set by the deployment. Hindsight takes the key as a RAW
# `Authorization: <key>` header — no "Bearer " prefix.

import os
from typing import Any, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        base_url: str = Field(
            default=os.getenv(
                "HINDSIGHT_URL", "http://hindsight.hindsight.svc.cluster.local:8888"
            ),
            description="Hindsight base URL.",
        )
        bank: str = Field(
            default=os.getenv("HINDSIGHT_BANK", "deep"), description="Memory bank."
        )
        api_key: str = Field(
            default=os.getenv("HINDSIGHT_API_KEY", ""),
            description="Raw Authorization header value.",
        )
        timeout_s: int = Field(default=60, description="Per-request timeout.")

    def __init__(self):
        self.valves = self.Valves()
        # Results come back to the model as text; no citation UI needed.
        self.citation = False

    # ---------- internal ------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": self.valves.api_key or os.getenv("HINDSIGHT_API_KEY", ""),
        }

    def _url(self, suffix: str) -> str:
        return f"{self.valves.base_url.rstrip('/')}/v1/default/banks/{self.valves.bank}{suffix}"

    async def _post(self, suffix: str, payload: dict) -> Any:
        timeout = aiohttp.ClientTimeout(total=self.valves.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                self._url(suffix), json=payload, headers=self._headers()
            ) as r:
                if r.status >= 400:
                    return {"_error": f"HTTP {r.status}: {(await r.text())[:200]}"}
                return await r.json()

    @staticmethod
    async def _status(emitter, text: str, done: bool):
        if emitter:
            await emitter(
                {"type": "status", "data": {"description": text, "done": done}}
            )

    # ---------- tools ---------------------------------------------------

    async def recall_memory(
        self,
        query: str,
        __event_emitter__: Optional[Callable[[Any], Any]] = None,
    ) -> str:
        """
        Search the user's long-term memory for facts relevant to a question.
        Use this when the user refers to their own past decisions, preferences,
        systems or history, or when you need context you were not given.

        :param query: What to look for, phrased as a natural-language question or topic.
        :return: Matching memories as text, or a message saying none were found.
        """
        await self._status(__event_emitter__, f"Searching memory: {query[:60]}", False)
        data = await self._post("/memories/recall", {"query": query, "max_tokens": 1200})
        if isinstance(data, dict) and data.get("_error"):
            await self._status(__event_emitter__, "Memory search failed", True)
            return f"Memory search failed: {data['_error']}"

        results = (data or {}).get("results") or []
        if not results:
            await self._status(__event_emitter__, "No memories found", True)
            return "No relevant memories found."

        out = []
        for m in results[:10]:
            text = (m.get("text") or "").strip()
            if not text:
                continue
            when = (m.get("mentioned_at") or "")[:10]
            ctx = (m.get("context") or "").strip()
            meta = " · ".join(x for x in (when, ctx) if x)
            out.append(f"- {text}" + (f"  [{meta}]" if meta else ""))

        await self._status(__event_emitter__, f"Found {len(out)} memories", True)
        return "Relevant memories:\n" + "\n".join(out)

    async def save_memory(
        self,
        fact: str,
        __event_emitter__: Optional[Callable[[Any], Any]] = None,
    ) -> str:
        """
        Save a durable fact to the user's long-term memory. Use this when the user
        states a preference, decision, or piece of context worth remembering in
        future conversations. Do not save transient chatter or secrets.

        :param fact: A single self-contained fact, written so it makes sense months later.
        :return: Confirmation string.
        """
        if not fact or len(fact.strip()) < 8:
            return "Nothing saved: the fact was empty or too short."

        await self._status(__event_emitter__, "Saving to memory…", False)
        data = await self._post(
            "/memories",
            {
                "items": [
                    {
                        "content": fact.strip(),
                        "context": "open-webui (explicit save)",
                        "tags": ["open-webui", "explicit"],
                    }
                ]
            },
        )
        if isinstance(data, dict) and data.get("_error"):
            await self._status(__event_emitter__, "Save failed", True)
            return f"Failed to save memory: {data['_error']}"
        await self._status(__event_emitter__, "Saved to memory", True)
        return f"Saved to long-term memory: {fact.strip()[:160]}"

    async def reflect_on_memory(
        self,
        question: str,
        __event_emitter__: Optional[Callable[[Any], Any]] = None,
    ) -> str:
        """
        Ask Hindsight to reason across many stored memories and return a synthesised
        answer, rather than raw matches. Use for "what patterns", "based on my past
        decisions", "what should I do about X given what you know about me".

        :param question: The question to reflect on.
        :return: A synthesised answer grounded in stored memories.
        """
        await self._status(__event_emitter__, "Reflecting over memory…", False)
        data = await self._post("/reflect", {"query": question, "budget": "mid"})
        if isinstance(data, dict) and data.get("_error"):
            await self._status(__event_emitter__, "Reflection failed", True)
            return f"Reflection failed: {data['_error']}"
        # /reflect returns {text, based_on, structured_output, usage, trace}
        answer = (data or {}).get("text") or ""
        await self._status(__event_emitter__, "Reflection complete", True)
        return answer.strip() or "Reflection returned no answer."
