"""
VLM-imageArena — llama-server (OpenAI-compatible) judge client + strict A/B parsing.
Version: 1.4.0

v1.4.0: query() takes prebuilt data-URL strings (from images.prepare_data_url)
instead of raw bytes — base64 is built once per image per run (arena/champ
preload dict) instead of once per query.
"""

VERSION = "1.4.0"

import json

import requests

from . import config


def server_healthy():
    """True if the llama-server health endpoint responds ok."""
    try:
        r = requests.get(config.SERVER_HEALTH_URL, timeout=5)
        return r.ok
    except requests.RequestException:
        return False


def _parse_vote(content):
    """
    Extract the vote from the VLM response. Accepts A/B as the first
    character when followed by a non-letter (so "A", "A.", "A because ..."
    all count as A); rejects multi-letter starts ("AB", "Alpha").
    Returns "A", "B", or None for malformed.
    """
    s = content.strip()
    if not s:
        return None
    first = s[0]
    if first not in ("A", "B"):
        return None
    if len(s) > 1 and s[1].isalpha():
        return None
    return first


# GBNF grammar for the strict A/B contract: the reply must be exactly one of
# these two characters; once the grammar is satisfied, only EOS is a valid
# continuation, so generation stops after a single token. Sent per-request as
# the non-standard "grammar" field (llama.cpp chat-completions extension, same
# family as id_slot) when config.JUDGE_USE_GRAMMAR is True.
VOTE_GRAMMAR = 'root ::= ("A" | "B")'


def query(question, first_url, second_url, seed, id_slot=None):
    """
    Ask the VLM which image is better. The system prompt binds A=first, B=second.

    first_url / second_url: data URLs from images.prepare_data_url(path),
    built once per image per run and held in the caller's preload dict —
    the base64 string is stored in memory, never rebuilt per query.

    id_slot: pin this request to a specific server slot (0-based, must be
    < LLAMA_ARG_N_PARALLEL). Pinning makes llama.cpp's prompt cache reusable: a
    repeat query with the same image order hits the same slot and skips the
    vision re-encode (the arena fires grouped orders to exploit this).

    Returns (letter, raw, tokens_cached): letter "A"/"B"/None (malformed),
    raw = full response text, tokens_cached = prompt tokens the server
    reused from a previous request (audit proof of cache hits; None if the
    server didn't report it). This build reports it under
    usage.prompt_tokens_details.cached_tokens.

    CRITICAL: each image MUST be in its own user message. When both images
    share one message (with this llama.cpp build + Qwen3.6-VL), the second
    image is silently dropped and the judge is blind to it (verified by
    perception probes — the model literally answers "There is no second
    image"). Separate messages fix it (probe: "Red, Blue" vs "Red").

    Returns (letter, raw_text) where letter is "A", "B", or None when the
    response is malformed. raw_text is the FULL response (may contain
    reasoning after the vote — saved to the battle log for auditing).

    When config.JUDGE_USE_GRAMMAR is True, the request carries VOTE_GRAMMAR,
    so the server can only emit "A" or "B" — malformed becomes ~impossible;
    the parser above stays as belt-and-suspenders + audit trail.
    """
    payload = {
        "model": config.MODEL_NAME,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": first_url}},
                    {"type": "text", "text": "This is the first image."},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": second_url}},
                    {"type": "text", "text": "This is the second image."},
                ],
            },
            {"role": "user", "content": question},
        ],
        "temperature": config.TEMPERATURE,
        "top_p": config.TOP_P,
        "top_k": config.TOP_K,
        "min_p": config.MIN_P,
        "presence_penalty": config.PRESENCE_PENALTY,
        "repetition_penalty": config.REPETITION_PENALTY,
        "max_tokens": config.MAX_TOKENS,
        "seed": seed,
    }
    if config.JUDGE_USE_GRAMMAR:
        payload["grammar"] = VOTE_GRAMMAR  # hard-constrain the reply to "A"|"B"
    if id_slot is not None:
        payload["id_slot"] = id_slot  # same slot -> prompt-cache prefix reuse
    resp = requests.post(config.SERVER_URL, json=payload, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"].strip()
    # tokens_cached = prompt tokens reused from a previous request (audit:
    # proves the same-order image prefix hit the prompt cache). This build
    # reports it as usage.prompt_tokens_details.cached_tokens; older builds
    # put it at the top level as "tokens_cached".
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    tokens_cached = details.get("cached_tokens") or data.get("tokens_cached")
    return _parse_vote(raw), raw, tokens_cached
