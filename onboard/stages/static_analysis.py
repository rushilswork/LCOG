"""Stage 1: Static structure mapping via tree-sitter."""
from __future__ import annotations
import itertools, os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import networkx as nx

LANGUAGE_EXTENSIONS: dict[str, list[str]] = {
    "python":     [".py"],
    "javascript": [".js", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
    "cpp":        [".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"],
    "java":       [".java"],
}
EXT_TO_LANG: dict[str, str] = {
    ext: lang for lang, exts in LANGUAGE_EXTENSIONS.items() for ext in exts
}

def _get_lang_module(language: str):
    """Return the tree-sitter language binding for *language* (lazy import)."""
    try:
        if language == "python":
            import tree_sitter_python; return tree_sitter_python.language()
        elif language == "javascript":
            import tree_sitter_javascript; return tree_sitter_javascript.language()
        elif language == "typescript":
            import tree_sitter_typescript; return tree_sitter_typescript.language_typescript()
        elif language == "cpp":
            import tree_sitter_cpp; return tree_sitter_cpp.language()
        elif language == "java":
            import tree_sitter_java; return tree_sitter_java.language()
    except ImportError as e:
        raise ImportError(
            f"tree-sitter binding for '{language}' not installed: {e}. "
            f"Run: pip install tree-sitter-{language}"
        ) from e
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

@dataclass
class Symbol:
    name: str
    kind: str
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

def _walk(root_node, target_types: set[str], results: list) -> None:
    """Iterative DFS — avoids Python recursion limit on deep ASTs."""
    stack = [root_node]
    while stack:
        node = stack.pop()
        if node.type in target_types:
            results.append(node)
        for child in reversed(node.children):
            stack.append(child)

def _node_name(node, language: str) -> Optional[str]:
    name_node = node.child_by_field_name("name")
    if name_node and name_node.text:
        return name_node.text.decode("utf-8", errors="replace")
    if language == "cpp":
        decl = node.child_by_field_name("declarator")
        if decl:
            fn_decl = decl.child_by_field_name("declarator")
            if fn_decl and fn_decl.text:
                return fn_decl.text.decode("utf-8", errors="replace")
    return None

def _extract_symbols(source: bytes, language: str) -> tuple[list[Symbol], list[str]]:
    try:
        parser = _load_parser(language)
    except Exception:
        return [], []
    tree = parser.parse(source)
    symbols: list[Symbol] = []
    imports: list[str] = []
    import_nodes: list = []
    _walk(tree.root_node, IMPORT_NODE_TYPES.get(language, set()), import_nodes)
    for n in import_nodes:
        if n.text:
            imports.append(n.text.decode("utf-8", errors="replace").strip())
    fn_nodes: list = []
    _walk(tree.root_node, FUNCTION_NODE_TYPES.get(language, set()), fn_nodes)
    for n in fn_nodes:
        name = _node_name(n, language)
        if name:
            symbols.append(Symbol(name=name, kind="function",
                start_line=n.start_point[0]+1, end_line=n.end_point[0]+1))
    cls_nodes: list = []
    _walk(tree.root_node, CLASS_NODE_TYPES.get(language, set()), cls_nodes)
    for n in cls_nodes:
        name = _node_name(n, language)
        if name:
            symbols.append(Symbol(name=name, kind="class",
                start_line=n.start_point[0]+1, end_line=n.end_point[0]+1))
    return symbols, imports

ENTRY_PATTERNS = {
    "python":     ['__name__ == "__main__"', "def main(", "app.run(", "uvicorn.run(", "asyncio.run("],
    "javascript": ["app.listen(", "server.listen(", "createServer(", "express()"],
    "typescript": ["app.listen(", "server.listen(", "bootstrap("],
    "cpp":        ["int main(", "void main("],
    "java":       ["public static void main("],
}

def _is_entry_point(source: str, language: str) -> bool:
    return any(p in source for p in ENTRY_PATTERNS.get(language, []))

def analyze_repo(repo_path: Path, max_file_kb: int = 500) -> nx.DiGraph:
    """Walk *repo_path* and build a directed module dependency graph."""
    graph: nx.DiGraph = nx.DiGraph()
    file_nodes: dict[str, FileNode] = {}
    ignore_dirs = {
        ".git", ".svn", "__pycache__", "node_modules", ".venv", "venv",
        "env", "dist", "build", ".next", "target", ".gradle", "onboarding-guide",
    }
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for filename in filenames:
            ext = Path(filename).suffix.lower()
            language = EXT_TO_LANG.get(ext)
            if not language:
                continue
            abs_path = Path(dirpath) / filename
            try:
                rel_path = abs_path.relative_to(repo_path)
            except ValueError:
                continue
            try:
                if abs_path.stat().st_size / 1024 > max_file_kb:
                    continue
                source_bytes = abs_path.read_bytes()
                source_str = source_bytes.decode("utf-8", errors="replace")
            except OSError:
                continue
            symbols, imports = _extract_symbols(source_bytes, language)
            is_entry = _is_entry_point(source_str, language)
            node = FileNode(path=rel_path, language=language, symbols=symbols,
                imports=imports, is_entry_point=is_entry, raw_source=source_str)
            key = rel_path.as_posix()  # always forward slashes — matches gitpython
            file_nodes[key] = node
            graph.add_node(key, language=language, symbols=symbols, imports=imports,
                is_entry_point=is_entry, label=rel_path.stem)
    path_index = _build_path_index(file_nodes)
    for src_key, node in file_nodes.items():
        for imp in node.imports:
            resolved = _resolve_import(imp, node, path_index)
            if resolved and resolved != src_key:
                graph.add_edge(src_key, resolved, label="imports")
    return graph

def _build_path_index(nodes: dict[str, FileNode]) -> dict[str, str]:
    index: dict[str, str] = {}
    for key, node in nodes.items():
        index[key] = key
        index[node.path.stem] = key
        dotted = node.path.with_suffix("").as_posix().replace("/", ".")
        index[dotted] = key
        if node.path.stem == "__init__" and node.path.parent != Path("."):
            index[node.path.parent.name] = key
    return index

def _resolve_import(imp_text: str, node: FileNode, index: dict[str, str]) -> Optional[str]:
    clean = imp_text.replace('"', " ").replace("'", " ").replace("<", " ").replace(">", " ")
    tokens = clean.split()
    candidates: list[str] = []
    if node.language == "python":
        for tok in tokens:
            if tok in ("import", "from", "as"):
                continue
            if "." in tok:
                candidates.append(tok)
                candidates.extend(tok.split("."))
            else:
                candidates.append(tok)
    elif node.language in ("javascript", "typescript"):
        for tok in tokens:
            if tok.startswith("."):
                candidates.append(Path(tok).stem)
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
    else:  # java
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
        cycles = list(itertools.islice(nx.simple_cycles(graph), 200))
    except Exception:
        cycles = []
    return {
        "total_files": graph.number_of_nodes(),
        "total_edges": graph.number_of_edges(),
        "languages": languages,
        "entry_points": entry_points,
        "circular_deps": len(cycles),
    }
