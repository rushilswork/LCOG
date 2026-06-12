# Legacy Codebase Onboarding Generator

Turn any legacy codebase into a structured, navigable onboarding guide — the kind a senior engineer would write, but generated automatically.

Existing tools give you call graphs *or* code explanations *or* git analytics. This tool synthesises all three into a human-readable narrative with a suggested reading order, hotspot warnings, dead code flags, and a Mermaid dependency graph — all served as a searchable MkDocs static site.

---

## Features

**4-stage analysis pipeline**

- **Static structure mapping** — tree-sitter AST parsing across Python, JavaScript, TypeScript, C/C++, and Java. Extracts classes, functions, import relationships, and entry points. Builds a module dependency graph.
- **Git history analysis** — identifies hotspots (frequently changed files), co-change coupling (files that always change together), dead code candidates (untouched for 2+ years), ownership by author, and recurring themes from commit messages.
- **Documentation fragment collection** — finds READMEs, architecture docs, Python docstrings, JSDoc, and Doxygen comments and links them to the source files they describe.
- **LLM narrative generation** — feeds each module's structure, history, and docs into an LLM and generates a walkthrough explaining what the module does, how it fits in, key design decisions, patterns to follow, and pitfalls to avoid.

**Generated site**

- System overview page with an interactive Mermaid dependency graph
- Per-module walkthrough pages with hotspot and dead code callouts
- Guided tour — a step-by-step reading sequence from entry point inward
- Full-text search across all narratives (via MkDocs Material)
- Regeneratable as the codebase evolves

**Provider flexibility** — works with Groq (fast, free tier) or Google Gemini (free tier). No Anthropic dependency.

---

## Supported languages

Python, JavaScript, TypeScript, C, C++, Java

---

## Installation

```bash
pip install -e .
pip install mkdocs-material
```

Requires Python 3.10+.

---

## Usage

### With Groq (default)

Get a free API key at [console.groq.com](https://console.groq.com).

```bash
export GROQ_API_KEY=gsk_...
onboard analyze /path/to/your/repo --output ./guide
cd guide && mkdocs serve
```

### With Gemini

Get a free API key at [aistudio.google.com](https://aistudio.google.com).

```bash
export GEMINI_API_KEY=AIza...
onboard analyze /path/to/your/repo --provider gemini --output ./guide
```

### Without any API key (stub mode)

Runs the full static analysis and git history pipeline and generates a structured site with placeholder narratives — useful for previewing the structure or testing.

```bash
onboard analyze /path/to/your/repo --skip-llm --output ./guide
```

### Serve an existing guide

```bash
onboard serve ./guide
```

---

## All options

```
onboard analyze <repo_path> [OPTIONS]

Arguments:
  repo_path     Path to the git repository to analyse

Options:
  -o, --output DIR        Output directory for the MkDocs site  [default: onboarding-guide]
  --provider TEXT         LLM provider: groq or gemini          [default: groq]
  --api-key TEXT          API key (overrides env var)
  --model TEXT            Override the default model
  --site-name TEXT        Title for the generated site
  --max-modules INT       Cap on modules sent to the LLM        [default: 50]
  --skip-llm              Generate stub narratives without calling any LLM
  --serve                 Run mkdocs serve immediately after generation
```

**Default models**

| Provider | Default model              | Notes                        |
|----------|---------------------------|------------------------------|
| groq     | llama-3.3-70b-versatile   | Fast, generous free tier     |
| gemini   | gemini-1.5-flash          | Free tier, 1M token context  |

**Environment variables**

| Variable        | Used by          |
|-----------------|------------------|
| `GROQ_API_KEY`  | `--provider groq`   |
| `GEMINI_API_KEY`| `--provider gemini` |

---

## How it works

```
repo/
  └── source files
        |
        v
  [Stage 1] tree-sitter AST walk
        |-- module dependency graph (networkx DiGraph)
        |-- symbols: classes, functions, entry points
        |
  [Stage 2] git log analysis
        |-- hotspot scores (change frequency)
        |-- co-change coupling matrix
        |-- dead code candidates (>2 years untouched)
        |-- commit message theme clustering
        |
  [Stage 3] doc fragment collection
        |-- READMEs, ARCHITECTURE.md, CHANGELOG.md
        |-- Python docstrings, JSDoc, Doxygen comments
        |
  [Stage 4] LLM narrative generation
        |-- per-module prompts: structure + history + docs
        |-- reading order: topological sort + hotspot tiebreaking
        |-- system overview, guided tour
        |
  [Output] MkDocs static site
        |-- docs/index.md        (overview + Mermaid graph)
        |-- docs/guided_tour.md  (step-by-step tour)
        |-- docs/modules/*.md    (per-module pages)
        |-- mkdocs.yml
```

---

## Project structure

```
onboard/
  cli.py                   Entry point (click CLI)
  stages/
    static_analysis.py     Stage 1: tree-sitter AST parsing
    git_analysis.py        Stage 2: git history metrics
    doc_collector.py       Stage 3: documentation fragments
    narrative_gen.py       Stage 4: LLM narrative generation
  output/
    mkdocs_builder.py      MkDocs site generation
```

---

## Tips

- **Large repos**: use `--max-modules 20` on the first run to keep API costs low, then increase.
- **Cost estimate**: each module uses roughly 1,000–2,000 tokens. At 50 modules that is ~100K tokens — well within Groq and Gemini free tier limits.
- **Regenerating**: re-run the same command with the same `--output` directory to update the guide as the codebase evolves.
- **Reading order**: the guided tour is sorted so you read dependencies before the files that use them, with hotspots prioritised early so you understand the most critical code first.
