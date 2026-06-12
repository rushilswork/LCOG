"""Stage 4: LLM-powered narrative generation.

Supported providers (set via --provider):
  groq    -- Groq cloud API (fast, free tier available). Default model: llama-3.3-70b-versatile
  gemini  -- Google Gemini API (free tier available).    Default model: gemini-1.5-flash

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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx

from onboard.stages.doc_collector import DocCorpus
from onboard.stages.git_analysis import FileHistory, RepoHistory
from onboard.stages.static_analysis import Symbol


# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS: dict[str, dict] = {
    "groq":   {"model": "llama-3.3-70b-versatile", "max_tokens": 1500},
    "gemini": {"model": "gemini-1.5-flash",         "max_tokens": 1500},
}


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


@dataclass
class OnboardingGuide:
    system_overview: str
    reading_order: list[str]
    modules: dict[str, ModuleNarrative] = field(default_factory=dict)
    guided_tour: str = ""
    major_themes: list[str] = field(default_factory=list)


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
# Section extraction (defined once, not inside the generation loop)
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


# ---------------------------------------------------------------------------
# Provider-agnostic LLM call with retry
# ---------------------------------------------------------------------------

_RATE_LIMIT_SIGNALS = ("rate_limit", "429", "too many", "quota", "resource_exhausted")
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 5  # seconds; doubles each attempt


def _call_llm(prompt: str, provider: str, api_key: str, model: str, max_tokens: int) -> str:
    """Call the specified LLM provider and return the response text.

    Retries up to _MAX_RETRIES times on rate-limit / transient errors with
    exponential backoff.  Raises on non-retriable errors or exhausted retries.
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
                wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
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
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        gemini_model = genai.GenerativeModel(model)
        resp = gemini_model.generate_content(prompt)
        # Gemini may block a response due to safety filters
        if not resp.parts:
            reason = getattr(resp.prompt_feedback, "block_reason", "unknown")
            raise ValueError(f"Gemini blocked the response (reason: {reason})")
        return resp.text.strip()

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

Write a concise onboarding walkthrough. Use these exact section headings:

## What this module does
## How it fits into the system
## Key design decisions
## Patterns to follow
## Pitfalls to avoid

Be concrete. Use the file name and symbol names. Avoid generic advice.
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
    max_tokens: int = 1500,
    max_modules: int = 50,
    console=None,
) -> OnboardingGuide:
    """Run the full LLM narrative generation pipeline."""

    defaults = PROVIDER_DEFAULTS.get(provider, {})
    model = model or defaults.get("model", "")

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
    modules_to_process = reading_order[:max_modules]

    log(f"Provider: {provider} | Model: {model}")
    log(f"Generating narratives for {len(modules_to_process)} modules...")

    for idx, path in enumerate(modules_to_process):
        node_data = graph.nodes.get(path, {})
        lang = node_data.get("language", "unknown")
        symbols: list[Symbol] = node_data.get("symbols", [])
        imports: list[str] = node_data.get("imports", [])
        fh = history.files.get(path)
        dependants = list(graph.predecessors(path))
        dependencies = list(graph.successors(path))

        prompt = MODULE_PROMPT.format(
            path=path,
            language=lang,
            symbols=_symbol_summary(symbols),
            imports=_imports_summary(imports),
            history=_history_summary(fh),
            docs=_doc_fragments_text(path, corpus),
            dependants=", ".join(dependants[:10]) or "none",
            dependencies=", ".join(dependencies[:10]) or "none",
        )

        log(f"  [{idx + 1}/{len(modules_to_process)}] {path}")

        try:
            raw = _call_llm(prompt, provider, api_key, model, max_tokens)
        except Exception as e:
            # Sanitize the error — strip the API key if it appears in the message
            err_msg = str(e).replace(api_key, "***") if api_key else str(e)
            raw = f"*Narrative generation failed: {err_msg}*"
            log(f"    [warning] {err_msg}")

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

        stem = Path(path).stem
        if stem == "__init__" and Path(path).parent != Path("."):
            mod_title = Path(path).parent.name.replace("_", " ").title() + " (init)"
        else:
            mod_title = stem.replace("_", " ").replace("-", " ").title()

        guide.modules[path] = ModuleNarrative(
            path=path,
            title=mod_title,
            summary=_extract_section(raw, "What this module does") or raw[:300],
            walkthrough=_extract_section(raw, "How it fits into the system"),
            design_notes=_extract_section(raw, "Key design decisions"),
            pitfalls=_extract_section(raw, "Pitfalls to avoid"),
            dead_code_warning=dead_warn,
            hotspot_warning=hotspot_warn,
            reading_order_index=idx,
        )

    # System overview
    log("Generating system overview...")
    hotspots_text = "\n".join(
        f"  {p} ({h.change_frequency} commits)"
        for p, h in sorted(history.files.items(), key=lambda kv: -kv[1].change_frequency)[:10]
    )
    arch_docs_text = "\n\n".join(f.content[:500] for f in corpus.arch_docs()[:2]) or "None found."

    overview_prompt = OVERVIEW_PROMPT.format(
        total_files=graph.number_of_nodes(),
        languages=", ".join(sorted({d.get("language", "?") for _, d in graph.nodes(data=True)})),
        entry_points=", ".join(entry_points[:5]) or "none detected",
        hotspots=hotspots_text,
        themes=", ".join(history.major_themes[:10]),
        arch_docs=arch_docs_text,
        reading_order="\n".join(f"  {i+1}. {p}" for i, p in enumerate(reading_order[:15])),
    )

    try:
        guide.system_overview = _call_llm(overview_prompt, provider, api_key, model, max_tokens)
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        guide.system_overview = f"*Overview generation failed: {err_msg}*"

    # Guided tour
    tour_parts = ["# Guided Tour\n\nFollow this sequence to build a mental model of the codebase.\n"]
    for idx, path in enumerate(reading_order[:15]):
        mod = guide.modules.get(path)
        if mod:
            import re as _re
            slug = _re.sub(r"[^\w\-]", "_", path)
            tour_parts.append(
                f"\n## Step {idx + 1}: `{path}`\n\n{mod.summary}\n\n"
                f"-> [Full walkthrough](modules/{slug}/)\n"
            )
    guide.guided_tour = "\n".join(tour_parts)

    return guide
