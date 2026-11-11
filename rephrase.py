"""Carnet — rewriting a fact with the small local model.

A dictated fact keeps the hesitations of speech ("like", "actually", "the thing
is"), the repetitions and the grammar slips. This module asks the local model to
rewrite the text cleanly — same meaning, same facts — and returns the proposal
WITHOUT WRITING ANYTHING: the human validates it in the interface.

The model is the one used by the watch, served by LM Studio already running for
embeddings: no extra process, no token cost.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:1234"
DEFAULT_MODEL = "gemma-4-e2b-it-mlx"
MAX_CHARS = 4000
TIMEOUT = 180
# LM Studio loads a model on the first request (JIT): without waiting for that
# load, the prompt goes nowhere and the answer comes back truncated or late.
LOAD_TIMEOUT = 300

SYSTEM_PROMPT = (
    "You correct memory notes written in English. Rewrite the text: spelling, "
    "grammar, punctuation, dictated hesitations removed. Keep exactly the same "
    "meaning and the same facts (names, numbers, dates, versions). A single "
    "self-contained sentence, without preamble, without quotes, without commentary. "
    "Start directly with the corrected text."
)

# The model sometimes comments on its work despite the instruction: we strip the
# wrapper, we do not touch the content.
_PREAMBLES = re.compile(
    r"^\s*(here is|here's|corrected text|corrected version|result)\s*:?\s*",
    re.IGNORECASE,
)


def clean(output: str) -> str:
    t = (output or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t).strip()
    t = _PREAMBLES.sub("", t)
    for opening, closing in (('"', '"'), ("“", "”"), ("'", "'")):
        if len(t) > 1 and t.startswith(opening) and t.endswith(closing):
            t = t[1:-1].strip()
    return re.sub(r"[ \t]+", " ", t).strip()


class ModelUnavailable(RuntimeError):
    """The local server does not answer, or the target model is not usable."""


def _get_json(url: str, path: str, timeout: float) -> dict:
    with urllib.request.urlopen(f"{url.rstrip('/')}{path}", timeout=timeout) as rep:
        return json.loads(rep.read().decode("utf-8"))


def model_state(url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                timeout: float = 3.0) -> str:
    """'loaded', LM Studio's raw state, or 'absent' if the model is not there.

    Raises ModelUnavailable if the server does not answer at all: that is a
    distinct case, to be announced as such to the human.
    """
    try:
        data = _get_json(url, "/api/v0/models", timeout)
    except Exception as exc:
        raise ModelUnavailable(
            f"LM Studio unreachable at {url} — is the local server running? ({type(exc).__name__})"
        ) from exc
    for m in data.get("data") or []:
        if m.get("id") == model:
            return str(m.get("state") or "")
    return "absent"


def _warmup(url: str, model: str, timeout: float) -> None:
    """A tiny request whose only purpose is to trigger the JIT load.

    Its result is useless: the failure is ignored, `model_state` is what tells
    the truth about progress.
    """
    body = {
        "model": model,
        "max_tokens": 1,
        "temperature": 0,
        "messages": [{"role": "user", "content": "ok"}],
    }
    try:
        _call(url, body, timeout)
    except Exception:
        pass


def ensure_loaded(url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                  delay: float = LOAD_TIMEOUT, step: float = 1.0) -> None:
    """Wait until `model` is actually loaded before letting the prompt through.

    LM Studio exposes no HTTP "load" endpoint: the only way to load a model is to
    send it a request. So a warmup is sent in the background and `state` is
    watched — the real prompt only leaves once the model is ready, otherwise its
    quality collapses.
    """
    state = model_state(url, model)
    if state == "loaded":
        return
    if state == "absent":
        raise ModelUnavailable(f"model {model} is absent from LM Studio")
    thread = threading.Thread(target=_warmup, args=(url, model, delay), daemon=True)
    thread.start()
    end = time.monotonic() + delay
    while time.monotonic() < end:
        try:
            if model_state(url, model) == "loaded":
                return
        except ModelUnavailable:
            pass
        time.sleep(step)
    raise ModelUnavailable(
        f"model {model} did not finish loading after {int(delay)} s "
        "(load it by hand in LM Studio)"
    )


def _chat_url(url: str) -> str:
    """Accept a base URL, a /v1 base or a complete chat-completions URL."""
    base = (url or "").rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _call(url: str, body: dict, timeout: float, api_key: str = "") -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        _chat_url(url),
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as rep:
        data = json.load(rep)
    return data["choices"][0]["message"]["content"] or ""


def rephrase(text: str, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
             limit: Optional[int] = None, timeout: int = TIMEOUT,
             api_key: str = "", wait: bool = True) -> str:
    """Return the rewritten text, under `limit` characters when it is given.

    The layer imposes its size (250 for a core seat, 500 for a fact, none for a
    sheet): a rewrite that overflows would be refused at save time, so it is
    shortened here — once, explicitly — rather than returning an unusable text.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    # LM Studio loads JIT; a custom OpenAI endpoint has no such probe.
    if wait:
        ensure_loaded(url, model)
    system = SYSTEM_PROMPT
    if limit:
        system += (f"\nThe corrected text must be UNDER {limit} characters: "
                   "condense without losing any fact.")
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": min(1200, max(220, len(text))),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:MAX_CHARS]},
        ],
    }
    proposal = clean(_call(url, body, timeout, api_key))
    if limit and len(proposal) > limit:
        body["messages"][0]["content"] = (
            system + f"\nIMPERATIVE: the full answer must fit in {limit} characters. "
            "Cut secondary details, never a name, a number or a date."
        )
        second = clean(_call(url, body, timeout, api_key))
        if second and len(second) < len(proposal):
            proposal = second
    return proposal
