"""llm_client.py — Talks to your local llama.cpp server via OpenAI-compatible API."""
import requests
import json

# ── CONFIGURE THESE for your llama.cpp setup ──
LLAMA_CPP_URL = "http://localhost:8080/v1/chat/completions"
MODEL_NAME = "gemma4"  # or whatever your llama.cpp server reports
TIMEOUT_SEC = 180  # 3 minutes max per LLM call
# ──────────────────────────────────────────────


def ask_gemma(prompt: str, system: str = "") -> str:
    """Send a prompt to llama.cpp and return the text response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4096,
        "stream": False,
    }

    try:
        resp = requests.post(
            LLAMA_CPP_URL,
            json=payload,
            timeout=TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except requests.exceptions.ConnectionError:
        print(f"  ⚠ Cannot reach llama.cpp at {LLAMA_CPP_URL}")
        print(f"     Make sure your server is running:")
        print(f"     .\\llama-server.exe -m gemma4.gguf --port 8080")
        return ""
    except Exception as e:
        print(f"  ⚠ LLM call failed: {e}")
        return ""

#import ollama
#MODEL = "gemma4:12b"
#def ask_gemma(prompt: str, system: str = "") -> str:
#    messages = []
#    if system:
#        messages.append({"role": "system", "content": system})
#    messages.append({"role": "user", "content": prompt})
#    try:
#        resp = ollama.chat(model=MODEL, messages=messages)
#        return resp["message"]["content"].strip()
#    except Exception as e:
#        print(f"  LLM call failed: {e}")
#        return ""
