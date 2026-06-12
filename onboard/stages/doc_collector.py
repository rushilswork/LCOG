"""Stage 3: Documentation fragment collection.

Scans the repo for:
- README files (any level)
- Inline comments above classes/functions
- Docstrings (Python, JS JSDoc, Java Javadoc)
- CHANGELOG, CONTRIBUTING, ARCHITECTURE docs

Each fragment is linked to a source file path and optional line range.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Shared ignore set
# ---------------------------------------------------------------------------

_IGNORE_DIRS = {
    ".git", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "env", "dist", "build", ".next", "target", ".gradle",
    "onboarding-guide",  # skip previously generated output
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DocFragment:
    source_file: str          # relative path (forward slashes)
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

_PROSE_EXTS = {".md", ".rst"}   # .txt excluded — too many false positives


def _collect_prose_docs(repo_path: Path, corpus: DocCorpus) -> None:
    """Walk repo, collecting README / architecture / changelog prose."""
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]

        for filename in filenames:
            name_lower = filename.lower()
            suffix = Path(filename).suffix.lower()
            if name_lower not in README_NAMES and suffix not in _PROSE_EXTS:
                continue

            abs_path = Path(dirpath) / filename
            try:
                size_kb = abs_path.stat().st_size / 1024
                if size_kb > 200:
                    continue
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if any(kw in name_lower for kw in ("arch", "design", "overview")):
                kind = "arch_doc"
            elif any(kw in name_lower for kw in ("change", "history")):
                kind = "changelog"
            else:
                kind = "readme"

            try:
                rel = abs_path.relative_to(repo_path).as_posix()
            except ValueError:
                continue

            corpus.fragments.append(DocFragment(
                source_file=rel,
                kind=kind,
                content=content[:4000],
            ))


# ---------------------------------------------------------------------------
# Python docstrings
# ---------------------------------------------------------------------------

_PY_DOCSTRING_RE = re.compile(
    r'(?:^[ \t]*(?:class|def)\s+(\w+)[^:]*:\s*\n)'
    r'[ \t]*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')',
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


def _process_source_file(abs_path: Path, rel: str) -> list[DocFragment]:
    """Extract doc fragments from one source file. Thread-safe (no shared state)."""
    try:
        if abs_path.stat().st_size / 1024 > 300:
            return []
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    local = DocCorpus()
    ext = abs_path.suffix.lower()
    if ext == ".py":
        _collect_python_docstrings(rel, source, local)
    elif ext in (".js", ".mjs", ".ts", ".tsx"):
        _collect_jsdoc(rel, source, local)
    elif ext in (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".java"):
        _collect_doxygen(rel, source, local)
    return local.fragments


def collect_docs(repo_path: Path) -> DocCorpus:
    """Walk *repo_path* and collect all documentation fragments."""
    corpus = DocCorpus()

    # Prose / README files — sequential (few, large files)
    _collect_prose_docs(repo_path, corpus)

    # Source-file inline docs — collect candidates, then process in parallel
    candidates: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]

        for filename in filenames:
            if Path(filename).suffix.lower() not in SOURCE_EXTS:
                continue
            abs_path = Path(dirpath) / filename
            try:
                rel = abs_path.relative_to(repo_path).as_posix()
            except ValueError:
                continue
            candidates.append((abs_path, rel))

    num_workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(_process_source_file, abs_path, rel)
            for abs_path, rel in candidates
        ]
        for future in futures:
            corpus.fragments.extend(future.result())

    return corpus
