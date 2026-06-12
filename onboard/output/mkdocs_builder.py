"""MkDocs site builder.

Generates:
  <output_dir>/
    mkdocs.yml
    docs/
      index.md          - system overview + Mermaid dependency graph
      guided_tour.md    - step-by-step narrative
      modules/
        <slug>.md       - per-module walkthrough
"""
from __future__ import annotations
import json, re
from collections import Counter
from pathlib import Path
import networkx as nx
import yaml
from onboard.stages.narrative_gen import OnboardingGuide, ModuleNarrative


def _slugify(path: str) -> str:
    return re.sub(r"[^\w\-]", "_", path)


def _mermaid_graph(graph: nx.DiGraph, max_nodes: int = 30) -> str:
    lines = [
        "```mermaid",
        '%%{init: {"flowchart": {"rankSpacing": 60, "nodeSpacing": 40}}}%%',
        "graph LR",
    ]
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
            lines.append(f'    {slug}["{label} (entry)"]:::entry')
        else:
            lines.append(f'    {slug}["{label}"]')
    for src, dst in sub.edges():
        lines.append(f"    {_slugify(src)} --> {_slugify(dst)}")
    lines.append("    classDef entry fill:#f9a,stroke:#c55,stroke-width:2px;")
    for node in sub.nodes():
        slug = _slugify(node)
        lines.append(f'    click {slug} "modules/{slug}/"')
    lines.append("```")
    return "\n".join(lines)


def _hotspot_table(guide: OnboardingGuide, top_n: int = 10) -> str:
    order_index = {p: i for i, p in enumerate(guide.reading_order)}
    rows = sorted(guide.modules.values(), key=lambda m: order_index.get(m.path, 999))
    hotspots = [m for m in rows if m.hotspot_warning][:top_n]
    if not hotspots:
        return ""
    lines = ["## Hotspots", "", "| File | Warning |", "|------|---------|"]
    for m in hotspots:
        warn = (m.hotspot_warning or "").replace("|", "\\|")
        lines.append(f"| `{m.path}` | {warn} |")
    return "\n".join(lines)


def _build_index(guide: OnboardingGuide, graph: nx.DiGraph) -> str:
    mermaid = _mermaid_graph(graph)
    hotspots = _hotspot_table(guide)
    themes = ""
    if guide.major_themes:
        themes = "**Recurring themes in git history:** " + ", ".join(
            f"`{t}`" for t in guide.major_themes[:8]
        )
    reading_list = "\n".join(
        f"{i+1}. [`{p}`](modules/{_slugify(p)}/)"
        for i, p in enumerate(guide.reading_order[:20])
    )
    return (
        "# System Overview\n\n"
        + guide.system_overview + "\n\n---\n\n"
        + themes + "\n\n## Dependency Graph\n\n"
        + "[Open interactive graph](graph.html){ .md-button }\n\n"
        + mermaid + "\n\n---\n\n## Suggested Reading Order\n\n"
        + reading_list + "\n\n---\n\n"
        + hotspots + "\n"
    )


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
    nodes_js = []
    for node, data in graph.nodes(data=True):
        label = data.get("label", Path(node).stem)
        is_entry = data.get("is_entry_point", False)
        slug = _slugify(node)
        color = ('{"background":"#f9a","border":"#c55"}' if is_entry
                 else '{"background":"#dce8f7","border":"#5a8fc2"}')
        nodes_js.append(
            "{" + f"id:{json.dumps(node)}, label:{json.dumps(label)}, "
            f"color:{color}, url:\"modules/{slug}/\"" + "}"
        )
    edges_js = []
    for src, dst in graph.edges():
        edges_js.append("{" + f"from:{json.dumps(src)}, to:{json.dumps(dst)}, arrows:\"to\"" + "}")

    nodes_str = ",\n    ".join(nodes_js)
    edges_str = ",\n    ".join(edges_js)
    nc = graph.number_of_nodes()
    ec = graph.number_of_edges()

    # Build HTML using string concatenation to avoid f-string brace escaping issues
    html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n<title>Dependency Graph</title>\n'
        '<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>\n'
        '<link rel="stylesheet" href="https://unpkg.com/vis-network@9.1.9/styles/vis-network.min.css">\n'
        '<style>\n'
        '  *,*::before,*::after{box-sizing:border-box}\n'
        '  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8f9fa;color:#212529}\n'
        '  #toolbar{position:fixed;top:0;left:0;right:0;z-index:100;height:56px;background:#3f51b5;color:#fff;\n'
        '    display:flex;align-items:center;gap:16px;padding:0 20px;box-shadow:0 2px 6px rgba(0,0,0,.25)}\n'
        '  #toolbar h1{margin:0;font-size:1rem;font-weight:600;flex:none}\n'
        '  #stats{font-size:0.78rem;opacity:.85}\n'
        '  #searchBox{margin-left:auto;padding:5px 12px;border-radius:20px;border:none;outline:none;\n'
        '    font-size:0.85rem;background:rgba(255,255,255,.15);color:#fff;width:180px;transition:background .2s,width .2s}\n'
        '  #searchBox::placeholder{color:rgba(255,255,255,.6)}\n'
        '  #searchBox:focus{background:rgba(255,255,255,.25);width:240px}\n'
        '  #controls{position:fixed;top:56px;left:0;right:0;z-index:99;height:44px;background:#fff;\n'
        '    border-bottom:1px solid #e0e0e0;display:flex;align-items:center;gap:8px;padding:0 20px;\n'
        '    box-shadow:0 1px 3px rgba(0,0,0,.06)}\n'
        '  .ctrl-select{font-size:0.8rem;border:1px solid #ccc;border-radius:6px;padding:3px 8px;\n'
        '    background:#fafafa;color:#333;cursor:pointer}\n'
        '  .ctrl-btn{font-size:0.78rem;padding:4px 12px;border-radius:6px;border:1px solid #ccc;\n'
        '    background:#fafafa;color:#333;cursor:pointer;transition:background .15s}\n'
        '  .ctrl-btn:hover{background:#e8eaf6;border-color:#3f51b5;color:#3f51b5}\n'
        '  .ctrl-sep{width:1px;height:20px;background:#e0e0e0;margin:0 4px}\n'
        '  .hint{margin-left:auto;font-size:0.75rem;color:#9e9e9e}\n'
        '  .legend{display:flex;align-items:center;gap:10px;margin-left:8px}\n'
        '  .legend-dot{width:12px;height:12px;border-radius:3px;border:2px solid}\n'
        '  #graph{position:fixed;top:100px;left:0;right:0;bottom:0}\n'
        '  #tooltip{position:fixed;background:rgba(30,30,30,.92);color:#fff;padding:5px 12px;\n'
        '    border-radius:6px;font-size:0.78rem;pointer-events:none;display:none;z-index:999;\n'
        '    box-shadow:0 2px 8px rgba(0,0,0,.3);max-width:320px;white-space:nowrap;\n'
        '    overflow:hidden;text-overflow:ellipsis}\n'
        '  #overlay{position:fixed;top:100px;left:0;right:0;bottom:0;background:rgba(248,249,250,.8);\n'
        '    display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:50;transition:opacity .4s}\n'
        '  #overlay.hidden{opacity:0;pointer-events:none}\n'
        '  .spinner{width:36px;height:36px;border:3px solid #e8eaf6;border-top-color:#3f51b5;\n'
        '    border-radius:50%;animation:spin .7s linear infinite;margin-bottom:12px}\n'
        '  @keyframes spin{to{transform:rotate(360deg)}}\n'
        '  #overlay p{margin:0;font-size:0.85rem;color:#555}\n'
        '</style>\n</head>\n<body>\n'
        '<div id="toolbar">\n'
        '  <h1>Dependency Graph</h1>\n'
        '  <span id="stats">' + str(nc) + ' modules &nbsp;&middot;&nbsp; ' + str(ec) + ' edges</span>\n'
        '  <div class="legend">\n'
        '    <div class="legend-dot" style="background:#f9a;border-color:#c55;"></div>\n'
        '    <span style="font-size:0.75rem;opacity:.9">Entry point</span>\n'
        '    <div class="legend-dot" style="background:#dce8f7;border-color:#5a8fc2;"></div>\n'
        '    <span style="font-size:0.75rem;opacity:.9">Module</span>\n'
        '  </div>\n'
        '  <input id="searchBox" type="search" placeholder="Search modules..." autocomplete="off">\n'
        '</div>\n'
        '<div id="controls">\n'
        '  <select id="layoutSelect" class="ctrl-select">\n'
        '    <option value="physics">Force-directed</option>\n'
        '    <option value="hierarchical">Hierarchical (top-down)</option>\n'
        '  </select>\n'
        '  <div class="ctrl-sep"></div>\n'
        '  <button class="ctrl-btn" id="fitBtn">Fit to screen</button>\n'
        '  <div class="ctrl-sep" id="freezeSep"></div>\n'
        '  <button class="ctrl-btn" id="freezeBtn">Freeze</button>\n'
        '  <span class="hint">Click node to open page &middot; Scroll to zoom &middot; Drag to pan</span>\n'
        '</div>\n'
        '<div id="graph"></div>\n'
        '<div id="tooltip"></div>\n'
        '<div id="overlay"><div class="spinner"></div><p>Laying out graph...</p></div>\n'
        '<script>\n'
        'var allNodes = [\n    ' + nodes_str + '\n];\n'
        'var allEdges = [\n    ' + edges_str + '\n];\n'
        'var nodes = new vis.DataSet(allNodes);\n'
        'var edges = new vis.DataSet(allEdges);\n'
        'var container = document.getElementById("graph");\n'
        'var network = null, frozen = false, activeNodes = nodes;\n'
        'var BASE_NODE_OPTS = {shape:"box",font:{size:13},margin:{top:8,bottom:8,left:12,right:12},\n'
        '  widthConstraint:{maximum:180},shadow:{enabled:true,color:"rgba(0,0,0,.08)",size:6,x:0,y:2},borderWidth:1.5};\n'
        'var BASE_EDGE_OPTS = {smooth:{type:"cubicBezier",roundness:0.4},\n'
        '  color:{color:"#b0bec5",highlight:"#3f51b5",hover:"#5c6bc0"},\n'
        '  width:1.2,selectionWidth:2.5,arrows:{to:{enabled:true,scaleFactor:0.7}}};\n'
        '\n'
        'function computeLevels() {\n'
        '  var adj = {};\n'
        '  allEdges.forEach(function(e) { if (!adj[e.from]) adj[e.from] = []; adj[e.from].push(e.to); });\n'
        '  var hasIncoming = {};\n'
        '  allEdges.forEach(function(e) { hasIncoming[e.to] = true; });\n'
        '  var roots = allNodes.filter(function(n) { return n.color && n.color.border === "#c55"; }).map(function(n) { return n.id; });\n'
        '  if (!roots.length) roots = allNodes.filter(function(n) { return !hasIncoming[n.id]; }).map(function(n) { return n.id; });\n'
        '  if (!roots.length && allNodes.length) roots = [allNodes[0].id];\n'
        '  var levels = {}, visited = {};\n'
        '  roots.forEach(function(id) { levels[id] = 0; });\n'
        '  var queue = roots.slice();\n'
        '  while (queue.length) {\n'
        '    var cur = queue.shift();\n'
        '    if (visited[cur]) continue;\n'
        '    visited[cur] = true;\n'
        '    (adj[cur] || []).forEach(function(nb) {\n'
        '      if (!visited[nb]) {\n'
        '        var proposed = (levels[cur] || 0) + 1;\n'
        '        if (levels[nb] === undefined || levels[nb] < proposed) levels[nb] = proposed;\n'
        '        queue.push(nb);\n'
        '      }\n'
        '    });\n'
        '  }\n'
        '  allNodes.forEach(function(n) { if (levels[n.id] === undefined) levels[n.id] = 0; });\n'
        '  return levels;\n'
        '}\n'
        '\n'
        'function computeHierarchicalPositions() {\n'
        '  var levels = computeLevels(), byLevel = {};\n'
        '  allNodes.forEach(function(n) { var lv = levels[n.id]; if (!byLevel[lv]) byLevel[lv] = []; byLevel[lv].push(n.id); });\n'
        '  var LEVEL_SEP = 140, NODE_SEP = 200, positions = {};\n'
        '  Object.keys(byLevel).forEach(function(lv) {\n'
        '    var ids = byLevel[lv], totalW = (ids.length - 1) * NODE_SEP;\n'
        '    ids.forEach(function(id, i) { positions[id] = {x: i * NODE_SEP - totalW / 2, y: parseInt(lv) * LEVEL_SEP}; });\n'
        '  });\n'
        '  return positions;\n'
        '}\n'
        '\n'
        'function buildOptions(layout) {\n'
        '  var base = {nodes: BASE_NODE_OPTS, edges: BASE_EDGE_OPTS,\n'
        '    layout: {hierarchical: {enabled: false}}, interaction: {hover: true, tooltipDelay: 80}};\n'
        '  if (layout === "hierarchical") {\n'
        '    base.physics = {enabled: false};\n'
        '  } else {\n'
        '    base.physics = {enabled: true,\n'
        '      barnesHut: {gravitationalConstant: -8000, springLength: 120, springConstant: 0.04},\n'
        '      stabilization: {iterations: 300, updateInterval: 25}};\n'
        '  }\n'
        '  return base;\n'
        '}\n'
        '\n'
        'function hideOverlay() {\n'
        '  var overlay = document.getElementById("overlay");\n'
        '  overlay.classList.add("hidden");\n'
        '  setTimeout(function() { overlay.style.display = "none"; }, 450);\n'
        '}\n'
        '\n'
        'function initNetwork(layout) {\n'
        '  if (network) { network.destroy(); network = null; }\n'
        '  var overlay = document.getElementById("overlay");\n'
        '  overlay.style.display = "flex"; overlay.classList.remove("hidden");\n'
        '  var nodeData, edgeData;\n'
        '  if (layout === "hierarchical") {\n'
        '    var positions = computeHierarchicalPositions();\n'
        '    nodeData = new vis.DataSet(allNodes.map(function(n) {\n'
        '      var p = positions[n.id];\n'
        '      return Object.assign({}, n, {x: p.x, y: p.y, opacity: 1, borderWidth: 1.5});\n'
        '    }));\n'
        '    edgeData = new vis.DataSet(allEdges);\n'
        '  } else {\n'
        '    nodes.update(allNodes.map(function(n) { return {id: n.id, opacity: 1, borderWidth: 1.5}; }));\n'
        '    nodeData = nodes; edgeData = edges;\n'
        '  }\n'
        '  activeNodes = nodeData;\n'
        '  network = new vis.Network(container, {nodes: nodeData, edges: edgeData}, buildOptions(layout));\n'
        '  if (layout === "hierarchical") {\n'
        '    requestAnimationFrame(function() { network.fit(); hideOverlay(); });\n'
        '  }\n'
        '  network.on("stabilizationIterationsDone", function() {\n'
        '    network.fit({animation: {duration: 300, easingFunction: "easeInOutQuad"}});\n'
        '    hideOverlay();\n'
        '  });\n'
        '  network.on("click", function(params) {\n'
        '    if (params.nodes.length > 0) {\n'
        '      var nd = nodes.get(params.nodes[0]);\n'
        '      if (nd && nd.url) window.location.href = nd.url;\n'
        '    }\n'
        '  });\n'
        '  network.on("hoverNode", function(params) { tooltip.textContent = params.node; tooltip.style.display = "block"; });\n'
        '  network.on("blurNode", function() { tooltip.style.display = "none"; });\n'
        '}\n'
        '\n'
        'initNetwork("physics");\n'
        'var tooltip = document.getElementById("tooltip");\n'
        'document.addEventListener("mousemove", function(e) {\n'
        '  tooltip.style.left = (e.clientX + 16) + "px"; tooltip.style.top = (e.clientY - 8) + "px";\n'
        '});\n'
        'document.getElementById("layoutSelect").addEventListener("change", function() {\n'
        '  frozen = false;\n'
        '  var freezeBtn = document.getElementById("freezeBtn");\n'
        '  freezeBtn.textContent = "Freeze";\n'
        '  var isH = this.value === "hierarchical";\n'
        '  freezeBtn.style.display = isH ? "none" : "";\n'
        '  document.getElementById("freezeSep").style.display = isH ? "none" : "";\n'
        '  initNetwork(this.value);\n'
        '});\n'
        'document.getElementById("fitBtn").addEventListener("click", function() {\n'
        '  if (network) network.fit({animation: {duration: 400, easingFunction: "easeInOutQuad"}});\n'
        '});\n'
        'document.getElementById("freezeBtn").addEventListener("click", function() {\n'
        '  frozen = !frozen;\n'
        '  if (network) network.setOptions({physics: {enabled: !frozen}});\n'
        '  this.textContent = frozen ? "Unfreeze" : "Freeze";\n'
        '});\n'
        'document.getElementById("searchBox").addEventListener("input", function() {\n'
        '  var q = this.value.trim().toLowerCase();\n'
        '  var updates = allNodes.map(function(n) {\n'
        '    var match = q && n.label.toLowerCase().includes(q);\n'
        '    return {id: n.id, opacity: q ? (match ? 1 : 0.15) : 1, borderWidth: match ? 3 : 1.5};\n'
        '  });\n'
        '  activeNodes.update(updates);\n'
        '  if (q) {\n'
        '    var matched = allNodes.filter(function(n) { return n.label.toLowerCase().includes(q); });\n'
        '    if (matched.length && network) network.selectNodes(matched.map(function(n) { return n.id; }));\n'
        '  } else {\n'
        '    if (network) network.unselectAll();\n'
        '  }\n'
        '});\n'
        '</script>\n</body>\n</html>\n'
    )
    return html


def _build_mkdocs_yml(site_name: str, output_dir: Path, module_paths: list) -> str:
    def _nav_title(p: str) -> str:
        stem = Path(p).stem
        if stem == "__init__" and Path(p).parent != Path("."):
            return Path(p).parent.name.replace("_", " ").title() + " (init)"
        return stem.replace("_", " ").title()

    raw_titles = [_nav_title(p) for p in module_paths]
    title_counts = Counter(raw_titles)
    seen_used: dict = {}
    final_titles = []
    for t, p in zip(raw_titles, module_paths):
        if title_counts[t] > 1:
            pkg = Path(p).parent.name
            t = f"{t} ({pkg})" if pkg and pkg != "." else t
        if t in seen_used:
            seen_used[t] += 1
            t = f"{t} {seen_used[t]}"
        else:
            seen_used[t] = 0
        final_titles.append(t)

    nav_modules_list = [
        {title: f"modules/{_slugify(p)}.md"}
        for title, p in zip(final_titles, module_paths)
    ]
    nav_modules_yaml = yaml.dump(nav_modules_list, default_flow_style=False, allow_unicode=True)
    nav_modules_indented = "\n".join(
        "      " + line if line.strip() else ""
        for line in nav_modules_yaml.splitlines()
    )
    safe_name = site_name.replace("'", "''")
    return (
        f"site_name: '{safe_name}'\n"
        "docs_dir: docs\n"
        "site_dir: site\n\n"
        "theme:\n"
        "  name: material\n"
        "  palette:\n"
        "    scheme: default\n"
        "    primary: indigo\n"
        "    accent: blue\n"
        "  features:\n"
        "    - navigation.tabs\n"
        "    - navigation.sections\n"
        "    - search.highlight\n"
        "    - content.code.copy\n\n"
        "markdown_extensions:\n"
        "  - attr_list\n"
        "  - pymdownx.superfences:\n"
        "      custom_fences:\n"
        "        - name: mermaid\n"
        "          class: mermaid\n"
        "          format: !!python/name:pymdownx.superfences.fence_code_format\n"
        "  - pymdownx.highlight\n"
        "  - pymdownx.tabbed:\n"
        "      alternate_style: true\n"
        "  - admonition\n"
        "  - tables\n\n"
        "extra_css:\n"
        "  - css/extra.css\n\n"
        "nav:\n"
        "  - Home: index.md\n"
        "  - Guided Tour: guided_tour.md\n"
        "  - Modules:\n"
        + nav_modules_indented + "\n"
    )


def build_site(guide: OnboardingGuide, graph: nx.DiGraph,
               output_dir: Path, site_name: str = "Codebase Onboarding") -> None:
    """Write the full MkDocs site to *output_dir*."""
    docs_dir = output_dir / "docs"
    modules_dir = docs_dir / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)

    (docs_dir / "index.md").write_text(_build_index(guide, graph), encoding="utf-8")
    (docs_dir / "guided_tour.md").write_text(guide.guided_tour, encoding="utf-8")

    for path, mod in guide.modules.items():
        slug = _slugify(path)
        (modules_dir / f"{slug}.md").write_text(_build_module_page(mod), encoding="utf-8")

    css_dir = docs_dir / "css"
    css_dir.mkdir(exist_ok=True)
    (css_dir / "extra.css").write_text(
        "/* do NOT hide .mermaid - Material replaces the div, not inserts SVG. */\n"
        ".mermaid { min-height: 20px; }\n",
        encoding="utf-8",
    )

    (docs_dir / "graph.html").write_text(_build_interactive_graph(graph), encoding="utf-8")

    yml = _build_mkdocs_yml(site_name, output_dir, list(guide.modules.keys()))
    (output_dir / "mkdocs.yml").write_text(yml, encoding="utf-8")

    print(f"MkDocs site written to {output_dir}")
    print(f"   Run: cd {output_dir} && mkdocs serve")
