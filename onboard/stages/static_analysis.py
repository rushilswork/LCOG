"""Stage 1: Static structure mapping via tree-sitter.

Produces a ModuleGraph containing:
- Every source file as a node with its extracted symbols (classes, functions)
- Directed import/include edges between files
- Identified entry points
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx

# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

LANGUAGE_EXTENSIONS: dict[str, list[str]] = {
    "python":     [".py"],
    "javascript": [".js", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
    "cpp":        [".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"],
    "java":       [".java"],
}

EXT_TO_LANG: dict[str, str] = {
    ext: lang
    for lang, exts in LANGUAGE_EXTENSIONS.items()
    for ext in exts
}


def _get_lang_module(language: str):
    """Return the tree-sitter language binding module for *language*."""
    import tree_sitter_python
    import tree_sitter_javascript
    import tree_sitter_cpp
    import tree_sitter_java

    if language == "python":
        return tree_sitter_python.language()
    elif language == "javascript":
        return tree_sitter_javascript.language()
    elif language == "typescript":
        import tree_sitter_typescript
        return tree_sitter_typescript.language_typescript()
    elif language == "cpp":
        return tree_sitter_cpp.language()
    elif language == "java":
        return tree_sitter_java.language()
    raise ValueError(f"Unsupported language: {language}")


_parser_cache: dict[str, object] = {}

def _load_parser(language: str):
    if language in _parser_cache:
        return _parser_cache[language]
    from tree_sitter import Language, Parser
    lang_obj = Language(_get_lang_module(language))
    parser = Parser(lang_obj)
    _parser_cache[language] = parser
    return parser


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    kind: str          # "class" | "function" | "method"
    start_line: int
    end_line: int


@dataclass
class FileNode:
    path: Path
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    is_entry_point: bool = False
    raw_source: str = ""


# ---------------------------------------------------------------------------
# AST node types per language
# ---------------------------------------------------------------------------

IMPORT_NODE_TYPES: dict[str, set[str]] = {
    "python":     {"import_statement", "import_from_statement"},
    "javascript": {"import_declaration", "require_call"},
    "typescript": {"import_declaration", "require_call"},
    "cpp":        {"preproc_include"},
    "java":       {"import_declaration"},
}

FUNCTION_NODE_TYPES: dict[str, set[str]] = {
    "python":     {"function_definition"},
    "javascript": {"function_declaration", "arrow_function", "method_definition"},
    "typescript": {"function_declaration", "arrow_function", "method_definition"},
    "cpp":        {"function_definition"},
    "java":       {"method_declaration"},
}

CLASS_NODE_TYPES: dict[str, set[str]] = {
    "python":     {"class_definition"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration", "interface_declaration"},
    "cpp":        {"class_specifier", "struct_specifier"},
    "java":       {"class_declaration", "interface_declaration"},
}


def _walk(node, target_types: set[str], results: list) -> None:
    """Depth-first walk; collect nodes whose type is in target_types."""
    if node.type in target_types:
        results.append(node)
    for child in node.children:
        _walk(child, target_types, results)


def _node_name(node, language: str) -> Optional[str]:
    """Extract the identifier name from a definition node."""
    # Most languages use a 'name' field
    name_node = node.child_by_field_name("name")
    if name_node and name_node.text:
        return name_node.text.decode("utf-8", errors="replace")

    # C++: class/struct specifier uses 'name' too but sometimes it's nested
    if language == "cpp":
        # Try declarator -> function_declarator -> identifier
        decl = node.child_by_field_name("declarator")
        if decl:
            fn_decl = decl.child_by_field_name("declarator")
            if fn_decl and fn_decl.text:
                return fn_decl.text.decode("utf-8", errors="replace")

    return None


def _extract_symbols(source: bytes, language: str) -> tuple[list[Symbol], list[str]]:
    """Parse *source* and return (symbols, import_strings)."""
    try:
        parser = _load_parser(language)
    except Exception:
        return [], []

    tree = parser.parse(source)
    symbols: list[Symbol] = []
    imports: list[str] = []

    # Collect imports
    import_nodes: list = []
    _walk(tree.root_node, IMPORT_NODE_TYPES.get(language, set()), import_nodes)
    for n in import_nodes:
        if n.text:
            imports.append(n.text.decode("utf-8", errors="replace").strip())

    # Collect functions
    fn_nodes: list = []
    _walk(tree.root_node, FUNCTION_NODE_TYPES.get(language, set()), fn_nodes)
    for n in fn_nodes:
        name = _node_name(n, language)
        if name:
            symbols.append(Symbol(
                name=name,
                kind="function",
                start_line=n.start_point[0] + 1,
                end_line=n.end_point[0] + 1,
            ))

    # Collect classes
    cls_nodes: list = []
    _walk(tree.root_node, CLASS_NODE_TYPES.get(language, set()), cls_nodes)
    for n in cls_nodes:
        name = _node_name(n, language)
        if name:
            symbols.append(Symbol(
                name=name,
                kind="class",
                start_line=n.start_point[0] + 1,
                end_line=n.end_point[0] + 1,
            ))

    return symbols, imports


# ---------------------------------------------------------------------------
# Entry-point heuristics (string-based — no AST needed)
# ---------------------------------------------------------------------------

ENTRY_PATTERNS = {
    "python":     ['__name__ == "__main__"', "def main(", "app.run(", "uvicorn.run(", "asyncio.run("],
    "javascript": ["app.listen(", "server.listen(", "createServer(", "express()"],
    "typescript": ["app.listen(", "server.listen(", "bootstrap("],
    "cpp":        ["int main(", "void main("],
    "java":       ["public static void main("],
}


def _is_entry_point(source: str, language: str) -> bool:
    return any(p in source for p in ENTRY_PATTERNS.get(language, []))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_repo(repo_path: Path, max_file_kb: int = 500) -> nx.DiGraph:
    """Walk *repo_path* and build a directed module dependency graph."""
    graph: nx.DiGraph = nx.DiGraph()
    file_nodes: dict[str, FileNode] = {}

    ignore_dirs = {
        ".git", ".svn", "__pycache__", "node_modules", ".venv", "venv",
        "env", "dist", "build", ".next", "target", ".gradle",
    }

    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]

        for filename in filenames:
            ext = Path(filename).suffix.lower()
            language = EXT_TO_LANG.get(ext)
            if not language:
                continue

            abs_path = Path(dirpath) / filename
            rel_path = abs_path.relative_to(repo_path)

            try:
                if abs_path.stat().st_size / 1024 > max_file_kb:
                    continue
                source_bytes = abs_path.read_bytes()
                source_str = source_bytes.decode("utf-8", errors="replace")
            except OSError:
                continue

            symbols, imports = _extract_symbols(source_bytes, language)
            is_entry = _is_entry_point(source_str, language)

            node = FileNode(
                path=rel_path,
                language=language,
                symbols=symbols,
                imports=imports,
                is_entry_point=is_entry,
                raw_source=source_str,
            )
            key = str(rel_path)
            file_nodes[key] = node
            graph.add_node(key,
                language=language,
                symbols=symbols,
                imports=imports,
                is_entry_point=is_entry,
                label=rel_path.stem,
            )

    # Resolve import strings -> edges
    path_index = _build_path_index(file_nodes)
    for src_key, node in file_nodes.items():
        for imp in node.imports:
            resolved = _resolve_import(imp, node, path_index)
            if resolved and resolved != src_key:
                graph.add_edge(src_key, resolved, label="imports")

    return graph


def _build_path_index(nodes: dict[str, FileNode]) -> dict[str, str]:
    """Map stem / module names / dotted paths -> node keys."""
    index: dict[str, str] = {}
    for key, node in nodes.items():
        # Direct key
        index[key] = key
        # Stem: "logger" -> "utils/logger.py"
        index[node.path.stem] = key
        # Dotted: "utils.logger" -> "utils/logger.py"
        dotted = str(node.path.with_suffix("")).replace(os.sep, ".").replace("/", ".")
        index[dotted] = key
        # Package dir: if __init__, also register parent dir name
        if node.path.stem == "__init__" and node.path.parent != Path("."):
            pkg_name = node.path.parent.name
            index[pkg_name] = key
    return index


def _resolve_import(imp_text: str, node: FileNode, index: dict[str, str]) -> Optional[str]:
    """Best-effort resolution of an import string to a node key."""
    # Strip quotes and common keywords to get module/path tokens
    clean = imp_text.replace('"', " ").replace("'", " ").replace("<", " ").replace(">", " ")
    tokens = clean.split()

    candidates: list[str] = []

    if node.language == "python":
        # "from utils.logger import foo"  -> try "utils.logger", "utils", "logger"
        # "import os.path"                -> try "os.path", "os", "path"
        for tok in tokens:
            if tok in ("import", "from", "as"):
                continue
            # For dotted names, add the full dotted path and each component
            if "." in tok:
                candidates.append(tok)
                candidates.extend(tok.split("."))
            else:
                candidates.append(tok)

    elif node.language in ("javascript", "typescript"):
        # 'import x from "./utils/helpers"' -> try "helpers", "utils/helpers"
        for tok in tokens:
            if tok.startswith("."):
                # relative: "./utils/helpers" -> stem is "helpers"
                candidates.append(Path(tok).stem)
                # also try without leading ./
                stripped = tok.lstrip("./")
                candidates.append(stripped)
                candidates.append(Path(stripped).stem)
            elif not tok.startswith("@") and tok not in ("import", "from", "default", "export"):
                candidates.append(tok)

    elif node.language == "cpp":
        for tok in tokens:
            if tok in ("#include",):
                continue
            candidates.append(Path(tok).stem)
            candidates.append(tok)

    else:
        # Java: try last component of dotted import
        for tok in tokens:
            if tok in ("import", "static", ";"):
                continue
            if "." in tok:
                candidates.append(tok)
                candidates.append(tok.split(".")[-1])
            else:
                candidates.append(tok)

    for cand in candidates:
        if cand in index:
            return index[cand]
    return None


def summarize_graph(graph: nx.DiGraph) -> dict:
    languages: dict[str, int] = {}
    entry_points: list[str] = []
    for node, data in graph.nodes(data=True):
        lang = data.get("language", "unknown")
        languages[lang] = languages.get(lang, 0) + 1
        if data.get("is_entry_point"):
            entry_points.append(node)

    try:
        cycles = list(nx.simple_cycles(graph))
    except Exception:
        cycles = []

    return {
        "total_files": graph.number_of_nodes(),
        "total_edges": graph.number_of_edges(),
        "languages": languages,
        "entry_points": entry_points,
        "circular_deps": len(cycles),
    }
