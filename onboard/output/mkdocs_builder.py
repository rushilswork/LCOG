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

import json
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


def _mermaid_graph(graph: nx.DiGraph, max_nodes: int = 30) -> str:
    """Render the dependency graph as a Mermaid flowchart."""
    lines = [
        "```mermaid",
        "%%{init: {\"flowchart\": {\"rankSpacing\": 60, \"nodeSpacing\": 40}}}%%",
        "graph LR",
    ]

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

    # Click directives: each node navigates to its module page
    # Use directory-style URLs (MkDocs serves pages as modules/<slug>/ not .md)
    for node in sub.nodes():
        slug = _slugify(node)
        lines.append(f'    click {slug} "modules/{slug}/"')

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

[Open interactive graph](graph.html){{ .md-button }}

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


def _build_interactive_graph(graph: nx.DiGraph) -> str:
    """Build a self-contained HTML page with a vis.js interactive dependency graph."""
    nodes_js = []
    for node, data in graph.nodes(data=True):
        label = data.get("label", Path(node).stem)
        is_entry = data.get("is_entry_point", False)
        slug = _slugify(node)
        color = '{"background":"#f9a","border":"#c55"}' if is_entry else '{"background":"#dce8f7","border":"#5a8fc2"}'
        nodes_js.append(
            f'{{id:{json.dumps(node)}, label:{json.dumps(label)}, '
            f'color:{color}, url:"modules/{slug}/"}}'
        )

    edges_js = []
    for src, dst in graph.edges():
        edges_js.append(f'{{from:{json.dumps(src)}, to:{json.dumps(dst)}, arrows:"to"}}')

    nodes_str = ",\n    ".join(nodes_js)
    edges_str = ",\n    ".join(edges_js)

    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Dependency Graph</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/vis-network@9.1.9/styles/vis-network.min.css">
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #f8f9fa; color: #212529; }}

  /* ── Top bar ── */
  #toolbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    height: 56px; background: #3f51b5; color: #fff;
    display: flex; align-items: center; gap: 16px; padding: 0 20px;
    box-shadow: 0 2px 6px rgba(0,0,0,.25);
  }}
  #toolbar h1 {{ margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: .3px; flex: none; }}
  #toolbar .divider {{ width: 1px; height: 24px; background: rgba(255,255,255,.3); }}
  #stats {{ font-size: 0.78rem; opacity: .85; }}

  /* search */
  #searchBox {{
    margin-left: auto; padding: 5px 12px; border-radius: 20px;
    border: none; outline: none; font-size: 0.85rem;
    background: rgba(255,255,255,.15); color: #fff; width: 180px;
    transition: background .2s, width .2s;
  }}
  #searchBox::placeholder {{ color: rgba(255,255,255,.6); }}
  #searchBox:focus {{ background: rgba(255,255,255,.25); width: 240px; }}

  /* controls panel */
  #controls {{
    position: fixed; top: 56px; left: 0; right: 0; z-index: 99;
    height: 44px; background: #fff; border-bottom: 1px solid #e0e0e0;
    display: flex; align-items: center; gap: 8px; padding: 0 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }}
  .ctrl-label {{ font-size: 0.8rem; color: #555; display: flex; align-items: center; gap: 6px; }}
  .ctrl-label input[type=checkbox] {{ accent-color: #3f51b5; width: 14px; height: 14px; }}
  .ctrl-select {{
    font-size: 0.8rem; border: 1px solid #ccc; border-radius: 6px;
    padding: 3px 8px; background: #fafafa; color: #333; cursor: pointer;
  }}
  .ctrl-btn {{
    font-size: 0.78rem; padding: 4px 12px; border-radius: 6px;
    border: 1px solid #ccc; background: #fafafa; color: #333;
    cursor: pointer; transition: background .15s;
  }}
  .ctrl-btn:hover {{ background: #e8eaf6; border-color: #3f51b5; color: #3f51b5; }}
  .ctrl-sep {{ width: 1px; height: 20px; background: #e0e0e0; margin: 0 4px; }}
  .hint {{ margin-left: auto; font-size: 0.75rem; color: #9e9e9e; }}

  /* legend */
  .legend {{ display: flex; align-items: center; gap: 10px; margin-left: 8px; }}
  .legend-dot {{ width: 12px; height: 12px; border-radius: 3px; border: 2px solid; }}

  /* graph canvas */
  #graph {{ position: fixed; top: 100px; left: 0; right: 0; bottom: 0; }}

  /* tooltip */
  #tooltip {{
    position: fixed; background: rgba(30,30,30,.92); color: #fff;
    padding: 5px 12px; border-radius: 6px; font-size: 0.78rem;
    pointer-events: none; display: none; z-index: 999;
    box-shadow: 0 2px 8px rgba(0,0,0,.3); max-width: 320px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}

  /* stabilising overlay */
  #overlay {{
    position: fixed; top: 100px; left: 0; right: 0; bottom: 0;
    background: rgba(248,249,250,.8); display: flex; flex-direction: column;
    align-items: center; justify-content: center; z-index: 50;
    transition: opacity .4s;
  }}
  #overlay.hidden {{ opacity: 0; pointer-events: none; }}
  .spinner {{
    width: 36px; height: 36px; border: 3px solid #e8eaf6;
    border-top-color: #3f51b5; border-radius: 50%;
    animation: spin .7s linear infinite; margin-bottom: 12px;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  #overlay p {{ margin: 0; font-size: 0.85rem; color: #555; }}
</style>
</head>
<body>

<div id="toolbar">
  <h1>Dependency Graph</h1>
  <div class="divider"></div>
  <span id="stats">{node_count} modules &nbsp;·&nbsp; {edge_count} edges</span>
  <div class="legend">
    <div class="legend-dot" style="background:#f9a;border-color:#c55;"></div>
    <span style="font-size:0.75rem;opacity:.9">Entry point</span>
    <div class="legend-dot" style="background:#dce8f7;border-color:#5a8fc2;"></div>
    <span style="font-size:0.75rem;opacity:.9">Module</span>
  </div>
  <input id="searchBox" type="search" placeholder="Search modules…" autocomplete="off">
</div>

<div id="controls">
  <select id="layoutSelect" class="ctrl-select">
    <option value="physics">Force-directed</option>
    <option value="hierarchical">Hierarchical (top-down)</option>
  </select>
  <div class="ctrl-sep"></div>
  <button class="ctrl-btn" id="fitBtn">Fit to screen</button>
  <div class="ctrl-sep" id="freezeSep"></div>
  <button class="ctrl-btn" id="freezeBtn">Freeze</button>
  <span class="hint">Click node to open page &nbsp;·&nbsp; Scroll to zoom &nbsp;·&nbsp; Drag to pan</span>
</div>

<div id="graph"></div>
<div id="tooltip"></div>

<div id="overlay">
  <div class="spinner"></div>
  <p>Laying out graph…</p>
</div>

<script>
var allNodes = [
    {nodes_str}
];
var allEdges = [
    {edges_str}
];
var nodes = new vis.DataSet(allNodes);
var edges = new vis.DataSet(allEdges);

var container = document.getElementById("graph");
var network = null;
var frozen = false;
var activeNodes = nodes; // points to whichever DataSet is currently in use

var BASE_NODE_OPTS = {{
  shape: "box",
  font: {{ size: 13, face: "-apple-system, sans-serif" }},
  margin: {{ top: 8, bottom: 8, left: 12, right: 12 }},
  widthConstraint: {{ maximum: 180 }},
  shadow: {{ enabled: true, color: "rgba(0,0,0,.08)", size: 6, x: 0, y: 2 }},
  borderWidth: 1.5,
}};
var BASE_EDGE_OPTS = {{
  smooth: {{ type: "cubicBezier", roundness: 0.4 }},
  color: {{ color: "#b0bec5", highlight: "#3f51b5", hover: "#5c6bc0" }},
  width: 1.2, selectionWidth: 2.5,
  arrows: {{ to: {{ enabled: true, scaleFactor: 0.7 }} }},
}};

// BFS level assignment — cycle-safe (never re-queues a visited node)
function computeLevels() {{
  var adj = {{}};
  allEdges.forEach(function(e) {{
    if (!adj[e.from]) adj[e.from] = [];
    adj[e.from].push(e.to);
  }});

  var hasIncoming = {{}};
  allEdges.forEach(function(e) {{ hasIncoming[e.to] = true; }});
  var roots = allNodes.filter(function(n) {{ return n.color && n.color.border === "#c55"; }})
                      .map(function(n) {{ return n.id; }});
  if (!roots.length) roots = allNodes.filter(function(n) {{ return !hasIncoming[n.id]; }}).map(function(n) {{ return n.id; }});
  if (!roots.length && allNodes.length) roots = [allNodes[0].id];

  var levels = {{}};
  var visited = {{}};
  roots.forEach(function(id) {{ levels[id] = 0; }});
  var queue = roots.slice();

  while (queue.length) {{
    var cur = queue.shift();
    if (visited[cur]) continue;   // cycle guard — never process a node twice
    visited[cur] = true;
    (adj[cur] || []).forEach(function(nb) {{
      if (!visited[nb]) {{
        var proposed = (levels[cur] || 0) + 1;
        if (levels[nb] === undefined || levels[nb] < proposed) {{
          levels[nb] = proposed;
        }}
        queue.push(nb);
      }}
    }});
  }}

  allNodes.forEach(function(n) {{ if (levels[n.id] === undefined) levels[n.id] = 0; }});
  return levels;
}}

function computeHierarchicalPositions() {{
  var levels = computeLevels();
  var byLevel = {{}};
  allNodes.forEach(function(n) {{
    var lv = levels[n.id];
    if (!byLevel[lv]) byLevel[lv] = [];
    byLevel[lv].push(n.id);
  }});

  var LEVEL_SEP = 140, NODE_SEP = 200;
  var positions = {{}};
  Object.keys(byLevel).forEach(function(lv) {{
    var ids = byLevel[lv];
    var totalW = (ids.length - 1) * NODE_SEP;
    ids.forEach(function(id, i) {{
      positions[id] = {{ x: i * NODE_SEP - totalW / 2, y: parseInt(lv) * LEVEL_SEP }};
    }});
  }});
  return positions;
}}

function buildOptions(layout) {{
  var base = {{
    nodes: BASE_NODE_OPTS, edges: BASE_EDGE_OPTS,
    layout: {{ hierarchical: {{ enabled: false }} }},
    interaction: {{ hover: true, tooltipDelay: 80 }},
  }};
  if (layout === "hierarchical") {{
    base.physics = {{ enabled: false }};
  }} else {{
    base.physics = {{
      enabled: true,
      barnesHut: {{ gravitationalConstant: -8000, springLength: 120, springConstant: 0.04 }},
      stabilization: {{ iterations: 300, updateInterval: 25 }},
    }};
  }}
  return base;
}}

function hideOverlay() {{
  var overlay = document.getElementById("overlay");
  overlay.classList.add("hidden");
  setTimeout(function() {{ overlay.style.display = "none"; }}, 450);
}}

function initNetwork(layout) {{
  if (network) {{ network.destroy(); network = null; }}

  var overlay = document.getElementById("overlay");
  overlay.style.display = "flex"; overlay.classList.remove("hidden");

  var nodeData, edgeData;
  if (layout === "hierarchical") {{
    // Bake BFS positions into a fresh DataSet — vis.js hierarchical breaks on cyclic graphs
    var positions = computeHierarchicalPositions();
    nodeData = new vis.DataSet(allNodes.map(function(n) {{
      var p = positions[n.id];
      return Object.assign({{}}, n, {{ x: p.x, y: p.y, opacity: 1, borderWidth: 1.5 }});
    }}));
    edgeData = new vis.DataSet(allEdges);
  }} else {{
    // Reset any style overrides from search
    nodes.update(allNodes.map(function(n) {{ return {{ id: n.id, opacity: 1, borderWidth: 1.5 }}; }}));
    nodeData = nodes;
    edgeData = edges;
  }}
  activeNodes = nodeData;

  network = new vis.Network(container, {{ nodes: nodeData, edges: edgeData }}, buildOptions(layout));

  if (layout === "hierarchical") {{
    // Positions already set — just fit and hide overlay
    requestAnimationFrame(function() {{
      network.fit();
      hideOverlay();
    }});
  }}

  network.on("stabilizationIterationsDone", function() {{
    network.fit({{ animation: {{ duration: 300, easingFunction: "easeInOutQuad" }} }});
    hideOverlay();
  }});

  network.on("click", function(params) {{
    if (params.nodes.length > 0) {{
      var nd = nodes.get(params.nodes[0]);
      if (nd && nd.url) window.location.href = nd.url;
    }}
  }});

  network.on("hoverNode", function(params) {{
    tooltip.textContent = params.node;
    tooltip.style.display = "block";
  }});
  network.on("blurNode", function() {{ tooltip.style.display = "none"; }});
}}

initNetwork("physics");

var tooltip = document.getElementById("tooltip");
document.addEventListener("mousemove", function(e) {{
  tooltip.style.left = (e.clientX + 16) + "px";
  tooltip.style.top = (e.clientY - 8) + "px";
}});

document.getElementById("layoutSelect").addEventListener("change", function() {{
  frozen = false;
  var freezeBtn = document.getElementById("freezeBtn");
  freezeBtn.textContent = "Freeze";
  var isHierarchical = this.value === "hierarchical";
  freezeBtn.style.display = isHierarchical ? "none" : "";
  document.getElementById("freezeSep").style.display = isHierarchical ? "none" : "";
  initNetwork(this.value);
}});

document.getElementById("fitBtn").addEventListener("click", function() {{
  if (network) network.fit({{ animation: {{ duration: 400, easingFunction: "easeInOutQuad" }} }});
}});

document.getElementById("freezeBtn").addEventListener("click", function() {{
  frozen = !frozen;
  if (network) network.setOptions({{ physics: {{ enabled: !frozen }} }});
  this.textContent = frozen ? "Unfreeze" : "Freeze";
}});

document.getElementById("searchBox").addEventListener("input", function() {{
  var q = this.value.trim().toLowerCase();
  var updates = allNodes.map(function(n) {{
    var match = q && n.label.toLowerCase().includes(q);
    return {{ id: n.id, opacity: q ? (match ? 1 : 0.15) : 1, borderWidth: match ? 3 : 1.5 }};
  }});
  activeNodes.update(updates);
  if (q) {{
    var matched = allNodes.filter(function(n) {{ return n.label.toLowerCase().includes(q); }});
    if (matched.length && network) network.selectNodes(matched.map(function(n) {{ return n.id; }}));
  }} else {{
    if (network) network.unselectAll();
  }}
}});
</script>
</body>
</html>
"""


def _build_mkdocs_yml(
    site_name: str,
    output_dir: Path,
    module_paths: list[str],
) -> str:
    # Build the nav modules section via yaml for safe quoting of titles/paths
    def _nav_title(p: str) -> str:
        stem = Path(p).stem
        # For __init__ or other collisions, prefix with parent package name
        if stem == "__init__" and Path(p).parent != Path("."):
            return Path(p).parent.name.replace("_", " ").title() + " (init)"
        return stem.replace("_", " ").title()

    # Disambiguate any remaining duplicate titles
    raw_titles = [_nav_title(p) for p in module_paths]
    seen: dict[str, int] = {}
    final_titles = []
    for t, p in zip(raw_titles, module_paths):
        if raw_titles.count(t) > 1:
            pkg = Path(p).parent.name
            t = f"{t} ({pkg})" if pkg and pkg != "." else t
        final_titles.append(t)

    nav_modules_list = [
        {title: f"modules/{_slugify(p)}.md"}
        for title, p in zip(final_titles, module_paths)
    ]
    nav_modules_yaml = yaml.dump(nav_modules_list, default_flow_style=False, allow_unicode=True)
    # indent each line by 6 spaces (under "    - Modules:")
    nav_modules_indented = "\n".join("      " + line if line.strip() else ""
                                      for line in nav_modules_yaml.splitlines())

    # Write mkdocs.yml as a literal string so !!python/name: is a real YAML tag,
    # not a quoted string (yaml.dump would quote it and break Mermaid rendering).
    safe_name = site_name.replace("'", "''")
    return f"""\
site_name: '{safe_name}'
docs_dir: docs
site_dir: site

theme:
  name: material
  palette:
    scheme: default
    primary: indigo
    accent: blue
  features:
    - navigation.tabs
    - navigation.sections
    - search.highlight
    - content.code.copy

markdown_extensions:
  - attr_list
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
  - pymdownx.highlight
  - pymdownx.tabbed:
      alternate_style: true
  - admonition
  - tables

extra_css:
  - css/extra.css

nav:
  - Home: index.md
  - Guided Tour: guided_tour.md
  - Modules:
{nav_modules_indented}
"""


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

    # CSS tweaks
    css_dir = docs_dir / "css"
    css_dir.mkdir(exist_ok=True)
    (css_dir / "extra.css").write_text(
        "/* Brief flash while Mermaid renders is acceptable;\n"
        "   do NOT hide .mermaid — Material replaces the div, not inserts SVG. */\n"
        ".mermaid { min-height: 20px; }\n",
        encoding="utf-8",
    )

    # interactive graph (standalone HTML, served as a static asset)
    graph_html = _build_interactive_graph(graph)
    (docs_dir / "graph.html").write_text(graph_html, encoding="utf-8")

    # mkdocs.yml
    yml = _build_mkdocs_yml(site_name, output_dir, list(guide.modules.keys()))
    (output_dir / "mkdocs.yml").write_text(yml, encoding="utf-8")

    print(f"MkDocs site written to {output_dir}")
    print(f"   Run: cd {output_dir} && mkdocs serve")
