import pytest
from pathlib import Path
from click.testing import CliRunner

from keepout.core import (
    DEFAULT_DB_NAME,
    LockBlock,
    LockParserError,
    parse_file_locks,
    compute_strict_hash,
    load_locks_db,
    save_locks_db,
    sync_blocks_to_db,
    prove_universal_equivalence,
)
from keepout.cli import cli


# =====================================================================
# 1. Multi-Language Z3 Formal Verification Engine Tests
# =====================================================================

def test_python_formal_proof_pass_and_fail():
    code_a = "def calc(x):\n    return x * 4"
    code_b = "def calc(x):\n    return x << 2"
    code_bad = "def calc(x):\n    return x * 5"

    res1 = prove_universal_equivalence(code_a, code_b, lang="py")
    assert res1.is_equivalent is True

    res2 = prove_universal_equivalence(code_a, code_bad, lang="py")
    assert res2.is_equivalent is False
    assert "ARG_x" in res2.counterexample["inputs"]


def test_mojo_formal_proof_pass_and_fail():
    code_a = "fn calc(x: Int) -> Int:\n    return x * 4"
    code_b = "fn calc(x: Int) -> Int:\n    return x << 2"
    code_bad = "fn calc(x: Int) -> Int:\n    return x * 5"

    res1 = prove_universal_equivalence(code_a, code_b, lang="mojo")
    assert res1.is_equivalent is True

    res2 = prove_universal_equivalence(code_a, code_bad, lang="mojo")
    assert res2.is_equivalent is False
    assert "ARG_x" in res2.counterexample["inputs"]


def test_scheme_lisp_formal_proof_pass_and_fail():
    code_a = "(* x 4)"
    code_b = "(ash x 2)"
    code_bad = "(* x 5)"

    res1 = prove_universal_equivalence(code_a, code_b, lang="scm")
    assert res1.is_equivalent is True

    res2 = prove_universal_equivalence(code_a, code_bad, lang="scm")
    assert res2.is_equivalent is False


def test_apl_array_formal_proof_pass_and_fail():
    code_a = "y ← 4 × x"
    code_b = "y ← x << 2"
    code_bad = "y ← 5 × x"

    res1 = prove_universal_equivalence(code_a, code_b, lang="apl")
    assert res1.is_equivalent is True

    res2 = prove_universal_equivalence(code_a, code_bad, lang="apl")
    assert res2.is_equivalent is False


def test_verilog_hardware_formal_proof_pass():
    code_a = "assign b = a * 4;"
    code_b = "assign b = a << 2;"
    res = prove_universal_equivalence(code_a, code_b, lang="v")
    assert res.is_equivalent is True


def test_solidity_smart_contract_formal_proof_pass():
    code_a = "return x * 4;"
    code_b = "return x << 2;"
    res = prove_universal_equivalence(code_a, code_b, lang="sol")
    assert res.is_equivalent is True


def test_llvm_ir_equivalence_proof():
    ir_a = "define i32 @calc(i32 %0) { %2 = mul i32 %0, 4 \n ret i32 %2 }"
    ir_b = "define i32 @calc(i32 %0) { %2 = shl i32 %0, 2 \n ret i32 %2 }"
    res = prove_universal_equivalence(ir_a, ir_b, lang="ll")
    assert res.is_equivalent is True


# =====================================================================
# 2. Parser, Storage & CLI Integration Tests
# =====================================================================

def test_parse_strict_lock(tmp_path: Path):
    file_path = tmp_path / "test.c"
    file_path.write_text(
        "int main() {\n"
        "// [LOCK: strict name=\"core_calc\"]\n"
        "    int a = 10;\n"
        "    int b = 20;\n"
        "    return a + b;\n"
        "// [/LOCK]\n"
        "}\n",
        encoding="utf-8"
    )

    blocks = parse_file_locks(file_path)
    assert len(blocks) == 1
    assert blocks[0].mode == "strict"


def test_cli_init_and_check_pass(tmp_path: Path):
    src_file = tmp_path / "main.py"
    src_file.write_text(
        "# [LOCK: logic name=\"calc\"]\n"
        "def calc(x):\n"
        "    return x * 4\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )

    runner = CliRunner()
    result_init = runner.invoke(cli, ["init", str(tmp_path)])
    assert result_init.exit_code == 0
    assert (tmp_path / DEFAULT_DB_NAME).exists()

    # Refactor python code
    src_file.write_text(
        "# [LOCK: logic name=\"calc\"]\n"
        "def calc(x):\n"
        "    return x << 2\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )

    result_check = runner.invoke(cli, ["check", str(tmp_path)])
    assert result_check.exit_code == 0
    assert "FORMAL PROOF PASS" in result_check.output
