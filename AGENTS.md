# DeepTutor — Agent-Native Architecture

## Overview

DeepTutor is an **agent-native** intelligent learning companion organized
around a two-layer plugin model — single-shot **Tools** invoked by the
LLM, and multi-stage **Capabilities** that take over a turn — exposed
through three entry points: CLI, WebSocket API, and Python SDK.

## Architecture

```
Entry Points:  CLI (Typer)  |  WebSocket /api/v1/ws  |  Python SDK
                    ↓                   ↓                   ↓
              ┌─────────────────────────────────────────────────┐
              │              ChatOrchestrator                    │
              │   routes UnifiedContext → selected Capability    │
              │   (defaults to `chat`)                           │
              └──────────┬──────────────┬───────────────────────┘
                         │              │
              ┌──────────▼──┐  ┌────────▼──────────┐
              │ ToolRegistry │  │ CapabilityRegistry │
              │  (Level 1)   │  │   (Level 2)        │
              └──────────────┘  └────────────────────┘
```

All capabilities emit on a shared `StreamBus`; the orchestrator fans
events out to consumers. Application settings live in
`data/user/settings/*.json`; the application does not parse a project-root
`.env`. Raw Compose or Podman Compose may still consume that file for YAML
interpolation only. The supported `scripts/docker_compose.py` wrapper instead
generates and passes `data/user/settings/docker.env` from the JSON settings.

### Level 1 — Tools

Single-function tools the LLM picks on demand. Seven user-toggleable tools
surface in `/settings/tools`:

| Tool | Description |
| --- | --- |
| `brainstorm` | Breadth-first idea exploration with rationale |
| `web_search` | Web search with citations |
| `paper_search` | arXiv preprint search |
| `reason` | Dedicated deep-reasoning LLM call |
| `geogebra_analysis` | Analyze an attached GeoGebra image |
| `imagegen` | Generate images when an image model is configured |
| `videogen` | Generate videos when a video model is configured |

`USER_TOGGLEABLE_TOOL_NAMES` in `deeptutor/tools/builtin/__init__.py` is the
canonical list. `imagegen` and `videogen` remain mountable when enabled; a
successful execution requires the corresponding generation model to be
configured. In `deeptutor/agents/_shared/tool_composition.py`, `ToolMountFlags`
gate `rag`, `kb_files`, `read_source`, `read_memory`, `list_notebook`,
`write_note`, `read_skill`, `load_tools`, `exec`, and `code_execution`;
`write_memory`, `web_fetch`, `github`, `ask_user`, and `cron` mount by default.
Capability-owned tools remain separate. `COMING_SOON_TOOL_TYPES` is currently
empty.

### Level 2 — Capabilities

Multi-stage pipelines that own the turn:

| Capability | Stages |
| --- | --- |
| `chat` | exploring → responding (single agentic loop, default) |
| `mastery_path` | responding (Guided Learning — chat loop + mastery tools, gated per topic type) |
| `deep_solve` | planning → reasoning → writing |
| `deep_question` | ideation → generation |
| `deep_research` | rephrasing → decomposing → researching → reporting |
| `visualize` | analyzing → generating → reviewing (SVG / Chart.js / Mermaid / HTML; or routes to Manim sub-stages via `render_type`) |
| `math_animator` | concept_analysis → concept_design → code_generation → code_retry → summary → render_output |

All capabilities converge on `emit_capability_result()` in
`deeptutor/agents/_shared/capability_result.py` so every turn emits the same
envelope (response payload + `cost_summary` from `UsageTracker`). Prompt
loading is centralized in `deeptutor/services/prompt/manager.py`; prompt files
live under both `deeptutor/agents/<name>/prompts/{en,zh}/` and
`deeptutor/capabilities/<name>/prompts/{en,zh}/`.

## CLI Usage

```bash
# Install from an authorized source checkout (Python >=3.11,<3.14)
python -m pip install -e .

# Or install only the compatible CLI distribution
python -m pip install -e ./packaging/deeptutor-cli

# Run any capability
deeptutor run chat "Explain Fourier transform"
deeptutor run deep_solve "Solve x^2=4" -t rag --kb my-kb
deeptutor run visualize "Animate sine wave" --config render_mode=manim_video

# Interactive REPL
deeptutor chat
# (inside the REPL: /regenerate or /retry re-runs the last user message)

# Partners (IM-connected companions)
deeptutor partner list

# Knowledge bases, memory, server
deeptutor kb list
deeptutor kb create my-kb --doc textbook.pdf
deeptutor memory show
deeptutor serve --port 8001       # API server only
deeptutor start                   # backend + frontend together
```

## Key Files

| Path | Purpose |
| --- | --- |
| `deeptutor/runtime/orchestrator.py` | `ChatOrchestrator` — unified entry |
| `deeptutor/runtime/launcher.py` | Backend + frontend lifecycle / port discovery |
| `deeptutor/runtime/registry/` | Tool + Capability registries |
| `deeptutor/runtime/bootstrap/builtin_capabilities.py` | Built-in capability class paths |
| `deeptutor/services/config/runtime_settings.py` | JSON settings + process-env overrides |
| `deeptutor/services/prompt/manager.py` | Shared prompt loading |
| `deeptutor/core/stream.py`, `stream_bus.py` | StreamEvent protocol + async fan-out |
| `deeptutor/core/tool_protocol.py` | `BaseTool` + `ToolDefinition` |
| `deeptutor/core/capability_protocol.py` | `BaseCapability` + `CapabilityManifest` |
| `deeptutor/core/context.py` | `UnifiedContext` dataclass |
| `deeptutor/tools/builtin/__init__.py` | Built-in tool wrappers and toggle lists |
| `deeptutor/agents/_shared/tool_composition.py` | Chat tool-mount policy |
| `deeptutor/agents/_shared/capability_result.py` | Shared capability-result envelope helper |
| `deeptutor/capabilities/` | Built-in capability implementations and prompts |
| `deeptutor/app/facade.py` | `DeepTutorApp` — Python SDK facade |
| `deeptutor_cli/main.py` | Typer CLI entry point |
| `deeptutor/api/routers/unified_ws.py` | Unified WebSocket endpoint |

## Dependency Layers

JustPass private source-install paths and extras are defined in `pyproject.toml`.
Requirements files mirror the same dependency groups for Docker/CI installs.

```
python -m pip install -e .                           — Full source install
python -m pip install -e ./packaging/deeptutor-cli   — Compatible CLI-only source install

Source extras (.[ extra ], defined in pyproject.toml):
.[cli]            — CLI-only dependency set
.[server]         — Web/API server dependencies
.[partners]       — Partner channel SDKs + MCP client  (legacy alias: .[tutorbot])
.[matrix]         — Matrix channel for Partners (matrix-nio; needs libolm)
.[matrix-e2e]     — Matrix with end-to-end encryption (matrix-nio[e2e])
.[math-animator]  — Manim addon (powers `visualize` Manim renders + `deeptutor run math_animator`)
.[dev]            — Test / lint tooling
.[all]            — Everything above
```
