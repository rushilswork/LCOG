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
- Hotspot warnings (files that change constantly)
- A guided tour that walks through the whole codebase in sequence
- Full-text search

---

## How it works

Four stages run in sequence:

**Stage 1 — Static analysis**
Walks the repo with tree-sitter, extracting classes, functions, and import relationships for Python, JavaScript, TypeScript, C/C++, and Java. Builds a directed dependency graph.

**Stage 2 — Git history**
Reads the git log to compute change frequency per file, co-change coupling (files that always change together), author ownership, and recurring themes from commit messages.

**Stage 3 — Doc collection**
Finds READMEs, architecture docs, Python docstrings, JSDoc, and Doxygen comments and links them to the files they describe.

**Stage 4 — LLM narratives** *(skipped with --skip-llm)*
Sends each module's structure + history + docs to an LLM. Gets back a walkthrough covering what the module does, how it connects to the rest of the system, key design decisions, patterns to follow, and what to watch out for.

---

## Setup

```bash
pip install -e .
pip install mkdocs-material
```

Requires Python 3.10+.

---

## Usage

### Without AI (no API key needed)

Stages 1-3 run. You get the full interactive dependency graph, static Mermaid graph, reading order, hotspot and dead code flags, and all extracted docs. Module narrative pages show stubs instead of LLM prose.

```bash
onboard analyze C:\path\to\repo --skip-llm
```

The guide is written to `<repo>\onboarding-guide` by default. Then serve it:

```bash
cd C:\path\to\repo\onboarding-guide
mkdocs serve
```

Open `http://127.0.0.1:8000`.

---

### With AI (full output)

Stages 1-4 all run. The LLM writes actual narratives for every module, a system overview, and a guided tour.

**Option 1: Groq** (default — fast, free, no card required)

1. Sign up at [console.groq.com](https://console.groq.com) and create an API key.
2. Run:

```bash
set GROQ_API_KEY=gsk_...        # Windows
export GROQ_API_KEY=gsk_...     # Mac/Linux

onboard analyze C:\path\to\repo
cd C:\path\to\repo\onboarding-guide && mkdocs serve
```

**Option 2: Gemini** (also free, no card required)

1. Sign up at [aistudio.google.com](https://aistudio.google.com) and create an API key.
2. Run:

```bash
set GEMINI_API_KEY=AIza...      # Windows
export GEMINI_API_KEY=AIza...   # Mac/Linux

onboard analyze C:\path\to\repo --provider gemini
cd C:\path\to\repo\onboarding-guide && mkdocs serve
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
```

**Providers**

| Provider | Default model            | Free tier | Sign up                   |
|----------|--------------------------|-----------|---------------------------|
| groq     | llama-3.3-70b-versatile  | Yes       | console.groq.com          |
| gemini   | gemini-1.5-flash         | Yes       | aistudio.google.com       |

**Env vars:** `GROQ_API_KEY`, `GEMINI_API_KEY`

To serve an already-generated guide without regenerating:

```bash
onboard serve C:\path\to\repo\onboarding-guide
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
- The guide is excluded from tree-sitter analysis via the `onboarding-guide` ignore rule, so running the tool on its own repo won't recurse.
