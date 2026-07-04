"""diagnostic.py — Tests each pipeline stage. Run from C:\freebuff\engg-docs."""
import requests, json, sys

print("="*60)
print("DIAGNOSTIC: Testing llama.cpp connection")
print("="*60)

# 1. Test llama.cpp server
try:
    resp = requests.get("http://localhost:8080/health", timeout=5)
    print(f"[OK] llama.cpp server reachable: {resp.status_code}")
except:
    try:
        resp = requests.get("http://localhost:8080/v1/models", timeout=5)
        print(f"[OK] llama.cpp API reachable: {resp.status_code}")
        print(f"     Models: {resp.json()}")
    except Exception as e:
        print(f"[FAIL] Cannot reach llama.cpp at localhost:8080")
        print(f"       Error: {e}")
        print(f"       Make sure: .\\llama-server.exe -m gemma4.gguf --port 8080")
        sys.exit(1)

# 2. Test chat completion
print("\n--- Testing chat completion ---")
payload = {
    "model": "gemma4",
    "messages": [
        {"role": "system", "content": "Respond with exactly: HELLO_OK"},
        {"role": "user", "content": "Say HELLO_OK"}
    ],
    "temperature": 0.1,
    "max_tokens": 50,
}
try:
    resp = requests.post(
        "http://localhost:8080/v1/chat/completions",
        json=payload,
        timeout=30,
    )
    result = resp.json()
    content = result["choices"][0]["message"]["content"]
    print(f"[OK] LLM response: {content[:100]}")
    print(f"     Full response keys: {list(result.keys())}")
    print(f"     Model reported: {result.get('model', 'N/A')}")
except Exception as e:
    print(f"[FAIL] Chat completion failed: {e}")
    print(f"       Response: {resp.text[:500] if 'resp' in dir() else 'N/A'}")
    sys.exit(1)

# 3. Test Graphviz
print("\n--- Testing Graphviz ---")
try:
    import graphviz
    g = graphviz.Digraph(name="test")
    g.node("A")
    g.node("B")
    g.edge("A", "B")
    out = g.render("test_graphviz", format="png", cleanup=True)
    print(f"[OK] Graphviz rendered: {out}")
except ImportError:
    print("[FAIL] graphviz Python package not installed")
    print("       Run: pip install graphviz")
except Exception as e:
    print(f"[FAIL] Graphviz render failed: {e}")
    print("       Graphviz BINARY likely not installed.")
    print("       Download from: https://graphviz.org/download/")
    print("       Run installer, then restart PowerShell.")

# 4. Test DOT code generation
print("\n--- Testing DOT code extraction ---")
test_dot = "```dot\ndigraph Test { A -> B }\n```"
import re
m = re.search(r'```(?:dot)?\s*(digraph\s+\w+\s*\{.*?\})\s*```', test_dot, re.DOTALL)
if m:
    print(f"[OK] DOT extraction regex works")
else:
    print("[FAIL] DOT extraction regex failed on known-good input")

print("\n" + "="*60)
print("DIAGNOSTIC COMPLETE")
print("="*60)
