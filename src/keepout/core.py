"""Core module for KeepOut: Smart Syntax Locking & Deterministic PBT with Counterexample Caching."""

import ast
import copy
from dataclasses import dataclass, field
import functools
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import textwrap
from typing import Dict, Any, List, Optional, Set, Tuple


# =====================================================================
# 1. Comment Parser & Directive Data Structures
# =====================================================================

@dataclass
class LockBlock:
    file_path: Path
    lock_index: int
    mode: str  # "strict", "ast", "pbt", or "logic"
    content: str
    start_line: int
    end_line: int
    target_symbol: Optional[str] = None
    unlock_reason: Optional[str] = None
    # The key within the file: the lock's name="..." if it has one,
    # otherwise lock_<n> counting only the unnamed locks before it.
    lock_id: str = ""


_COMMENT_PREFIX = r'(?:#|//|/\*|;|⍝)'
# Directives must open a comment line. Matched anywhere in a line, a string
# such as print("# [LOCK]") opened a lock.
LOCK_START_PATTERN = re.compile(
    rf'^\s*(?P<comment_prefix>{_COMMENT_PREFIX})\s*\[LOCK(?:\s*:\s*(?P<mode>[A-Za-z]+))?(?P<attrs>[^\]]*)\](?P<comment_suffix>.*)'
)
LOCK_END_PATTERN = re.compile(rf'^\s*{_COMMENT_PREFIX}\s*\[/LOCK\]')
# Only a comment line unlocks. Matched anywhere, UNLOCK_REASON: inside a
# string literal unlocked the region.
UNLOCK_REASON_PATTERN = re.compile(rf'^\s*(?:{_COMMENT_PREFIX}|\*)\s*UNLOCK_REASON:\s*(?P<reason>.+)')
DIRECTIVE_UNLOCK_PATTERN = re.compile(r'UNLOCK_REASON:\s*(?P<reason>.+)')
ATTR_PATTERN = re.compile(r'(?P<key>\w+)\s*=\s*(?:"(?P<dq>[^"]*)"|\'(?P<sq>[^\']*)\')')
KNOWN_ATTRS = {"name", "target_symbol"}
LOCK_NAME_PATTERN = re.compile(r'[A-Za-z0-9_.\-]+')
FUNC_DECL_PATTERN = re.compile(
    r'^\s*(?:def|fn|function|procedure|int|void|float|double|char|long|unsigned|struct|class|auto|module|contract)\s+(?P<func_name>[a-zA-Z_][a-zA-Z0-9_]*)\b'
)


class LockParserError(Exception):
    """Exception raised when lock directive comments are invalid."""
    pass


def _clean_reason(reason: str) -> str:
    reason = reason.strip()
    return reason[:-2].rstrip() if reason.endswith("*/") else reason


def _parse_directive(match: "re.Match[str]", file_path: Path, idx: int) -> Tuple[str, Dict[str, str]]:
    """The mode and attributes of a [LOCK ...] directive.

    A mode that is not one of the four, or text that is not key="value", is
    an error. The mode used to be matched in lowercase only and anything
    else fell into the attributes, so `[LOCK: STRICT]` or `[LOCK: strct]`
    silently became an ast lock.
    """
    mode = (match.group("mode") or "ast").lower()
    if mode not in ("strict", "ast", "pbt", "logic"):
        raise LockParserError(
            f"Unknown lock mode '{match.group('mode')}' at {file_path}:{idx}. "
            f"Use strict, ast, pbt or logic."
        )
    attrs_text = match.group("attrs") or ""
    attrs: Dict[str, str] = {}
    for attr in ATTR_PATTERN.finditer(attrs_text):
        key = attr.group("key")
        if key not in KNOWN_ATTRS:
            raise LockParserError(
                f"Unknown attribute '{key}' in [LOCK] at {file_path}:{idx}. "
                f"Known attributes: {', '.join(sorted(KNOWN_ATTRS))}."
            )
        attrs[key] = attr.group("dq") if attr.group("dq") is not None else attr.group("sq")
    leftover = ATTR_PATTERN.sub("", attrs_text).strip()
    if leftover:
        raise LockParserError(f"Unrecognised text '{leftover}' in [LOCK] at {file_path}:{idx}.")
    name = attrs.get("name")
    if name is not None and not LOCK_NAME_PATTERN.fullmatch(name):
        raise LockParserError(
            f"Lock name '{name}' at {file_path}:{idx} may use only letters, digits, '_', '.' and '-'."
        )
    return mode, attrs


def parse_file_locks(file_path: Path) -> List[LockBlock]:
    """Scans a file for [LOCK] ... [/LOCK] directives across any language."""
    if not file_path.exists():
        return []

    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []

    lines = raw_text.replace("\r\n", "\n").split("\n")
    blocks: List[LockBlock] = []
    seen_ids: Dict[str, int] = {}

    in_lock = False
    current_mode = "ast"  # Default to Smart Syntax Lock (AST)
    current_attrs: Dict[str, str] = {}
    current_unlock_reason = None
    current_start_line = 0
    current_lines: List[str] = []
    lock_counter = 0
    unnamed_counter = 0

    for idx, line in enumerate(lines, start=1):
        start_match = LOCK_START_PATTERN.search(line)
        end_match = LOCK_END_PATTERN.search(line)

        if start_match:
            if in_lock:
                raise LockParserError(
                    f"Nested [LOCK] directive found at {file_path}:{idx}. "
                    f"Lock opened at line {current_start_line} must be closed with [/LOCK] first."
                )
            in_lock = True
            current_mode, current_attrs = _parse_directive(start_match, file_path, idx)
            current_start_line = idx
            current_lines = []
            reason_match = DIRECTIVE_UNLOCK_PATTERN.search(start_match.group("comment_suffix") or "")
            current_unlock_reason = _clean_reason(reason_match.group("reason")) if reason_match else None

        elif end_match:
            if not in_lock:
                raise LockParserError(
                    f"Unmatched [/LOCK] directive found at {file_path}:{idx} without prior [LOCK]."
                )

            content_str = "\n".join(current_lines)

            target_symbol = current_attrs.get("target_symbol")
            if not target_symbol and current_lines:
                func_match = FUNC_DECL_PATTERN.search(current_lines[0])
                if func_match:
                    target_symbol = func_match.group("func_name")

            # Keyed by position alone, adding a lock above shifted the key of
            # every lock below it onto another lock's record.
            if "name" in current_attrs:
                lock_id = current_attrs["name"]
            else:
                lock_id = f"lock_{unnamed_counter}"
                unnamed_counter += 1
            if lock_id in seen_ids:
                raise LockParserError(
                    f"Duplicate lock id '{lock_id}' at {file_path}:{current_start_line} "
                    f"(also used by the lock at line {seen_ids[lock_id]}). Give each lock a distinct name."
                )
            seen_ids[lock_id] = current_start_line

            block = LockBlock(
                file_path=file_path,
                lock_index=lock_counter,
                mode=current_mode,
                content=content_str,
                start_line=current_start_line,
                end_line=idx,
                target_symbol=target_symbol,
                unlock_reason=current_unlock_reason,
                lock_id=lock_id,
            )
            blocks.append(block)
            lock_counter += 1
            in_lock = False

        elif in_lock:
            reason_match = UNLOCK_REASON_PATTERN.search(line)
            if reason_match:
                current_unlock_reason = _clean_reason(reason_match.group("reason"))
            current_lines.append(line)

    if in_lock:
        raise LockParserError(
            f"Unclosed [LOCK] directive at {file_path}:{current_start_line}. "
            f"Missing [/LOCK] tag before end of file."
        )

    return blocks


# =====================================================================
# 2. Database Storage Manager (keepout.json)
# =====================================================================

DEFAULT_DB_NAME = "keepout.json"
# 2.1.0: AST hashes keep every name visible outside a function (callees,
# builtins, globals, what the region defines at module level); indented
# Python regions are parsed after removing their common indentation; other
# languages get a token hash that keeps preprocessor lines, strings and
# meaningful whitespace; named locks are keyed by name. Records written by
# 2.0.0 are not comparable with these.
VERSION = "2.1.0"

KNOWN_MODES = {"strict", "ast", "default", "pbt", "logic"}
MODE_ALIASES = {"default": "ast", "logic": "pbt"}


def canonical_mode(mode: str) -> str:
    """Maps a mode to the one it is an alias of ('logic' -> 'pbt')."""
    return MODE_ALIASES.get(mode, mode)


def compute_strict_hash(content: str) -> str:
    """Computes SHA-256 hash of exact normalized code content."""
    normalized = content.replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


@dataclass
class _Scope:
    kind: str  # "function" (also lambdas and comprehensions) or "class"
    bound: Set[str]
    declared_global: Set[str]
    renamed: Dict[str, str] = field(default_factory=dict)


def _all_args(args: ast.arguments) -> List[ast.arg]:
    every = args.posonlyargs + args.args + args.kwonlyargs
    return every + [a for a in (args.vararg, args.kwarg) if a is not None]


def _evaluated_where_defined(node: ast.AST) -> List[ast.AST]:
    """The parts of a def, lambda or class that run in the enclosing scope."""
    parts: List[ast.AST] = list(getattr(node, "decorator_list", []))
    if isinstance(node, ast.ClassDef):
        return parts + node.bases + [k.value for k in node.keywords]
    args = node.args
    parts += args.defaults + [d for d in args.kw_defaults if d is not None]
    if not isinstance(node, ast.Lambda):
        parts += [a.annotation for a in _all_args(args) if a.annotation is not None]
        if node.returns is not None:
            parts.append(node.returns)
    return parts


def _scope_bindings(body: List[ast.AST], params: List[str]) -> Tuple[Set[str], Set[str]]:
    """Names one scope binds, and the names it declares global.

    Python's rule: a name assigned anywhere in a body is local to the whole
    body. Nested functions, lambdas, classes and comprehensions are scopes of
    their own -- only their names, and what is evaluated where they are
    defined, belong to this one.
    """
    bound: Set[str] = set(params)
    declared_global: Set[str] = set()
    declared_nonlocal: Set[str] = set()
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            stack.extend(_evaluated_where_defined(node))
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(_evaluated_where_defined(node))
            continue
        if isinstance(node, _COMPREHENSIONS):
            # The targets belong to the comprehension; a walrus inside it
            # binds here, and the first iterable is evaluated here.
            bound.update(n.target.id for n in ast.walk(node) if isinstance(n, ast.NamedExpr))
            stack.append(node.generators[0].iter)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            declared_nonlocal.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        stack.extend(ast.iter_child_nodes(node))
    return bound - declared_global - declared_nonlocal, declared_global


class AstVariableNormalizer(ast.NodeVisitor):
    """Alpha-renames, in place, the names that are local to a function in the region.

    A parameter or local variable can be renamed without changing what the
    code does, so those become VAR_0, VAR_1, ... in order of first use. Every
    other name is kept as written: builtins, imports, module globals, callees
    defined outside the region, and what the region itself defines at module
    level (its functions, classes and constants), because code outside the
    region refers to those by name. Renaming them too made `max(a, b)` ->
    `min(a, b)` and `verify_signature(u)` -> `always_true(u)` hash the same.
    """

    def __init__(self):
        # Innermost scope last. Module level is the empty stack: nothing
        # bound there is renamed.
        self.scopes: List[_Scope] = []
        self.counter = 0

    def _resolve(self, name: str) -> str:
        for depth, scope in enumerate(reversed(self.scopes)):
            if name in scope.declared_global:
                return name
            if scope.kind == "class":
                # A class body's names are attributes, and the functions
                # nested in it cannot see them.
                if depth == 0 and name in scope.bound:
                    return name
                continue
            if name in scope.bound:
                if name not in scope.renamed:
                    scope.renamed[name] = f"VAR_{self.counter}"
                    self.counter += 1
                return scope.renamed[name]
        return name

    def _visit_all(self, nodes) -> None:
        for node in nodes:
            self.visit(node)

    def _visit_function(self, node) -> None:
        self._visit_all(_evaluated_where_defined(node))
        if not isinstance(node, ast.Lambda):
            node.name = self._resolve(node.name)
        body = [node.body] if isinstance(node, ast.Lambda) else node.body
        params = _all_args(node.args)
        bound, declared_global = _scope_bindings(body, [a.arg for a in params])
        self.scopes.append(_Scope("function", bound, declared_global))
        for param in params:
            param.arg = self._resolve(param.arg)
        self._visit_all(body)
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_function

    def visit_ClassDef(self, node) -> None:
        self._visit_all(_evaluated_where_defined(node))
        node.name = self._resolve(node.name)
        bound, declared_global = _scope_bindings(node.body, [])
        self.scopes.append(_Scope("class", bound, declared_global))
        self._visit_all(node.body)
        self.scopes.pop()

    def _visit_comprehension(self, node) -> None:
        first, *rest = node.generators
        self.visit(first.iter)
        targets = {n.id for g in node.generators for n in ast.walk(g.target) if isinstance(n, ast.Name)}
        self.scopes.append(_Scope("function", targets, set()))
        self.visit(first.target)
        self._visit_all(first.ifs)
        for gen in rest:
            self.visit(gen.target)
            self.visit(gen.iter)
            self._visit_all(gen.ifs)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.scopes.pop()

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_Name(self, node) -> None:
        node.id = self._resolve(node.id)

    def visit_ExceptHandler(self, node) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            node.name = self._resolve(node.name)
        self._visit_all(node.body)

    def visit_alias(self, node) -> None:
        if node.asname:
            node.asname = self._resolve(node.asname)

    def visit_Nonlocal(self, node) -> None:
        node.names = [self._resolve(n) for n in node.names]

    def visit_MatchAs(self, node) -> None:
        if node.pattern is not None:
            self.visit(node.pattern)
        if node.name:
            node.name = self._resolve(node.name)

    def visit_MatchStar(self, node) -> None:
        if node.name:
            node.name = self._resolve(node.name)

    def visit_MatchMapping(self, node) -> None:
        self.generic_visit(node)
        if node.rest:
            node.rest = self._resolve(node.rest)


def _dedent_region(content: str) -> str:
    """Removes the indentation every line of a region shares.

    A region cut from inside a function or class is indented, and ast.parse
    rejects it as it stands. Stripping the first line only (what .strip()
    did) left the rest misaligned, so the region fell back to the token hash,
    which ignores indentation: moving a statement into or out of an `if`
    hashed the same.
    """
    lines = content.replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return textwrap.dedent("\n".join(lines))


def _mojo_to_python(code: str) -> str:
    """Rewrites Mojo `fn` signatures into Python so ast can read them.

    Lossy -- it drops every `name: Capitalized` pair, which in Python is also
    a dict entry or a slice -- so it is applied to Mojo regions only.
    """
    if "fn " not in code and "->" not in code:
        return code
    code = re.sub(r'\bfn\s+([a-zA-Z0-9_]+)\s*\((.*?)\)\s*(?:->\s*[^:]+)?\s*:', r'def \1(\2):', code)
    return re.sub(r'\b([a-zA-Z0-9_]+)\s*:\s*(?:Int|Float|String|Bool|[A-Z][a-zA-Z0-9_]*)\b', r'\1', code)


def _normalized_ast_digest(source: str) -> Optional[str]:
    try:
        tree = ast.parse(_dedent_region(source))
    except (SyntaxError, ValueError):
        return None
    AstVariableNormalizer().visit(tree)
    return hashlib.sha256(ast.dump(tree).encode("utf-8")).hexdigest()


def python_ast_hash(content: str, lang: str) -> Optional[str]:
    """The AST hash of a Python region, or None when it is not Python or does not parse.

    Only a hash from here means "the same code up to comments, formatting and
    local renames". Everything else gets the token hash below, which knows
    comments, strings and which whitespace matters, but nothing about
    meaning: a rename there is a change.
    """
    if lang.lower().lstrip(".") not in ("py", "python"):
        return None
    return _normalized_ast_digest(content)


@dataclass(frozen=True)
class _TokenRules:
    line_comments: Tuple[str, ...] = ("//",)
    block_comments: Tuple[Tuple[str, str], ...] = (("/*", "*/"),)
    quotes: Tuple[str, ...] = ('"', "'")
    newlines: str = "never"  # "never", "always", or "directives" (C preprocessor lines)
    spaces: bool = False  # whitespace between tokens on a line matters (shell)
    indentation: bool = False  # leading whitespace matters (Python-like)


_C_LIKE = _TokenRules(newlines="directives")
_C_LIKE_BACKTICK = _TokenRules(quotes=('"', "'", "`"), newlines="directives")
_PYTHON_LIKE = _TokenRules(line_comments=("#",), block_comments=(), quotes=('"""', "'''", '"', "'"),
                           newlines="always", indentation=True)
# '#' is a comment only where the language says so. The old fallback dropped
# everything from '#' in every language: C preprocessor lines, Rust
# attributes, JS private fields and ARM immediates all hashed as nothing.
_TOKEN_RULES: Dict[str, _TokenRules] = {
    **{ext: _C_LIKE for ext in ("c", "cpp", "cc", "cxx", "h", "hpp", "rs", "java", "sol", "v", "sv", "sac")},
    **{ext: _C_LIKE_BACKTICK for ext in ("js", "ts", "jsx", "tsx", "go")},
    **{ext: _PYTHON_LIKE for ext in ("py", "python", "mojo", "🔥")},
    "sh": _TokenRules(line_comments=("#",), block_comments=(), newlines="always", spaces=True),
    "bash": _TokenRules(line_comments=("#",), block_comments=(), newlines="always", spaces=True),
    **{ext: _TokenRules(line_comments=(";",), block_comments=(("#|", "|#"),), quotes=('"',))
       for ext in ("scm", "ss", "lisp", "clj")},
    **{ext: _TokenRules(line_comments=(";",), block_comments=(), quotes=('"',), newlines="always")
       for ext in ("ll", "llvm")},
    **{ext: _TokenRules(line_comments=(";", "//"), newlines="always") for ext in ("s", "asm")},
    "apl": _TokenRules(line_comments=("⍝",), block_comments=(), quotes=("'",), newlines="always"),
    "ijs": _TokenRules(line_comments=("NB.",), block_comments=(), quotes=("'",), newlines="always"),
}


@functools.lru_cache(maxsize=None)
def _token_scanner(rules: _TokenRules) -> "re.Pattern[str]":
    parts = [r"(?P<nl>\n)", r"(?P<ws>[^\S\n]+)"]
    for open_, close in rules.block_comments:
        parts.append(rf"(?P<block{len(parts)}>{re.escape(open_)}.*?{re.escape(close)})")
    for marker in rules.line_comments:
        # A comment marker made of letters ('NB.') or '#' in a shell only
        # starts a comment at the beginning of a word: `$#`, `${#x}`.
        guard = r"(?<!\S)" if rules.spaces or marker[0].isalpha() else ""
        parts.append(rf"(?P<line{len(parts)}>{guard}{re.escape(marker)}[^\n]*)")
    for quote in rules.quotes:
        q = re.escape(quote)
        if len(quote) == 3:
            parts.append(rf"(?P<str{len(parts)}>{q}.*?{q})")
        elif quote == "`":
            parts.append(rf"(?P<str{len(parts)}>{q}(?:\\.|[^\\{q}])*{q})")
        else:
            parts.append(rf"(?P<str{len(parts)}>{q}(?:\\.|[^\\{q}\n])*{q})")
    parts += [r"(?P<word>\w+)", r"(?P<punct>[^\w\s])"]
    return re.compile("|".join(parts), re.DOTALL)


def _token_fallback_digest(content: str, lang: str) -> str:
    """Hashes the code's tokens: comments dropped, strings kept whole, and
    whitespace kept wherever the language gives it meaning.

    The old fallback deleted everything from '#', '//' or ';' and then joined
    the remaining tokens with nothing between them, so `#if 0`, a URL in a
    string, `if (ok);` and `rm -rf / tmp` all hashed like the original.
    """
    rules = _TOKEN_RULES.get(lang, _C_LIKE)
    text = _dedent_region(content) if rules.indentation else content.replace("\r\n", "\n")
    out: List[str] = []
    line_start = True  # no token on this line yet
    indent = ""  # leading whitespace of this line
    spaced = False  # whitespace or a comment since the last token
    directive = False  # this line is a preprocessor line (or continues one)
    last = ""  # last token on this line
    for match in _token_scanner(rules).finditer(text):
        kind, token = match.lastgroup or "", match.group()
        if kind.startswith(("block", "line")):
            spaced = True
        elif kind == "ws":
            if line_start:
                indent += token
            spaced = True
        elif kind == "nl":
            if not line_start and (rules.newlines == "always" or (rules.newlines == "directives" and directive)):
                out.append("\n")
            # A preprocessor line ending in a backslash continues on the next.
            directive = directive and last == "\\"
            line_start, indent, spaced, last = True, "", False, ""
        else:
            if line_start:
                if rules.indentation:
                    out.append(f"<indent {indent}>")
                directive = directive or token == "#"
            out.append(f" {token}" if spaced and rules.spaces else token)
            line_start, spaced, last = False, False, token
    return hashlib.sha256("\x00".join(out).encode("utf-8")).hexdigest()


def compute_ast_hash(content: str, lang: str = "py") -> str:
    """Computes Smart Syntax Lock AST Hash (alpha-renaming, comments/formatting ignored)."""
    lang = lang.lower().lstrip(".")
    digest = None
    if lang in ("py", "python"):
        digest = python_ast_hash(content, lang)
    elif lang in ("mojo", "🔥"):
        digest = _normalized_ast_digest(_mojo_to_python(content))
    if digest is not None:
        return digest
    return _token_fallback_digest(content, lang)


class LockDatabaseError(Exception):
    """Raised when keepout.json exists but cannot be used as a lock record."""
    pass


def validate_locks_db(data: Any, db_path: Path) -> None:
    """Raises LockDatabaseError unless data has the shape `keepout init` writes."""
    if not isinstance(data, dict):
        raise LockDatabaseError(f"{db_path}: expected a JSON object, found {type(data).__name__}.")
    locks = data.get("locks")
    if not isinstance(locks, dict):
        raise LockDatabaseError(f"{db_path}: 'locks' is missing or is not an object.")
    for key, entry in locks.items():
        if not isinstance(entry, dict):
            raise LockDatabaseError(f"{db_path}: entry '{key}' is not an object.")
        if entry.get("mode") not in KNOWN_MODES:
            raise LockDatabaseError(
                f"{db_path}: entry '{key}' has mode {entry.get('mode')!r}; "
                f"expected one of {sorted(KNOWN_MODES)}."
            )
        for name in ("strict_hash", "ast_hash", "original_code"):
            if not isinstance(entry.get(name), str):
                raise LockDatabaseError(f"{db_path}: entry '{key}' has no '{name}'.")
        # original_code is executed by a logic lock. Recorded by init it
        # always hashes to strict_hash; anything else was edited after init.
        if compute_strict_hash(entry["original_code"]) != entry["strict_hash"]:
            raise LockDatabaseError(
                f"{db_path}: entry '{key}' has an original_code that does not match its "
                f"strict_hash, so it was changed after 'keepout init' recorded it."
            )
        if not isinstance(entry.get("cached_counterexamples", []), list):
            raise LockDatabaseError(f"{db_path}: entry '{key}' has a 'cached_counterexamples' that is not a list.")


def load_locks_db(db_path: Path) -> Dict[str, Any]:
    """Loads keepout.json database.

    A missing file is an empty database. A file that is present but cannot be
    read, or does not have the shape `keepout init` writes, raises
    LockDatabaseError. It used to be read as empty, which made every lock in
    the source look unregistered -- and an unregistered lock was only a
    warning, so `{}` or a truncated file made every check pass.
    """
    if not db_path.exists():
        return {"version": VERSION, "locks": {}}
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise LockDatabaseError(f"{db_path}: cannot be read: {err}") from err
    validate_locks_db(data, db_path)
    return data


def save_locks_db(db_path: Path, db_data: Dict[str, Any]) -> None:
    """Saves keepout.json database atomically."""
    tmp_path = db_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(db_path)


def make_lock_key(rel_file_path: str, lock_id: str) -> str:
    """Generates lock key e.g. 'src/main.c#lock_0' or 'src/main.c#calc' for name="calc"."""
    return f"{rel_file_path}#{lock_id}"


def sync_blocks_to_db(db_path: Path, blocks: List[LockBlock], root_dir: Path) -> Dict[str, Any]:
    """Updates keepout.json with LockBlocks while preserving cached counterexamples."""
    db_data = load_locks_db(db_path)
    existing_locks = db_data.get("locks", {})
    new_locks: Dict[str, Any] = {}

    for block in blocks:
        try:
            rel_path = block.file_path.relative_to(root_dir).as_posix()
        except ValueError:
            rel_path = block.file_path.as_posix()

        key = make_lock_key(rel_path, block.lock_id)
        content_hash = compute_strict_hash(block.content)
        ext = block.file_path.suffix.lstrip(".")
        ast_hash = compute_ast_hash(block.content, lang=ext)

        cached_ces = existing_locks.get(key, {}).get("cached_counterexamples", [])

        new_locks[key] = {
            "file_path": rel_path,
            "lock_index": block.lock_index,
            "lock_id": block.lock_id,
            "mode": block.mode,
            "strict_hash": content_hash,
            "ast_hash": ast_hash,
            "original_code": block.content,
            "cached_counterexamples": cached_ces,
            "target_symbol": block.target_symbol,
            "start_line": block.start_line,
            "end_line": block.end_line,
        }

    db_data["version"] = VERSION
    db_data["locks"] = new_locks
    save_locks_db(db_path, db_data)
    return db_data




# =====================================================================
# 3. Deterministic Property-Based Testing (PBT) Engine with Counterexample Caching
# =====================================================================

@dataclass
class EquivalenceResult:
    is_equivalent: bool
    status_str: str  # "pbt_pass", "pbt_fail", "pbt_unverifiable"
    message: str
    counterexample: Optional[Dict[str, Any]] = None


PBT_LANGS = ("py", "python", "mojo", "🔥")
EDGE_CASES = [
    0, 1, -1, 2, -2, 255, 256, 2**16 - 1, 2**16, 2**31 - 1, -2**31,
    2**32, 2**63 - 1, -2**63, 2**64, 10**9, -10**9,
]
# Random inputs in ±1e9 almost never land on a small value, so a boundary
# such as `x >= 18` -> `x > 18` went unseen. Every small integer is tried.
SMALL_RANGE = range(-256, 257)
# A function the original cannot finish on a few inputs is skipped on those;
# past this many it is too slow to test in this engine at all.
MAX_ORIGINAL_TIMEOUTS = 5
_MODULE_CODE = "<module-level code>"


def _integer_constants(tree: ast.AST) -> List[int]:
    """Neighbours of every integer literal, and of integer-looking strings.

    A boundary or a backdoor is usually written as a literal -- `x >= 18`,
    `x == 123456789` -- and random inputs do not hit it, so each literal c
    contributes c - 1, c, c + 1 and -c.
    """
    found: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        if isinstance(node.value, int):
            found.append(node.value)
        elif isinstance(node.value, str) and re.fullmatch(r"[+-]?\d{1,30}", node.value.strip()):
            found.append(int(node.value))
    return [n for c in found for n in (c - 1, c, c + 1, -c)]


def build_test_inputs(trees: List[ast.AST], num_samples: int, seed: int) -> List[int]:
    """Edge cases, literal neighbours and small integers, then seeded random
    integers until there are num_samples. Deterministic for the same code."""
    inputs: Dict[int, None] = dict.fromkeys(EDGE_CASES)
    for tree in trees:
        for value in _integer_constants(tree):
            inputs.setdefault(value)
    for value in SMALL_RANGE:
        inputs.setdefault(value)
    rng = random.Random(seed)
    while len(inputs) < num_samples:
        inputs.setdefault(rng.randint(-10**9, 10**9))
    return list(inputs)


class _CallTimeout(BaseException):
    """Raised inside a call that ran past its limit. A BaseException, so the
    region's own `except Exception` cannot swallow it."""


def _raise_call_timeout(signum, frame):
    raise _CallTimeout()


def _call(func, value, limit: float) -> Tuple[str, Any, str]:
    """One call, as (kind, value-or-exception-type, detail)."""
    timer = bool(limit) and hasattr(signal, "setitimer")
    try:
        if timer:
            signal.setitimer(signal.ITIMER_REAL, limit)
        try:
            return ("returned", func(value), "")
        finally:
            if timer:
                signal.setitimer(signal.ITIMER_REAL, 0)
    except _CallTimeout:
        return ("timeout", None, "")
    except KeyboardInterrupt:
        raise
    except BaseException as err:
        # SystemExit too: a region that exits must not end the check.
        kind = f"{type(err).__module__}.{type(err).__qualname__}"
        return ("raised", kind, f"{type(err).__name__}: {err}")


def _same(a: Tuple[str, Any, str], b: Tuple[str, Any, str]) -> bool:
    """Same outcome: both returned equal values of the same type, both raised
    the same exception type, or both ran out of time."""
    if a[0] != b[0]:
        return False
    if a[0] != "returned":
        return a[1] == b[1]
    x, y = a[1], b[1]
    if type(x) is not type(y):
        return False
    if isinstance(x, float) and x != x and y != y:
        return True
    try:
        return bool(x == y)
    except Exception:
        return False


def _describe(outcome: Tuple[str, Any, str]) -> str:
    kind, value, detail = outcome
    if kind == "raised":
        text = f"raises {detail}"
    elif kind == "timeout":
        text = "did not finish within the time limit"
    else:
        try:
            text = repr(value)
        except Exception as err:
            text = f"<unprintable {type(value).__name__}: {err}>"
    return text if len(text) <= 200 else text[:197] + "..."


def _changed_definitions(tree_a: ast.Module, tree_b: ast.Module) -> Set[str]:
    """Top-level functions whose definition differs beyond comments,
    formatting and local renames, plus _MODULE_CODE if anything else at
    module level does."""
    def digests(tree):
        defs: Dict[str, str] = {}
        rest: List[str] = []
        for node in tree.body:
            single = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
            AstVariableNormalizer().visit(single)
            if isinstance(node, ast.FunctionDef):
                defs[node.name] = ast.dump(single)
            else:
                rest.append(ast.dump(single))
        return defs, rest

    defs_a, rest_a = digests(tree_a)
    defs_b, rest_b = digests(tree_b)
    changed = {name for name in defs_a if defs_a[name] != defs_b.get(name)}
    if rest_a != rest_b:
        changed.add(_MODULE_CODE)
    return changed


def _result(status: str, message: str, counterexample: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"status": status, "message": message, "counterexample": counterexample}


def _compare_regions(
    code_a: str,
    code_b: str,
    lang: str,
    num_samples: int,
    seed: int,
    cached_counterexamples: List[Any],
    per_call_timeout: float,
) -> Dict[str, Any]:
    """The comparison itself. Runs in the child process started by
    property_based_test_equivalence; returns only plain data."""
    def to_python(code):
        code = _dedent_region(code)
        return _mojo_to_python(code) if lang in ("mojo", "🔥") else code

    # One namespace per version, as a module has: the functions see the
    # region's constants, helpers and imports, and can call themselves. With
    # separate globals and locals they could not, so a recursive function
    # was compared only on its base case.
    try:
        tree_a = ast.parse(to_python(code_a))
        ns_a: Dict[str, Any] = {"__name__": "keepout_pbt_original"}
        exec(compile(tree_a, "<locked region: original>", "exec"), ns_a)
    except BaseException as err:
        return _result("pbt_unverifiable", (
            f"🔍 Nothing was verified: the recorded original region could not be run.\n"
            f"  {type(err).__name__}: {err}"
        ))
    try:
        tree_b = ast.parse(to_python(code_b))
        ns_b: Dict[str, Any] = {"__name__": "keepout_pbt_modified"}
        exec(compile(tree_b, "<locked region: modified>", "exec"), ns_b)
    except BaseException as err:
        return _result("pbt_fail", (
            f"🚨 The modified region no longer runs.\n  {type(err).__name__}: {err}"
        ))

    # Every top-level function is compared. Taking the first callable found
    # meant a helper defined first, or a function imported at the top, was
    # the only thing tested -- the function the lock was for went unchecked.
    names = list(dict.fromkeys(n.name for n in tree_a.body if isinstance(n, ast.FunctionDef)))
    if not names:
        return _result("pbt_unverifiable", (
            "🔍 Nothing was verified: the region defines no top-level function to call. "
            "Use [LOCK: ast] or [LOCK: strict] for it."
        ))

    generated = build_test_inputs([tree_a, tree_b], num_samples, seed)
    returned = {name: 0 for name in names}
    first_error: Dict[str, str] = {}
    compared = 0
    original_timeouts = 0

    for phase, values in (("cached", list(cached_counterexamples)), ("generated", generated)):
        for name in names:
            func_a, func_b = ns_a.get(name), ns_b.get(name)
            if not callable(func_a):
                continue
            if not callable(func_b):
                return _result("pbt_fail", (
                    f"🚨 `{name}` is no longer defined as a function by the modified region."
                ))
            for value in values:
                out_a = _call(func_a, value, per_call_timeout)
                if out_a[0] == "timeout":
                    original_timeouts += 1
                    if original_timeouts > MAX_ORIGINAL_TIMEOUTS:
                        return _result("pbt_unverifiable", (
                            f"🔍 Nothing was verified: the original `{name}` did not finish within "
                            f"{per_call_timeout}s on {original_timeouts} inputs, so it is too slow "
                            f"for this engine. Use [LOCK: ast] for it."
                        ))
                    continue
                out_b = _call(func_b, value, per_call_timeout)
                compared += 1
                if out_a[0] == "returned":
                    returned[name] += 1
                elif name not in first_error:
                    first_error[name] = out_a[2]
                if not _same(out_a, out_b):
                    expected, actual = _describe(out_a), _describe(out_b)
                    headline = (
                        "🚨 Instant Regression Detected (Cached Counterexample)! Mismatch found:\n"
                        f"  Function: {name}\n  Cached Input: {value!r}\n"
                        if phase == "cached" else
                        "🚨 Logic Mismatch Detected (Deterministic PBT)! Counterexample found:\n"
                        f"  Function: {name}\n  Input Parameter: {value!r}\n"
                    )
                    return _result("pbt_fail", (
                        headline
                        + f"  Expected Output (Original): {expected}\n"
                        + f"  Actual Output (Modified): {actual}"
                    ), {"function": name, "input": value, "output_a": expected, "output_b": actual})

    # A function the original never returned from was never really observed:
    # every call raised (one integer argument is all this engine passes).
    # That is only acceptable when nothing it depends on in the region changed.
    changed = _changed_definitions(tree_a, tree_b)
    never_returned = [name for name in names if returned[name] == 0 and callable(ns_a.get(name))]
    unverified = [name for name in never_returned if name in changed or _MODULE_CODE in changed]
    if unverified:
        details = "\n".join(
            f"  `{name}`: first error {first_error.get(name, 'none recorded')}" for name in unverified
        )
        return _result("pbt_unverifiable", (
            f"🔍 Nothing was verified for {', '.join(f'`{n}`' for n in unverified)}: the original "
            f"raised on every one of {len(generated)} generated inputs, so the two versions were "
            f"never compared on a returned value.\n{details}\n"
            f"  This engine calls each top-level function with ONE INTEGER argument. A function "
            f"taking several arguments, or other types, cannot be reached -- use [LOCK: ast] for "
            f"it, or hold its behaviour in a test written for its real signature.\n"
            f"  Reporting a pass here would be worse than having no lock: an absent check is "
            f"visible and a check that cannot fail is not."
        ))

    verified = [name for name in names if returned[name] > 0]
    skipped = [name for name in never_returned if name not in unverified]
    note = f" Not callable here but unchanged: {', '.join(skipped)}." if skipped else ""
    return _result("pbt_pass", (
        f"✨ Passed Deterministic PBT: {', '.join(verified)} matched on {len(generated)} "
        f"reproducible inputs (seed={seed}; {compared} calls compared, exceptions included).{note}"
    ))


def _pbt_child_main() -> None:
    """Entry point of the child process (see property_based_test_equivalence).

    Reads the job as JSON on stdin and writes the result as JSON to the
    original stdout. Everything the locked code prints goes to /dev/null.
    """
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    result_fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if hasattr(signal, "setitimer"):
        signal.signal(signal.SIGALRM, _raise_call_timeout)
    try:
        result = _compare_regions(**payload)
    except BaseException as err:
        result = _result("pbt_unverifiable", (
            f"🔍 Nothing was verified: the test process failed.\n  {type(err).__name__}: {err}"
        ))
    with os.fdopen(result_fd, "w", encoding="utf-8") as out:
        json.dump(result, out, default=repr)
    # The region may have left threads running; they must not keep the
    # process, and the check, alive.
    os._exit(0)


_CHILD_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {path!r}); "
    "from keepout.core import _pbt_child_main; _pbt_child_main()"
)


def _stop_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def property_based_test_equivalence(
    code_a: str,
    code_b: str,
    lang: str = "py",
    num_samples: int = 10000,
    seed: int = 42,
    cached_counterexamples: Optional[List[Any]] = None,
    per_call_timeout: float = 1.0,
    time_budget: float = 60.0,
) -> EquivalenceResult:
    """
    Executes Deterministic Property-Based Testing (PBT) across 10,000+ reproducible inputs.
    - Fixes random seed to guarantee 100% deterministic, zero-flaky CI runs.
    - Prioritizes cached counterexamples first for instant regression detection.
    - Runs the locked code in a separate Python process, with a limit per
      call and for the whole run: it executes whatever the region contains,
      and a region that called sys.exit(0) used to end the check with
      exit status 0, every lock after it unchecked.
    """
    lang = lang.lower().lstrip(".")
    if lang not in PBT_LANGS:
        return EquivalenceResult(
            is_equivalent=False,
            status_str="pbt_unverifiable",
            message=(
                f"🔍 Nothing was verified: no property-based engine exists for "
                f"'{lang}'. Only Python and Mojo regions can be checked this way; "
                f"use [LOCK: ast] or [LOCK: strict] for the rest."
            ),
        )

    payload = json.dumps({
        "code_a": code_a,
        "code_b": code_b,
        "lang": lang,
        "num_samples": num_samples,
        "seed": seed,
        "cached_counterexamples": list(cached_counterexamples or []),
        "per_call_timeout": per_call_timeout,
    })
    package_parent = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_BOOTSTRAP.format(path=package_parent)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    reason = None
    try:
        out, err = proc.communicate(payload.encode("utf-8"), timeout=time_budget)
    except subprocess.TimeoutExpired:
        reason = f"the run did not finish within {time_budget}s and was stopped"
    finally:
        _stop_process_tree(proc)

    result = None
    if reason is None:
        try:
            result = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            reason = (
                f"the test process ended without a result (exit code {proc.returncode}); "
                f"the region may call os._exit or crash the interpreter. {tail[0]}"
            ).rstrip()

    if result is None:
        return EquivalenceResult(
            is_equivalent=False,
            status_str="pbt_unverifiable",
            message=f"🔍 Nothing was verified: {reason}.",
        )

    return EquivalenceResult(
        is_equivalent=result["status"] == "pbt_pass",
        status_str=result["status"],
        message=result["message"],
        counterexample=result["counterexample"],
    )
