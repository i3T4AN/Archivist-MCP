"""Symbol extraction with optional Tree-sitter and deterministic fallbacks."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from tree_sitter_languages import get_parser as _get_ts_parser                
except Exception:                                          
    _get_ts_parser = None


@dataclass(frozen=True)
class Symbol:
    stable_id: str
    name: str
    kind: str
    signature: str
    file_path: str
    start_line: int
    end_line: int
    language: str
    backend: str


@dataclass(frozen=True)
class ExtractionResult:
    symbols: list[Symbol]
    imports: list[str]
    calls: list[str]
    language: str
    backend: str


def detect_language(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in {".py"}:
        return "python"
    if ext in {".ts", ".tsx"}:
        return "typescript"
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if ext in {".go"}:
        return "go"
    return None


def extract_symbols_for_file(path: Path, text: str, project_id: str) -> ExtractionResult:
    language = detect_language(path)
    if language is None:
        return ExtractionResult([], [], [], "unknown", "unsupported")

    tree_sitter = _extract_tree_sitter(path, text, project_id, language)
    if tree_sitter is not None:
        return tree_sitter

    if language == "python":
        return _extract_python(path, text, project_id)
    if language in {"typescript", "javascript"}:
        return _extract_js_ts(path, text, project_id, language)
    if language == "go":
        return _extract_go(path, text, project_id)
    return ExtractionResult([], [], [], language, "unsupported")


def _extract_tree_sitter(path: Path, text: str, project_id: str, language: str) -> ExtractionResult | None:
    if _get_ts_parser is None:
        return None

    lang_name = "javascript" if language == "typescript" else language
    try:
        parser = _get_ts_parser(lang_name)
    except Exception:
        return None

    source = text.encode("utf-8")
    try:
        tree = parser.parse(source)
    except Exception:
        return None

    symbols: list[Symbol] = []
    imports: list[str] = []
    calls: list[str] = []

    def node_text(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")

    def walk(node) -> None:
        ntype = node.type
        if ntype in {"function_definition", "function_declaration", "method_definition"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = node_text(name_node).strip()
                sig = node_text(node).split("{", 1)[0].strip()
                symbols.append(
                    Symbol(
                        stable_id=stable_symbol_id(project_id, str(path), language, "function", name),
                        name=name,
                        kind="function",
                        signature=sig,
                        file_path=str(path),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        language=language,
                        backend="tree-sitter",
                    )
                )
        elif ntype in {"class_definition", "class_declaration"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = node_text(name_node).strip()
                symbols.append(
                    Symbol(
                        stable_id=stable_symbol_id(project_id, str(path), language, "class", name),
                        name=name,
                        kind="class",
                        signature=f"class {name}",
                        file_path=str(path),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        language=language,
                        backend="tree-sitter",
                    )
                )
        elif "import" in ntype:
            text_value = node_text(node)
            imports.extend(re.findall(r"[\"']([^\"']+)[\"']", text_value))
        elif ntype in {"call_expression", "call"}:
            func = node.child_by_field_name("function")
            if func is not None:
                calls.append(node_text(func).split(".")[-1].strip())

        for child in node.children:
            walk(child)

    walk(tree.root_node)
    if not symbols and not imports and not calls:
        return None

    return ExtractionResult(
        symbols=_dedupe_symbols(symbols),
        imports=sorted(set(filter(None, imports))),
        calls=sorted(set(filter(None, calls))),
        language=language,
        backend="tree-sitter",
    )


def stable_symbol_id(project_id: str, file_path: str, language: str, kind: str, name: str) -> str:
    seed = f"{project_id}|{file_path}|{language}|{kind}|{name}".encode("utf-8")
    return "sym_" + hashlib.sha1(seed).hexdigest()[:24]


def _extract_python(path: Path, text: str, project_id: str) -> ExtractionResult:
    symbols: list[Symbol] = []
    imports: list[str] = []
    calls: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ExtractionResult([], [], [], "python", "ast")

    file_path = str(path)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            sig = f"def {node.name}({', '.join(args)})"
            symbols.append(
                Symbol(
                    stable_id=stable_symbol_id(project_id, file_path, "python", "function", node.name),
                    name=node.name,
                    kind="function",
                    signature=sig,
                    file_path=file_path,
                    start_line=getattr(node, "lineno", 1),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                    language="python",
                    backend="ast",
                )
            )
        elif isinstance(node, ast.ClassDef):
            bases = [getattr(b, "id", "?") for b in node.bases]
            sig = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
            symbols.append(
                Symbol(
                    stable_id=stable_symbol_id(project_id, file_path, "python", "class", node.name),
                    name=node.name,
                    kind="class",
                    signature=sig,
                    file_path=file_path,
                    start_line=getattr(node, "lineno", 1),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                    language="python",
                    backend="ast",
                )
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)

    return ExtractionResult(
        symbols=_dedupe_symbols(symbols),
        imports=sorted(set(imports)),
        calls=sorted(set(calls)),
        language="python",
        backend="ast",
    )


def _extract_js_ts(path: Path, text: str, project_id: str, language: str) -> ExtractionResult:
    file_path = str(path)
    symbols: list[Symbol] = []
    imports = re.findall(r"^\s*import\s+.*?from\s+['\"]([^'\"]+)['\"]", text, flags=re.MULTILINE)
    imports.extend(re.findall(r"^\s*import\s+['\"]([^'\"]+)['\"]", text, flags=re.MULTILINE))

    for m in re.finditer(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", text):
        name = m.group(1)
        sig = f"function {name}({m.group(2).strip()})"
        line = text[: m.start()].count("\n") + 1
        symbols.append(
            Symbol(
                stable_id=stable_symbol_id(project_id, file_path, language, "function", name),
                name=name,
                kind="function",
                signature=sig,
                file_path=file_path,
                start_line=line,
                end_line=line,
                language=language,
                backend="regex",
            )
        )

    for m in re.finditer(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)", text):
        name = m.group(1)
        line = text[: m.start()].count("\n") + 1
        symbols.append(
            Symbol(
                stable_id=stable_symbol_id(project_id, file_path, language, "class", name),
                name=name,
                kind="class",
                signature=f"class {name}",
                file_path=file_path,
                start_line=line,
                end_line=line,
                language=language,
                backend="regex",
            )
        )

    calls = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)

    return ExtractionResult(
        symbols=_dedupe_symbols(symbols),
        imports=sorted(set(imports)),
        calls=sorted(set(calls)),
        language=language,
        backend="regex",
    )


def _extract_go(path: Path, text: str, project_id: str) -> ExtractionResult:
    file_path = str(path)
    symbols: list[Symbol] = []

    import_block = re.findall(r"import\s*\((.*?)\)", text, flags=re.DOTALL)
    imports: list[str] = []
    for block in import_block:
        imports.extend(re.findall(r'"([^"]+)"', block))
    imports.extend(re.findall(r'^\s*import\s+"([^"]+)"', text, flags=re.MULTILINE))

    for m in re.finditer(r"\bfunc\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", text):
        name = m.group(1)
        sig = f"func {name}({m.group(2).strip()})"
        line = text[: m.start()].count("\n") + 1
        symbols.append(
            Symbol(
                stable_id=stable_symbol_id(project_id, file_path, "go", "function", name),
                name=name,
                kind="function",
                signature=sig,
                file_path=file_path,
                start_line=line,
                end_line=line,
                language="go",
                backend="regex",
            )
        )

    for m in re.finditer(r"\btype\s+([A-Za-z_][A-Za-z0-9_]*)\s+struct\b", text):
        name = m.group(1)
        line = text[: m.start()].count("\n") + 1
        symbols.append(
            Symbol(
                stable_id=stable_symbol_id(project_id, file_path, "go", "struct", name),
                name=name,
                kind="struct",
                signature=f"type {name} struct",
                file_path=file_path,
                start_line=line,
                end_line=line,
                language="go",
                backend="regex",
            )
        )

    calls = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)

    return ExtractionResult(
        symbols=_dedupe_symbols(symbols),
        imports=sorted(set(imports)),
        calls=sorted(set(calls)),
        language="go",
        backend="regex",
    )


def _dedupe_symbols(symbols: Iterable[Symbol]) -> list[Symbol]:
    seen: set[str] = set()
    out: list[Symbol] = []
    for s in symbols:
        if s.stable_id in seen:
            continue
        seen.add(s.stable_id)
        out.append(s)
    return out
