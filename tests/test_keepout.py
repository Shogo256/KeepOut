import json
import os
import shutil
import subprocess
import sys

import pytest
from pathlib import Path
from click.testing import CliRunner

from keepout.core import (
    DEFAULT_DB_NAME,
    LockBlock,
    LockParserError,
    parse_file_locks,
    compute_strict_hash,
    compute_ast_hash,
    load_locks_db,
    save_locks_db,
    sync_blocks_to_db,
    property_based_test_equivalence,
)
from keepout.cli import cli


# =====================================================================
# 1. Smart Syntax Lock (AST Alpha-Renaming & Formatting Neutrality)
# =====================================================================

def test_ast_alpha_renaming_and_formatting_neutrality():
    code1 = """
def calc_product(input_value):
    # Calculate result
    output_result = input_value * 4
    return output_result
"""

    code2 = """
def calc_product(x):

    r = x * 4; return r
"""

    code_struct_mod = """
def calc_product(x):
    r = x * 5
    return r
"""

    h1 = compute_ast_hash(code1, lang="py")
    h2 = compute_ast_hash(code2, lang="py")
    h_mod = compute_ast_hash(code_struct_mod, lang="py")

    assert h1 == h2  # Variable renames, comments, and whitespace ignored!
    assert h1 != h_mod


# =====================================================================
# 2. Deterministic Property-Based Testing & Counterexample Caching
# =====================================================================

def test_deterministic_pbt_reproducibility_and_caching():
    code_a = "def calc(x):\n    return x * 4"
    code_b = "def calc(x):\n    return x << 2"
    code_bad = "def calc(x):\n    return x * 5"

    # Deterministic PBT pass
    res1 = property_based_test_equivalence(code_a, code_b, lang="py", num_samples=10000, seed=42)
    assert res1.is_equivalent is True
    assert res1.status_str == "pbt_pass"
    assert "seed=42" in res1.message

    # Deterministic PBT fail
    res2 = property_based_test_equivalence(code_a, code_bad, lang="py", num_samples=10000, seed=42)
    assert res2.is_equivalent is False
    assert res2.status_str == "pbt_fail"

    # Regression check using cached counterexample (input = 1)
    res_cached = property_based_test_equivalence(
        code_a, code_bad, lang="py", num_samples=10000, seed=42, cached_counterexamples=[1]
    )
    assert res_cached.is_equivalent is False
    assert "Instant Regression Detected" in res_cached.message
    assert res_cached.counterexample["input"] == 1


# =====================================================================
# 3. CLI & Parser Integration Tests
# =====================================================================

def test_cli_smart_syntax_lock_mode(tmp_path: Path):
    src_file = tmp_path / "main.py"
    src_file.write_text(
        "# [LOCK: ast name=\"calc\"]\n"
        "def calc_product(input_val):\n"
        "    # Initial comment\n"
        "    res = input_val * 4\n"
        "    return res\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )

    runner = CliRunner()
    result_init = runner.invoke(cli, ["init", str(tmp_path)])
    assert result_init.exit_code == 0

    # Rename variables, update comments, change whitespace (should PASS!)
    src_file.write_text(
        "# [LOCK: ast name=\"calc\"]\n"
        "def calc_product(x):\n"
        "    # Renamed variable input_val -> x, res -> r, formatting changed\n\n"
        "    r = x * 4; return r\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )
    res_pass = runner.invoke(cli, ["check", str(tmp_path)])
    assert res_pass.exit_code == 0
    assert "Smart Syntax Lock" in res_pass.output

    # Modify code AST structure (should FAIL!)
    src_file.write_text(
        "# [LOCK: ast name=\"calc\"]\n"
        "def calc_product(x):\n"
        "    r = x * 5; return r\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )
    res_fail = runner.invoke(cli, ["check", str(tmp_path)])
    assert res_fail.exit_code != 0
    assert "AST STRUCTURAL VIOLATION" in res_fail.output


def test_a_function_of_several_arguments_is_compared_not_skipped():
    """The engine used to call every function with one integer, so a
    function of three arguments raised TypeError on every call. That was
    first reported as a pass, then as unverifiable. Arguments now come from
    the parameters (int where unannotated), so gutting it is a mismatch."""
    original = (
        "FLOOR = 0.5\n"
        "def verdict(share, ceiling, sampled):\n"
        "    if share < FLOOR or ceiling > 0.05:\n"
        "        return 'no'\n"
        "    return 'maybe' if sampled else 'yes'\n"
    )
    gutted = (
        "FLOOR = 0.5\n"
        "def verdict(share, ceiling, sampled):\n"
        "    return 'yes'\n"
    )
    result = property_based_test_equivalence(original, gutted, lang="py")
    assert result.status_str == "pbt_fail"
    assert "verdict(" in result.message


def test_a_function_the_engine_cannot_call_is_not_a_pass():
    """A check that cannot fail is worse than no check: an absent lock is
    visible, and a green one is not. A parameter of a type with no generator
    leaves the function uncalled, and a change to it unverifiable."""
    original = (
        "class Shape:\n    def __init__(self, w):\n        self.w = w\n"
        "def area(s: Shape) -> int:\n    return s.w * s.w\n"
    )
    gutted = original.replace("return s.w * s.w", "return 0")
    result = property_based_test_equivalence(original, gutted, lang="py")
    assert not result.is_equivalent
    assert result.status_str == "pbt_unverifiable"
    assert "Nothing was verified" in result.message
    assert "Shape" in result.message


def test_a_language_with_no_engine_is_not_a_pass():
    """There is no property-based engine for Rust or C. Returning a pass for
    them said the behaviour had been checked when nothing had run."""
    result = property_based_test_equivalence(
        "fn add(a: i64) -> i64 { a + 1 }",
        "fn add(a: i64) -> i64 { a + 2 }",
        lang="rs",
    )
    assert not result.is_equivalent
    assert result.status_str == "pbt_unverifiable"


def test_a_callable_function_still_verifies_and_still_catches_a_change():
    """The fix must not turn working locks into failures: a single-argument
    function with no outside references is exactly what this engine is for."""
    same = property_based_test_equivalence(
        "def f(x):\n    return x * 2\n",
        "def f(y):\n    result = y + y\n    return result\n",
        lang="py",
    )
    assert same.is_equivalent
    assert same.status_str == "pbt_pass"

    changed = property_based_test_equivalence(
        "def f(x):\n    return x * 2\n",
        "def f(x):\n    return x * 3\n",
        lang="py",
    )
    assert not changed.is_equivalent
    assert changed.status_str == "pbt_fail"
    assert changed.counterexample is not None


def test_inputs_a_function_rejects_do_not_make_it_unverifiable():
    """Some inputs raising is normal. The count that matters is whether ANY
    comparison happened, not whether every one did."""
    body = "def f(x):\n    if x < 0:\n        raise ValueError('negative')\n    return x * 2\n"
    result = property_based_test_equivalence(body, body, lang="py")
    assert result.is_equivalent
    assert result.status_str == "pbt_pass"


# =====================================================================
# 4. A keepout.json that cannot be trusted fails the check
# =====================================================================

STRICT_SRC = "# [LOCK: strict]\nMAX = 5\n# [/LOCK]\n"
STRICT_MOD = "# [LOCK: strict]\nMAX = 999\n# [/LOCK]\n"
PBT_SRC = "# [LOCK: pbt]\ndef f(x):\n    return x * 2\n# [/LOCK]\n"
PBT_MOD = "# [LOCK: pbt]\ndef f(x):\n    return x * 3\n# [/LOCK]\n"


def _init_then_edit(tmp_path: Path, source: str, modified: str, name: str = "main.py"):
    src = tmp_path / name
    src.write_text(source, encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(tmp_path)]).exit_code == 0
    src.write_text(modified, encoding="utf-8")
    return runner, tmp_path / DEFAULT_DB_NAME


def _edit_db(db: Path, change) -> None:
    data = json.loads(db.read_text(encoding="utf-8"))
    change(data)
    db.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    ["not json", "{}", "[]", '{"locks": []}'],
    ids=["invalid-json", "empty-object", "array", "locks-not-an-object"],
)
def test_an_unreadable_keepout_json_fails_the_check(tmp_path: Path, payload: str):
    """keepout.json was read as empty whenever it could not be parsed, and
    every lock then looked unregistered -- which was only a warning. The
    locked constant went from 5 to 999 and the check exited 0."""
    runner, db = _init_then_edit(tmp_path, STRICT_SRC, STRICT_MOD)
    db.write_text(payload, encoding="utf-8")
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "PASS" not in result.output
    assert "All lock checks passed" not in result.output


@pytest.mark.parametrize(
    "tamper",
    [
        lambda entry: entry.pop("original_code"),
        lambda entry: entry.update(mode="off"),
        lambda entry: entry.pop("strict_hash"),
        lambda entry: entry.update(cached_counterexamples="1"),
    ],
    ids=["no-original-code", "unknown-mode", "no-strict-hash", "counterexamples-not-a-list"],
)
def test_a_malformed_lock_entry_fails_the_check(tmp_path: Path, tamper):
    """Without original_code a logic lock compared the edit with itself and
    passed; with an unknown mode no branch ran and nothing was reported."""
    runner, db = _init_then_edit(tmp_path, PBT_SRC, PBT_MOD)
    _edit_db(db, lambda data: tamper(data["locks"]["main.py#lock_0"]))
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "PASS" not in result.output


def test_a_lock_with_no_record_fails_the_check(tmp_path: Path):
    """Emptying "locks" (or deleting one entry) switched those locks off: an
    unregistered lock was a warning and the check stayed green."""
    runner, db = _init_then_edit(tmp_path, STRICT_SRC, STRICT_MOD)
    _edit_db(db, lambda data: data.update(locks={}))
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "UNREGISTERED LOCK" in result.output


def test_the_mode_in_keepout_json_must_match_the_directive(tmp_path: Path):
    """The check runs the mode keepout.json records. Changing it there from
    strict to ast let an edit through that the source still declares strict."""
    commented = "# [LOCK: strict]\n# harmless?\nMAX = 5\n# [/LOCK]\n"
    runner, db = _init_then_edit(tmp_path, STRICT_SRC, commented)
    _edit_db(db, lambda data: data["locks"]["main.py#lock_0"].update(mode="ast"))
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "MODE MISMATCH" in result.output


def test_a_keepout_json_from_another_hash_format_is_refused(tmp_path: Path):
    """AST hashes from 2.0.0 renamed every name; compared with this version's
    they would report every ast lock as modified. The check says why instead."""
    runner, db = _init_then_edit(tmp_path, STRICT_SRC, STRICT_SRC)
    _edit_db(db, lambda data: data.update(version="2.0.0"))
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "keepout init" in result.output


def test_init_does_not_overwrite_an_unreadable_keepout_json(tmp_path: Path):
    runner, db = _init_then_edit(tmp_path, STRICT_SRC, STRICT_MOD)
    db.write_text("not json", encoding="utf-8")
    result = runner.invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code != 0
    assert db.read_text(encoding="utf-8") == "not json"


# =====================================================================
# 5. A logic lock trusts an AST match only where it proves equivalence
# =====================================================================

def test_a_logic_lock_does_not_pass_on_a_token_hash_match(tmp_path: Path):
    """A logic lock passed on any AST hash match without running PBT. For
    anything but Python that hash is a token hash that drops every line from
    '#' on, so adding `#[cfg(test)]` -- which compiles the assert out of
    release builds -- matched and passed."""
    original = (
        "// [LOCK: pbt]\n"
        "fn withdraw(balance: u64, amt: u64) -> u64 {\n"
        "    assert!(balance >= amt);\n"
        "    balance - amt\n"
        "}\n"
        "// [/LOCK]\n"
    )
    modified = original.replace("    assert!", "    #[cfg(test)]\n    assert!")
    runner, _ = _init_then_edit(tmp_path, original, modified, name="bank.rs")
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "PASS" not in result.output


def test_a_logic_lock_runs_pbt_when_a_callee_is_swapped(tmp_path: Path):
    """max -> min matched the AST hash of a logic lock, so it passed without
    PBT running."""
    runner, _ = _init_then_edit(
        tmp_path,
        "# [LOCK: pbt]\ndef clamp(x):\n    return max(x, 0)\n# [/LOCK]\n",
        "# [LOCK: pbt]\ndef clamp(x):\n    return min(x, 0)\n# [/LOCK]\n",
    )
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "LOGIC VIOLATION" in result.output


def test_a_logic_lock_still_accepts_formatting_and_local_renames(tmp_path: Path):
    """The shortcut stays where it is sound: Python whose only changes are
    comments, formatting and renames of locals. It matters most for a
    function PBT cannot call -- three arguments here -- which would otherwise
    be reported unverifiable for a pure reformat."""
    runner, _ = _init_then_edit(
        tmp_path,
        "# [LOCK: pbt]\n"
        "def verdict(share, ceiling, sampled):\n"
        "    if share < 0.5 or ceiling > 0.05:\n"
        "        return 'no'\n"
        "    return 'maybe' if sampled else 'yes'\n"
        "# [/LOCK]\n",
        "# [LOCK: pbt]\n"
        "def verdict(s, c, was_sampled):\n"
        "    # tidied\n"
        "    if s < 0.5 or c > 0.05: return 'no'\n"
        "    return 'maybe' if was_sampled else 'yes'\n"
        "# [/LOCK]\n",
    )
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code == 0
    assert "AST structural match" in result.output


# =====================================================================
# 6. The AST lock renames only names local to a function
# =====================================================================

@pytest.mark.parametrize(
    "original, modified",
    [
        ("def f(a, b):\n    return max(a, b)", "def f(a, b):\n    return min(a, b)"),
        ("def check(u):\n    return verify_signature(u)", "def check(u):\n    return always_true(u)"),
        ("def load(data):\n    return json.loads(data)", "def load(data):\n    return pickle.loads(data)"),
        ("transfer(src, dst, amount)", "transfer(dst, src, amount)"),
        (
            "def a(x):\n    return x + 1\ndef b(x):\n    return x - 1\ndef f(x):\n    return a(x)",
            "def a(x):\n    return x + 1\ndef b(x):\n    return x - 1\ndef f(x):\n    return b(x)",
        ),
        ('ADMIN_ROLE = "admin"\nGUEST_ROLE = "guest"', 'GUEST_ROLE = "admin"\nADMIN_ROLE = "guest"'),
        ("def bump():\n    global counter\n    counter += 1", "def bump():\n    global total\n    total += 1"),
        ("def calc(x):\n    return x * 4", "def calculate(x):\n    return x * 4"),
        ("def perms(user) -> dict:\n    return {user: True}", "def perms(user) -> dict:\n    return {user: False}"),
    ],
    ids=[
        "builtin-callee", "callee", "module", "free-argument-order", "region-helper",
        "module-constants-swapped", "declared-global", "function-own-name",
        "return-annotation-dict",
    ],
)
def test_ast_lock_sees_names_visible_outside_the_function(original: str, modified: str):
    """Every name used to be renamed by order of first appearance, so swapping
    one callee, module or global for another hashed the same. Names the region
    defines at module level are kept too: code outside refers to them by name.
    (The last case is the Mojo rewrite, which deleted `name: True` pairs from
    any Python with a return annotation.)"""
    assert compute_ast_hash(original, "py") != compute_ast_hash(modified, "py")


def test_ast_lock_still_ignores_renames_of_locals_in_every_scope():
    original = """
def process(items, factor):
    total = 0
    def scale(v):
        return v * factor
    for item in items:
        try:
            total += scale(item)
        except ValueError as err:
            log(err)
    squares = [n * n for n in items if n > 0]
    key = lambda pair: pair[1]
    return sorted(squares, key=key), total
"""
    renamed = """
def process(xs, k):
    acc = 0
    def mul(w):
        return w * k
    for x in xs:
        try:
            acc += mul(x)
        except ValueError as e:
            log(e)
    sq = [m * m for m in xs if m > 0]
    kf = lambda p: p[1]
    return sorted(sq, key=kf), acc
"""
    assert compute_ast_hash(original, "py") == compute_ast_hash(renamed, "py")

    method = "class Account:\n    def withdraw(self, amount):\n        self.balance -= amount\n"
    method_renamed = "class Account:\n    def withdraw(me, amt):\n        me.balance -= amt\n"
    method_changed = "class Account:\n    def withdraw(me, amt):\n        me.balance += amt\n"
    assert compute_ast_hash(method, "py") == compute_ast_hash(method_renamed, "py")
    assert compute_ast_hash(method, "py") != compute_ast_hash(method_changed, "py")

    comprehension = "totals = [price(i) for i in items]"
    assert compute_ast_hash(comprehension, "py") == compute_ast_hash("totals = [price(j) for j in items]", "py")


# =====================================================================
# 7. PBT compares what the function does, on inputs that reach it
# =====================================================================

PBT_FAST = {"per_call_timeout": 0.5, "time_budget": 30}


@pytest.mark.parametrize(
    "original, modified",
    [
        ("def adult(x):\n    return x >= 18", "def adult(x):\n    return x > 18"),
        ("def ok(x):\n    return x <= 100", "def ok(x):\n    return x < 100"),
        ("def f(x):\n    return x * 2", "def f(x):\n    return 0 if 2 <= x <= 41 else x * 2"),
        ("def f(x):\n    return x * 2", "def f(x):\n    if x == 123456789:\n        return 0\n    return x * 2"),
    ],
    ids=["off-by-one-18", "off-by-one-100", "small-values", "literal-backdoor"],
)
def test_pbt_tries_boundaries_and_the_literals_in_the_code(original: str, modified: str):
    """Only 0, ±1 and ±42 were tried among small integers, and the rest were
    random in ±1e9, so `x >= 18` -> `x > 18` passed. Small integers and the
    neighbours of every integer literal in either version are tried now."""
    result = property_based_test_equivalence(original, modified, lang="py", **PBT_FAST)
    assert result.status_str == "pbt_fail"


@pytest.mark.parametrize(
    "original, modified",
    [
        (
            "def withdraw(x):\n    if x < 0:\n        raise ValueError('negative')\n    return 1000 - x",
            "def withdraw(x):\n    return 1000 - x",
        ),
        ("def f(x):\n    return x * 2", "def f(x):\n    if x < 0:\n        raise ValueError\n    return x * 2"),
        (
            "def f(x):\n    if x < 0:\n        raise ValueError\n    return x",
            "def f(x):\n    if x < 0:\n        raise TypeError\n    return x",
        ),
    ],
    ids=["validation-removed", "raise-added", "exception-type-changed"],
)
def test_pbt_compares_exceptions(original: str, modified: str):
    """An input that raised on either side was skipped, so deleting input
    validation -- or adding a raise for half the domain -- passed."""
    result = property_based_test_equivalence(original, modified, lang="py", **PBT_FAST)
    assert result.status_str == "pbt_fail"


@pytest.mark.parametrize(
    "original, modified",
    [
        (
            "def _helper(x):\n    return x + 1\ndef f(x):\n    return x * 2",
            "def _helper(x):\n    return x + 1\ndef f(x):\n    return 0",
        ),
        (
            "from math import floor\ndef half(x):\n    return floor(x / 2)",
            "from math import floor\ndef half(x):\n    return 0",
        ),
        (
            "def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)",
            "def fact(n):\n    return 1 if n <= 1 else 0",
        ),
        ("def f(x):\n    return {x: True}", "def f(x):\n    return {x: False}"),
        ("def f(x):\n    return x * 2", "def g(x):\n    return x * 2"),
    ],
    ids=["helper-first", "imported-first", "recursive-branch", "dict-values", "function-removed"],
)
def test_pbt_tests_every_function_the_region_defines(original: str, modified: str):
    """The first callable in the region was the only thing tested -- a helper
    defined first, or a function imported at the top. The region ran with
    separate globals and locals, so a recursive call raised NameError and
    only the base case was compared; and the Mojo rewrite turned
    `{x: True}` into `{x}` in Python too."""
    result = property_based_test_equivalence(original, modified, lang="py", **PBT_FAST)
    assert result.status_str == "pbt_fail"


def test_a_helper_pbt_cannot_call_is_fine_only_while_it_is_unchanged():
    helper = (
        "class Box:\n    def __init__(self, v):\n        self.v = v\n"
        "def _label(b: Box) -> str:\n    return f'box:{b.v}'\n"
    )
    unchanged = property_based_test_equivalence(
        helper + "def f(x):\n    return x * 2\n",
        helper + "def f(y):\n    out = y + y\n    return out\n",
        lang="py", **PBT_FAST,
    )
    assert unchanged.status_str == "pbt_pass"

    changed = property_based_test_equivalence(
        helper + "def f(x):\n    return x\n",
        helper.replace("box:", "BOX:") + "def f(x):\n    return x\n",
        lang="py", **PBT_FAST,
    )
    assert changed.status_str == "pbt_unverifiable"
    assert "_label" in changed.message


def test_a_region_that_exits_does_not_end_the_check(tmp_path: Path):
    """PBT called the region in the checking process. `sys.exit(0)` in it
    ended `keepout check` with status 0 -- the lock after it, edited too,
    was never looked at."""
    original = (
        "# [LOCK: pbt]\ndef f(x):\n    return x * 2\n# [/LOCK]\n"
        "# [LOCK: strict]\nMAX = 5\n# [/LOCK]\n"
    )
    modified = (
        "# [LOCK: pbt]\ndef f(x):\n    import sys\n    sys.exit(0)\n# [/LOCK]\n"
        "# [LOCK: strict]\nMAX = 999\n# [/LOCK]\n"
    )
    runner, _ = _init_then_edit(tmp_path, original, modified)
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "LOGIC VIOLATION" in result.output
    assert "LOCK VIOLATION" in result.output


@pytest.mark.parametrize(
    "modified, status",
    [
        ("def f(x):\n    while True:\n        pass", "pbt_fail"),
        ("def f(x):\n    import os\n    os._exit(0)", "pbt_unverifiable"),
        (
            "def f(x):\n    import signal\n"
            "    signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGALRM])\n"
            "    while True:\n        pass",
            "pbt_unverifiable",
        ),
    ],
    ids=["infinite-loop", "os-exit", "uninterruptible-loop"],
)
def test_pbt_survives_a_region_that_hangs_or_kills_its_process(modified: str, status: str):
    """There was no time limit: an infinite loop hung `keepout check`."""
    result = property_based_test_equivalence(
        "def f(x):\n    return x\n", modified, lang="py", per_call_timeout=0.3, time_budget=3,
    )
    assert result.status_str == status


# =====================================================================
# 8. The pre-commit hook checks what is committed, and fails closed
# =====================================================================

needs_git = pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt", reason="needs git and a POSIX shell"
)


def _git_env(tmp_path: Path, with_keepout: bool) -> dict:
    """An environment whose PATH has git, a `keepout` shim for this checkout
    when asked, and nothing from the user's own git config."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    src_dir = Path(__import__("keepout").__file__).resolve().parent.parent
    if with_keepout:
        shim = bin_dir / "keepout"
        shim.write_text(f'#!/bin/sh\nPYTHONPATH="{src_dir}" exec "{sys.executable}" -m keepout.cli "$@"\n')
        shim.chmod(0o755)
    path = os.pathsep.join([str(bin_dir), os.path.dirname(shutil.which("git")), "/usr/bin", "/bin"])
    if not with_keepout and shutil.which("keepout", path=path):
        pytest.skip("a keepout executable is installed system-wide")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": path, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def _repo_with_hook(tmp_path: Path, env: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    (repo / "main.py").write_text(STRICT_SRC, encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(repo)]).exit_code == 0
    assert runner.invoke(cli, ["install-hook", str(repo)]).exit_code == 0
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, env=env, check=True, capture_output=True)
    return repo


@needs_git
def test_the_hook_checks_the_staged_content_not_the_working_tree(tmp_path: Path):
    """The hook ran `keepout check .` on the working tree. Staging a violation
    and then putting the file on disk back committed the violation."""
    env = _git_env(tmp_path, with_keepout=True)
    repo = _repo_with_hook(tmp_path, env)
    (repo / "main.py").write_text(STRICT_MOD, encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=repo, env=env, check=True)
    (repo / "main.py").write_text(STRICT_SRC, encoding="utf-8")

    commit = subprocess.run(["git", "commit", "-qm", "sneaky"], cwd=repo, env=env, capture_output=True, text=True)
    assert commit.returncode != 0
    committed = subprocess.run(["git", "show", "HEAD:main.py"], cwd=repo, env=env, capture_output=True, text=True).stdout
    assert "MAX = 5" in committed


@needs_git
def test_the_hook_blocks_the_commit_when_keepout_is_missing(tmp_path: Path):
    """Without keepout on PATH the hook printed "Skipping check" and the
    commit went through unchecked."""
    env = _git_env(tmp_path, with_keepout=True)
    repo = _repo_with_hook(tmp_path, env)
    (repo / "main.py").write_text(STRICT_MOD, encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=repo, env=env, check=True)

    no_keepout = dict(env)
    no_keepout["PATH"] = os.pathsep.join([os.path.dirname(shutil.which("git")), "/usr/bin", "/bin"])
    if shutil.which("keepout", path=no_keepout["PATH"]):
        pytest.skip("a keepout executable is installed system-wide")
    commit = subprocess.run(["git", "commit", "-qm", "unchecked"], cwd=repo, env=no_keepout, capture_output=True, text=True)
    assert commit.returncode != 0
    assert "not found" in commit.stdout + commit.stderr


@needs_git
def test_install_hook_replaces_an_old_block_and_goes_before_existing_hooks(tmp_path: Path):
    env = _git_env(tmp_path, with_keepout=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)

    hook.write_text("#!/bin/sh\nrun-linters\nexit 0\n", encoding="utf-8")
    assert CliRunner().invoke(cli, ["install-hook", str(repo)]).exit_code == 0
    text = hook.read_text(encoding="utf-8")
    assert text.index("check --staged") < text.index("exit 0")

    old_block = "# KEEPOUT_PRE_COMMIT_HOOK\nkeepout check .\n# KEEPOUT_PRE_COMMIT_HOOK_END\n"
    hook.write_text("#!/bin/sh\n" + old_block + "run-linters\n", encoding="utf-8")
    assert CliRunner().invoke(cli, ["install-hook", str(repo)]).exit_code == 0
    text = hook.read_text(encoding="utf-8")
    assert "check --staged" in text and "keepout check .\n" not in text
    assert text.count("# KEEPOUT_PRE_COMMIT_HOOK\n") == 1
    assert "run-linters" in text


# =====================================================================
# 9. Regions that are not whole Python modules
# =====================================================================

@pytest.mark.parametrize(
    "original, modified, lang",
    [
        ("#define MAX 5\nint f(){ return MAX; }", "#define MAX 999\nint f(){ return MAX; }", "c"),
        (
            "int ok(int u) {\n    if (u != ADMIN) return 0;\n    return 1;\n}",
            "int ok(int u) {\n#if 0\n    if (u != ADMIN) return 0;\n#endif\n    return 1;\n}",
            "c",
        ),
        ("if (check_password(pw))\n    grant_access();", "if (check_password(pw));\n    grant_access();", "c"),
        ("#define CHECK(x) if (!(x)) return -1;\ndo_work();", "#define CHECK(x) if (!(x)) return -1; do_work();", "c"),
        ('const API = "https://api.trusted.example/v1";', 'const API = "https://evil.example/v1";', "js"),
        ("this.#balance -= amount;", "this.#balance += amount;", "js"),
        (
            "fn w(&mut self, a: u64) {\n    assert!(self.b >= a);\n    self.b -= a;\n}",
            "fn w(&mut self, a: u64) {\n    #[cfg(test)]\n    assert!(self.b >= a);\n    self.b -= a;\n}",
            "rs",
        ),
        ("rm -rf /tmp/build", "rm -rf / tmp/build", "sh"),
        ("cd /srv/app\nrm -rf cache", "cd /srv/app rm -rf cache", "sh"),
        ("mov r0, #5", "mov r0, #6", "s"),
        (
            "    x = load()\n    if ok:\n        a()\n    commit(x)",
            "    x = load()\n    if ok:\n        a()\n        commit(x)",
            "py",
        ),
        ("        a()\n    b()", "        a()\n        b()", "py"),
    ],
    ids=[
        "c-define", "c-if-0", "c-empty-if", "c-macro-swallows-line", "js-url-in-string",
        "js-private-field", "rust-cfg-attribute", "sh-space", "sh-newline", "asm-immediate",
        "py-indented-region", "py-ragged-region",
    ],
)
def test_the_fallback_hash_keeps_what_the_language_gives_meaning(original: str, modified: str, lang: str):
    """The fallback for anything ast cannot parse deleted everything from
    '#', '//' or ';' in every language and joined the rest with nothing
    between, so each of these hashed like the original. An indented Python
    region (a lock inside a method) always took that fallback, and lost its
    indentation."""
    assert compute_ast_hash(original, lang) != compute_ast_hash(modified, lang)


@pytest.mark.parametrize(
    "original, modified, lang",
    [
        ("int f(int x){return x*2;}", "int f(int x) {\n    // doubles\n    return x * 2; /* ok */\n}", "c"),
        ('char *u = "http://a";  int y=1;', 'char *u = "http://a";\nint y = 1; // note', "c"),
        ("rm -rf /tmp/build\necho done", "# clean\nrm -rf /tmp/build\n\necho done  # ok", "sh"),
        ("(foo a b)", "(foo a   ; why\n b)", "lisp"),
        ("    x = load()\n    if ok:\n        a()", "    x = load()  # get\n\n    if ok:\n        # yes\n        a()", "py"),
    ],
    ids=["c", "c-url", "sh", "lisp", "py-indented-region"],
)
def test_the_fallback_hash_still_ignores_comments_and_formatting(original: str, modified: str, lang: str):
    assert compute_ast_hash(original, lang) == compute_ast_hash(modified, lang)


# =====================================================================
# 10. Directives, unlocking and lock identity
# =====================================================================

def _write(tmp_path: Path, text: str, name: str = "main.py") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "directive",
    ["# [LOCK: strct]", "# [LOCK: ast nmae=\"calc\"]", "# [LOCK:]", "# [LOCK: strict name=\"has space\"]"],
    ids=["mode-typo", "attribute-typo", "empty-mode", "bad-name"],
)
def test_a_directive_keepout_does_not_understand_is_an_error(tmp_path: Path, directive: str):
    """An unknown mode fell into the attributes and became an ast lock:
    `[LOCK: strct]` meant to be strict was checked as ast, silently."""
    path = _write(tmp_path, f"{directive}\nMAX = 5\n# [/LOCK]\n")
    with pytest.raises(LockParserError):
        parse_file_locks(path)


def test_an_uppercase_mode_is_the_mode_it_names(tmp_path: Path):
    path = _write(tmp_path, "# [LOCK: STRICT]\nMAX = 5\n# [/LOCK]\n")
    assert parse_file_locks(path)[0].mode == "strict"


def test_a_directive_inside_a_string_is_not_a_lock(tmp_path: Path):
    path = _write(tmp_path, 'print("# [LOCK]")\nx = 1\nprint("# [/LOCK]")\n')
    assert parse_file_locks(path) == []


def test_unlock_reason_inside_a_string_does_not_unlock(tmp_path: Path):
    """`UNLOCK_REASON:` anywhere in a line unlocked the region, a string
    literal included."""
    runner, _ = _init_then_edit(
        tmp_path, STRICT_SRC, '# [LOCK: strict]\nMAX = 999; NOTE = "UNLOCK_REASON: x"\n# [/LOCK]\n'
    )
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert "UNLOCKED" not in result.output


def test_unlock_reason_comment_unlocks_unless_no_unlock_is_given(tmp_path: Path):
    runner, _ = _init_then_edit(
        tmp_path, STRICT_SRC, "# [LOCK: strict]\n# UNLOCK_REASON: raise the cap for the migration\nMAX = 999\n# [/LOCK]\n"
    )
    unlocked = runner.invoke(cli, ["check", str(tmp_path)])
    assert unlocked.exit_code == 0
    assert "UNLOCKED" in unlocked.output

    refused = runner.invoke(cli, ["check", "--no-unlock", str(tmp_path)])
    assert refused.exit_code != 0
    assert "LOCK VIOLATION" in refused.output


def test_adding_a_named_lock_above_does_not_shift_the_others(tmp_path: Path):
    """Locks were keyed by position, so a lock added at the top took the key
    of the one below, and every lock after it was compared with its
    neighbour's record."""
    two = (
        '# [LOCK: strict name="cap"]\nMAX = 5\n# [/LOCK]\n'
        '# [LOCK: strict name="floor"]\nMIN = 1\n# [/LOCK]\n'
    )
    runner, _ = _init_then_edit(
        tmp_path, two, '# [LOCK: strict name="rate"]\nRATE = 2\n# [/LOCK]\n' + two
    )
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert "✅ [PASS] main.py#cap" in result.output
    assert "✅ [PASS] main.py#floor" in result.output
    assert "LOCK VIOLATION" not in result.output
    assert "UNREGISTERED LOCK] main.py#rate" in result.output


def test_two_locks_with_one_name_is_an_error(tmp_path: Path):
    path = _write(
        tmp_path,
        '# [LOCK: strict name="cap"]\nMAX = 5\n# [/LOCK]\n# [LOCK: strict name="cap"]\nMIN = 1\n# [/LOCK]\n',
    )
    with pytest.raises(LockParserError):
        parse_file_locks(path)


def test_a_hand_edited_original_code_is_refused_before_it_runs(tmp_path: Path):
    """A logic lock executes original_code from keepout.json. One that does
    not hash to the recorded strict_hash was written by something other
    than `keepout init`, and is not run."""
    marker = tmp_path / "ran.txt"
    runner, db = _init_then_edit(tmp_path, PBT_SRC, PBT_MOD)
    _edit_db(db, lambda data: data["locks"]["main.py#lock_0"].update(
        original_code=f"open({str(marker)!r}, 'w').write('x')\ndef f(x):\n    return x * 3\n"
    ))
    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code != 0
    assert not marker.exists()


# =====================================================================
# 11. --base: the baseline is the code in git, not keepout.json
# =====================================================================

def _plain_git_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        **os.environ, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def _commit(repo: Path, env: dict, files: dict, message: str = "change") -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, env=env, check=True)


def _repo(tmp_path: Path, files: dict, init: bool = True):
    env = _plain_git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], env=env, check=True)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    if init:
        assert CliRunner().invoke(cli, ["init", str(repo)]).exit_code == 0
    _commit(repo, env, {}, "base")
    return repo, env


@needs_git
def test_base_check_catches_a_change_rerecorded_in_keepout_json(tmp_path: Path):
    """Compared with keepout.json, editing a locked region and running
    `keepout init` passed: the record now said the edit was the original.
    Compared with the code at the base commit, it is a change."""
    repo, _ = _repo(tmp_path, {"main.py": STRICT_SRC})
    (repo / "main.py").write_text(STRICT_MOD, encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(repo)]).exit_code == 0

    against_record = runner.invoke(cli, ["check", str(repo)])
    assert against_record.exit_code == 0  # the hole --base closes

    against_git = runner.invoke(cli, ["check", "--base", "HEAD", str(repo)])
    assert against_git.exit_code != 0
    assert "LOCK VIOLATION" in against_git.output
    assert "re-records main.py#lock_0" in against_git.output


@needs_git
def test_base_check_reports_a_removed_lock(tmp_path: Path):
    repo, _ = _repo(tmp_path, {"main.py": STRICT_SRC})
    (repo / "main.py").write_text("MAX = 999\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["check", "--base", "HEAD", str(repo)])
    assert result.exit_code != 0
    assert "LOCK REMOVED" in result.output


@needs_git
def test_base_check_rejects_weakening_the_mode(tmp_path: Path):
    repo, _ = _repo(tmp_path, {"main.py": STRICT_SRC})
    (repo / "main.py").write_text("# [LOCK: ast]\n# harmless?\nMAX = 5\n# [/LOCK]\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["check", "--base", "HEAD", str(repo)])
    assert result.exit_code != 0
    assert "MODE MISMATCH" in result.output


@needs_git
def test_base_check_lets_a_new_lock_in(tmp_path: Path):
    repo, _ = _repo(tmp_path, {"main.py": STRICT_SRC})
    (repo / "extra.py").write_text('# [LOCK: strict name="rate"]\nRATE = 2\n# [/LOCK]\n', encoding="utf-8")
    result = CliRunner().invoke(cli, ["check", "--base", "HEAD", str(repo)])
    assert result.exit_code == 0
    assert "NEW LOCK] extra.py#rate" in result.output


@needs_git
def test_base_check_runs_pbt_on_the_committed_code_not_on_keepout_json(tmp_path: Path):
    """A logic lock executes the original. Under --base that is the base
    commit's code; keepout.json's original_code is never run."""
    marker = tmp_path / "ran.txt"
    repo, _ = _repo(tmp_path, {"main.py": PBT_SRC})
    _edit_db(repo / DEFAULT_DB_NAME, lambda data: data["locks"]["main.py#lock_0"].update(
        original_code=f"open({str(marker)!r}, 'w').write('x')\ndef f(x):\n    return x * 3\n"
    ))
    (repo / "main.py").write_text(PBT_MOD, encoding="utf-8")
    result = CliRunner().invoke(cli, ["check", "--base", "HEAD", str(repo)])
    assert result.exit_code != 0
    assert "LOGIC VIOLATION" in result.output
    assert not marker.exists()


@needs_git
def test_base_check_compares_with_the_branch_point(tmp_path: Path):
    """A lock changed on main after the branch was cut is not the branch's
    change: the baseline is the merge base, as in a pull request diff."""
    repo, env = _repo(tmp_path, {"main.py": STRICT_SRC})
    subprocess.run(["git", "switch", "-qc", "feature"], cwd=repo, env=env, check=True)
    _commit(repo, env, {"notes.py": "x = 1\n"}, "feature work")
    subprocess.run(["git", "switch", "-q", "main"], cwd=repo, env=env, check=True)
    _commit(repo, env, {"main.py": "# [LOCK: strict]\n# UNLOCK_REASON: raise the cap\nMAX = 7\n# [/LOCK]\n"}, "raise cap")
    subprocess.run(["git", "switch", "-q", "feature"], cwd=repo, env=env, check=True)

    result = CliRunner().invoke(cli, ["check", "--base", "main", str(repo)])
    assert result.exit_code == 0, result.output


@needs_git
def test_base_check_explains_a_ref_it_cannot_find(tmp_path: Path):
    repo, _ = _repo(tmp_path, {"main.py": STRICT_SRC})
    result = CliRunner().invoke(cli, ["check", "--base", "origin/main", str(repo)])
    assert result.exit_code != 0
    assert "fetch-depth" in result.output


@needs_git
def test_base_head_in_a_repository_without_commits_treats_every_lock_as_new(tmp_path: Path):
    env = _plain_git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    (repo / "main.py").write_text(STRICT_SRC, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    result = CliRunner().invoke(cli, ["check", "--staged", "--base", "HEAD", str(repo)])
    assert result.exit_code == 0, result.output
    assert "NEW LOCK" in result.output


@needs_git
def test_an_unlock_reason_already_in_the_baseline_does_not_unlock_the_next_change(tmp_path: Path):
    """UNLOCK_REASON left in the code kept the region unlocked for every
    later edit. It now unlocks only a change whose baseline does not carry
    the same reason."""
    unlocked_once = "# [LOCK: strict]\n# UNLOCK_REASON: raise the cap\nMAX = 7\n# [/LOCK]\n"
    repo, _ = _repo(tmp_path, {"main.py": unlocked_once})
    runner = CliRunner()

    (repo / "main.py").write_text(unlocked_once.replace("MAX = 7", "MAX = 999"), encoding="utf-8")
    for args in (["check", "--base", "HEAD", str(repo)], ["check", str(repo)]):
        stale = runner.invoke(cli, args)
        assert stale.exit_code != 0, args
        assert "already in" in stale.output

    (repo / "main.py").write_text(
        unlocked_once.replace("MAX = 7", "MAX = 999").replace("raise the cap", "cap for the migration"),
        encoding="utf-8",
    )
    fresh = runner.invoke(cli, ["check", "--base", "HEAD", str(repo)])
    assert fresh.exit_code == 0
    assert "UNLOCKED" in fresh.output


# =====================================================================
# 12. PBT arguments come from the parameters' types
# =====================================================================

@pytest.mark.parametrize(
    "original, modified",
    [
        ("def add(a: int, b: int) -> int:\n    return a + b", "def add(a: int, b: int) -> int:\n    return a - b"),
        ("def slug(s: str) -> str:\n    return s.strip().lower()", "def slug(s: str) -> str:\n    return s.lower()"),
        (
            "def tier(code: str) -> str:\n    return 'gold' if code == 'VIP' else 'std'",
            "def tier(code: str) -> str:\n    return 'gold' if code == 'vip' else 'std'",
        ),
        ("def third(x: float) -> float:\n    return x / 3", "def third(x: float) -> float:\n    return x * 0.3333"),
        (
            "def fee(amount: int, *, member: bool) -> int:\n    return amount // 2 if member else amount",
            "def fee(amount: int, *, member: bool) -> int:\n    return amount // 2 if not member else amount",
        ),
        (
            "def name(n: str | None) -> str:\n    return n if n is not None else 'anon'",
            "def name(n: str | None) -> str:\n    return n or 'anon'",
        ),
        (
            "from typing import Literal\ndef price(plan: Literal['basic', 'pro']) -> int:\n    return 10 if plan == 'basic' else 30",
            "from typing import Literal\ndef price(plan: Literal['basic', 'pro']) -> int:\n    return 10 if plan == 'pro' else 30",
        ),
        ("def limit():\n    return 5", "def limit():\n    return 6"),
    ],
    ids=["two-ints", "str", "str-literal", "float", "keyword-only-bool", "optional", "literal", "no-arguments"],
)
def test_pbt_calls_functions_with_arguments_of_their_types(original: str, modified: str):
    """Every function used to get one int. A function of two ints, of a
    string, of a float or of nothing at all raised TypeError on every call,
    and a change to it could not be seen."""
    result = property_based_test_equivalence(original, modified, lang="py", **PBT_FAST)
    assert result.status_str == "pbt_fail", result.message


def test_pbt_compares_what_a_function_does_to_its_arguments():
    result = property_based_test_equivalence(
        "def push(xs: list[int], v: int) -> None:\n    xs.append(v)",
        "def push(xs: list[int], v: int) -> None:\n    xs.insert(0, v)",
        lang="py", **PBT_FAST,
    )
    assert result.status_str == "pbt_fail"
    assert "Arguments after the call" in result.message


@pytest.mark.parametrize(
    "original, modified",
    [
        ("def half(x: float) -> float:\n    return x / 2", "def half(x: float) -> float:\n    return x * 0.5"),
        ("def same(x: float) -> float:\n    return x * 1.0", "def same(y: float) -> float:\n    return y + 0.0"),
        (
            "def total(xs: list[int]) -> int:\n    return sum(xs)",
            "def total(xs: list[int]) -> int:\n    t = 0\n    for x in xs:\n        t += x\n    return t",
        ),
    ],
    ids=["float-exact", "float-nan-and-signed-zero", "list"],
)
def test_pbt_still_passes_an_equivalent_refactor(original: str, modified: str):
    result = property_based_test_equivalence(original, modified, lang="py", **PBT_FAST)
    assert result.status_str == "pbt_pass", result.message


def test_a_counterexample_of_several_arguments_is_cached_as_a_call(tmp_path: Path):
    runner, db = _init_then_edit(
        tmp_path,
        "# [LOCK: pbt]\ndef add(a: int, b: int) -> int:\n    return a + b\n# [/LOCK]\n",
        "# [LOCK: pbt]\ndef add(a: int, b: int) -> int:\n    return a - b\n# [/LOCK]\n",
    )
    first = runner.invoke(cli, ["check", str(tmp_path)])
    assert first.exit_code != 0
    cached = json.loads(db.read_text(encoding="utf-8"))["locks"]["main.py#lock_0"]["cached_counterexamples"]
    assert cached and cached[0]["function"] == "add" and len(cached[0]["args"]) == 2

    again = runner.invoke(cli, ["check", str(tmp_path)])
    assert "Instant Regression Detected" in again.output


_ENUM = "import enum\nclass Plan(enum.Enum):\n    BASIC = 1\n    PRO = 2\n"
_POINT = "from dataclasses import dataclass\n@dataclass\nclass Pt:\n    x: int\n    y: int\n"
_ERROR = "class Bad(Exception):\n    pass\n"


@pytest.mark.parametrize(
    "original, modified, status",
    [
        (_ENUM + "def price(p: Plan) -> int:\n    return 10 if p is Plan.BASIC else 30",
         _ENUM + "def price(p: Plan) -> int:\n    return 30 if p is not Plan.BASIC else 10", "pbt_pass"),
        (_ENUM + "def price(p: Plan) -> int:\n    return 10 if p is Plan.BASIC else 30",
         _ENUM + "def price(p: Plan) -> int:\n    return 10 if p is Plan.PRO else 30", "pbt_fail"),
        (_ENUM + "def plan(x: int) -> Plan:\n    return Plan.PRO if x > 0 else Plan.BASIC",
         _ENUM + "def plan(x: int) -> Plan:\n    return Plan.BASIC if x <= 0 else Plan.PRO", "pbt_pass"),
        (_POINT + "def mk(x: int) -> Pt:\n    return Pt(x, x)", _POINT + "def mk(v: int) -> Pt:\n    return Pt(x=v, y=v)", "pbt_pass"),
        (_POINT + "def mk(x: int) -> Pt:\n    return Pt(x, x)", _POINT + "def mk(x: int) -> Pt:\n    return Pt(x, -x)", "pbt_fail"),
        (_ERROR + "def f(x: int) -> int:\n    if x < 0:\n        raise Bad()\n    return x",
         _ERROR + "def f(x: int) -> int:\n    if x >= 0:\n        return x\n    raise Bad()", "pbt_pass"),
        (_ERROR + "def f(x: int) -> int:\n    if x < 0:\n        raise Bad()\n    return x",
         _ERROR + "def f(x: int) -> int:\n    if x < 0:\n        raise ValueError()\n    return x", "pbt_fail"),
    ],
    ids=["enum-arg-same", "enum-arg-changed", "enum-return-same", "dataclass-same", "dataclass-changed",
         "own-exception-same", "own-exception-changed"],
)
def test_classes_the_region_defines_compare_by_name_and_content(original: str, modified: str, status: str):
    """Each version of the region is executed on its own, so a class it
    defines exists twice. Compared by identity, Plan.PRO from the original
    was never the modified version's Plan.PRO, and every equivalent refactor
    of code using its own enum, dataclass or exception failed."""
    result = property_based_test_equivalence(original, modified, lang="py", **PBT_FAST)
    assert result.status_str == status, result.message


# =====================================================================
# 13. Configuration and infrastructure files; scanning what git tracks
# =====================================================================

@pytest.mark.parametrize(
    "original, modified, lang",
    [
        ("env:\n  - name: A\ndebug: true\n", "env:\n  - name: A\n  debug: true\n", "yaml"),
        ("retries: 3\n", "retries:3\n", "yml"),
        ("url: http://x/#frag\n", "url: http://x/#other\n", "yaml"),
        ("timeout = 30\n", "timeout = 300\n", "toml"),
        ('ingress { cidr_blocks = ["10.0.0.0/8"] }\n', 'ingress { cidr_blocks = ["0.0.0.0/0"] }\n', "tf"),
        ("SELECT * FROM orders WHERE tenant_id = :tid;\n", "SELECT * FROM orders WHERE 1 = 1;\n", "sql"),
        ("RUN rm -rf /tmp/build\n", "RUN rm -rf / tmp/build\n", "dockerfile"),
        ("RUN echo #a\n", "RUN echo #b\n", "dockerfile"),
        ("deploy:\n\t./deploy.sh --dry-run\n", "deploy:\n\t./deploy.sh\n", "make"),
    ],
    ids=["yaml-nesting", "yaml-mapping-space", "yaml-hash-in-value", "toml", "terraform", "sql",
         "dockerfile-space", "dockerfile-hash-mid-line", "make"],
)
def test_configuration_changes_are_changes(original: str, modified: str, lang: str):
    assert compute_ast_hash(original, lang) != compute_ast_hash(modified, lang)


@pytest.mark.parametrize(
    "original, modified, lang",
    [
        ("env:\n  - name: A\n", "# vars\nenv:\n\n  - name: A   # first\n", "yaml"),
        ("timeout = 30 # s\n", "timeout = 30 # seconds\n", "toml"),
        ('a = 1\nb = "x"\n', '# one\na = 1 // still one\n/* b */ b = "x"\n', "tf"),
        ("SELECT 1;\n", "-- check\nSELECT /* one */ 1;\n", "sql"),
        ("FROM python:3.12\nRUN make\n", "# base image\nFROM python:3.12\n\n  # build\nRUN make\n", "dockerfile"),
        ("deploy:\n\t./deploy.sh\n", "# ship it\ndeploy:\n\t./deploy.sh  # go\n", "make"),
    ],
    ids=["yaml", "toml", "terraform", "sql", "dockerfile", "make"],
)
def test_configuration_comments_are_not_changes(original: str, modified: str, lang: str):
    assert compute_ast_hash(original, lang) == compute_ast_hash(modified, lang)


@pytest.mark.parametrize(
    "name, text",
    [
        ("Makefile", "# [LOCK: strict name=\"deploy\"]\ndeploy:\n\t./deploy.sh\n# [/LOCK]\n"),
        ("Dockerfile.prod", "# [LOCK: strict]\nUSER app\n# [/LOCK]\n"),
        ("compose.yml", "# [LOCK: ast]\nports:\n  - \"127.0.0.1:8181:8181\"\n# [/LOCK]\n"),
        ("schema.sql", "-- [LOCK: strict]\nCREATE POLICY tenant ON orders USING (tenant_id = current_tenant());\n-- [/LOCK]\n"),
        ("main.tf", "# [LOCK: strict]\nresource \"x\" \"y\" {}\n# [/LOCK]\n"),
    ],
)
def test_locks_in_configuration_files_are_found(tmp_path: Path, name: str, text: str):
    (tmp_path / name).write_text(text, encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0
    assert f"{name}#" in result.output


@needs_git
def test_files_gitignore_excludes_are_not_scanned(tmp_path: Path):
    repo, env = _repo(tmp_path, {".gitignore": "generated/\n"}, init=False)
    (repo / "generated").mkdir()
    (repo / "generated" / "out.py").write_text("# [LOCK]\nx = 1\n# [/LOCK]\n", encoding="utf-8")
    (repo / "new.py").write_text("# [LOCK]\ny = 2\n# [/LOCK]\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(repo)])
    assert "new.py#lock_0" in result.output  # untracked but not ignored
    assert "generated/out.py" not in result.output


def test_only_the_files_own_comment_syntax_unlocks(tmp_path: Path):
    """`; UNLOCK_REASON: x` is an empty statement and a label in C -- code
    that runs -- and it unlocked the region because ';' is a comment in Lisp."""
    src = "// [LOCK: strict]\nint max = 5;\n// [/LOCK]\n"
    runner, _ = _init_then_edit(tmp_path, src, "// [LOCK: strict]\n; UNLOCK_REASON: x\nint max = 999;\n// [/LOCK]\n", name="limits.c")
    assert runner.invoke(cli, ["check", str(tmp_path)]).exit_code != 0

    (tmp_path / "limits.c").write_text("// [LOCK: strict]\n// UNLOCK_REASON: raise for the migration\nint max = 999;\n// [/LOCK]\n", encoding="utf-8")
    unlocked = runner.invoke(cli, ["check", str(tmp_path)])
    assert unlocked.exit_code == 0
    assert "UNLOCKED" in unlocked.output
