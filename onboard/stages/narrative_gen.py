"""Stage 4: LLM-powered narrative generation.

Supported providers (set via --provider):
  groq    -- Groq cloud API (fast, free tier available). Default model: llama-3.1-8b-instant
  gemini  -- Google Gemini API (free tier available).    Default model: gemini-2.0-flash

For each module / file group, synthesises:
- What this module does
- How it fits into the larger system
- Key design decisions
- Patterns to follow
- Pitfalls / dead code / weird-but-intentional callouts

Also produces:
- A global system overview
- A reading order (topological sort + hotspot tiebreaking)
- A guided tour
"""

from __future__ import annotations

import hashlib
import pickle
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import networkx as nx

from onboard.stages.doc_collector import DocCorpus
from onboard.stages.git_analysis import FileHistory, RepoHistory
from onboard.stages.static_analysis import Symbol

# Bump this whenever prompt templates change — invalidates all cached narratives.
_CACHE_VERSION = "v1"
_NARRATIVE_CACHE_FILE = "narrative_cache.pkl"


# ---------------------------------------------------------------------------
# Provider defaults  (fast by default to avoid 429s)
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS: dict[str, dict] = {
    "groq":   {"model": "llama-3.1-8b-instant", "max_tokens": 2500},
    "gemini": {"model": "gemini-2.0-flash",      "max_tokens": 2500},
}

MODEL_TIERS: dict[str, dict[str, str]] = {
    "groq": {
        "fast":     "llama-3.1-8b-instant",    # ~10× higher RPM than 70b
        "balanced": "llama-3.3-70b-versatile",
        "best":     "llama-3.3-70b-versatile",
    },
    "gemini": {
        "fast":     "gemini-2.0-flash",
        "balanced": "gemini-2.0-flash",
        "best":     "gemini-1.5-pro",
    },
}

# Max concurrent LLM requests — 8b-instant supports much higher throughput
_LLM_MAX_CONCURRENT = 8


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ModuleNarrative:
    path: str
    title: str
    summary: str
    walkthrough: str
    design_notes: str
    pitfalls: str
    dead_code_warning: Optional[str] = None
    hotspot_warning: Optional[str] = None
    reading_order_index: int = 0
    patterns: str = ""
    architecture_notes: str = ""
    entry_points_usage: str = ""
    code_walkthrough: str = ""
    sequence_diagram: str = ""       # Mermaid sequenceDiagram (AI, conditional)
    state_machine_diagram: str = ""  # Mermaid stateDiagram (AI, conditional)
    data_flow_snippet: str = ""      # Mermaid flowchart for this module's data flow (AI, conditional)


@dataclass
class OnboardingGuide:
    system_overview: str
    reading_order: list[str]
    modules: dict[str, ModuleNarrative] = field(default_factory=dict)
    guided_tour: str = ""
    major_themes: list[str] = field(default_factory=list)
    # ── Architecture docs ──────────────────────────────────────────────────
    arc42: str = ""
    c4_context_mermaid: str = ""
    c4_container_mermaid: str = ""
    domain_model_mermaid: str = ""
    data_flow_mermaid: str = ""
    dir_c4_components: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _symbol_summary(symbols: list[Symbol], max_items: int = 30) -> str:
    if not symbols:
        return "No symbols extracted."
    lines = [f"  - [{s.kind}] {s.name}  (lines {s.start_line}-{s.end_line})"
             for s in symbols[:max_items]]
    if len(symbols) > max_items:
        lines.append(f"  ... and {len(symbols) - max_items} more")
    return "\n".join(lines)


def _history_summary(fh: Optional[FileHistory]) -> str:
    if fh is None:
        return "No git history available."
    parts = [f"Change frequency: {fh.change_frequency} commits"]
    if fh.last_modified_days is not None:
        parts.append(f"Last modified: {fh.last_modified_days:.0f} days ago")
    if fh.top_authors:
        parts.append("Top authors: " + ", ".join(f"{a} ({n})" for a, n in fh.top_authors))
    if fh.commit_messages:
        parts.append("Recent commits: " + "; ".join(fh.commit_messages[:5]))
    if fh.co_changes:
        parts.append("Co-changes with: " + ", ".join(list(fh.co_changes.keys())[:5]))
    return "\n".join(parts)


def _doc_fragments_text(path: str, corpus: DocCorpus, max_chars: int = 2000) -> str:
    frags = corpus.for_file(path)
    if not frags:
        return "No inline documentation found."
    parts = []
    total = 0
    for f in frags:
        snippet = f.content[:500]
        parts.append(snippet)
        total += len(snippet)
        if total >= max_chars:
            break
    return "\n---\n".join(parts)


def _imports_summary(imports: list[str], max_items: int = 15) -> str:
    if not imports:
        return "No imports."
    result = "\n".join(f"  {i}" for i in imports[:max_items])
    if len(imports) > max_items:
        result += f"\n  ... and {len(imports) - max_items} more"
    return result


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

def _extract_section(text: str, heading: str) -> str:
    """Extract the content of a ## heading from an LLM response."""
    tag = f"## {heading}"
    start = text.find(tag)
    if start == -1:
        return ""
    start = text.find("\n", start) + 1
    end = text.find("## ", start)
    return text[start:end].strip() if end != -1 else text[start:].strip()


def _extract_mermaid(text: str, diagram_type: str) -> str:
    """Extract the first Mermaid block of a given type from LLM output."""
    pattern = r"```mermaid\s*\n(.*?)```"
    for match in re.finditer(pattern, text, re.DOTALL):
        body = match.group(1)
        if diagram_type.lower() in body.lower():
            return f"```mermaid\n{body.rstrip()}\n```"
    return ""


# ---------------------------------------------------------------------------
# Format-string injection guard
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    """Escape curly braces in user content so str.format() does not crash."""
    return s.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# Provider-agnostic LLM call with retry + jitter
# ---------------------------------------------------------------------------

_RATE_LIMIT_SIGNALS = ("rate_limit", "429", "too many", "quota", "resource_exhausted")
_MAX_RETRIES = 5
_RETRY_BACKOFF_BASE = 8  # seconds; doubles each attempt


def _call_llm(prompt: str, provider: str, api_key: str, model: str, max_tokens: int) -> str:
    """Call the specified LLM provider and return the response text.

    Retries up to _MAX_RETRIES times on rate-limit / transient errors with
    exponential backoff + jitter to prevent thundering herd under concurrency.
    Raises on non-retriable errors or exhausted retries.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(_MAX_RETRIES):
        try:
            return _call_llm_once(prompt, provider, api_key, model, max_tokens)
        except Exception as exc:
            last_exc = exc
            err_lower = str(exc).lower()
            is_retriable = any(sig in err_lower for sig in _RATE_LIMIT_SIGNALS)
            if is_retriable and attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 3)
                time.sleep(wait)
                continue
            raise

    raise last_exc  # type: ignore[misc]


def _call_llm_once(prompt: str, provider: str, api_key: str, model: str, max_tokens: int) -> str:
    """Single (non-retried) LLM call."""
    if provider == "groq":
        from groq import Groq
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()

    elif provider == "gemini":
        from google import genai
        from google.genai import types as genai_types
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
            ),
        )
        # In google-genai SDK, resp.text raises ValueError on blocked/empty
        # responses, so we surface a clear message rather than letting the SDK
        # raise an opaque error.
        try:
            text = resp.text
        except ValueError:
            candidates = getattr(resp, "candidates", [])
            reason = (
                candidates[0].finish_reason if candidates else "unknown"
            )
            raise ValueError(
                f"Gemini returned no usable content (finish_reason={reason}). "
                "The response may have been blocked by safety filters."
            )
        if not text:
            raise ValueError("Gemini returned an empty response.")
        return text.strip()

    else:
        raise ValueError(f"Unknown provider '{provider}'. Choose 'groq' or 'gemini'.")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

MODULE_PROMPT = """\
You are a senior engineer writing an onboarding guide for a new team member.

FILE: {path}
LANGUAGE: {language}

SYMBOLS DEFINED:
{symbols}

IMPORTS:
{imports}

GIT HISTORY:
{history}

EXISTING DOCUMENTATION:
{docs}

GRAPH CONTEXT:
  Dependants (files that import this): {dependants}
  Dependencies (files this imports):   {dependencies}

SOURCE CODE:
{source_code}

Write a comprehensive onboarding guide. Use these exact section headings:

## What this module does
## How it fits into the system
## Architecture notes
## Key design decisions
## How to use this module
## Code walkthrough
## Patterns to follow
## Pitfalls to avoid

Guidelines:
- "Architecture notes": explain the architectural pattern/paradigm used (e.g. repository pattern, event-driven, middleware chain, factory, etc.) and WHY that choice was made.
- "How to use this module": show the primary entry points, key public API, and a short usage example if applicable.
- "Code walkthrough": walk through the actual source code section by section (e.g. "Lines 1-20: imports and constants", "Lines 22-45: class X does Y"). Reference actual function/class names and line numbers.
- Be concrete. Use the file name and symbol names. Avoid generic advice.
"""

# ── Sequence / state / DFD addition (appended to MODULE_PROMPT when triggered) ──
MODULE_DIAGRAM_ADDENDUM = """
Additionally, if this module has complex behaviour, generate ONE OR MORE of the
following Mermaid diagrams — only when genuinely applicable:

If the module orchestrates a meaningful multi-step flow (e.g. handles requests,
processes jobs, runs a pipeline), add:

## Sequence diagram
```mermaid
sequenceDiagram
    ...
```

If the module manages lifecycle state (e.g. order status, job status, connection
state), add:

## State machine
```mermaid
stateDiagram-v2
    ...
```

If the module transforms or routes data through multiple steps, add:

## Data flow
```mermaid
flowchart LR
    ...
```

Omit any diagram that does not apply. Do NOT invent behaviour — only diagram
what the source code clearly shows.
"""

ARC42_PROMPT = """\
You are a senior software architect writing an Arc42 architecture document for
a new team member joining this project.

SYSTEM NAME: {system_name}
LANGUAGES: {languages}
FRAMEWORKS: {frameworks}
EXTERNAL SYSTEMS: {external_systems}
ENTRY POINTS: {entry_points}
TOP HOTSPOTS: {hotspots}
MAJOR THEMES FROM GIT: {themes}
ARCHITECTURE DOCS FOUND: {arch_docs}
PACKAGE STRUCTURE: {packages}
READING ORDER (top modules): {reading_order}

Write a complete Arc42 document using EXACTLY these section headings (all 12):

## 1. Introduction and Goals
## 2. Constraints
## 3. Context and Scope
## 4. Solution Strategy
## 5. Building Block View
## 6. Runtime View
## 7. Deployment View
## 8. Cross-cutting Concepts
## 9. Architecture Decisions
## 10. Quality Requirements
## 11. Risks and Technical Debt
## 12. Glossary

Rules:
- Section 3 MUST contain a Mermaid C4Context diagram inside ```mermaid fences
- Section 5 MUST contain a Mermaid C4Container diagram inside ```mermaid fences
- Section 6 MUST contain at least one Mermaid sequenceDiagram showing a key runtime flow
- Use actual file names, class names, and module names from the context above
- Section 11 should reference the hotspot files by name
- Section 12 should define 5-10 domain terms found in the symbol/module names
- Be specific and technical — avoid boilerplate generic text
"""

DOMAIN_MODEL_PROMPT = """\
You are a senior software architect extracting the domain model from a codebase.

SYSTEM NAME: {system_name}
LANGUAGES: {languages}
TOP-LEVEL CLASSES AND ENTITIES:
{entities}
DATABASE / ORM IMPORTS: {db_imports}
TOP MODULE NAMES: {modules}

Generate:

## Domain overview
2-3 paragraphs describing the core domain concepts and their relationships.

## Entity-relationship diagram
A Mermaid erDiagram showing the key domain entities and their relationships:
```mermaid
erDiagram
    ...
```

## Key entities
For each major entity: one sentence on its role in the domain.

Use actual class and module names from the context. Only model entities that
clearly exist in the code — do not invent domain concepts.
"""

DFD_PROMPT = """\
You are a senior software architect documenting data flow through a system.

SYSTEM NAME: {system_name}
FRAMEWORKS: {frameworks}
EXTERNAL SYSTEMS: {external_systems}
ENTRY POINTS: {entry_points}
DATA-RELATED MODULES (sorted by centrality):
{data_modules}
PACKAGE STRUCTURE: {packages}

Generate:

## Data flow overview
2-3 paragraphs describing how data enters, moves through, and exits the system.

## Data flow diagram
A Mermaid flowchart showing data sources → processing layers → outputs/sinks:
```mermaid
flowchart TD
    ...
```

## Key transformations
For each major data transformation: what goes in, what comes out, which module handles it.

Be specific — use actual module names and external system names from the context.
"""

C4_COMPONENT_PROMPT = """\
You are a senior software architect writing a C4 Level 3 Component diagram.

PACKAGE: {package_name}
FILES IN THIS PACKAGE:
{files}
IMPORTS FROM OTHER PACKAGES: {external_imports}
IMPORTED BY: {imported_by}

Generate:

## Package overview
1-2 sentences on this package's role in the overall system.

## Component diagram
A Mermaid C4Component diagram showing the components inside this package:
```mermaid
C4Component
    ...
```

Use actual file/class names. Keep to the real components — don't invent structure.
"""

OVERVIEW_PROMPT = """\
You are a senior engineer writing a system overview for a new team member.

CODEBASE STATS:
  Total source files: {total_files}
  Languages: {languages}
  Entry points: {entry_points}

TOP HOTSPOTS (most changed files):
{hotspots}

MAJOR COMMIT THEMES:
{themes}

ARCHITECTURE DOCS FOUND:
{arch_docs}

SUGGESTED READING ORDER:
{reading_order}

Write a system overview in 3-5 paragraphs:
1. What this system does
2. High-level architecture
3. Where to start reading and why
4. The most important things to know before touching the code

Use actual file and module names. Be specific.
"""


# ---------------------------------------------------------------------------
# Reading order
# ---------------------------------------------------------------------------

def _reading_order(graph: nx.DiGraph, history: RepoHistory, entry_points: list[str]) -> list[str]:
    try:
        topo = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        cond = nx.condensation(graph)
        topo_sccs = list(nx.topological_sort(cond))
        topo = []
        for scc_node in topo_sccs:
            topo.extend(list(cond.nodes[scc_node]["members"]))

    depth: dict[str, int] = {}
    for ep in entry_points:
        if ep in graph:
            for node, d in nx.single_source_shortest_path_length(graph, ep).items():
                depth[node] = min(depth.get(node, 9999), d)

    def sort_key(path: str):
        freq = history.files.get(path, FileHistory(path=path)).change_frequency
        return (depth.get(path, 9999), -freq)

    return sorted(topo, key=sort_key)


# ---------------------------------------------------------------------------
# Per-module generation helper (thread-safe, no shared mutable state)
# ---------------------------------------------------------------------------

_SOURCE_CODE_CHAR_LIMIT = 6000  # ~1500 tokens; keeps prompts manageable

# Trivial module threshold — skip LLM for tiny modules with no git heat
_TRIVIAL_MAX_SYMBOLS = 3


def _read_source(repo_path: Path, rel_path: str) -> str:
    """Read source from disk, capped at _SOURCE_CODE_CHAR_LIMIT chars."""
    try:
        full = (repo_path / rel_path).read_text(encoding="utf-8", errors="replace")
        if len(full) > _SOURCE_CODE_CHAR_LIMIT:
            return full[:_SOURCE_CODE_CHAR_LIMIT] + f"\n... (truncated at {_SOURCE_CODE_CHAR_LIMIT} chars)"
        return full
    except OSError:
        return "(source file not readable)"


def _make_mod_title(path: str) -> str:
    stem = Path(path).stem
    if stem == "__init__" and Path(path).parent != Path("."):
        return Path(path).parent.name.replace("_", " ").title() + " (init)"
    return stem.replace("_", " ").replace("-", " ").title()


def _generate_module(
    path: str,
    graph: nx.DiGraph,
    history: RepoHistory,
    corpus: DocCorpus,
    provider: str,
    api_key: str,
    model: str,
    max_tokens: int,
    repo_path: Optional[Path] = None,
) -> tuple[str, ModuleNarrative, Optional[str]]:
    """Generate narrative for one module. Designed to run inside a thread pool.

    Returns (path, ModuleNarrative, err_logged). Never raises — errors are
    captured as a stub narrative so the rest of the guide can still be built.
    """
    node_data = graph.nodes.get(path, {})
    lang = node_data.get("language", "unknown")
    symbols: list[Symbol] = node_data.get("symbols", [])
    imports: list[str] = node_data.get("imports", [])
    fh = history.files.get(path)
    dependants = list(graph.predecessors(path))
    dependencies = list(graph.successors(path))

    # ── Trivial module fast-path: skip LLM entirely ──────────────────────
    # A module is trivial if: few symbols, nothing imports it, no git history.
    # These are typically small helpers / __init__ re-exports.
    _is_trivial = (
        len(symbols) < _TRIVIAL_MAX_SYMBOLS
        and not dependants
        and (fh is None or fh.change_frequency == 0)
    )
    if _is_trivial:
        return path, ModuleNarrative(
            path=path,
            title=_make_mod_title(path),
            summary=f"*Small utility module ({lang}) — no significant symbols or git activity.*",
            walkthrough="",
            design_notes="",
            pitfalls="",
            patterns="",
            architecture_notes="",
            entry_points_usage="",
            code_walkthrough="",
            sequence_diagram="",
            state_machine_diagram="",
            data_flow_snippet="",
        ), None

    source_code = _read_source(repo_path, path) if repo_path else "(source not available)"

    # Determine if this module warrants extra diagrams:
    _name_lower = Path(path).stem.lower()
    _diagram_hints = ("state", "flow", "pipeline", "process", "handler",
                      "worker", "job", "task", "service", "manager", "engine",
                      "router", "controller", "middleware", "dispatcher")
    _needs_diagrams = (
        (fh and fh.change_frequency > 20)
        or len(symbols) > 8
        or any(h in _name_lower for h in _diagram_hints)
    )

    prompt_base = MODULE_PROMPT.format(
        path=_esc(path),
        language=_esc(lang),
        symbols=_esc(_symbol_summary(symbols)),
        imports=_esc(_imports_summary(imports)),
        history=_esc(_history_summary(fh)),
        docs=_esc(_doc_fragments_text(path, corpus)),
        dependants=_esc(", ".join(dependants[:10]) or "none"),
        dependencies=_esc(", ".join(dependencies[:10]) or "none"),
        source_code=_esc(source_code),
    )
    prompt = prompt_base + (MODULE_DIAGRAM_ADDENDUM if _needs_diagrams else "")

    try:
        raw = _call_llm(prompt, provider, api_key, model, max_tokens)
        err_logged = None
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        # Write a clean MkDocs admonition instead of raw error text
        raw = (
            "!!! warning \"Narrative unavailable\"\n"
            "    Generation failed — re-run with `--retry-failed` to retry this module.\n\n"
            f"    Error: `{err_msg[:200]}`\n"
        )
        err_logged = err_msg

    dead_warn = None
    if fh and fh.is_dead_code_candidate:
        dead_warn = (
            f"**Potential dead code**: not modified in {fh.last_modified_days:.0f} days. "
            "Verify it is still in use before editing."
        )

    hotspot_warn = None
    if fh and fh.change_frequency > 50:
        hotspot_warn = (
            f"**Hotspot**: changed {fh.change_frequency} times -- high churn. "
            "Add tests before modifying."
        )

    narrative = ModuleNarrative(
        path=path,
        title=_make_mod_title(path),
        summary=_extract_section(raw, "What this module does") or raw[:300],
        walkthrough=_extract_section(raw, "How it fits into the system"),
        design_notes=_extract_section(raw, "Key design decisions"),
        pitfalls=_extract_section(raw, "Pitfalls to avoid"),
        patterns=_extract_section(raw, "Patterns to follow"),
        architecture_notes=_extract_section(raw, "Architecture notes"),
        entry_points_usage=_extract_section(raw, "How to use this module"),
        code_walkthrough=_extract_section(raw, "Code walkthrough"),
        sequence_diagram=_extract_mermaid(raw, "sequenceDiagram"),
        state_machine_diagram=_extract_mermaid(raw, "stateDiagram"),
        data_flow_snippet=_extract_mermaid(raw, "flowchart"),
        dead_code_warning=dead_warn,
        hotspot_warning=hotspot_warn,
        reading_order_index=0,  # set by caller after collection
    )
    return path, narrative, err_logged


# ---------------------------------------------------------------------------
# Architecture doc generators (each makes one LLM call, gracefully degrades)
# ---------------------------------------------------------------------------

def _generate_arc42(
    graph: nx.DiGraph,
    history: RepoHistory,
    corpus: DocCorpus,
    tech: "TechContext",
    reading_order: list[str],
    provider: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    from onboard.stages.static_analysis import summarize_graph
    summary = summarize_graph(graph)
    hotspot_list = sorted(
        history.files.values(), key=lambda fh: fh.change_frequency, reverse=True
    )[:8]
    hotspots_text = ", ".join(
        f"{fh.path} ({fh.change_frequency} commits)" for fh in hotspot_list
    ) or "none"
    arch_docs = corpus.arch_docs()
    arch_text = "\n".join(f.content[:400] for f in arch_docs[:3]) or "None found."
    packages = sorted({
        Path(n).parts[0] for n in graph.nodes() if len(Path(n).parts) > 1
    })
    packages_text = ", ".join(packages[:15]) or "flat structure"
    nodes_list = list(graph.nodes())
    sys_name = (
        Path(nodes_list[0]).parts[0]
        if nodes_list and len(Path(nodes_list[0]).parts) > 1 else "system"
    )
    prompt = ARC42_PROMPT.format(
        system_name=_esc(sys_name),
        languages=_esc(str(summary.get("languages", {}))),
        frameworks=_esc(tech.frameworks_text()),
        external_systems=_esc(tech.external_systems_text()),
        entry_points=_esc(", ".join(summary.get("entry_points", [])[:8]) or "none"),
        hotspots=_esc(hotspots_text),
        themes=_esc(", ".join(history.major_themes[:10]) or "none"),
        arch_docs=_esc(arch_text),
        packages=_esc(packages_text),
        reading_order=_esc(", ".join(reading_order[:10])),
    )
    try:
        return _call_llm(prompt, provider, api_key, model, max_tokens)
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        return f"*Arc42 generation failed: {err_msg}*"


def _generate_domain_model(
    graph: nx.DiGraph,
    tech: "TechContext",
    provider: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    from onboard.stages.static_analysis import summarize_graph
    from onboard.stages.static_analysis import Symbol
    summary = summarize_graph(graph)
    entity_lines = []
    for node, data in list(graph.nodes(data=True))[:60]:
        symbols: list[Symbol] = data.get("symbols", [])
        classes = [s for s in symbols if s.kind == "class"]
        for cls in classes[:5]:
            entity_lines.append(f"  [{node}] {cls.name}")
    entities_text = "\n".join(entity_lines[:40]) or "No class definitions found."
    db_imports = [
        imp for _node, data in graph.nodes(data=True)
        for imp in data.get("imports", [])
        if any(k in imp.lower() for k in ("model", "schema", "entity", "orm",
                                           "sqlalchemy", "django.db", "peewee",
                                           "tortoise", "mongoengine"))
    ]
    db_text = ", ".join(sorted(set(db_imports))[:10]) or "none detected"
    nodes_list = list(graph.nodes())
    sys_name = (
        Path(nodes_list[0]).parts[0]
        if nodes_list and len(Path(nodes_list[0]).parts) > 1 else "system"
    )
    top_modules = ", ".join(nodes_list[:10])
    prompt = DOMAIN_MODEL_PROMPT.format(
        system_name=_esc(sys_name),
        languages=_esc(str(summary.get("languages", {}))),
        entities=_esc(entities_text),
        db_imports=_esc(db_text),
        modules=_esc(top_modules),
    )
    try:
        return _call_llm(prompt, provider, api_key, model, max_tokens)
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        return f"*Domain model generation failed: {err_msg}*"


def _generate_dfd(
    graph: nx.DiGraph,
    tech: "TechContext",
    provider: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> str:
    from onboard.stages.static_analysis import summarize_graph
    summary = summarize_graph(graph)
    centrality = sorted(
        graph.nodes(), key=lambda n: graph.degree(n), reverse=True
    )[:15]
    data_modules_text = "\n".join(f"  {n}" for n in centrality) or "  (no graph data)"
    packages = sorted({
        Path(n).parts[0] for n in graph.nodes() if len(Path(n).parts) > 1
    })
    packages_text = ", ".join(packages[:15]) or "flat structure"
    nodes_list = list(graph.nodes())
    sys_name = (
        Path(nodes_list[0]).parts[0]
        if nodes_list and len(Path(nodes_list[0]).parts) > 1 else "system"
    )
    prompt = DFD_PROMPT.format(
        system_name=_esc(sys_name),
        frameworks=_esc(tech.frameworks_text()),
        external_systems=_esc(tech.external_systems_text()),
        entry_points=_esc(", ".join(summary.get("entry_points", [])[:8]) or "none"),
        data_modules=_esc(data_modules_text),
        packages=_esc(packages_text),
    )
    try:
        return _call_llm(prompt, provider, api_key, model, max_tokens)
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        return f"*Data flow generation failed: {err_msg}*"


def _generate_c4_components(
    graph: nx.DiGraph,
    provider: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> dict[str, str]:
    from collections import defaultdict as _defaultdict
    dir_nodes: dict[str, list[str]] = _defaultdict(list)
    for node in graph.nodes():
        top = Path(node).parts[0] if len(Path(node).parts) > 1 else "_root_"
        dir_nodes[top].append(node)
    results: dict[str, str] = {}
    for dir_name, files in dir_nodes.items():
        if len(files) < 3:
            continue
        external_imports: set[str] = set()
        for f in files:
            data = graph.nodes.get(f, {})
            for imp in data.get("imports", []):
                root = imp.split(".")[0] if "." in imp else imp
                if root and root != dir_name.replace("/", "."):
                    external_imports.add(imp)
        imported_by: set[str] = set()
        for f in files:
            for pred in graph.predecessors(f):
                pred_dir = (
                    Path(pred).parts[0] if len(Path(pred).parts) > 1 else "_root_"
                )
                if pred_dir != dir_name:
                    imported_by.add(pred_dir)
        files_text = "\n".join(f"  {f}" for f in sorted(files)[:20])
        ext_text = ", ".join(sorted(external_imports)[:15]) or "none"
        iby_text = ", ".join(sorted(imported_by)[:10]) or "none"
        prompt = C4_COMPONENT_PROMPT.format(
            package_name=_esc(dir_name),
            files=_esc(files_text),
            external_imports=_esc(ext_text),
            imported_by=_esc(iby_text),
        )
        try:
            results[dir_name] = _call_llm(prompt, provider, api_key, model, max_tokens)
        except Exception as e:
            err_msg = str(e).replace(api_key, "***") if api_key else str(e)
            results[dir_name] = (
                f"*C4Component generation failed for {dir_name}: {err_msg}*"
            )
    return results


def _build_overview_prompt(
    graph: nx.DiGraph,
    history: RepoHistory,
    corpus: DocCorpus,
    modules_to_process: list[str],
) -> str:
    from onboard.stages.static_analysis import summarize_graph
    summary = summarize_graph(graph)

    hotspot_lines = []
    sorted_files = sorted(
        history.files.values(),
        key=lambda fh: fh.change_frequency,
        reverse=True,
    )
    for fh in sorted_files[:10]:
        hotspot_lines.append(f"  {fh.path}  ({fh.change_frequency} commits)")

    arch_docs = corpus.arch_docs()
    arch_text = "\n".join(f.content[:300] for f in arch_docs[:3]) or "None found."

    return OVERVIEW_PROMPT.format(
        total_files=summary["total_files"],
        languages=_esc(str(summary["languages"])),
        entry_points=_esc(", ".join(summary["entry_points"][:10]) or "none detected"),
        hotspots=_esc("\n".join(hotspot_lines) or "  (no git history)"),
        themes=_esc(", ".join(history.major_themes[:10]) or "none detected"),
        arch_docs=_esc(arch_text),
        reading_order=_esc("\n".join(f"  {p}" for p in modules_to_process[:20])),
    )


# ---------------------------------------------------------------------------
# LLM Output Cache
# ---------------------------------------------------------------------------

@dataclass
class _ModuleCacheEntry:
    cache_key: str
    narrative: "ModuleNarrative"
    timestamp: float = field(default_factory=time.time)


@dataclass
class _ArchCacheEntry:
    cache_key: str
    system_overview: str
    arc42: str
    domain_model_mermaid: str
    data_flow_mermaid: str
    dir_c4_components: dict
    c4_context_mermaid: str
    c4_container_mermaid: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class _NarrativeCache:
    modules: dict[str, _ModuleCacheEntry] = field(default_factory=dict)
    arch: Optional[_ArchCacheEntry] = None


def _load_narrative_cache(cache_dir: Path) -> _NarrativeCache:
    path = cache_dir / _NARRATIVE_CACHE_FILE
    try:
        if path.exists():
            with path.open("rb") as f:
                obj = pickle.load(f)
            if isinstance(obj, _NarrativeCache):
                return obj
    except Exception:
        pass
    return _NarrativeCache()


def _save_narrative_cache(cache_dir: Path, cache: _NarrativeCache) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / _NARRATIVE_CACHE_FILE
        with path.open("wb") as f:
            pickle.dump(cache, f)
    except Exception:
        pass  # cache write failure is non-fatal


def _module_cache_key(
    path: str,
    repo_path: Path,
    graph: nx.DiGraph,
    model: str,
) -> str:
    """SHA256 over: own content + direct neighbors' content + model + cache version.

    Neighbors = files this module imports (successors) + files that import this
    module (predecessors). Both affect the LLM prompt context, so a change to
    either invalidates the cached narrative.
    """
    h = hashlib.sha256()
    # Own file content
    try:
        h.update((repo_path / path).read_bytes())
    except OSError:
        h.update(path.encode())
    # All direct neighbors (both directions) — sorted for determinism
    neighbors = sorted(set(graph.predecessors(path)) | set(graph.successors(path)))
    for dep in neighbors:
        try:
            h.update((repo_path / dep).read_bytes())
        except OSError:
            h.update(dep.encode())
    # Model + prompt version — different model or changed prompt = different output
    h.update(f"|{model}|{_CACHE_VERSION}".encode())
    return h.hexdigest()


def _arch_cache_key(graph: nx.DiGraph, model: str) -> str:
    """SHA256 over: full graph topology + model + cache version.

    Arch docs (arc42, C4, domain model, data flow, overview) reflect the entire
    codebase structure, so any topology change — added file, new import edge,
    removed module — invalidates them.
    """
    h = hashlib.sha256()
    for node in sorted(graph.nodes()):
        h.update(node.encode())
    for u, v in sorted(graph.edges()):
        h.update(f"{u}\x00{v}".encode())
    h.update(f"|{model}|{_CACHE_VERSION}".encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_guide(
    graph: nx.DiGraph,
    history: RepoHistory,
    corpus: DocCorpus,
    repo_path: Path,
    provider: str,
    api_key: str,
    model: Optional[str] = None,
    max_tokens: int = 2500,
    max_modules: int = 50,
    console=None,
    module_done_callback: Optional[Callable[[str, ModuleNarrative], None]] = None,
    only_paths: Optional[list[str]] = None,
    cache_dir: Optional[Path] = None,
) -> OnboardingGuide:
    """Run the full LLM narrative generation pipeline.

    module_done_callback: called on the thread-pool thread each time a module
        narrative finishes — use to write progressive page updates.
    only_paths: if set, only narrate these specific paths (used by --retry-failed).
    cache_dir: if set, load/save LLM output cache here. Cache keys are SHA256
        hashes of file content + neighbors + model + prompt version, so the cache
        is invalidated precisely when the underlying code or context changes.
    """
    from onboard.stages.tech_detector import detect_tech_context
    from onboard.stages.tech_detector import TechContext

    defaults = PROVIDER_DEFAULTS.get(provider, {})
    model = model or defaults.get("model", "")
    max_tokens = max_tokens or defaults.get("max_tokens", 2500)

    def log(msg: str):
        if console:
            console.log(msg)

    guide = OnboardingGuide(
        system_overview="",
        reading_order=[],
        major_themes=history.major_themes,
    )

    entry_points = [n for n, d in graph.nodes(data=True) if d.get("is_entry_point")]
    reading_order = _reading_order(graph, history, entry_points)
    guide.reading_order = reading_order

    # If retrying specific paths, only process those; otherwise use reading order
    if only_paths:
        modules_to_process = [p for p in reading_order if p in set(only_paths)]
        modules_to_process += [p for p in only_paths if p not in set(reading_order)]
    else:
        modules_to_process = reading_order[:max_modules]

    # ── Load LLM output cache ────────────────────────────────────────────
    _cache: _NarrativeCache = _load_narrative_cache(cache_dir) if cache_dir else _NarrativeCache()
    _cache_dirty = False

    log(f"Provider: {provider} | Model: {model}")
    log(f"Generating narratives for {len(modules_to_process)} modules "
        f"({_LLM_MAX_CONCURRENT} concurrent)...")

    # ── Concurrent module narrative generation ────────────────────────────
    raw_results: dict[str, ModuleNarrative] = {}
    done_count = 0
    cache_hits = 0

    # Separate cached modules from those that need LLM calls.
    # Precompute cache keys so they are not recalculated at store time.
    needs_llm: list[tuple[str, Optional[str]]] = []  # (path, precomputed_key_or_None)
    for path in modules_to_process:
        if cache_dir:
            ck = _module_cache_key(path, repo_path, graph, model)
            entry = _cache.modules.get(path)
            if entry is not None and entry.cache_key == ck:
                raw_results[path] = entry.narrative
                cache_hits += 1
                # Fire progressive callback for cached result too
                if module_done_callback:
                    try:
                        module_done_callback(path, entry.narrative)
                    except Exception:
                        pass
                continue
            needs_llm.append((path, ck))
        else:
            needs_llm.append((path, None))

    if cache_hits:
        log(f"  [cache] {cache_hits} module(s) served from cache, "
            f"{len(needs_llm)} need LLM calls")

    _mod_exec = ThreadPoolExecutor(max_workers=_LLM_MAX_CONCURRENT)
    try:
        # future_map: future → (path, precomputed_cache_key)
        future_map = {
            _mod_exec.submit(
                _generate_module,
                path, graph, history, corpus,
                provider, api_key, model, max_tokens, repo_path,
            ): (path, ck)
            for path, ck in needs_llm
        }
        for future in as_completed(future_map):
            path, ck = future_map[future]
            try:
                _path, narrative, err_logged = future.result()
            except Exception as exc:
                log(f"  [error] unexpected thread error: {exc}")
                continue
            done_count += 1
            log(f"  [{done_count}/{len(needs_llm)}] {path}")
            if err_logged:
                log(f"    [warning] {err_logged}")
            raw_results[path] = narrative
            # Store in cache using the precomputed key — no second file read
            if cache_dir and ck is not None:
                _cache.modules[path] = _ModuleCacheEntry(cache_key=ck, narrative=narrative)
                _cache_dirty = True
            # Progressive callback — fires as soon as each module is done
            if module_done_callback:
                try:
                    module_done_callback(path, narrative)
                except Exception:
                    pass
        _mod_exec.shutdown(wait=True)
    except KeyboardInterrupt:
        if _cache_dirty and cache_dir:
            _save_narrative_cache(cache_dir, _cache)
        _mod_exec.shutdown(wait=False, cancel_futures=True)
        raise
    except Exception:
        if _cache_dirty and cache_dir:
            _save_narrative_cache(cache_dir, _cache)
        _mod_exec.shutdown(wait=False, cancel_futures=True)
        raise

    # Persist cache after module generation completes
    if _cache_dirty and cache_dir:
        _save_narrative_cache(cache_dir, _cache)
        _cache_dirty = False

    # Restore reading order and assign index
    for idx, path in enumerate(modules_to_process):
        if path in raw_results:
            raw_results[path].reading_order_index = idx
            guide.modules[path] = raw_results[path]

    # ── Tech context (static, no LLM) ────────────────────────────────────
    log("Detecting technology context...")
    try:
        tech = detect_tech_context(graph)
        log(f"  {tech.summary()}")
    except Exception as e:
        log(f"  [warning] tech detection failed: {e}")
        tech = TechContext()

    # ── Architecture docs — cache check then parallel generation ─────────
    log("Generating architecture documents (parallel)...")
    overview_prompt = _build_overview_prompt(graph, history, corpus, modules_to_process)

    arch_key = _arch_cache_key(graph, model) if cache_dir else None
    arch_entry = _cache.arch if (arch_key and _cache.arch and _cache.arch.cache_key == arch_key) else None

    if arch_entry:
        log("  [cache] architecture docs served from cache")
        guide.system_overview    = arch_entry.system_overview
        guide.arc42              = arch_entry.arc42
        guide.domain_model_mermaid = arch_entry.domain_model_mermaid
        guide.data_flow_mermaid  = arch_entry.data_flow_mermaid
        guide.dir_c4_components  = arch_entry.dir_c4_components
        guide.c4_context_mermaid = arch_entry.c4_context_mermaid
        guide.c4_container_mermaid = arch_entry.c4_container_mermaid
    else:
        _arch_exec = ThreadPoolExecutor(max_workers=5)
        try:
            f_overview = _arch_exec.submit(
                _call_llm, overview_prompt, provider, api_key, model, max_tokens
            )
            f_arc42 = _arch_exec.submit(
                _generate_arc42,
                graph, history, corpus, tech, modules_to_process[:20],
                provider, api_key, model, max_tokens,
            )
            f_domain = _arch_exec.submit(
                _generate_domain_model,
                graph, tech, provider, api_key, model, max_tokens,
            )
            f_dfd = _arch_exec.submit(
                _generate_dfd,
                graph, tech, provider, api_key, model, max_tokens,
            )
            f_c4 = _arch_exec.submit(
                _generate_c4_components,
                graph, provider, api_key, model, max_tokens,
            )

            # Collect results (order matters for C4 extraction).
            # Track failures — only cache arch docs if ALL succeeded, so a
            # transient error (429, timeout) doesn't get permanently cached.
            _arch_any_failed = False

            try:
                guide.system_overview = f_overview.result()
            except Exception as e:
                err_msg = str(e).replace(api_key, "***") if api_key else str(e)
                guide.system_overview = f"*Overview generation failed: {err_msg}*"
                log(f"  [warning] overview: {err_msg}")
                _arch_any_failed = True

            guide.c4_context_mermaid = _extract_mermaid(guide.system_overview, "C4Context")

            try:
                arc42_text = f_arc42.result()
            except Exception as e:
                err_msg = str(e).replace(api_key, "***") if api_key else str(e)
                arc42_text = f"*Arc42 generation failed: {err_msg}*"
                log(f"  [warning] arc42: {err_msg}")
                _arch_any_failed = True
            guide.arc42 = arc42_text
            guide.c4_context_mermaid = (
                guide.c4_context_mermaid or _extract_mermaid(arc42_text, "C4Context")
            )
            guide.c4_container_mermaid = _extract_mermaid(arc42_text, "C4Container")

            try:
                guide.domain_model_mermaid = f_domain.result()
            except Exception as e:
                err_msg = str(e).replace(api_key, "***") if api_key else str(e)
                guide.domain_model_mermaid = f"*Domain model generation failed: {err_msg}*"
                log(f"  [warning] domain model: {err_msg}")
                _arch_any_failed = True

            try:
                guide.data_flow_mermaid = f_dfd.result()
            except Exception as e:
                err_msg = str(e).replace(api_key, "***") if api_key else str(e)
                guide.data_flow_mermaid = f"*Data flow generation failed: {err_msg}*"
                log(f"  [warning] data flow: {err_msg}")
                _arch_any_failed = True

            try:
                guide.dir_c4_components = f_c4.result()
            except Exception as e:
                err_msg = str(e).replace(api_key, "***") if api_key else str(e)
                guide.dir_c4_components = {}
                log(f"  [warning] C4 components: {err_msg}")
                _arch_any_failed = True

            _arch_exec.shutdown(wait=True)
        except KeyboardInterrupt:
            _arch_exec.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception:
            _arch_exec.shutdown(wait=False, cancel_futures=True)
            raise

        # Only cache arch docs if all 5 succeeded — a transient failure
        # (rate limit, timeout) must not be permanently cached.
        if cache_dir and arch_key and not _arch_any_failed:
            _cache.arch = _ArchCacheEntry(
                cache_key=arch_key,
                system_overview=guide.system_overview,
                arc42=guide.arc42,
                domain_model_mermaid=guide.domain_model_mermaid,
                data_flow_mermaid=guide.data_flow_mermaid,
                dir_c4_components=guide.dir_c4_components,
                c4_context_mermaid=guide.c4_context_mermaid,
                c4_container_mermaid=guide.c4_container_mermaid,
            )
            _save_narrative_cache(cache_dir, _cache)
        elif _arch_any_failed:
            log("  [cache] arch docs not cached — re-run to retry failed docs")

    # ── Guided tour ───────────────────────────────────────────────────────
    guide.guided_tour = _build_guided_tour(guide, graph, history)

    return guide


# ---------------------------------------------------------------------------
# Guided tour builder
# ---------------------------------------------------------------------------

def _build_guided_tour(
    guide: OnboardingGuide,
    graph: nx.DiGraph,
    history: RepoHistory,
) -> str:
    """Build a markdown guided tour from the generated narratives."""
    lines = ["# Guided Tour\n"]
    lines.append(
        "This tour walks you through the codebase in the recommended reading order. "
        "Follow the links to dive deeper into each module.\n"
    )

    for idx, path in enumerate(guide.reading_order):
        mod = guide.modules.get(path)
        if mod is None:
            continue

        slug = re.sub(r"[^\w\-]", "_", path)
        lines.append(f"## Step {idx + 1}: [{mod.title}](modules/{slug}.md)\n")

        if mod.summary:
            first_para = mod.summary.split("\n\n")[0].strip()
            # Don't include admonition blocks in the tour summary
            if not first_para.startswith("!!!"):
                lines.append(first_para + "\n")

        fh = history.files.get(path)
        if fh:
            if fh.is_dead_code_candidate:
                lines.append(
                    f"> Potential dead code: not modified in "
                    f"{fh.last_modified_days:.0f} days.\n"
                )
            elif fh.change_frequency > 50:
                lines.append(
                    f"> Hotspot: changed {fh.change_frequency} times.\n"
                )

    return "\n".join(lines)
