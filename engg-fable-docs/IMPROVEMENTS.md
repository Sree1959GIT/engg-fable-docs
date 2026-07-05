# Improvements — 2026-07 Rework

This document records the diagnosis of the previous codebase, the technology
decisions taken, and what changed. Companion to `master.md`.

---

## 1. Root-Cause Diagnosis

### 1.1 Why the LLM "frequently returned empty responses"
- `supervisor.py` fired **10 parallel requests** at llama.cpp. llama.cpp
  serializes requests unless started with `--parallel N`; queued requests sat
  past the 180 s timeout and came back as `""`.
- `max_tokens=4096` on every call. On an Intel Iris iGPU (shared memory) that
  is minutes of generation for tasks that need a paragraph.
- No retries, no health check: when the server was down, every agent still
  waited out a full connection timeout before falling back.

**Fixes** (`src/llm_client.py`, `src/config.py`):
- Cached `/health` check — if the server is down we fall back instantly.
- Semaphore caps concurrency (`LLM_MAX_CONCURRENT`, default 2).
- 3 retries with exponential backoff (2 s/4 s/8 s); an empty response counts
  as a failure and is retried.
- Per-task token budgets (256 for BOM lines, 512 for research, 1024 for prose).
- Everything configurable via environment variables — switching models or
  ports requires no code change.

### 1.2 Why signal lines crossed through blocks
- Edges attached to **node centers**; with `splines=true` and tight spacing,
  Graphviz routed lines straight across neighboring boxes.
- Edge `label=` attributes create invisible virtual nodes that drag edges
  through the layout.
- **Data bug**: in the old `diagram_lead_agent.py`, a row's `Make`/`Model`
  (which describe `Component_ID` only) were also assigned to `Target_ID` —
  so U1 could be labeled "Phoenix Contact MSTBVA".
- Shape/refdes inference searched for words like "connector" in the *model
  string* ("MSTBVA 2.5/2-G" contains none), so every part rendered as a
  plain box labeled "U".

**Fixes** (`agents/diagram_lead_agent.py`, `src/component_registry.py`):
- Components are drawn as **IC-style pin tables** (HTML labels) with one
  `PORT` per pin; edges attach with `tailport/headport` to pin cells on the
  node boundary → lines physically cannot pass through blocks.
- `splines=ortho` + generous `nodesep`/`ranksep` → Manhattan routing.
- Signal names use `xlabel` (no virtual label nodes).
- A **component registry** resolves make/model for targets from rows where
  they appear as source, or from the validated BOM's `Reference_IDs`.
- IEC 81346-2 class letters resolved from BOM `Type` → offline knowledge
  base → component-ID pattern (`CON1` → J, `D1` → D...).
- Every sheet gets a title block (doc no / rev / date / sheet) and a
  signal-class legend. Output is PNG + SVG.
- Color coding per master.md §6.1: red=power, blue=data, green=control,
  purple=motor, grey dashed=ground, orange=analog/sense.

### 1.3 Why descriptions were basic or empty
- A single monolithic LLM call per subsystem; the fallback was a raw dump of
  component/signal lists.
- No component- or module-level text, no cross-subsystem context.

**Fixes** (`agents/description_agent.py`, `src/system_analysis.py`):
- Four-level pipeline, each level seeded with the level below
  (component → module → subsystem → system).
- Subsystems are generated **sequentially** and each prompt includes the
  previously written overviews (`prior_context`) — contextual continuity.
- Cross-subsystem analysis (`find_bridges`, `interconnections_for`) is pure
  pandas — always available, feeds descriptions, tables, and the system diagram.
- Offline knowledge base (`KNOWN_COMPONENTS`) provides professional prose for
  recognized parts (BQ76952, STM32G474, IMC300A, SS34, ...) so the fallback
  path reads like an engineer wrote it. Extend the dict for your parts.

### 1.4 Document structure
Old output had no cover page, no TOC, no revision history, and the "system
overview" was just the first subsystem's text.

**Fixes** (`agents/documentation_agent.py`): full structure per master.md §4 —
cover page, revision history, TOC (Word field in DOCX; auto-generated
two-level TOC with page numbers in PDF via `multiBuild`), BOM, system
overview with block diagram and bridge table, per-subsystem sections
(diagram → functional description → component roles → interconnection table
→ signal list), connectivity appendix, page footers with doc number and page
numbers.

---

## 2. Technology Decisions (explore-and-decide items)

| Question | Decision | Rationale |
|---|---|---|
| Schemdraw / D2Lang instead of Graphviz? | **Keep Graphviz** (fixed properly) | Port-based HTML nodes + ortho splines solved the routing problem. Schemdraw draws beautiful IEC 60617 *schematic symbols* but has no auto-router — placement is manual, which doesn't scale to arbitrary Excel input. D2/ELK is cleaner but adds a non-Python binary to a Windows-offline install. Schemdraw remains the right Phase-2 upgrade for pin-accurate schematics. |
| LLM-generated DOT? | **Removed entirely** | Connectivity is already structured data; deterministic generation is faster and can't hallucinate. The LLM now only writes prose. |
| Different local model? | **Recommend Qwen2.5-Coder-7B-Instruct Q4_K_M** (~4.7 GB) or **Phi-4-mini** for 16 GB RAM + Iris iGPU | Better instruction-following for structured prose than Gemma at this size. Code is model-agnostic: set `LLM_MODEL`/`LLM_BASE_URL` env vars. Start llama.cpp with `--parallel 2 -c 8192` to match `LLM_MAX_CONCURRENT=2`. |
| Streamlit → React? | **Keep Streamlit for now** (Phase 4: React + React-Flow) | Offline, zero build step, sufficient for the 3-step wizard. React earns its complexity only when interactive diagram *editing* is needed. |
| MCP servers? | Deferred — not needed for the offline pipeline | Useful in a Claude-assisted dev loop: Filesystem (file ops), Web Search (datasheet lookup when online), GitHub (version control). None belong in the runtime path of an offline generator. |
| Missing libraries? | Added **Pillow** to requirements (was imported but not declared). svgwrite/cadquery not needed — Graphviz already emits SVG; CadQuery is 3D CAD, out of scope. |

---

## 3. Running

```bat
pip install -r requirements.txt
:: Graphviz binaries must be on PATH (bundled zip or https://graphviz.org/download/)

:: optional — for AI-enhanced prose:
llama-server.exe -m qwen2.5-coder-7b-instruct-q4_k_m.gguf --port 8080 --parallel 2 -c 8192

streamlit run web_ui.py
```

Environment overrides (all optional):
`LLM_BASE_URL`, `LLM_MODEL`, `LLM_TIMEOUT_SEC`, `LLM_MAX_CONCURRENT`,
`DOC_VERSION`, `DOC_AUTHOR`, `DOC_NUMBER`, `DOC_ORGANIZATION`.

The pipeline is fully functional with **no LLM running** — descriptions come
from the knowledge base + template narratives, and diagrams are always
deterministic.

Headless test without the UI:

```bat
python generate_sample_data.py
python run_pipeline.py
```

---

## 4. File Changes

| File | Change |
|---|---|
| `src/config.py` | **new** — central env-driven configuration |
| `src/llm_client.py` | rewritten — health check, retries, semaphore, per-call budgets |
| `src/component_registry.py` | **new** — registry, IEC 81346 classes, signal classes, knowledge base |
| `src/system_analysis.py` | **new** — cross-subsystem bridges, interconnection & signal tables |
| `src/bom_generator.py` | knowledge-base fallback, no more 'Unknown' |
| `agents/component_research_agent.py` | robust JSON parsing + offline fallback |
| `agents/diagram_lead_agent.py` | rewritten — pin ports, ortho routing, title blocks, legend, SVG |
| `agents/description_agent.py` | rewritten — 4-level pipeline with continuity |
| `agents/documentation_agent.py` | rewritten — full professional document structure |
| `supervisor.py` | new staged pipeline, bounded concurrency, progress callback |
| `web_ui.py` | LLM status banner, live progress, system overview panel |
| `run_pipeline.py` | **new** — headless end-to-end runner for testing |

Old `* - V_0.py` / `*-V_0.py` variants are superseded and were removed.
