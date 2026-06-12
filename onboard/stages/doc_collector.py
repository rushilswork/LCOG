"""Stage 3: Documentation fragment collection.

Scans the repo for:
- README files (any level)
- Inline comments above classes/functions
- Docstrings (Python, JS JSDoc, Java Javadoc)
- CHANGELOG, CONTRIBUTING, ARCHITECTURE docs

Each fragment is linked to a source file path and optional line range.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DocFragment:
    source_file: str          # relative path
    kind: str                 # "readme" | "docstring" | "comment" | "changelog" | "arch_doc"
    content: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    symbol_name: Optional[str] = None   # function/class the comment belongs to


@dataclass
class DocCorpus:
    fragments: list[DocFragment] = field(default_factory=list)

    def for_file(self, path: str) -> list[DocFragment]:
        return [f for f in self.fragments if f.source_file == path]

    def readmes(self) -> list[DocFragment]:
        return [f for f in self.fragments if f.kind == "readme"]

    def arch_docs(self) -> list[DocFragment]:
        return [f for f in self.fragments if f.kind == "arch_doc"]


# ---------------------------------------------------------------------------
# README / prose docs
# ---------------------------------------------------------------------------

README_NAMES = {
    "readme", "readme.md", "readme.rst", "readme.txt",
    "contributing", "contributing.md",
    "changelog", "changelog.md", "history.md",
    "architecture", "architecture.md", "arch.md", "design.md",
    "overview.md", "hacking.md",
}


def _collect_prose_docs(repo_path: Path, corpus: DocCorpus) -> None:
    for p in repo_path.rglob("*"):
        if p.is_dir():
            continue
        if p.name.lower() in README_NAMES or p.suffix.lower() in (".md", ".rst", ".txt"):
            # Skip very large files
            try:
                size_kb = p.stat().st_size / 1024
                if size_kb > 200:
                    continue
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            name_lower = p.name.lower()
            if any(kw in name_lower for kw in ("arch", "design", "overview")):
                kind = "arch_doc"
            elif any(kw in name_lower for kw in ("change", "history")):
                kind = "changelog"
            else:
                kind = "readme"

            rel = str(p.relative_to(repo_path))
            corpus.fragments.append(DocFragment(
                source_file=rel,
                kind=kind,
                content=content[:4000],   # cap at 4 KB
            ))


# ---------------------------------------------------------------------------
# Python docstrings
# ---------------------------------------------------------------------------

_PY_DOCSTRING_RE = re.compile(
    r'(?:^[ \t]*(?:class|def)\s+(\w+)[^:]*:\s*\n)'   # def/class line → group 1: name
    r'[ \t]*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')',         # triple-quoted docstring
    re.DOTALL | re.MULTILINE,
)


def _collect_python_docstrings(rel_path: str, source: str, corpus: DocCorpus) -> None:
    for m in _PY_DOCSTRING_RE.finditer(source):
        symbol = m.group(1)
        doc = m.group(2) or m.group(3) or ""
        doc = doc.strip()
        if doc:
            line = source[: m.start()].count("\n") + 1
            corpus.fragments.append(DocFragment(
                source_file=rel_path,
                kind="docstring",
                content=doc[:2000],
                start_line=line,
                symbol_name=symbol,
            ))


# ---------------------------------------------------------------------------
# JS/TS JSDoc  /** … */
# ---------------------------------------------------------------------------

_JSDOC_RE = re.compile(r'/\*\*(.*?)\*/', re.DOTALL)
_JSDOC_FUNC_RE = re.compile(r'(?:function\s+(\w+)|(\w+)\s*[=:]\s*(?:async\s+)?function)')


def _collect_jsdoc(rel_path: str, source: str, corpus: DocCorpus) -> None:
    for m in _JSDOC_RE.finditer(source):
        doc = m.group(1).strip()
        if not doc:
            continue
        # Try to find the function name immediately after
        after = source[m.end():m.end() + 200]
        fn_m = _JSDOC_FUNC_RE.search(after)
        symbol = (fn_m.group(1) or fn_m.group(2)) if fn_m else None
        line = source[: m.start()].count("\n") + 1
        corpus.fragments.append(DocFragment(
            source_file=rel_path,
            kind="docstring",
            content=doc[:2000],
            start_line=line,
            symbol_name=symbol,
        ))


# ---------------------------------------------------------------------------
# C++ / Java Doxygen  /** … */  and //! comments
# ---------------------------------------------------------------------------

_DOXYGEN_RE = re.compile(r'/\*[*!](.*?)\*/', re.DOTALL)
_SLASHBANG_RE = re.compile(r'(?:^[ \t]*//[/!][ \t]?(.+)$)+', re.MULTILINE)


def _collect_doxygen(rel_path: str, source: str, corpus: DocCorpus) -> None:
    for m in _DOXYGEN_RE.finditer(source):
        doc = m.group(1).strip()
        if doc:
            line = source[: m.start()].count("\n") + 1
            corpus.fragments.append(DocFragment(
                source_file=rel_path,
                kind="docstring",
                content=doc[:2000],
                start_line=line,
            ))

    for m in _SLASHBANG_RE.finditer(source):
        doc = m.group(0).strip()
        if len(doc) > 20:
            line = source[: m.start()].count("\n") + 1
            corpus.fragments.append(DocFragment(
                source_file=rel_path,
                kind="comment",
                content=doc[:500],
                start_line=line,
            ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SOURCE_EXTS = {".py", ".js", ".mjs", ".ts", ".tsx", ".cpp", ".cc", ".cxx",
               ".c", ".h", ".hpp", ".java"}


def collect_docs(repo_path: Path) -> DocCorpus:
    """Walk *repo_path* and collect all documentation fragments."""
    corpus = DocCorpus()

    # Prose / README files
    _collect_prose_docs(repo_path, corpus)

    ignore_dirs = {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "env", "dist", "build", ".next", "target",
    }

    for p in repo_path.rglob("*"):
        if p.is_dir():
            continue
        if any(part in ignore_dirs for part in p.parts):
            continue
        if p.suffix.lower() not in SOURCE_EXTS:
            continue
        try:
            if p.stat().st_size / 1024 > 300:
                continue
            source = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(p.relative_to(repo_path))
        ext = p.suffix.lower()

        if ext == ".py":
            _collect_python_docstrings(rel, source, corpus)
        elif ext in (".js", ".mjs", ".ts", ".tsx"):
            _collect_jsdoc(rel, source, corpus)
        elif ext in (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".java"):
            _collect_doxygen(rel, source, corpus)

    return corpus
