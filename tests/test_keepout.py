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
    prove_universal_equivalence,
    property_based_test_equivalence,
)
from keepout.cli import cli


# =====================================================================
# 1. AST Structural Hash Lock & PBT Engine Tests
# =====================================================================

def test_ast_hash_ignores_formatting_and_comments():
    code1 = "def calc(x):\n    # Comment line\n    int_a = 10\n    return x * 4"
    code2 = "def calc(x):\n\n    int_a = 10; return x * 4"
    code_mod = "def calc(x):\n    int_a = 20\n    return x * 4"

    h1 = compute_ast_hash(code1, lang="py")
    h2 = compute_ast_hash(code2, lang="py")
    h_mod = compute_ast_hash(code_mod, lang="py")

    assert h1 == h2
    assert h1 != h_mod


def test_property_based_testing_fuzzing():
    code_a = "def calc(x):\n    return x * 4"
    code_b = "def calc(x):\n    return x << 2"
    code_bad = "def calc(x):\n    return x * 5"

    res1 = property_based_test_equivalence(code_a, code_b, lang="py", num_samples=1000)
    assert res1.is_equivalent is True
    assert res1.status_str == "pbt_pass"

    res2 = property_based_test_equivalence(code_a, code_bad, lang="py", num_samples=1000)
    assert res2.is_equivalent is False
    assert res2.status_str == "pbt_fail"
    assert res2.counterexample["input"] == 1 or res2.counterexample["input"] != 0


# =====================================================================
# 2. Multi-Language Formal Verification Engine Tests
# =====================================================================

def test_python_formal_proof_pass_and_fail():
    code_a = "def calc(x):\n    return x * 4"
    code_b = "def calc(x):\n    return x << 2"
    code_bad = "def calc(x):\n    return x * 5"

    res1 = prove_universal_equivalence(code_a, code_b, lang="py")
    assert res1.is_equivalent is True

    res2 = prove_universal_equivalence(code_a, code_bad, lang="py")
    assert res2.is_equivalent is False


def test_mojo_formal_proof_pass_and_fail():
    code_a = "fn calc(x: Int) -> Int:\n    return x * 4"
    code_b = "fn calc(x: Int) -> Int:\n    return x << 2"
    code_bad = "fn calc(x: Int) -> Int:\n    return x * 5"

    res1 = prove_universal_equivalence(code_a, code_b, lang="mojo")
    assert res1.is_equivalent is True

    res2 = prove_universal_equivalence(code_a, code_bad, lang="mojo")
    assert res2.is_equivalent is False


# =====================================================================
# 3. Parser, Storage & CLI Integration Tests
# =====================================================================

def test_cli_ast_lock_mode(tmp_path: Path):
    src_file = tmp_path / "main.py"
    src_file.write_text(
        "# [LOCK: ast name=\"calc\"]\n"
        "def calc(x):\n"
        "    # Initial comment\n"
        "    return x * 4\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )

    runner = CliRunner()
    result_init = runner.invoke(cli, ["init", str(tmp_path)])
    assert result_init.exit_code == 0

    # Modify whitespace and comments (should PASS)
    src_file.write_text(
        "# [LOCK: ast name=\"calc\"]\n"
        "def calc(x):\n"
        "    # Updated comment with different indentation\n\n"
        "    return x * 4\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )
    res_pass = runner.invoke(cli, ["check", str(tmp_path)])
    assert res_pass.exit_code == 0
    assert "AST structural lock" in res_pass.output

    # Modify code AST structure (should FAIL)
    src_file.write_text(
        "# [LOCK: ast name=\"calc\"]\n"
        "def calc(x):\n"
        "    return x * 5\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )
    res_fail = runner.invoke(cli, ["check", str(tmp_path)])
    assert res_fail.exit_code != 0
    assert "AST STRUCTURAL VIOLATION" in res_fail.output
