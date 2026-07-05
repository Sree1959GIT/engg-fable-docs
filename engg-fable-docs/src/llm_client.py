"""src/llm_client.py — Hardened client for a local llama.cpp server (OpenAI-compatible API).

Fixes over the previous version:
- Health check (cached 30 s): if the server is down we fail fast instead of
  burning N x 180 s timeouts before every fallback.
- Semaphore caps concurrent requests (llama.cpp serializes requests unless
  started with --parallel; flooding it caused the empty responses).
- Retries with exponential backoff, and an empty response counts as a failure.
- Per-call max_tokens so short tasks stay fast on an Iris iGPU.
"""
import threading
import time

import requests

from src.config import (
    LLM_CHAT_URL,
    LLM_HEALTH_URL,
    LLM_MAX_CONCURRENT,
    LLM_MODEL,
    LLM_RETRIES,
    LLM_RETRY_BACKOFF_SEC,
    LLM_TIMEOUT_SEC,
    MAX_TOKENS_DESCRIPTION,
)

_semaphore = threading.Semaphore(LLM_MAX_CONCURRENT)
_health_lock = threading.Lock()
_health_state = {"ok": None, "checked_at": 0.0}
_HEALTH_TTL_SEC = 30.0


def llm_available(force: bool = False) -> bool:
    """Cheap cached check that the llama.cpp server is up and has a model loaded."""
    with _health_lock:
        now = time.monotonic()
        if not force and _health_state["ok"] is not None and now - _health_state["checked_at"] < _HEALTH_TTL_SEC:
            return _health_state["ok"]
        ok = False
        try:
            resp = requests.get(LLM_HEALTH_URL, timeout=3)
            ok = resp.status_code == 200
        except requests.RequestException:
            ok = False
        _health_state.update(ok=ok, checked_at=now)
        if not ok:
            print(f"  ⚠ LLM server not reachable at {LLM_HEALTH_URL} — using programmatic fallbacks")
        return ok


def ask_llm(prompt: str, system: str = "", max_tokens: int = MAX_TOKENS_DESCRIPTION,
            temperature: float = 0.2, retries: int = LLM_RETRIES) -> str:
    """Send a chat request. Returns "" if the server is unavailable or all retries fail,
    so callers can fall back to programmatic generation."""
    if not llm_available():
        return ""

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    for attempt in range(1, retries + 1):
        try:
            with _semaphore:
                resp = requests.post(LLM_CHAT_URL, json=payload, timeout=LLM_TIMEOUT_SEC)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                return content
            print(f"  ⚠ LLM returned empty response (attempt {attempt}/{retries})")
        except requests.exceptions.ConnectionError:
            print(f"  ⚠ Cannot reach llama.cpp at {LLM_CHAT_URL}")
            llm_available(force=True)
            return ""
        except Exception as e:
            print(f"  ⚠ LLM call failed (attempt {attempt}/{retries}): {e}")
        if attempt < retries:
            time.sleep(LLM_RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))
    return ""


# Backwards-compatible alias (older agents import ask_gemma)
def ask_gemma(prompt: str, system: str = "") -> str:
    return ask_llm(prompt, system)
