"""MkDocs site builder.

Generates:
  <output_dir>/
    mkdocs.yml
    docs/
      index.md           -- system overview + Mermaid dependency graph
      guided_tour.md     -- step-by-step narrative
      file_tree.md       -- full source file tree with hotspot/dead-code badges
      graph.html         -- interactive vis.js dependency graph (colour-coded by dir)
      dirs/
        <dir_slug>.md    -- per-directory overview page
      modules/
        <slug>.md        -- per-module walkthrough
      css/
        extra.css
"""
from __future__ import annotations
import html as _html
import json, re
from collections import Counter, defaultdict
from pathlib import Path
import networkx as nx
import yaml
from onboard.stages.narrative_gen import OnboardingGuide, ModuleNarrative


def _slugify(path: str) -> str:
    return re.sub(r"[^\w\-]", "_", path)


# ── Directory colour palette ──────────────────────────────────────────────────

_DIR_PALETTE: list[tuple[str, str]] = [
    ("#dce8f7", "#5a8fc2"),  # blue
    ("#d4f7dc", "#4caf6e"),  # green
    ("#f7f0d4", "#c2a24a"),  # amber
    ("#f7d4d4", "#c24a4a"),  # red
    ("#e8d4f7", "#8a4ac2"),  # purple
    ("#d4f7f4", "#4ac2bb"),  # teal
    ("#f7e8d4", "#c27a4a"),  # orange
    ("#f4d4f7", "#c24aad"),  # pink
    ("#d4d4f7", "#4a4ac2"),  # navy
    ("#d4f7e8", "#4ac278"),  # mint
]


def _dir_color_map(graph: nx.DiGraph) -> dict[str, tuple[str, str]]:
    """Map each top-level directory to a (bg_color, border_color) pair."""
    dirs = sorted({
        Path(n).parts[0] if len(Path(n).parts) > 1 else "_root_"
        for n in graph.nodes()
    })
    return {d: _DIR_PALETTE[i % len(_DIR_PALETTE)] for i, d in enumerate(dirs)}


def _node_label(node: str) -> str:
    """Label as 'parent_dir/stem' so duplicates across packages are distinguishable."""
    p = Path(node)
    if str(p.parent) == "." or p.parent.name == "":
        return p.stem
    return f"{p.parent.name}/{p.stem}"


# ── Mermaid static graph ──────────────────────────────────────────────────────

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
        label = _node_label(node).replace('"', "'")
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


# ── Hotspot table ─────────────────────────────────────────────────────────────

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


# ── Index page ────────────────────────────────────────────────────────────────

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
    return (
        "# System Overview\n\n"
        + guide.system_overview + "\n\n---\n\n"
        + themes + "\n\n## Dependency Graph\n\n"
        + "[Open interactive graph](graph.html){ .md-button }\n\n"
        + "[Browse file tree](file_tree.md){ .md-button }\n\n"
        + mermaid + "\n\n---\n\n## Suggested Reading Order\n\n"
        + reading_list + "\n\n---\n\n"
        + hotspots + "\n"
    )


# ── Module page ───────────────────────────────────────────────────────────────

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


# ── File tree page ────────────────────────────────────────────────────────────

def _build_file_tree_page(guide: OnboardingGuide, graph: nx.DiGraph) -> str:
    """One page showing every parsed source file grouped by top-level directory."""
    groups: dict[str, list[str]] = defaultdict(list)
    for node in sorted(graph.nodes()):
        top = Path(node).parts[0] if len(Path(node).parts) > 1 else "_root_"
        groups[top].append(node)

    narrated = set(guide.modules.keys())
    hotspots = {p for p, m in guide.modules.items() if m.hotspot_warning}
    dead = {p for p, m in guide.modules.items() if m.dead_code_warning}

    total = graph.number_of_nodes()
    lang_counts: dict[str, int] = {}
    for _, data in graph.nodes(data=True):
        lang = data.get("language", "unknown")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    lang_str = ", ".join(
        f"{v} {k}" for k, v in sorted(lang_counts.items(), key=lambda x: -x[1])
    )

    lines = [
        "# File Tree\n",
        f"*{total:,} source files — {lang_str}*\n",
        f"*{len(narrated)} narrated · {len(hotspots)} hotspots · {len(dead)} dead code candidates*\n",
        "\n---\n",
    ]

    for top_dir in sorted(groups.keys()):
        files = groups[top_dir]
        dir_narrated = [f for f in files if f in narrated]
        dir_other = [f for f in files if f not in narrated]
        n_hot = sum(1 for f in files if f in hotspots)
        n_dead = sum(1 for f in files if f in dead)

        badges = []
        if n_hot:
            badges.append(f"🔥 {n_hot}")
        if n_dead:
            badges.append(f"💀 {n_dead}")
        badge_str = "  " + " · ".join(badges) if badges else ""
        display = top_dir if top_dir != "_root_" else "(root)"
        lines.append(f"\n## {display}/\n")
        lines.append(f"*{len(files):,} files, {len(dir_narrated)} narrated{badge_str}*\n")

        if dir_narrated or dir_other:
            lines.append("\n| File | Language | Status |")
            lines.append("|------|----------|--------|")
            for f in dir_narrated:
                data = graph.nodes.get(f, {})
                lang = data.get("language", "")
                slug = _slugify(f)
                disp = f.replace("|", "\\|")
                flags = ("🔥" if f in hotspots else "") + ("💀" if f in dead else "")
                status = f"{flags} Narrated" if flags else "📄 Narrated"
                lines.append(f"| [`{disp}`](modules/{slug}.md) | {lang} | {status} |")
            shown = 0
            for f in dir_other:
                if shown >= 50:
                    rem = len(dir_other) - 50
                    lines.append(f"| *…and {rem:,} more* | | |")
                    break
                data = graph.nodes.get(f, {})
                lang = data.get("language", "")
                disp = f.replace("|", "\\|")
                lines.append(f"| `{disp}` | {lang} | |")
                shown += 1

    return "\n".join(lines) + "\n"


# ── Directory overview page ───────────────────────────────────────────────────

def _build_directory_page(dir_name: str, modules: list, graph: nx.DiGraph) -> str:
    """Overview page for one top-level directory."""
    all_files = [
        n for n in graph.nodes()
        if (Path(n).parts[0] if len(Path(n).parts) > 1 else "_root_") == dir_name
    ]
    hotspot_mods = [m for m in modules if m.hotspot_warning]
    dead_mods = [m for m in modules if m.dead_code_warning]
    display = dir_name if dir_name != "_root_" else "(root)"

    lines = [f"# {display}/\n"]
    stats = f"**{len(all_files):,} files** · **{len(modules)} narrated**"
    if hotspot_mods:
        s = "s" if len(hotspot_mods) > 1 else ""
        stats += f" · **{len(hotspot_mods)} hotspot{s} 🔥**"
    if dead_mods:
        stats += f" · **{len(dead_mods)} dead code 💀**"
    lines.append(stats + "\n")

    if modules:
        lines.append("\n## Modules\n")
        lines.append("| Module | Summary | Flags |")
        lines.append("|--------|---------|-------|")
        for m in sorted(modules, key=lambda x: x.path):
            slug = _slugify(m.path)
            raw = (m.summary or "")
            summary = raw[:120].replace("|", "\\|").replace("\n", " ")
            if len(raw) > 120:
                summary += "…"
            flags = ("🔥" if m.hotspot_warning else "") + ("💀" if m.dead_code_warning else "")
            title = m.title or Path(m.path).stem
            lines.append(f"| [{title}](../modules/{slug}.md) | {summary} | {flags} |")

    return "\n".join(lines) + "\n"


# ── Interactive vis.js graph ──────────────────────────────────────────────────

_MAX_INTERACTIVE_NODES = 200


def _build_interactive_graph(graph: nx.DiGraph) -> str:
    # Cap for performance on large repos
    if graph.number_of_nodes() > _MAX_INTERACTIVE_NODES:
        top = sorted(graph.nodes(), key=lambda n: graph.degree(n), reverse=True)[:_MAX_INTERACTIVE_NODES]
        sub = graph.subgraph(top)
    else:
        sub = graph

    color_map = _dir_color_map(sub)
    all_dirs = sorted(color_map.keys())

    nodes_js = []
    for node, data in sub.nodes(data=True):
        label = _node_label(node)
        is_entry = data.get("is_entry_point", False)
        slug = _slugify(node)
        top_dir = Path(node).parts[0] if len(Path(node).parts) > 1 else "_root_"
        if is_entry:
            color_str = '{"background":"#f9a","border":"#c55"}'
        else:
            bg, border = color_map.get(top_dir, ("#dce8f7", "#5a8fc2"))
            color_str = f'{{"background":"{bg}","border":"{border}"}}'
        nodes_js.append(
            "{" + f"id:{json.dumps(node)},label:{json.dumps(label)},"
            f"title:{json.dumps(node)},group:{json.dumps(top_dir)},"
            f"color:{color_str},url:\"modules/{slug}/\"" + "}"
        )

    edges_js = [
        "{" + f"from:{json.dumps(s)},to:{json.dumps(d)},arrows:\"to\"" + "}"
        for s, d in sub.edges()
    ]

    nc = sub.number_of_nodes()
    ec = sub.number_of_edges()
    total_nc = graph.number_of_nodes()
    trunc = f" (top {nc} of {total_nc} by connectivity)" if total_nc > nc else ""

    dir_options_html = '    <option value="all">All directories</option>\n'
    for d in all_dirs:
        esc = _html.escape(d)
        dir_options_html += f'    <option value="{esc}">{esc}</option>\n'

    dir_colors_js = "var dirColors = {\n"
    for d, (bg, border) in color_map.items():
        dir_colors_js += f'  {json.dumps(d)}: {{bg:{json.dumps(bg)},border:{json.dumps(border)}}},\n'
    dir_colors_js += "};\n"

    legend_html = ""
    for d in all_dirs[:10]:
        bg, border = color_map[d]
        legend_html += (
            f'<div style="display:flex;align-items:center;gap:4px;white-space:nowrap;">'
            f'<div style="width:10px;height:10px;border-radius:2px;background:{bg};'
            f'border:2px solid {border};flex-shrink:0;"></div>'
            f'<span style="font-size:0.7rem;opacity:.88;">{_html.escape(d)}</span></div>\n'
        )

    nodes_str = ",\n    ".join(nodes_js)
    edges_str = ",\n    ".join(edges_js)

    html = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n<title>Dependency Graph</title>\n'
        '<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>\n'
        '<link rel="stylesheet" href="https://unpkg.com/vis-network@9.1.9/styles/vis-network.min.css">\n'
        '<style>\n'
        '  *,*::before,*::after{box-sizing:border-box}\n'
        '  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8f9fa;color:#212529}\n'
        '  #toolbar{position:fixed;top:0;left:0;right:0;z-index:100;background:#3f51b5;color:#fff;padding:0 20px;box-shadow:0 2px 6px rgba(0,0,0,.25)}\n'
        '  #tb-top{height:48px;display:flex;align-items:center;gap:12px}\n'
        '  #toolbar h1{margin:0;font-size:0.92rem;font-weight:600;flex:none}\n'
        '  #stats{font-size:0.74rem;opacity:.85}\n'
        '  #searchBox{margin-left:auto;padding:4px 12px;border-radius:20px;border:none;outline:none;font-size:0.82rem;background:rgba(255,255,255,.15);color:#fff;width:160px;transition:background .2s,width .2s}\n'
        '  #searchBox::placeholder{color:rgba(255,255,255,.6)}\n'
        '  #searchBox:focus{background:rgba(255,255,255,.25);width:220px}\n'
        '  #tb-legend{height:34px;display:flex;align-items:center;gap:8px;overflow-x:auto;overflow-y:hidden;padding-bottom:4px}\n'
        '  #tb-legend::-webkit-scrollbar{height:3px}\n'
        '  #tb-legend::-webkit-scrollbar-thumb{background:rgba(255,255,255,.3)}\n'
        '  #controls{position:fixed;top:82px;left:0;right:0;z-index:99;height:44px;background:#fff;border-bottom:1px solid #e0e0e0;display:flex;align-items:center;gap:8px;padding:0 20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}\n'
        '  .ctrl-select{font-size:0.8rem;border:1px solid #ccc;border-radius:6px;padding:3px 8px;background:#fafafa;color:#333;cursor:pointer}\n'
        '  .ctrl-btn{font-size:0.78rem;padding:4px 12px;border-radius:6px;border:1px solid #ccc;background:#fafafa;color:#333;cursor:pointer;transition:background .15s}\n'
        '  .ctrl-btn:hover{background:#e8eaf6;border-color:#3f51b5;color:#3f51b5}\n'
        '  .ctrl-sep{width:1px;height:20px;background:#e0e0e0;margin:0 4px}\n'
        '  .ctrl-lbl{font-size:0.78rem;color:#666}\n'
        '  .hint{margin-left:auto;font-size:0.74rem;color:#9e9e9e}\n'
        '  #graph{position:fixed;top:126px;left:0;right:0;bottom:0}\n'
        '  #tooltip{position:fixed;background:rgba(20,20,20,.92);color:#fff;padding:6px 12px;border-radius:6px;font-size:0.78rem;pointer-events:none;display:none;z-index:999;box-shadow:0 2px 8px rgba(0,0,0,.3);max-width:420px;white-space:pre-wrap;word-break:break-all;line-height:1.5}\n'
        '  #overlay{position:fixed;top:126px;left:0;right:0;bottom:0;background:rgba(248,249,250,.85);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:50;transition:opacity .4s}\n'
        '  #overlay.hidden{opacity:0;pointer-events:none}\n'
        '  .spinner{width:36px;height:36px;border:3px solid #e8eaf6;border-top-color:#3f51b5;border-radius:50%;animation:spin .7s linear infinite;margin-bottom:12px}\n'
        '  @keyframes spin{to{transform:rotate(360deg)}}\n'
        '  #overlay p{margin:0;font-size:0.85rem;color:#555}\n'
        '</style>\n</head>\n<body>\n'
        '<div id="toolbar">\n'
        '  <div id="tb-top">\n'
        '    <h1>Dependency Graph</h1>\n'
        '    <span id="stats">' + str(nc) + ' modules &middot; ' + str(ec) + ' edges' + trunc + '</span>\n'
        '    <input id="searchBox" type="search" placeholder="Search modules..." autocomplete="off">\n'
        '  </div>\n'
        '  <div id="tb-legend">\n'
        '    <div style="display:flex;align-items:center;gap:4px;white-space:nowrap;flex-shrink:0;">'
        '<div style="width:10px;height:10px;border-radius:2px;background:#f9a;border:2px solid #c55;flex-shrink:0;"></div>'
        '<span style="font-size:0.7rem;opacity:.88;">Entry point</span></div>\n'
        '    <div style="width:1px;height:12px;background:rgba(255,255,255,.3);flex-shrink:0;"></div>\n'
        + legend_html +
        '  </div>\n'
        '</div>\n'
        '<div id="controls">\n'
        '  <select id="layoutSelect" class="ctrl-select">\n'
        '    <option value="physics">Force-directed</option>\n'
        '    <option value="hierarchical">Hierarchical (top-down)</option>\n'
        '  </select>\n'
        '  <div class="ctrl-sep"></div>\n'
        '  <span class="ctrl-lbl">Dir:</span>\n'
        '  <select id="dirFilter" class="ctrl-select">\n'
        + dir_options_html +
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
        + dir_colors_js +
        'var allNodes=[\n    ' + nodes_str + '\n];\n'
        'var allEdges=[\n    ' + edges_str + '\n];\n'
        'var nodes=new vis.DataSet(allNodes);\n'
        'var edges=new vis.DataSet(allEdges);\n'
        'var container=document.getElementById("graph");\n'
        'var tooltip=document.getElementById("tooltip");\n'
        'var network=null,frozen=false,activeNodes=nodes;\n'
        'var currentDir="all",currentSearch="";\n'
        'var BASE_NODE={shape:"box",font:{size:12},margin:{top:6,bottom:6,left:10,right:10},widthConstraint:{maximum:200},shadow:{enabled:true,color:"rgba(0,0,0,.08)",size:6,x:0,y:2},borderWidth:1.5};\n'
        'var BASE_EDGE={smooth:{type:"cubicBezier",roundness:0.4},color:{color:"#b0bec5",highlight:"#3f51b5",hover:"#5c6bc0"},width:1.2,selectionWidth:2.5,arrows:{to:{enabled:true,scaleFactor:0.7}}};\n'
        '\n'
        'function applyFilters(){\n'
        '  var updates=allNodes.map(function(n){\n'
        '    var dOk=currentDir==="all"||n.group===currentDir;\n'
        '    var sOk=!currentSearch||n.label.toLowerCase().includes(currentSearch)||n.id.toLowerCase().includes(currentSearch);\n'
        '    var visible=dOk&&sOk;\n'
        '    return{id:n.id,opacity:visible?1:(dOk?0.12:0.05),borderWidth:visible?1.5:1};\n'
        '  });\n'
        '  activeNodes.update(updates);\n'
        '  if(currentSearch){\n'
        '    var matched=allNodes.filter(function(n){return n.label.toLowerCase().includes(currentSearch)||n.id.toLowerCase().includes(currentSearch);});\n'
        '    if(matched.length&&network)network.selectNodes(matched.map(function(n){return n.id;}));\n'
        '  }else{if(network)network.unselectAll();}\n'
        '}\n'
        '\n'
        'function computeLevels(){\n'
        '  var adj={},hasIn={};\n'
        '  allEdges.forEach(function(e){if(!adj[e.from])adj[e.from]=[];adj[e.from].push(e.to);hasIn[e.to]=true;});\n'
        '  var roots=allNodes.filter(function(n){return n.color&&n.color.border==="#c55";}).map(function(n){return n.id;});\n'
        '  if(!roots.length)roots=allNodes.filter(function(n){return!hasIn[n.id];}).map(function(n){return n.id;});\n'
        '  if(!roots.length&&allNodes.length)roots=[allNodes[0].id];\n'
        '  var levels={},visited={},queue=roots.slice();\n'
        '  roots.forEach(function(id){levels[id]=0;});\n'
        '  while(queue.length){\n'
        '    var cur=queue.shift();if(visited[cur])continue;visited[cur]=true;\n'
        '    (adj[cur]||[]).forEach(function(nb){\n'
        '      if(!visited[nb]){var proposed=(levels[cur]||0)+1;if(levels[nb]===undefined||levels[nb]<proposed)levels[nb]=proposed;queue.push(nb);}\n'
        '    });\n'
        '  }\n'
        '  allNodes.forEach(function(n){if(levels[n.id]===undefined)levels[n.id]=0;});\n'
        '  return levels;\n'
        '}\n'
        '\n'
        'function computeHierPos(){\n'
        '  var levels=computeLevels(),byLevel={};\n'
        '  allNodes.forEach(function(n){var lv=levels[n.id];if(!byLevel[lv])byLevel[lv]=[];byLevel[lv].push(n.id);});\n'
        '  var SEP=140,NSEP=200,pos={};\n'
        '  Object.keys(byLevel).forEach(function(lv){\n'
        '    var ids=byLevel[lv],tw=(ids.length-1)*NSEP;\n'
        '    ids.forEach(function(id,i){pos[id]={x:i*NSEP-tw/2,y:parseInt(lv)*SEP};});\n'
        '  });\n'
        '  return pos;\n'
        '}\n'
        '\n'
        'function buildOpts(layout){\n'
        '  var o={nodes:BASE_NODE,edges:BASE_EDGE,layout:{hierarchical:{enabled:false}},interaction:{hover:true,tooltipDelay:80}};\n'
        '  o.physics=layout==="hierarchical"?{enabled:false}:{enabled:true,barnesHut:{gravitationalConstant:-8000,springLength:120,springConstant:0.04},stabilization:{iterations:300,updateInterval:25}};\n'
        '  return o;\n'
        '}\n'
        '\n'
        'function hideOverlay(){\n'
        '  var ov=document.getElementById("overlay");ov.classList.add("hidden");setTimeout(function(){ov.style.display="none";},450);\n'
        '}\n'
        '\n'
        'function initNetwork(layout){\n'
        '  if(network){network.destroy();network=null;}\n'
        '  var ov=document.getElementById("overlay");ov.style.display="flex";ov.classList.remove("hidden");\n'
        '  var nd,ed;\n'
        '  if(layout==="hierarchical"){\n'
        '    var pos=computeHierPos();\n'
        '    nd=new vis.DataSet(allNodes.map(function(n){var p=pos[n.id];return Object.assign({},n,{x:p.x,y:p.y,opacity:1,borderWidth:1.5});}));\n'
        '    ed=new vis.DataSet(allEdges);\n'
        '  }else{\n'
        '    nodes.update(allNodes.map(function(n){return{id:n.id,opacity:1,borderWidth:1.5};}));\n'
        '    nd=nodes;ed=edges;\n'
        '  }\n'
        '  activeNodes=nd;\n'
        '  network=new vis.Network(container,{nodes:nd,edges:ed},buildOpts(layout));\n'
        '  if(layout==="hierarchical"){requestAnimationFrame(function(){network.fit();hideOverlay();});}\n'
        '  network.on("stabilizationIterationsDone",function(){network.fit({animation:{duration:300,easingFunction:"easeInOutQuad"}});hideOverlay();});\n'
        '  network.on("click",function(params){\n'
        '    if(params.nodes.length>0){\n'
        '      var n=allNodes.find(function(x){return x.id===params.nodes[0];});\n'
        '      if(n&&n.url)window.location.href=n.url;\n'
        '    }\n'
        '  });\n'
        '  network.on("hoverNode",function(p){tooltip.textContent=p.node;tooltip.style.display="block";});\n'
        '  network.on("blurNode",function(){tooltip.style.display="none";});\n'
        '  applyFilters();\n'
        '}\n'
        '\n'
        'initNetwork("physics");\n'
        'document.addEventListener("mousemove",function(e){\n'
        '  tooltip.style.left=(e.clientX+16)+"px";tooltip.style.top=(e.clientY-8)+"px";\n'
        '});\n'
        'document.getElementById("layoutSelect").addEventListener("change",function(){\n'
        '  frozen=false;\n'
        '  var fb=document.getElementById("freezeBtn");fb.textContent="Freeze";\n'
        '  var isH=this.value==="hierarchical";\n'
        '  fb.style.display=isH?"none":"";document.getElementById("freezeSep").style.display=isH?"none":"";\n'
        '  initNetwork(this.value);\n'
        '});\n'
        'document.getElementById("dirFilter").addEventListener("change",function(){\n'
        '  currentDir=this.value;applyFilters();\n'
        '});\n'
        'document.getElementById("fitBtn").addEventListener("click",function(){\n'
        '  if(network)network.fit({animation:{duration:400,easingFunction:"easeInOutQuad"}});\n'
        '});\n'
        'document.getElementById("freezeBtn").addEventListener("click",function(){\n'
        '  frozen=!frozen;if(network)network.setOptions({physics:{enabled:!frozen}});\n'
        '  this.textContent=frozen?"Unfreeze":"Freeze";\n'
        '});\n'
        'document.getElementById("searchBox").addEventListener("input",function(){\n'
        '  currentSearch=this.value.trim().toLowerCase();applyFilters();\n'
        '});\n'
        '</script>\n</body>\n</html>\n'
    )
    return html


# ── mkdocs.yml ────────────────────────────────────────────────────────────────

def _build_mkdocs_yml(site_name: str, module_paths: list,
                      dir_page_names: list | None = None) -> str:
    """Build mkdocs.yml with grouped nav (by top-level directory)."""
    dir_page_names = dir_page_names or []

    def _nav_title(p: str) -> str:
        stem = Path(p).stem
        if stem == "__init__" and Path(p).parent != Path("."):
            return Path(p).parent.name.replace("_", " ").title() + " (init)"
        return stem.replace("_", " ").title()

    def _dedup_titles(paths: list[str]) -> list[str]:
        """Return a de-duplicated title list matching *paths*."""
        raw = [_nav_title(p) for p in paths]
        counts = Counter(raw)
        seen: dict[str, int] = {}
        out = []
        for t, p in zip(raw, paths):
            if counts[t] > 1:
                pkg = Path(p).parent.name
                t = f"{t} ({pkg})" if pkg and pkg != "." else t
            if t in seen:
                seen[t] += 1
                t = f"{t} {seen[t]}"
            else:
                seen[t] = 0
            out.append(t)
        return out

    # ── Build modules section of nav ──────────────────────────────────────────
    modules_nav_list: list = []
    if module_paths:
        groups: dict[str, list[str]] = defaultdict(list)
        for p in module_paths:
            top = Path(p).parts[0] if len(Path(p).parts) > 1 else "_root_"
            groups[top].append(p)

        if len(groups) <= 1:
            # Flat list — single directory or all at root
            titles = _dedup_titles(module_paths)
            modules_nav_list = [
                {title: f"modules/{_slugify(p)}.md"}
                for title, p in zip(titles, module_paths)
            ]
        else:
            # Group by top-level directory
            for dir_name in sorted(groups.keys()):
                dir_paths = groups[dir_name]
                titles = _dedup_titles(dir_paths)
                entries = [
                    {title: f"modules/{_slugify(p)}.md"}
                    for title, p in zip(titles, dir_paths)
                ]
                display = dir_name if dir_name != "_root_" else "(root)"
                modules_nav_list.append({display: entries})

    # ── Assemble full nav list ────────────────────────────────────────────────
    nav_list: list = [
        {"Home": "index.md"},
        {"Guided Tour": "guided_tour.md"},
        {"File Tree": "file_tree.md"},
    ]
    if dir_page_names:
        dirs_entries = []
        for d in sorted(dir_page_names):
            label = (d if d != "_root_" else "(root)") + "/"
            dirs_entries.append({label: f"dirs/{_slugify(d)}.md"})
        nav_list.append({"Directories": dirs_entries})
    if modules_nav_list:
        nav_list.append({"Modules": modules_nav_list})

    nav_yaml = yaml.dump(nav_list, default_flow_style=False, allow_unicode=True)
    nav_indented = "\n".join(
        "  " + line if line.strip() else ""
        for line in nav_yaml.splitlines()
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
        "    - navigation.expand\n"
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
        + nav_indented + "\n"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def build_site(guide: OnboardingGuide, graph: nx.DiGraph,
               output_dir: Path, site_name: str = "Codebase Onboarding") -> None:
    """Write the full MkDocs site to *output_dir*."""
    docs_dir = output_dir / "docs"
    modules_dir = docs_dir / "modules"
    dirs_dir = docs_dir / "dirs"
    modules_dir.mkdir(parents=True, exist_ok=True)
    dirs_dir.mkdir(parents=True, exist_ok=True)

    # Core pages
    (docs_dir / "index.md").write_text(_build_index(guide, graph), encoding="utf-8")
    (docs_dir / "guided_tour.md").write_text(guide.guided_tour, encoding="utf-8")
    (docs_dir / "file_tree.md").write_text(_build_file_tree_page(guide, graph), encoding="utf-8")

    # Per-module pages
    for path, mod in guide.modules.items():
        slug = _slugify(path)
        (modules_dir / f"{slug}.md").write_text(_build_module_page(mod), encoding="utf-8")

    # Per-directory overview pages
    dir_modules: dict[str, list] = defaultdict(list)
    for path, mod in guide.modules.items():
        top = Path(path).parts[0] if len(Path(path).parts) > 1 else "_root_"
        dir_modules[top].append(mod)

    dir_page_names: list[str] = []
    for dir_name, mods in sorted(dir_modules.items()):
        slug = _slugify(dir_name)
        content = _build_directory_page(dir_name, mods, graph)
        (dirs_dir / f"{slug}.md").write_text(content, encoding="utf-8")
        dir_page_names.append(dir_name)

    # CSS
    css_dir = docs_dir / "css"
    css_dir.mkdir(exist_ok=True)
    (css_dir / "extra.css").write_text(
        "/* do NOT hide .mermaid - Material replaces the div, not inserts SVG. */\n"
        ".mermaid { min-height: 20px; }\n",
        encoding="utf-8",
    )

    # Interactive graph
    (docs_dir / "graph.html").write_text(_build_interactive_graph(graph), encoding="utf-8")

    # mkdocs.yml
    yml = _build_mkdocs_yml(site_name, list(guide.modules.keys()), dir_page_names)
    (output_dir / "mkdocs.yml").write_text(yml, encoding="utf-8")
