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
# 2. Smart Property-Based Testing Sub-Engine (PBT / 10,000 Fuzzing Samples)
# =====================================================================

def test_property_based_testing_pass_and_fail():
    code_a = "def calc(x):\n    return x * 4"
    code_b = "def calc(x):\n    return x << 2"
    code_bad = "def calc(x):\n    return x * 5"

    res1 = property_based_test_equivalence(code_a, code_b, lang="py", num_samples=10000)
    assert res1.is_equivalent is True
    assert res1.status_str == "pbt_pass"

    res2 = property_based_test_equivalence(code_a, code_bad, lang="py", num_samples=10000)
    assert res2.is_equivalent is False
    assert res2.status_str == "pbt_fail"
    assert res2.counterexample["input"] == 1 or res2.counterexample["input"] != 0


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
