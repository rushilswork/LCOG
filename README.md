# Legacy Codebase Onboarding Generator

A CLI tool that analyses a git repository and generates a searchable MkDocs site explaining how the codebase works — module by module, in the order you should read it.

---

## What it does

Most codebases have no onboarding docs. The ones that do have them scattered across READMEs, wikis, and tribal knowledge. This tool pulls everything together: the code structure, the git history, the existing comments, and (optionally) uses an LLM to write a walkthrough for each module.

The output is a static site with:

- An interactive dependency graph — zoom, pan, drag nodes, click to open any module page
- A static Mermaid dependency graph with clickable nodes
- A suggested reading order — dependencies first, hotspots surfaced early
- A per-module page: what it does, how it fits, design decisions, pitfalls
- Dead code callouts (files untouched for 2+ years)
- Hotspot warnings (files that change constantly — add tests before touching)
- A guided tour that walks through the whole codebase in sequence
- Full-text search

---

## How it works

Stages 1–3 run **in parallel** (they are fully independent). Stage 4 starts once all three finish.

**Stage 1 — Static analysis**
Walks the repo with tree-sitter, extracting classes, functions, and import relationships for Python, JavaScript, TypeScript, TSX, C/C++, and Java. Files are parsed in parallel using a thread pool (tree-sitter releases the GIL, so threads run on real cores). Builds a directed dependency graph. Files larger than 500 KB and standard noise directories (`node_modules`, `__pycache__`, `.venv`, `dist`, `build`, etc.) are skipped automatically.

**Stage 2 — Git history**
Reads the entire git log in a single subprocess call (`git log --name-only`), then computes change frequency per file, co-change coupling, author ownership, and recurring themes from commit messages. This is 10–50x faster than per-commit diff approaches on large histories. Degrades gracefully on repos with no history, bare repos, or missing git.

**Stage 3 — Doc collection**
Finds READMEs, architecture docs, Python docstrings, JSDoc, and Doxygen comments and links them to the files they describe. Source file processing runs in parallel. Restricts prose collection to `.md` and `.rst` to avoid false positives.

**Stage 4 — LLM narratives** *(skipped with --skip-llm)*
Sends each module's structure + history + docs to an LLM. Runs up to 3 concurrent API requests with per-request exponential backoff + jitter on rate limits. Produces a walkthrough covering what the module does, how it connects to the rest of the system, key design decisions, patterns to follow, and what to watch out for.

### Performance on large codebases

| | Sequential (old) | Parallel (current) |
|---|---|---|
| Stage 1 (2 000 files) | ~60s | ~8s |
| Stage 2 (5 000 commits) | ~120s | ~2s |
| Stage 3 (2 000 files) | ~20s | ~4s |
| Stages 1–3 combined | ~200s | ~8s (parallel) |
| Stage 4 (50 modules, LLM) | ~150s | ~55s (3 concurrent) |
| **Total (with LLM)** | **~6 min** | **~1 min** |

`--skip-llm` on a large monolith: ~200s → ~8s (~25x faster).

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

Stages 1–3 run in parallel. You get the full interactive dependency graph, static Mermaid graph, reading order, hotspot and dead code flags, and all extracted docs. Module narrative pages show stubs instead of LLM prose.

```bash
onboard analyze /path/to/repo --skip-llm
```

The guide is written to `<repo>/onboarding-guide` by default. Then serve it:

```bash
cd /path/to/repo/onboarding-guide
mkdocs serve
```

Open `http://127.0.0.1:8000`.

---

### With AI (full output)

All four stages run. The LLM writes actual narratives for every module, a system overview, and a guided tour.

**Option 1: Groq** (default — fast, free tier available, no card required)

1. Sign up at [console.groq.com](https://console.groq.com) and create an API key.
2. Run:

```bash
# Windows
set GROQ_API_KEY=gsk_...
onboard analyze C:\path\to\repo
cd C:\path\to\repo\onboarding-guide && mkdocs serve

# Mac / Linux
export GROQ_API_KEY=gsk_...
onboard analyze /path/to/repo
cd /path/to/repo/onboarding-guide && mkdocs serve
```

**Option 2: Gemini** (free tier available, no card required)

1. Sign up at [aistudio.google.com](https://aistudio.google.com) and create an API key.
2. Run:

```bash
# Windows
set GEMINI_API_KEY=AIza...
onboard analyze C:\path\to\repo --provider gemini

# Mac / Linux
export GEMINI_API_KEY=AIza...
onboard analyze /path/to/repo --provider gemini
```

---

## All options

```
onboard analyze <repo_path> [OPTIONS]

  -o, --output DIR        Where to write the site  [default: <repo>/onboarding-guide]
  --provider TEXT         groq or gemini           [default: groq]
  --api-key TEXT          API key (or use env var)
  --model TEXT            Override the default model
  --site-name TEXT        Site title
  --max-modules INT       Max modules sent to LLM  [default: 50]
  --skip-llm              Run stages 1-3 only, no LLM
  --serve                 Run mkdocs serve after generation
  --workers INT           Parallel workers for file parsing  [default: auto]
```

`--workers` defaults to `min(8, cpu_count)`. Increase on machines with more cores; reduce if memory is limited on very large repos.

**Providers**

| Provider | Default model            | Free tier | Sign up                   |
|----------|--------------------------|-----------|---------------------------|
| groq     | llama-3.3-70b-versatile  | Yes       | console.groq.com          |
| gemini   | gemini-1.5-flash         | Yes       | aistudio.google.com       |

**Env vars:** `GROQ_API_KEY`, `GEMINI_API_KEY`

To serve an already-generated guide without regenerating:

```bash
onboard serve /path/to/repo/onboarding-guide
```

---

## Interactive graph

The generated site includes a full interactive dependency graph (`graph.html`) accessible via the "Open interactive graph" button on the home page.

- **Force-directed** — nodes settle organically; edges act like springs
- **Hierarchical (top-down)** — strict top-to-bottom layout; entry points at top, shared utilities at bottom
- **Fit to screen** — zoom to fit all nodes in view
- **Freeze / Unfreeze** — lock node positions once the layout has settled
- **Search** — type a module name to dim everything else

Click any node to open that module's page.

---

## Notes

- If you hit rate limits on a large repo, use `--max-modules 20` on the first run and increase from there.
- Re-run the same command against the same repo to refresh the guide as the codebase changes. The output directory is overwritten in place.
- The `onboarding-guide` output directory is excluded from analysis, so running the tool on its own repo won't recurse.
- Repos with no git history, bare repos, or repos on machines without git installed all run fine — Stage 2 degrades gracefully and returns empty history.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'tree_sitter_python'`** — install the binding: `pip install tree-sitter-python`. Only install bindings for languages you need.

**`onboard: command not found`** — run `pip install -e .` from the repo root, or invoke directly with `python -m onboard`.

**CLI shows "Stage 1 -- Static structure mapping" (old UI)** — your `onboard` executable is stale. Force a reinstall: `pip install -e . --force-reinstall`, then re-run.

**Rate limit errors (429)** — Stage 4 retries automatically with backoff and jitter. If consistently throttled, try `--provider gemini` or reduce `--max-modules`.

**Module pages show stubs** — you ran with `--skip-llm`. Re-run without the flag and with a valid API key to get full narratives.

**Windows encoding errors in terminal** — set `PYTHONUTF8=1` before running: `set PYTHONUTF8=1 && onboard analyze ...`

**Stage 1 seems slow on first run** — tree-sitter compiles language grammars on first use and caches them. Subsequent runs are faster.
