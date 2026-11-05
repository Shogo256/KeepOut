"""Argument generation for the PBT engine.

Each parameter gets a deterministic pool of values chosen from its type hint
(int where there is none): edge cases, the literals the code itself
mentions and their neighbours, then seeded random values. The same code and
seed always produce the same calls.
"""

import ast
import collections.abc
from dataclasses import dataclass, field
import datetime
import decimal
import enum
import inspect
import math
import random
import types
import typing
from typing import Any, Dict, List, Tuple

INT_EDGES = [
    0, 1, -1, 2, -2, 255, 256, 2**16 - 1, 2**16, 2**31 - 1, -2**31,
    2**32, 2**63 - 1, -2**63, 2**64, 10**9, -10**9,
]
# Random inputs in ±1e9 almost never land on a small value, so a boundary
# such as `x >= 18` -> `x > 18` went unseen. Every small integer is tried.
SMALL_INTS = range(-256, 257)
FLOAT_EDGES = [
    1.0, 0.0, -0.0, -1.0, 0.5, -0.5, 0.1, 1e-9, -1e-9, 1e9, -1e9,
    1e308, -1e308, 5e-324, math.inf, -math.inf, math.nan,
]
STR_EDGES = [
    "abc", "", " ", "a", "A", "Abc", "ABC", "a b", "  padded  ", "\n", "\t",
    "0", "1", "-1", "3.14", "None", "true", "false", "a,b", "a/b", "a.b",
    "日本語", "ｶﾀｶﾅ", "😀", "x" * 1000,
]
BYTES_EDGES = [b"abc", b"", b"a", b"\x00", b"\xff", "日本語".encode()]
DATE_EDGES = [
    datetime.date(2000, 1, 1), datetime.date(1970, 1, 1), datetime.date(2000, 2, 29),
    datetime.date(2024, 2, 29), datetime.date(2024, 12, 31), datetime.date(2025, 1, 1),
    datetime.date(2038, 1, 19), datetime.date(1, 1, 1), datetime.date(9999, 12, 31),
]
CONTAINER_ELEMENTS = 6  # how many of an element pool feed the container shapes


class NoStrategy(Exception):
    """No values can be generated for a parameter's type."""


@dataclass
class Literals:
    """Literals found in either version of the region."""
    ints: List[int] = field(default_factory=list)
    floats: List[float] = field(default_factory=list)
    strs: List[str] = field(default_factory=list)
    bytes_: List[bytes] = field(default_factory=list)


def mine_literals(trees: List[ast.AST]) -> Literals:
    found = Literals()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
                continue
            value = node.value
            if isinstance(value, int):
                found.ints.append(value)
            elif isinstance(value, float):
                found.floats.append(value)
            elif isinstance(value, str) and len(value) <= 200:
                found.strs.append(value)
                if value.strip().lstrip("+-").isdigit() and len(value.strip()) <= 30:
                    found.ints.append(int(value))
            elif isinstance(value, bytes) and len(value) <= 200:
                found.bytes_.append(value)
    return found


def _unique(values) -> List[Any]:
    """Order-preserving dedupe that keeps 1, 1.0 and True apart."""
    seen, out = set(), []
    for value in values:
        key = (type(value), repr(value))
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _int_pool(lit: Literals) -> List[int]:
    near = [n for c in lit.ints for n in (c - 1, c, c + 1, -c)]
    return _unique(INT_EDGES + near + list(SMALL_INTS))


def _float_pool(lit: Literals) -> List[float]:
    near = []
    for c in lit.floats + [float(i) for i in lit.ints if abs(i) < 2**53]:
        near += [c, c - 1, c + 1, c - 0.5, c + 0.5, math.nextafter(c, -math.inf), math.nextafter(c, math.inf), -c]
    return _unique(FLOAT_EDGES + near + [float(i) for i in range(-10, 11)])


def _str_pool(lit: Literals) -> List[str]:
    near = []
    for s in lit.strs:
        near += [s, s.upper(), s.lower(), s.title(), " " + s, s + " ", s[:-1], s + s[-1:]]
    return _unique(STR_EDGES + near + [str(i) for i in lit.ints])


def _containers(elements: List[Any], make) -> List[Any]:
    e = elements[:CONTAINER_ELEMENTS] or [None]
    shapes = [[], [e[0]], [e[-1]], e[:2], e[:2][::-1], [e[0], e[0]], e, e[::-1]]
    out = []
    for shape in shapes:
        try:
            out.append(make(shape))
        except TypeError:  # unhashable elements for a set
            continue
    return _unique(out)


def pool_for(annotation: Any, lit: Literals) -> List[Any]:
    """Deterministic candidate values for one parameter type."""
    if annotation is inspect.Parameter.empty or annotation in (typing.Any, object):
        return _int_pool(lit)
    if isinstance(annotation, str):
        named = {"int": int, "float": float, "str": str, "bool": bool, "bytes": bytes}
        if annotation in named:
            return pool_for(named[annotation], lit)
        raise NoStrategy(annotation)
    supertype = getattr(annotation, "__supertype__", None)  # NewType
    if supertype is not None:
        return pool_for(supertype, lit)
    if isinstance(annotation, typing.TypeVar):
        options = annotation.__constraints__ or ((annotation.__bound__,) if annotation.__bound__ else ())
        return _unique(v for t in options for v in pool_for(t, lit)) if options else _int_pool(lit)

    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, getattr(types, "UnionType", typing.Union)):
        ordered = sorted(args, key=lambda t: t is not type(None))  # None first
        return _unique(v for t in ordered for v in pool_for(t, lit))
    if origin is typing.Literal:
        return list(args)
    if origin in (list, collections.abc.Sequence, collections.abc.MutableSequence,
                  collections.abc.Iterable, collections.abc.Collection):
        return _containers(pool_for(args[0] if args else int, lit), list)
    if origin in (set, frozenset, collections.abc.Set, collections.abc.MutableSet):
        return _containers(pool_for(args[0] if args else int, lit), frozenset if origin is frozenset else set)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return _containers(pool_for(args[0], lit), tuple)
        if not args:
            return _containers(_int_pool(lit), tuple)
        pools = [pool_for(t, lit)[:CONTAINER_ELEMENTS] for t in args]
        width = max(len(p) for p in pools)
        return _unique(tuple(p[i % len(p)] for p in pools) for i in range(width))
    if origin in (dict, collections.abc.Mapping, collections.abc.MutableMapping):
        keys = pool_for(args[0] if args else str, lit)[:CONTAINER_ELEMENTS]
        values = pool_for(args[1] if len(args) > 1 else int, lit)[:CONTAINER_ELEMENTS]
        return _unique([{}, {keys[0]: values[0]}, {keys[0]: values[-1]}, {keys[-1]: values[0]},
                        dict(zip(keys, values)), dict(zip(keys, reversed(values)))])

    if annotation is bool:
        return [True, False]
    if annotation is int:
        return _int_pool(lit)
    if annotation is float:
        return _float_pool(lit)
    if annotation is str:
        return _str_pool(lit)
    if annotation is bytes:
        return _unique(BYTES_EDGES + lit.bytes_ + [s.encode() for s in lit.strs])
    if annotation is type(None) or annotation is None:
        return [None]
    if annotation is list:
        return _containers(_int_pool(lit), list)
    if annotation is dict:
        return pool_for(Dict[str, int], lit)
    if annotation is decimal.Decimal:
        texts = ["1", "0", "-1", "0.1", "0.01", "1.005", "-0.5", "1e-9", "123456789.123456789"]
        texts += [str(i) for i in lit.ints] + [repr(f) for f in lit.floats if math.isfinite(f)]
        return _unique(decimal.Decimal(t) for t in texts)
    if annotation is datetime.datetime:
        moments = []
        for d in DATE_EDGES:
            moments += [datetime.datetime(d.year, d.month, d.day),
                        datetime.datetime(d.year, d.month, d.day, 23, 59, 59, 999999)]
        return moments
    if annotation is datetime.date:
        return list(DATE_EDGES)
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return list(annotation)
    raise NoStrategy(getattr(annotation, "__name__", repr(annotation)))


def _random_value(annotation: Any, pool: List[Any], rng: random.Random) -> Any:
    """A value outside the fixed pool, for the random part of the run."""
    if rng.random() < 0.5:
        return rng.choice(pool)
    if annotation is inspect.Parameter.empty or annotation in (int, typing.Any, object):
        return rng.randint(-10**9, 10**9)
    if annotation is float:
        return rng.uniform(-1e6, 1e6)
    if annotation is str:
        alphabet = "abcXYZ019 -_.,/あ日"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
    return rng.choice(pool)


def argument_sets(func, globalns: Dict[str, Any], lit: Literals, num_samples: int, seed: int) -> List[Tuple[tuple, dict]]:
    """Calls to make: (args, kwargs) pairs, deterministic for the same code.

    Raises NoStrategy when a parameter's type has no generator.
    """
    signature = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func, globalns=globalns)
    except Exception:
        hints = {}

    params = []  # (name, annotation, keyword_only)
    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = hints.get(param.name, param.annotation)
        try:
            pool = pool_for(annotation, lit)
        except NoStrategy as err:
            raise NoStrategy(f"{param.name}: {err}") from None
        params.append((param.name, annotation, param.kind == param.KEYWORD_ONLY, pool))

    if not params:
        return [((), {})]

    def build(values) -> Tuple[tuple, dict]:
        args = tuple(v for (_, _, kw, _), v in zip(params, values) if not kw)
        kwargs = {name: v for (name, _, kw, _), v in zip(params, values) if kw}
        return args, kwargs

    base = [next((v for v in pool if _typical(v)), pool[0]) for _, _, _, pool in params]
    calls: Dict[str, Tuple[tuple, dict]] = {}

    def add(values) -> None:
        call = build(values)
        calls.setdefault(repr(call), call)

    # Every value of every pool, the other parameters held at a typical value.
    for i, (_, _, _, pool) in enumerate(params):
        for value in pool:
            add(base[:i] + [value] + base[i + 1:])
    # Edges together: the j-th value of every pool at once (all zeros, ...).
    if len(params) > 1:
        for j in range(max(len(pool) for _, _, _, pool in params)):
            add([pool[j % len(pool)] for _, _, _, pool in params])
    # Then seeded random combinations.
    rng = random.Random(seed)
    attempts = 0
    while len(calls) < num_samples and attempts < num_samples * 4:
        attempts += 1
        add([_random_value(annotation, pool, rng) for _, annotation, _, pool in params])
    return list(calls.values())


def _typical(value: Any) -> bool:
    if isinstance(value, float) and value != value:
        return False
    try:
        return bool(value)
    except Exception:
        return False
