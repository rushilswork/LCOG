# Legacy Codebase Onboarding Generator

A CLI tool that analyses a git repository and generates a searchable MkDocs site explaining how the codebase works — module by module, in the order you should read it.

---

## What it does

Most codebases have no onboarding docs. The ones that do have them scattered across READMEs, wikis, and tribal knowledge. This tool pulls everything together: the code structure, the git history, the existing comments, and uses an LLM to write a walkthrough for each module.

The output is a static site with:

- A dependency graph of the codebase (Mermaid)
- A suggested reading order — dependencies before the files that use them, hotspots surfaced early
- A per-module page: what it does, how it fits, design decisions, pitfalls
- Dead code callouts (files untouched for 2+ years)
- Hotspot warnings (files that change constantly — high risk to touch)
- A guided tour that walks you through the whole thing in sequence
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

**Stage 4 — LLM narratives**
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

### With AI (full output)

Stages 1–4 all run. The LLM writes actual narratives for every module — what it does, how it connects to the rest of the system, design decisions, pitfalls, a guided tour, and a system overview.

**Option 1: Groq** (default — fast, free, no card required)

1. Sign up at [console.groq.com](https://console.groq.com) and create an API key.
2. Run:

```bash
export GROQ_API_KEY=gsk_...
onboard analyze /path/to/repo --output ./guide
cd guide && mkdocs serve
```

**Option 2: Gemini** (also free, no card required)

1. Sign up at [aistudio.google.com](https://aistudio.google.com) and create an API key.
2. Run:

```bash
export GEMINI_API_KEY=AIza...
onboard analyze /path/to/repo --provider gemini --output ./guide
cd guide && mkdocs serve
```

Open `http://127.0.0.1:8000` to view the site.

---

### Without AI (no API key needed)

Pass `--skip-llm`. Stages 1–3 still run — you get the full dependency graph, reading order, hotspot and dead code flags, and all extracted documentation. The only thing missing is the LLM-written prose for each module.

```bash
onboard analyze /path/to/repo --skip-llm --output ./guide
cd guide && mkdocs serve
```

This is useful for a quick preview of the site structure, or if you just want the graph and analytics without the narratives.

---

## All options

```
onboard analyze <repo_path> [OPTIONS]

  -o, --output DIR        Where to write the site          [default: onboarding-guide]
  --provider TEXT         groq or gemini                   [default: groq]
  --api-key TEXT          API key (or use env var)
  --model TEXT            Override the default model
  --site-name TEXT        Site title
  --max-modules INT       Max modules sent to LLM          [default: 50]
  --skip-llm              Run stages 1-3 only, no LLM
  --serve                 Run mkdocs serve after generation
```

**Providers**

| Provider | Default model             | Sign up                        |
|----------|--------------------------|-------------------------------|
| groq     | llama-3.3-70b-versatile  | console.groq.com              |
| gemini   | gemini-1.5-flash         | aistudio.google.com           |

Both are free with no credit card required.

**Env vars:** `GROQ_API_KEY`, `GEMINI_API_KEY`

To serve an already-generated guide later:

```bash
onboard serve ./guide
```

---

## Notes

- If you hit rate limits on a large repo, use `--max-modules 20` on the first run and increase from there.
- Re-run the same command against the same `--output` directory to refresh the guide as the codebase changes.
