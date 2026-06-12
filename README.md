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

```bash
# Groq (default)
export GROQ_API_KEY=gsk_...
onboard analyze /path/to/repo --output ./guide
cd guide && mkdocs serve

# Gemini
export GEMINI_API_KEY=AIza...
onboard analyze /path/to/repo --provider gemini --output ./guide

# No API key — runs stages 1-3 and generates stub narratives
onboard analyze /path/to/repo --skip-llm --output ./guide
```

---

## Options

```
onboard analyze <repo_path> [OPTIONS]

  -o, --output DIR        Where to write the site          [default: onboarding-guide]
  --provider TEXT         groq or gemini                   [default: groq]
  --api-key TEXT          API key (or use env var)
  --model TEXT            Override the default model
  --site-name TEXT        Site title
  --max-modules INT       Max modules sent to LLM          [default: 50]
  --skip-llm              Skip LLM stage, stub narratives only
  --serve                 Run mkdocs serve after generation
```

**Providers and defaults**

| Provider | Default model             | Free tier |
|----------|--------------------------|-----------|
| groq     | llama-3.3-70b-versatile  | Free, no card required |
| gemini   | gemini-1.5-flash         | Free, no card required |

**Env vars:** `GROQ_API_KEY`, `GEMINI_API_KEY`

---

## Notes

- Use `--max-modules 20` on a first pass against a large repo if you hit rate limits on the free tier.
- At 50 modules, expect roughly 75-100K tokens total — within both providers' free tiers.
- Re-run with the same `--output` directory to refresh the guide as the codebase changes.
