# Legacy Codebase Onboarding Generator

A CLI tool that analyses a git repository and generates a searchable MkDocs site explaining how the codebase works — module by module, in the order you should read it.

---

## What it does

Most codebases have no onboarding docs. The ones that do have them scattered across READMEs, wikis, and tribal knowledge. This tool pulls everything together: the code structure, the git history, the existing comments, and uses an LLM to write a walkthrough for each module.

The output is a static site with:

- **System overview** — AI-generated narrative explaining the entire system
- **Arc42 architecture document** — full 12-section architecture spec (Introduction, Constraints, Context, Solution Strategy, Building Blocks, Runtime View, Deployment, Cross-cutting Concepts, Decisions, Quality Requirements, Risks, Glossary)
- **C4 diagrams** — Level 1 Context, Level 2 Container, and Level 3 Component diagrams (Mermaid-rendered, no plugins needed)
- **Domain model** — ER diagram of your entities and their relationships
- **Data flow diagram** — DFD showing how data moves through the system
- **Per-module pages** — 11 sections each: summary, architecture notes, dependencies (local graph), structure (class diagram), how to use, code walkthrough, key design decisions, patterns to follow, pitfalls, sequence diagram, state machine diagram, data flow snippet
- **Interactive dependency graph** — zoom, pan, drag, click to open any module page; nodes colour-coded by top-level directory with a live legend and directory filter
- **Static Mermaid dependency graph** — clickable nodes in the index page
- **Directory overview pages** — one per top-level package, with C4 Component diagram, file counts, module table, hotspot/dead code stats
- **File tree page** — every parsed source file listed by directory, with hotspot 🔥 and dead code 💀 badges, narrated files linked to their module page
- **Guided tour** — AI-narrated walkthrough of the whole codebase in reading order
- **Full-text search**

---

## How it works

Stages 1–3 run **in parallel** (fully independent). A skeleton site is written immediately after they finish — you can open it in the browser while Stage 4 runs in the background.

**Stage 1 — Static analysis**
Walks the repo with tree-sitter, extracting classes, functions, and import relationships for Python, JavaScript, TypeScript, TSX, C/C++, and Java. Files are parsed in parallel using a thread pool (tree-sitter releases the GIL, so threads run on real cores). Builds a directed dependency graph. Files larger than 500 KB and standard noise directories (`node_modules`, `__pycache__`, `.venv`, `dist`, `build`, etc.) are skipped automatically. Parse results are cached in `.onboard_cache/parse_cache.pkl` keyed by file path + mtime + size — unchanged files are skipped entirely on subsequent runs.

**Stage 2 — Git history**
Reads the entire git log in a single subprocess call (`git log --name-only`), then computes change frequency per file, co-change coupling, author ownership, and recurring themes from commit messages. This is 10–50x faster than per-commit diff approaches on large histories. Degrades gracefully on repos with no history, bare repos, or missing git.

**Stage 3 — Doc collection**
Finds READMEs, architecture docs, Python docstrings, JSDoc, and Doxygen comments and links them to the files they describe. Source file processing runs in parallel. Restricts prose collection to `.md` and `.rst` to avoid false positives.

**Skeleton site** *(between stages 3 and 4)*
Immediately after stages 1–3 finish, the tool writes a full skeleton site with stub pages for every module. If `--serve` is passed, `mkdocs serve` starts and the browser opens at this point — you don't wait for the LLM. As Stage 4 completes each module narrative, the corresponding `.md` file is overwritten in-place and mkdocs serve's file watcher picks it up automatically (live refresh).

**Stage 4 — LLM narratives** *(skipped with --skip-llm)*
Sends each module's structure + history + docs to an LLM and generates rich per-module content. Also runs five additional generation passes (all five run in parallel at the end):

1. **Per-module narratives** — summary, architecture notes, code walkthrough, how-to-use guide, key design decisions, patterns, pitfalls, plus conditional sequence/state/data-flow diagrams for complex modules. Up to 8 modules generated concurrently. Trivial modules (fewer than 3 symbols, no dependants, no git history) are skipped — typically 30–40% of files in large repos.
2. **System overview** — high-level narrative covering the whole codebase
3. **Arc42 document** — 12-section architecture spec; C4 Context and Container diagrams are extracted from it automatically
4. **Domain model** — ER diagram of entities and relationships
5. **Data flow diagram** — DFD showing data movement through the system
6. **C4 Component diagrams** — one per top-level directory, showing internal components and their interactions

The five architecture passes (2–6) run concurrently in a dedicated 5-worker pool after all module narratives complete. Any single failed LLM call produces a clean warning admonition in the page — the guide always completes. Re-run with `--retry-failed` to regenerate only the failed pages.

**Tech detector (zero LLM, always runs)**
Before any AI calls, a static scan identifies frameworks (Flask, FastAPI, Django, etc.) and external systems (PostgreSQL, Redis, AWS, Kafka, etc.) directly from import statements. This grounds every AI prompt with real signal, reducing hallucination in diagrams.

### Performance

| | Before | After |
|---|---|---|
| Stage 1 (2 000 files, cold) | ~60s | ~8s |
| Stage 1 (2 000 files, warm cache) | ~60s | ~1s |
| Stage 2 (5 000 commits) | ~120s | ~2s |
| Stage 3 (2 000 files) | ~20s | ~4s |
| Stages 1–3 combined (parallel) | ~200s | ~8s cold / ~3s warm |
| **Skeleton site ready** | after stage 4 | **after stage 3** |
| Stage 4 (50 modules, 8b-instant) | ~150s (3 concurrent) | ~40s (8 concurrent + trivial skip) |
| **Total (with LLM)** | **~6 min** | **~50s** |

`--skip-llm` on a large monolith: cold ~8s, warm ~3s.

---

## Setup

Requires Python 3.10+.

```bash
pip install -e .
pip install mkdocs-material
```

Install tree-sitter language bindings for the languages in your repo:

```bash
pip install tree-sitter-python          # Python
pip install tree-sitter-javascript      # JavaScript
pip install tree-sitter-typescript      # TypeScript / TSX
pip install tree-sitter-cpp             # C / C++
pip install tree-sitter-java            # Java
```

You only need bindings for languages actually present in the target repo. Missing bindings are skipped silently.

---

## Usage

### Without AI (no API key needed)

Stages 1–3 run in parallel. You get the full interactive dependency graph, static Mermaid graph, file tree, directory overviews, reading order, hotspot and dead code flags, and all extracted docs. Module narrative pages and architecture documents show stubs.

```bash
onboard analyze /path/to/repo --skip-llm
```

The guide is written to `<repo>/onboarding-guide` by default. Pass `--serve` to launch MkDocs and open the browser automatically:

```bash
onboard analyze /path/to/repo --skip-llm --serve
```

Or serve an already-generated guide:

```bash
onboard serve /path/to/repo/onboarding-guide
```

Both commands open `http://127.0.0.1:8000` in your browser automatically after a 2-second startup delay.

---

### With AI (full output)

All four stages run. The LLM writes module narratives, a system overview, guided tour, Arc42 architecture document, domain model, data flow diagram, and C4 diagrams at all three levels.

**Option 1: Groq** (default — fast, free tier available, no card required)

1. Sign up at [console.groq.com](https://console.groq.com) and create an API key.
2. Run:

```bash
# Windows
set GROQ_API_KEY=gsk_...
onboard analyze C:\path\to\repo --serve

# Mac / Linux
export GROQ_API_KEY=gsk_...
onboard analyze /path/to/repo --serve
```

**Option 2: Gemini** (free tier available, no card required)

1. Sign up at [aistudio.google.com](https://aistudio.google.com) and create an API key.
2. Run:

```bash
# Windows
set GEMINI_API_KEY=AIza...
onboard analyze C:\path\to\repo --provider gemini --serve

# Mac / Linux
export GEMINI_API_KEY=AIza...
onboard analyze /path/to/repo --provider gemini --serve
```

---

## All options

```
onboard analyze <repo_path> [OPTIONS]

  -o, --output DIR        Where to write the site  [default: <repo>/onboarding-guide]
  --provider TEXT         groq or gemini           [default: groq]
  --api-key TEXT          API key (or use env var)
  --model TEXT            Override the default model (takes precedence over --model-tier)
  --model-tier TEXT       fast | balanced | best   [default: fast]
  --site-name TEXT        Site title
  --max-modules INT       Max modules sent to LLM  [default: 50]
  --skip-llm              Run stages 1-3 only, no LLM calls
  --serve                 Start mkdocs serve after skeleton build; browser opens immediately
  --workers INT           Parallel workers for file parsing  [default: auto]
  --retry-failed          Re-generate only modules whose narrative previously failed
  --no-cache              Skip parse cache; re-parse all files from scratch
```

`--workers` defaults to `min(8, cpu_count)`. Increase on machines with more cores; reduce if memory is limited on very large repos.

### Model tiers

`--model-tier` picks a preset model without requiring you to know the exact model string. `--model` overrides everything.

| Provider | `--model-tier fast` | `--model-tier balanced` / `best` |
|----------|---------------------|-----------------------------------|
| groq     | llama-3.1-8b-instant | llama-3.3-70b-versatile |
| gemini   | gemini-2.0-flash | gemini-2.0-flash / gemini-1.5-pro |

`fast` is the default. It uses the highest-throughput model for each provider — dramatically fewer 429 errors on large repos, and 2–3× faster wall time than the 70b model.

**Providers**

| Provider | Default model            | Free tier | Sign up                   |
|----------|--------------------------|-----------|---------------------------|
| groq     | llama-3.1-8b-instant     | Yes       | console.groq.com          |
| gemini   | gemini-2.0-flash         | Yes       | aistudio.google.com       |

**Env vars:** `GROQ_API_KEY`, `GEMINI_API_KEY`

### Retrying failed modules

If some modules failed during generation (rate limit exhausted, network blip), re-run with `--retry-failed` instead of regenerating everything:

```bash
onboard analyze /path/to/repo --retry-failed
```

This scans the existing `docs/modules/*.md` files for `"Narrative unavailable"` admonitions, extracts the affected file paths, and only re-runs the LLM for those modules. The rest of the guide is untouched.

### Parse cache

On the second run against an unchanged repo, Stage 1 skips tree-sitter parsing for every file whose path, modification time, and size match the cached result. The cache lives at `<repo>/.onboard_cache/parse_cache.pkl`. Pass `--no-cache` to force a full re-parse.

---

## Generated site structure

```
<repo>/onboarding-guide/
  mkdocs.yml
  docs/
    index.md           ← system overview, C4 context diagram, Mermaid graph, reading order
    guided_tour.md     ← LLM-narrated walkthrough of the whole codebase
    file_tree.md       ← every parsed file, grouped by directory, with badges
    arc42.md           ← full 12-section Arc42 architecture document
    domain_model.md    ← ER diagram of entities and relationships
    data_flow.md       ← data flow diagram (DFD)
    graph.html         ← interactive vis.js dependency graph
    dirs/
      <dir>.md         ← one overview page per top-level package (with C4 Component diagram)
    modules/
      <slug>.md        ← per-module page: 11 sections including code walkthrough + diagrams
    css/
      extra.css

<repo>/.onboard_cache/
  parse_cache.pkl      ← tree-sitter parse results (path + mtime + size keyed)
```

The MkDocs nav groups modules under their top-level directory automatically. Arc42, Domain Model, and Data Flow appear as top-level nav items under an "Architecture" section.

---

## Per-module pages (with AI)

Each module page contains 11 sections:

1. **What it does** — one-paragraph summary
2. **How it fits** — where this module sits in the overall architecture
3. **Architecture notes** — patterns used, layer responsibilities, design rationale
4. **Dependencies** — local Mermaid graph showing direct importers and imports, with this file highlighted
5. **Structure** — Mermaid class diagram built from tree-sitter symbols (no AI needed)
6. **How to use** — entry points, typical call patterns, code examples
7. **Code walkthrough** — section-by-section tour of the actual source
8. **Key design decisions** — why things are the way they are
9. **Patterns to follow** — conventions to preserve when modifying this file
10. **Pitfalls** — what breaks, what's fragile, what surprised past contributors
11. **Sequence / State machine / Data flow diagrams** — AI-generated Mermaid diagrams for complex modules (hotspots, modules with many symbols, managers, handlers, pipelines)

Section 11 diagrams only appear for modules that meet a complexity threshold: hotspot (>20 commits), high symbol count (>8), or name hints (`manager`, `handler`, `pipeline`, `service`, `processor`, `router`, `dispatcher`).

Trivial modules (fewer than 3 symbols, nothing imports them, no git history) are skipped by the LLM and receive a short stub instead. This is typically 30–40% of files in a large repo and cuts Stage 4 time proportionally.

---

## Architecture pages (with AI)

### Arc42 (`arc42.md`)
A full 12-section architecture document covering: Introduction & Goals, Constraints, Context & Scope (with C4 Context diagram), Solution Strategy, Building Block View (with C4 Container diagram), Runtime View (with sequence diagrams), Deployment View, Cross-cutting Concepts, Architecture Decisions, Quality Requirements, Risks & Technical Debt, Glossary.

### Domain Model (`domain_model.md`)
An ER diagram showing entities, their attributes, and relationships across the codebase — inferred from class names, ORM models, and data structures detected statically and refined by the LLM.

### Data Flow (`data_flow.md`)
A DFD-style flowchart showing how data enters the system, is processed, stored, and returned — grounded in the statically detected external systems (databases, queues, APIs, cloud services).

### C4 diagrams
- **Level 1 — Context** (`index.md`): the system in relation to users and external systems
- **Level 2 — Container** (`arc42.md`, Building Block View section): major containers and their relationships
- **Level 3 — Component** (each `dirs/<dir>.md`): internal components within each top-level package

All C4 diagrams use Mermaid's native `C4Context`, `C4Container`, and `C4Component` diagram types, which render in MkDocs Material without any additional plugins.

---

## Interactive graph

The generated site includes a full interactive dependency graph (`graph.html`) accessible via the "Open interactive graph" button on the home page.

**Layout**
- **Force-directed** — nodes settle organically; edges act like springs
- **Hierarchical (top-down)** — strict top-to-bottom layout; entry points at top, shared utilities at bottom

**Toolbar**
- **Colour legend** — each top-level directory gets a distinct colour; entry points are always salmon/pink regardless of directory
- **Directory filter** — select a directory from the dropdown to highlight only those nodes; all other nodes dim to near-invisible. Combines with search.
- **Search** — type a module name to dim everything else; matches on both display label (`auth/manager`) and full path
- **Hover tooltip** — shows the full file path (`src/core/auth/manager.py`) on hover
- **Fit to screen / Freeze** — lock positions once the layout has settled

**Node labels** show `parent_dir/stem` (e.g. `auth/manager`) so files with the same name in different packages are immediately distinguishable.

**Performance cap:** On repos with more than 200 parsed files, the graph shows the top 200 by connectivity (highest-degree nodes). The rest appear in the file tree page.

Click any node to navigate directly to that module's page.

---

## Notes

- If you hit rate limits on a large repo, use `--max-modules 20` on the first run and increase from there, or switch to `--model-tier fast`.
- Re-run the same command against the same repo to refresh the guide as the codebase changes. The output directory is overwritten in place; the parse cache speeds up Stage 1 significantly.
- The `onboarding-guide` output directory is excluded from analysis, so running the tool on its own repo won't recurse.
- Repos with no git history, bare repos, or repos on machines without git installed all run fine — Stage 2 degrades gracefully and returns empty history.
- The interactive graph requires an internet connection to load vis.js from the unpkg CDN. All other pages are fully offline once generated.
- C4 and Mermaid diagrams render using MkDocs Material's built-in Mermaid support — no extra plugins needed.
- The tech detector runs even with `--skip-llm` (it is pure static analysis) and its output is printed in the Stage 1 summary.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'tree_sitter_python'`** — install the binding: `pip install tree-sitter-python`. Only install bindings for languages you need.

**`onboard: command not found`** — run `pip install -e .` from the repo root, or invoke directly with `python -m onboard`.

**Rate limit errors (429)** — Stage 4 retries automatically up to 5 times with exponential backoff and jitter. If consistently throttled, switch to `--model-tier fast` (uses llama-3.1-8b-instant / gemini-2.0-flash, which have much higher throughput limits) or reduce `--max-modules`.

**Module pages show "Narrative unavailable"** — one or more modules failed during generation (usually rate limits). Re-run with `--retry-failed` to regenerate only those pages without re-running the whole pipeline.

**Module pages show stubs ("Generating narrative...")** — you ran with `--skip-llm`. Re-run without the flag with a valid API key.

**Arc42 / domain model / data flow pages show stubs** — same as above; these are LLM-generated. Re-run without `--skip-llm`.

**Skeleton site looks empty** — normal. The skeleton site contains stub pages while Stage 4 runs in the background. Module pages fill in progressively if `--serve` is active; otherwise do a full refresh after Stage 4 completes.

**Windows encoding errors in terminal** — set `PYTHONUTF8=1` before running: `set PYTHONUTF8=1 && onboard analyze ...`

**Stage 1 seems slow on first run** — tree-sitter compiles language grammars on first use and caches them. Subsequent runs are faster. The parse cache also only kicks in from the second run onward.

**Graph shows fewer nodes than expected** — the interactive graph caps at 200 nodes (top by connectivity). All files appear in the File Tree page regardless of the cap.

**C4 diagrams not rendering** — make sure you are using `mkdocs-material` (not plain `mkdocs`). Run `pip install mkdocs-material` and check that `mkdocs.yml` has `markdown_extensions: [pymdownx.superfences]` — the builder adds this automatically.

**Node clicks in the graph give 404 errors** — this should not happen with current versions. If you have a guide generated by an older version, re-run `onboard analyze` to regenerate it with root-relative URLs.
