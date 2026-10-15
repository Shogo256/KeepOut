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
def calculate(x):

    r = x * 4; return r
"""

    code_struct_mod = """
def calculate(x):
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
        "def calculate(x):\n"
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
        "def calculate(x):\n"
        "    r = x * 5; return r\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )
    res_fail = runner.invoke(cli, ["check", str(tmp_path)])
    assert res_fail.exit_code != 0
    assert "AST STRUCTURAL VIOLATION" in res_fail.output


def test_a_region_the_engine_cannot_call_is_not_a_pass():
    """The PBT engine calls the target with one integer argument and execs
    the region in an empty namespace. A function taking several arguments, or
    reading a module-level name, raises on every generated input.

    Those raises are skipped, which is right for a function that rejects some
    inputs. It used to be the only behaviour, so a function that could not be
    called at all ended the loop having compared nothing and was reported as
    passing -- including when its body was replaced wholesale.

    A check that cannot fail is worse than no check: an absent lock is
    visible, and a green one is not.
    """
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
    assert not result.is_equivalent
    assert result.status_str == "pbt_unverifiable"
    assert "verified nothing" in result.message or "Nothing was verified" in result.message
    assert "TypeError" in result.message


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
