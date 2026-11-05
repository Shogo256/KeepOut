"""Structural hashes for Go, JavaScript, TypeScript and Rust, via tree-sitter.

The same idea as the Python AST hash: comments and formatting are dropped,
and the names a function in the region declares -- its parameters and
locals -- are renamed VAR_0, VAR_1, ... in order of first use. Everything
else is kept as written: callees, package and module names, fields,
properties, paths, and what the region declares at the top level.
"""

from dataclasses import dataclass
import functools
import hashlib
from typing import Callable, Dict, FrozenSet, List, Optional

# Bumped with any change to how a tree becomes tokens, or to the pinned
# grammars: either changes the hashes keepout.json records.
ENGINE = "tree-sitter-1"
TREE_SITTER_LANGS = ("go", "js", "jsx", "ts", "tsx", "rs")


class TreeSitterUnavailable(RuntimeError):
    """tree-sitter or a grammar is missing; hashes would not match other installs."""


@dataclass(frozen=True)
class _Spec:
    grammar: str
    functions: FrozenSet[str]  # node types that open a scope of their own
    comments: FrozenSet[str]
    skipped_tokens: FrozenSet[str]  # anonymous tokens that carry no meaning here
    wrap: str  # a function around a statement fragment that does not parse alone
    declared: Callable  # node -> identifier nodes it declares
    renamable: Callable  # identifier node -> whether it may be a local name
    nested_declaration: Callable  # nested scope node -> its own name node, bound in the enclosing scope


# --------------------------------------------------------------------- Go

def _go_ids(expression_list) -> list:
    return [c for c in expression_list.named_children if c.type == "identifier"] if expression_list else []


def _go_declared(node) -> list:
    t = node.type
    if t in ("parameter_declaration", "variadic_parameter_declaration", "var_spec", "const_spec"):
        return [c for c in node.children_by_field_name("name") if c.type == "identifier"]
    if t == "short_var_declaration":
        return _go_ids(node.child_by_field_name("left"))
    if t == "range_clause" and any(c.type == ":=" for c in node.children):
        return _go_ids(node.child_by_field_name("left"))
    if t == "type_switch_statement":
        return _go_ids(node.child_by_field_name("alias"))
    return []


def _go_renamable(node) -> bool:
    if node.text == b"_":
        return False
    parent = node.parent
    # The key of a composite literal (`Point{X: 1}`) names a field.
    if parent is not None and parent.type == "literal_element" and parent.parent is not None \
            and parent.parent.type == "keyed_element" and parent.parent.named_children[0] == parent:
        return False
    return True


# ----------------------------------------------------------- JS / TS

def _js_pattern(node) -> list:
    if node is None:
        return []
    t = node.type
    if t in ("identifier", "shorthand_property_identifier_pattern"):
        return [node]
    if t == "assignment_pattern":
        return _js_pattern(node.child_by_field_name("left"))
    if t == "pair_pattern":
        return _js_pattern(node.child_by_field_name("value"))
    if t in ("object_pattern", "array_pattern", "rest_pattern"):
        return [i for c in node.named_children for i in _js_pattern(c)]
    return []


def _js_declared(node) -> list:
    t = node.type
    if t == "variable_declarator":
        return _js_pattern(node.child_by_field_name("name"))
    if t in ("required_parameter", "optional_parameter"):
        return _js_pattern(node.child_by_field_name("pattern"))
    if t == "formal_parameters":  # JavaScript puts the patterns here directly
        return [i for c in node.named_children
                if c.type in ("identifier", "object_pattern", "array_pattern", "assignment_pattern", "rest_pattern")
                for i in _js_pattern(c)]
    if t == "arrow_function":
        param = node.child_by_field_name("parameter")
        return [param] if param is not None and param.type == "identifier" else []
    if t == "catch_clause":
        return _js_pattern(node.child_by_field_name("parameter"))
    if t == "for_in_statement" and any(c.type in ("const", "let", "var") for c in node.children):
        return _js_pattern(node.child_by_field_name("left"))
    return []


def _js_nested(node):
    if node.type in ("function_declaration", "generator_function_declaration", "class_declaration"):
        return node.child_by_field_name("name")
    return None


# --------------------------------------------------------------- Rust

def _rs_pattern(node) -> list:
    if node is None:
        return []
    t = node.type
    if t == "identifier":
        # A capitalised name in a pattern is a unit variant or constant (None).
        return [] if node.text[:1].isupper() else [node]
    if t == "shorthand_field_identifier":
        return [node]
    if t == "tuple_struct_pattern":
        kind = node.child_by_field_name("type")
        return [i for c in node.named_children if c != kind for i in _rs_pattern(c)]
    if t == "field_pattern":
        inner = node.child_by_field_name("pattern")
        return _rs_pattern(inner) if inner is not None else _rs_pattern(node.child_by_field_name("name"))
    if t in ("struct_pattern", "tuple_pattern", "slice_pattern", "mut_pattern", "ref_pattern",
             "reference_pattern", "or_pattern", "captured_pattern", "match_pattern"):
        return [i for c in node.named_children for i in _rs_pattern(c)]
    return []


def _rs_declared(node) -> list:
    t = node.type
    if t in ("parameter", "let_declaration", "for_expression", "let_condition"):
        return _rs_pattern(node.child_by_field_name("pattern"))
    if t == "closure_parameters":
        return [i for c in node.named_children if c.type != "parameter" for i in _rs_pattern(c)]
    if t == "match_pattern":
        guard = node.child_by_field_name("condition")
        return [i for c in node.named_children if c != guard for i in _rs_pattern(c)]
    return []


_RS_PATH_PARENTS = frozenset({
    "scoped_identifier", "scoped_type_identifier", "scoped_use_list", "use_declaration", "use_as_clause",
    "use_list", "use_wildcard", "shorthand_field_initializer", "attribute", "meta_item",
})


def _rs_renamable(node) -> bool:
    parent = node.parent
    if parent is None:
        return True
    if parent.type in _RS_PATH_PARENTS:
        return False
    if parent.type == "macro_invocation" and parent.child_by_field_name("macro") == node:
        return False
    if parent.type == "tuple_struct_pattern" and parent.child_by_field_name("type") == node:
        return False
    return True


_JS_FUNCTIONS = frozenset({
    "function_declaration", "function_expression", "function", "arrow_function", "method_definition",
    "generator_function_declaration", "generator_function",
})

_SPECS: Dict[str, _Spec] = {
    "go": _Spec("go", frozenset({"function_declaration", "method_declaration", "func_literal"}),
                frozenset({"comment"}), frozenset({";", "\n"}), "func _keepout_region() {{\n{}\n}}",
                _go_declared, _go_renamable, lambda node: None),
    "js": _Spec("javascript", _JS_FUNCTIONS, frozenset({"comment", "html_comment"}), frozenset({";"}),
                "function _keepout_region() {{\n{}\n}}", _js_declared, lambda node: True, _js_nested),
    "ts": _Spec("typescript", _JS_FUNCTIONS, frozenset({"comment"}), frozenset({";"}),
                "function _keepout_region() {{\n{}\n}}", _js_declared, lambda node: True, _js_nested),
    "tsx": _Spec("tsx", _JS_FUNCTIONS, frozenset({"comment"}), frozenset({";"}),
                 "function _keepout_region() {{\n{}\n}}", _js_declared, lambda node: True, _js_nested),
    # Rust keeps ';': `sum` and `sum;` as a block's last line return
    # different things.
    "rs": _Spec("rust", frozenset({"function_item", "closure_expression"}),
                frozenset({"line_comment", "block_comment"}), frozenset(), "fn _keepout_region() {{\n{}\n}}",
                _rs_declared, _rs_renamable, lambda node: None),
}
_SPECS["jsx"] = _SPECS["js"]


@functools.lru_cache(maxsize=None)
def _parser(grammar: str):
    try:
        import tree_sitter
        if grammar == "go":
            import tree_sitter_go as module
            language = module.language()
        elif grammar == "javascript":
            import tree_sitter_javascript as module
            language = module.language()
        elif grammar == "rust":
            import tree_sitter_rust as module
            language = module.language()
        else:
            import tree_sitter_typescript as module
            language = module.language_tsx() if grammar == "tsx" else module.language_typescript()
    except ImportError as err:
        raise TreeSitterUnavailable(
            f"keepout needs tree-sitter and its {grammar} grammar to compare this region ({err}). "
            f"Install keepout with its dependencies (pip install keepout from the release)."
        ) from err
    return tree_sitter.Parser(tree_sitter.Language(language))


def _declarations(scope_node, spec: _Spec) -> Dict[int, set]:
    """Names declared in each function scope, keyed by the scope node's id."""
    scopes: Dict[int, set] = {}

    def visit(node, owner) -> None:
        if node.type in spec.functions and node.id != owner:
            scopes.setdefault(node.id, set())
            nested_name = spec.nested_declaration(node)
            if nested_name is not None and owner is not None:
                scopes[owner].add(nested_name.text)
            owner = node.id
        if owner is not None:
            for ident in spec.declared(node):
                scopes[owner].add(ident.text)
        for child in node.children:
            visit(child, owner)

    visit(scope_node, None)
    return scopes


def _tokens(root, spec: _Spec) -> List[str]:
    scopes = _declarations(root, spec)
    out: List[str] = []
    stack: List[Dict[str, object]] = []  # innermost last: {"names": set, "map": dict}
    counter = [0]

    def resolve(name: bytes, skip_innermost: bool = False) -> Optional[str]:
        frames = stack[:-1] if skip_innermost else stack
        for frame in reversed(frames):
            if name in frame["names"]:
                mapping = frame["map"]
                if name not in mapping:
                    mapping[name] = f"VAR_{counter[0]}"
                    counter[0] += 1
                return mapping[name]
        return None

    own_names: set = set()  # a function's own name belongs to the scope around it

    def visit(node) -> None:
        if node.type in spec.comments:
            return
        opened = node.type in spec.functions
        if opened:
            stack.append({"names": scopes.get(node.id, set()), "map": {}})
            name = node.child_by_field_name("name")
            if name is not None:
                own_names.add(name.id)
        if node.child_count == 0:
            if not node.is_named:
                if node.type not in spec.skipped_tokens:
                    out.append(node.type)
            elif node.type == "identifier" and spec.renamable(node):
                local = resolve(node.text, skip_innermost=node.id in own_names)
                out.append(f"identifier={local or node.text.decode('utf-8', 'replace')}")
            else:
                out.append(f"{node.type}={node.text.decode('utf-8', 'replace')}")
        else:
            if node.is_named:
                out.append(f"({node.type}")
            for child in node.children:
                visit(child)
            if node.is_named:
                out.append(")")
        if opened:
            stack.pop()

    visit(root)
    return out


def tree_sitter_hash(content: str, lang: str) -> Optional[str]:
    """The structural hash of a region in one of TREE_SITTER_LANGS, or None
    when the language is another one or the region does not parse, even
    wrapped in a function."""
    spec = _SPECS.get(lang)
    if spec is None:
        return None
    parser = _parser(spec.grammar)
    for text in (content, spec.wrap.format(content)):
        tree = parser.parse(text.encode("utf-8"))
        if not tree.root_node.has_error:
            tokens = _tokens(tree.root_node, spec)
            material = f"{ENGINE}:{lang}:" + "\x00".join(tokens)
            return hashlib.sha256(material.encode("utf-8")).hexdigest()
    return None
