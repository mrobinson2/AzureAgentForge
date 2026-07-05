"""Router-backed LLM client. The governor never talks to a model provider
directly — everything goes through the model-router sidecar so tier routing
and daily budget enforcement apply (services/model-router)."""

from __future__ import annotations

import json
import logging
import re

import httpx

from . import config
from .memory.classifier import (
    ClassificationResult,
    build_classify_messages,
    parse_classification,
    _fallback,  # shared fallback shape for transport failures
)

log = logging.getLogger("governor.llm")


async def classify(content: str, context: str | None = None) -> ClassificationResult:
    """One classification call. Transport failures degrade to the same
    event_only fallback as a garbage response — classification must never
    block or crash an admission request."""
    try:
        async with httpx.AsyncClient(timeout=config.CLASSIFIER_TIMEOUT_S) as client:
            resp = await client.post(
                f"{config.ROUTER_BASE_URL}/chat/completions",
                json={
                    "model": config.CLASSIFIER_MODEL,
                    "messages": build_classify_messages(content, context),
                    "temperature": 0.0,
                    "max_tokens": 400,
                },
                headers={"Authorization": "Bearer router-internal"},
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
            return parse_classification(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("classifier LLM call failed: %s", exc)
        return _fallback(f"llm transport failure: {exc}")


async def embed(text: str) -> list[float] | None:
    """Embed text via the router's /v1/embeddings (text-embedding-3-small — the
    same space as Honcho's stored document embeddings). Returns None on ANY
    failure so Plane C retrieval falls back to trigram; the embedding is a
    ranker, never a gate."""
    try:
        async with httpx.AsyncClient(timeout=config.EMBEDDING_TIMEOUT_S) as client:
            resp = await client.post(
                f"{config.ROUTER_BASE_URL}/embeddings",
                json={"input": text, "model": config.EMBEDDING_MODEL},
                headers={"Authorization": "Bearer router-internal"},
            )
            resp.raise_for_status()
            vec = resp.json()["data"][0]["embedding"]
            return vec if isinstance(vec, list) and vec else None
    except Exception as exc:  # noqa: BLE001 — best-effort; caller falls back to trigram
        log.warning("embedding call failed: %s", exc)
        return None


_CONTRADICTION_SYSTEM = """You compare two memory statements about the same \
user/system and classify their relationship. Reply with ONLY one word:
- none: unrelated or compatible; no conflict.
- supersede: same attribute, new value replaces old ("cloud is Azure" vs "cloud is AWS").
- scope_refine: both true in different contexts ("prefers terse" vs "wants detail for architecture").
- coexist: competing but both validly hold at once.
- needs_review: they conflict but you cannot tell which resolution applies.
Reply with the single word only."""

_VALID_CONTRADICTION_OUTCOMES = {"none", "supersede", "scope_refine", "coexist", "needs_review"}


def parse_contradiction_outcome(raw: str) -> str:
    """Pure: map a judge response to a valid outcome; default 'none' on noise."""
    if not raw or not raw.strip():
        return "none"
    token = raw.strip().lower().split()[0].strip(".:,\"'")
    return token if token in _VALID_CONTRADICTION_OUTCOMES else "none"


async def judge_contradiction(a: str, b: str) -> str:
    """Ask the classifier tier whether statements A and B conflict (the LLM
    suggests, the operator finalizes). Returns one of
    none/supersede/scope_refine/coexist/needs_review. Any failure → 'none' so a
    transport error never flags a memory."""
    try:
        async with httpx.AsyncClient(timeout=config.CLASSIFIER_TIMEOUT_S) as client:
            resp = await client.post(
                f"{config.ROUTER_BASE_URL}/chat/completions",
                json={
                    "model": config.CLASSIFIER_MODEL,
                    "messages": [
                        {"role": "system", "content": _CONTRADICTION_SYSTEM},
                        {"role": "user", "content": f"A: {a}\n\nB: {b}"},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 8,
                },
                headers={"Authorization": "Bearer router-internal"},
            )
            resp.raise_for_status()
            return parse_contradiction_outcome(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("contradiction judge failed: %s", exc)
        return "none"


# ─── Skill synthesis (automatic repetition detection -> skill autogen) ────────

_SKILL_SYNTH_SYSTEM = """You are given several short procedural notes that the \
SAME agent recorded across DIFFERENT tasks. Together they describe a recurring \
procedure the agent performs. Distill them into ONE reusable skill.

Reply with STRICT JSON and nothing else:
{"name": "<kebab-case slug, 2-5 words>", "body": "<markdown>"}

The body is a short skill playbook:
- First line exactly: "When to use: <one sentence>".
- Then numbered steps capturing the procedure, concrete but generic enough to \
reuse (omit task-specific ids, hostnames, and ticket numbers).
If the notes do NOT describe a coherent reusable procedure, reply {"name": "", \
"body": ""}."""

_SKILL_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify_skill_name(name: str) -> str:
    """Filesystem-safe kebab slug; bounded length."""
    s = _SKILL_SLUG_RE.sub("-", str(name).strip().lower().replace(" ", "-")).strip("-")
    return s[:60]


def parse_skill_synthesis(raw: str) -> dict | None:
    """Pure: extract {name, body} from a (possibly fenced) JSON reply. Returns
    None when there is no coherent skill — empty name/body or unparseable —
    so a noisy model response never produces a junk candidate."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text).strip("`").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    name = _slugify_skill_name(obj.get("name", ""))
    body = str(obj.get("body", "")).strip()
    if not name or not body:
        return None
    return {"name": name, "body": body}


async def synthesize_skill(agent: str, contents: list[str]) -> dict | None:
    """Crystallize a cluster of recurring procedural notes into a {name, body}
    skill draft via the classifier tier. Any failure → None so the miner simply
    skips this cluster (never persists a junk candidate)."""
    joined = "\n\n".join(f"- {c}" for c in contents if c)
    if not joined:
        return None
    try:
        async with httpx.AsyncClient(timeout=config.CLASSIFIER_TIMEOUT_S) as client:
            resp = await client.post(
                f"{config.ROUTER_BASE_URL}/chat/completions",
                json={
                    "model": config.CLASSIFIER_MODEL,
                    "messages": [
                        {"role": "system", "content": _SKILL_SYNTH_SYSTEM},
                        {"role": "user", "content": f"Agent: {agent}\nNotes:\n{joined}"},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 600,
                },
                headers={"Authorization": "Bearer router-internal"},
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
        return parse_skill_synthesis(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("skill synthesis failed: %s", exc)
        return None
