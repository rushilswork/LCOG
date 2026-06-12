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

import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Max concurrent LLM requests (tune down if hitting rate limits)
_LLM_MAX_CONCURRENT = 3


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


# ---------------------------------------------------------------------------
# Format-string injection guard
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    """Escape curly braces in user content so str.format() does not crash.

    Docstrings, commit messages, and symbol names regularly contain curly
    braces: Python dicts ("returns {key: val}"), TypeScript generics
    ("{T extends object}"), commit messages ("add {env} support"), etc.
    Without escaping these, MODULE_PROMPT.format(...) raises KeyError.
    """
    return s.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# Provider-agnostic LLM call with retry + jitter
# ---------------------------------------------------------------------------

_RATE_LIMIT_SIGNALS = ("rate_limit", "429", "too many", "quota", "resource_exhausted")
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 5  # seconds; doubles each attempt


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
                # Exponential backoff + random jitter so concurrent threads
                # don't all retry at the same instant (thundering herd)
                wait = _RETRY_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 2)
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
# Per-module generation helper (thread-safe, no shared mutable state)
# ---------------------------------------------------------------------------

def _generate_module(
    path: str,
    graph: nx.DiGraph,
    history: RepoHistory,
    corpus: DocCorpus,
    provider: str,
    api_key: str,
    model: str,
    max_tokens: int,
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

    # _esc() is CRITICAL: docstrings, commit messages, and symbol names
    # regularly contain { } (dicts, generics, format strings, etc.) which
    # would cause KeyError/IndexError in str.format() without escaping.
    prompt = MODULE_PROMPT.format(
        path=_esc(path),
        language=_esc(lang),
        symbols=_esc(_symbol_summary(symbols)),
        imports=_esc(_imports_summary(imports)),
        history=_esc(_history_summary(fh)),
        docs=_esc(_doc_fragments_text(path, corpus)),
        dependants=_esc(", ".join(dependants[:10]) or "none"),
        dependencies=_esc(", ".join(dependencies[:10]) or "none"),
    )

    try:
        raw = _call_llm(prompt, provider, api_key, model, max_tokens)
        err_logged = None
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        raw = f"*Narrative generation failed: {err_msg}*"
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

    stem = Path(path).stem
    if stem == "__init__" and Path(path).parent != Path("."):
        mod_title = Path(path).parent.name.replace("_", " ").title() + " (init)"
    else:
        mod_title = stem.replace("_", " ").replace("-", " ").title()

    narrative = ModuleNarrative(
        path=path,
        title=mod_title,
        summary=_extract_section(raw, "What this module does") or raw[:300],
        walkthrough=_extract_section(raw, "How it fits into the system"),
        design_notes=_extract_section(raw, "Key design decisions"),
        pitfalls=_extract_section(raw, "Pitfalls to avoid"),
        dead_code_warning=dead_warn,
        hotspot_warning=hotspot_warn,
        reading_order_index=0,  # set by caller after collection
    )
    return path, narrative, err_logged


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
    log(f"Generating narratives for {len(modules_to_process)} modules "
        f"({_LLM_MAX_CONCURRENT} concurrent)...")

    # ---- Concurrent module narrative generation ----------------------------
    # Each _generate_module call is independent (read-only graph/history/corpus).
    # A bounded thread pool caps concurrent API requests to avoid rate limits.
    # Results are collected in arrival order and re-sorted to reading order.

    raw_results: dict[str, ModuleNarrative] = {}
    done_count = 0

    with ThreadPoolExecutor(max_workers=_LLM_MAX_CONCURRENT) as executor:
        future_map = {
            executor.submit(
                _generate_module,
                path, graph, history, corpus,
                provider, api_key, model, max_tokens,
            ): path
            for path in modules_to_process
        }
        for future in as_completed(future_map):
            path, narrative, err_logged = future.result()
            done_count += 1
            log(f"  [{done_count}/{len(modules_to_process)}] {path}")
            if err_logged:
                log(f"    [warning] {err_logged}")
            raw_results[path] = narrative

    # Restore reading order and assign index
    for idx, path in enumerate(modules_to_process):
        if path in raw_results:
            raw_results[path].reading_order_index = idx
            guide.modules[path] = raw_results[path]

    # ---- System overview ---------------------------------------------------
    log("Generating system overview...")

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

    # _esc() applied to all user content — arch docs and commit themes can
    # contain { } characters that would crash OVERVIEW_PROMPT.format().
    overview_prompt = OVERVIEW_PROMPT.format(
        total_files=summary["total_files"],
        languages=_esc(str(summary["languages"])),
        entry_points=_esc(", ".join(summary["entry_points"][:10]) or "none detected"),
        hotspots=_esc("\n".join(hotspot_lines) or "  (no git history)"),
        themes=_esc(", ".join(history.major_themes[:10]) or "none detected"),
        arch_docs=_esc(arch_text),
        reading_order=_esc("\n".join(f"  {p}" for p in modules_to_process[:20])),
    )

    try:
        guide.system_overview = _call_llm(
            overview_prompt, provider, api_key, model, max_tokens
        )
    except Exception as e:
        err_msg = str(e).replace(api_key, "***") if api_key else str(e)
        guide.system_overview = f"*Overview generation failed: {err_msg}*"
        log(f"  [warning] overview: {err_msg}")

    # ---- Guided tour -------------------------------------------------------
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

        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", path)
        lines.append(f"## Step {idx + 1}: [{mod.title}](modules/{slug}.md)\n")

        if mod.summary:
            # First paragraph of summary only
            first_para = mod.summary.split("\n\n")[0].strip()
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
