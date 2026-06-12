"""MkDocs site builder.

Generates:
  <output_dir>/
    mkdocs.yml
    docs/
      index.md          ← system overview + Mermaid dependency graph
      guided_tour.md    ← step-by-step narrative
      modules/
        <slug>.md       ← per-module walkthrough
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import networkx as nx
import yaml

from onboard.stages.narrative_gen import OnboardingGuide, ModuleNarrative


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(path: str) -> str:
    """Convert a file path to a safe filename slug."""
    return re.sub(r"[^\w\-]", "_", path)


def _mermaid_graph(graph: nx.DiGraph, max_nodes: int = 60) -> str:
    """Render the dependency graph as a Mermaid flowchart."""
    lines = ["```mermaid", "graph TD"]

    # Use the most-connected nodes if the graph is large
    if graph.number_of_nodes() > max_nodes:
        top = sorted(graph.nodes(), key=lambda n: graph.degree(n), reverse=True)[:max_nodes]
        sub = graph.subgraph(top)
    else:
        sub = graph

    for node, data in sub.nodes(data=True):
        label = data.get("label", Path(node).stem)
        is_entry = data.get("is_entry_point", False)
        slug = _slugify(node)
        if is_entry:
            lines.append(f'    {slug}["{label} 🚪"]:::entry')
        else:
            lines.append(f'    {slug}["{label}"]')

    for src, dst in sub.edges():
        lines.append(f"    {_slugify(src)} --> {_slugify(dst)}")

    lines.append("    classDef entry fill:#f9a,stroke:#c55,stroke-width:2px;")
    lines.append("```")
    return "\n".join(lines)


def _hotspot_table(guide: OnboardingGuide, top_n: int = 10) -> str:
    """Render a markdown table of hotspots."""
    rows = sorted(
        guide.modules.values(),
        key=lambda m: guide.reading_order.index(m.path)
        if m.path in guide.reading_order else 999,
    )
    # filter for hotspot warnings
    hotspots = [m for m in rows if m.hotspot_warning][:top_n]
    if not hotspots:
        return ""

    lines = [
        "## 🔥 Hotspots",
        "",
        "| File | Warning |",
        "|------|---------|",
    ]
    for m in hotspots:
        warn = (m.hotspot_warning or "").replace("|", "\\|")
        lines.append(f"| `{m.path}` | {warn} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------

def _build_index(guide: OnboardingGuide, graph: nx.DiGraph) -> str:
    mermaid = _mermaid_graph(graph)
    hotspots = _hotspot_table(guide)

    themes = ""
    if guide.major_themes:
        themes = "**Recurring themes in git history:** " + ", ".join(
            f"`{t}`" for t in guide.major_themes[:8]
        )

    reading_list = "\n".join(
        f"{i+1}. [`{p}`](modules/{_slugify(p)}.md)"
        for i, p in enumerate(guide.reading_order[:20])
    )

    return f"""\
# System Overview

{guide.system_overview}

---

{themes}

## Dependency Graph

{mermaid}

---

## Suggested Reading Order

{reading_list}

---

{hotspots}
"""


def _build_module_page(mod: ModuleNarrative) -> str:
    sections = [f"# {mod.title}\n\n**File:** `{mod.path}`\n"]

    if mod.dead_code_warning:
        sections.append(f"\n{mod.dead_code_warning}\n")
    if mod.hotspot_warning:
        sections.append(f"\n{mod.hotspot_warning}\n")

    sections.append(f"\n## What this module does\n\n{mod.summary}\n")

    if mod.walkthrough:
        sections.append(f"\n## How it fits into the system\n\n{mod.walkthrough}\n")
    if mod.design_notes:
        sections.append(f"\n## Key design decisions\n\n{mod.design_notes}\n")
    if mod.pitfalls:
        sections.append(f"\n## Pitfalls to avoid\n\n{mod.pitfalls}\n")

    return "".join(sections)


def _build_mkdocs_yml(
    site_name: str,
    output_dir: Path,
    module_paths: list[str],
) -> str:
    nav_modules = [
        {Path(p).stem.replace("_", " ").title(): f"modules/{_slugify(p)}.md"}
        for p in module_paths
    ]

    config = {
        "site_name": site_name,
        "docs_dir": "docs",
        "site_dir": "site",
        "theme": {
            "name": "material",
            "palette": {
                "scheme": "default",
                "primary": "indigo",
                "accent": "blue",
            },
            "features": [
                "navigation.tabs",
                "navigation.sections",
                "search.highlight",
                "content.code.copy",
            ],
        },
        "markdown_extensions": [
            "pymdownx.superfences",
            {"pymdownx.superfences": {
                "custom_fences": [{"name": "mermaid", "class": "mermaid",
                                   "format": "!!python/name:pymdownx.superfences.fence_code_format"}]
            }},
            "pymdownx.highlight",
            "pymdownx.tabbed",
            "admonition",
            "tables",
        ],
        "nav": [
            {"Home": "index.md"},
            {"Guided Tour": "guided_tour.md"},
            {"Modules": nav_modules},
        ],
    }

    return yaml.dump(config, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_site(
    guide: OnboardingGuide,
    graph: nx.DiGraph,
    output_dir: Path,
    site_name: str = "Codebase Onboarding",
) -> None:
    """Write the full MkDocs site to *output_dir*."""
    docs_dir = output_dir / "docs"
    modules_dir = docs_dir / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)

    # index.md
    (docs_dir / "index.md").write_text(_build_index(guide, graph), encoding="utf-8")

    # guided_tour.md
    (docs_dir / "guided_tour.md").write_text(guide.guided_tour, encoding="utf-8")

    # per-module pages
    for path, mod in guide.modules.items():
        slug = _slugify(path)
        page = _build_module_page(mod)
        (modules_dir / f"{slug}.md").write_text(page, encoding="utf-8")

    # mkdocs.yml
    yml = _build_mkdocs_yml(site_name, output_dir, list(guide.modules.keys()))
    (output_dir / "mkdocs.yml").write_text(yml, encoding="utf-8")

    print(f"✅ MkDocs site written to {output_dir}")
    print(f"   Run: cd {output_dir} && mkdocs serve")
