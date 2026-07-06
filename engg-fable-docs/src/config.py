"""src/config.py — Central configuration. All values overridable via environment variables.

Tuned for a Windows 11 laptop: i7, 16 GB RAM, Intel Iris (shared memory) running
llama.cpp at localhost:8080 with a GGUF model (Gemma / Qwen2.5-Coder / Phi-4-mini).
"""
import os

# ── LLM server ────────────────────────────────────────────────────────────
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8080")
LLM_CHAT_URL = f"{LLM_BASE_URL}/v1/chat/completions"
LLM_HEALTH_URL = f"{LLM_BASE_URL}/health"          # llama.cpp built-in endpoint
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma4")   # name is informational for llama.cpp

# On an Iris iGPU, big generations are slow. Keep per-call budgets small and
# let the number of retries (not the timeout) absorb flakiness.
LLM_TIMEOUT_SEC = int(os.environ.get("LLM_TIMEOUT_SEC", "120"))
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "3"))
LLM_RETRY_BACKOFF_SEC = 2.0                          # 2s, 4s, 8s

# llama.cpp serializes requests unless started with --parallel N.
# Flooding it with 10 threads is what caused the empty responses.
LLM_MAX_CONCURRENT = int(os.environ.get("LLM_MAX_CONCURRENT", "2"))

# Context window PER REQUEST. NOTE: llama.cpp SPLITS -c across --parallel
# slots, so `-c 8192 --parallel 2` gives each request ~4096 tokens — that is
# the safe default here. Set LLM_CTX to match your server if different.
LLM_CTX = int(os.environ.get("LLM_CTX", "4096"))

# Per-task token budgets (small = fast = fewer timeouts on iGPU)
MAX_TOKENS_RESEARCH = int(os.environ.get("MAX_TOKENS_RESEARCH", "512"))
MAX_TOKENS_DESCRIPTION = int(os.environ.get("MAX_TOKENS_DESCRIPTION", "1024"))
MAX_TOKENS_SHORT = int(os.environ.get("MAX_TOKENS_SHORT", "256"))

# ── Document metadata ─────────────────────────────────────────────────────
DOC_VERSION = os.environ.get("DOC_VERSION", "1.0")
DOC_AUTHOR = os.environ.get("DOC_AUTHOR", "Engineering Documentation Generator")
DOC_ORGANIZATION = os.environ.get("DOC_ORGANIZATION", "")
DOC_NUMBER = os.environ.get("DOC_NUMBER", "TUM-001")

# ── Output ────────────────────────────────────────────────────────────────
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
DIAGRAM_DIR = os.path.join(OUTPUT_DIR, "diagrams")
