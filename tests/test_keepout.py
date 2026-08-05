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
    prove_asm_equivalence,
)
from keepout.cli import cli


# =====================================================================
# 1. Parser Tests
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
    assert blocks[0].target_symbol == "core_calc"
    assert "int a = 10;" in blocks[0].content
    assert blocks[0].start_line == 2
    assert blocks[0].end_line == 6


def test_parse_logic_lock(tmp_path: Path):
    file_path = tmp_path / "test.py"
    file_path.write_text(
        "# [LOCK: logic]\n"
        "def compute(x):\n"
        "    return x * 4\n"
        "# [/LOCK]\n",
        encoding="utf-8"
    )

    blocks = parse_file_locks(file_path)
    assert len(blocks) == 1
    assert blocks[0].mode == "logic"
    assert blocks[0].target_symbol == "compute"


def test_parse_unlock_reason(tmp_path: Path):
    file_path = tmp_path / "test.rs"
    file_path.write_text(
        "// [LOCK: strict] // UNLOCK_REASON: Refactoring algorithm\n"
        "fn mult(x: i32) -> i32 {\n"
        "    x << 2\n"
        "}\n"
        "// [/LOCK]\n",
        encoding="utf-8"
    )

    blocks = parse_file_locks(file_path)
    assert len(blocks) == 1
    assert blocks[0].unlock_reason == "Refactoring algorithm"


def test_unmatched_end_tag(tmp_path: Path):
    file_path = tmp_path / "bad.c"
    file_path.write_text("// [/LOCK]\n", encoding="utf-8")
    with pytest.raises(LockParserError, match="Unmatched"):
        parse_file_locks(file_path)


def test_unclosed_tag(tmp_path: Path):
    file_path = tmp_path / "unclosed.c"
    file_path.write_text("// [LOCK: strict]\nint a = 1;\n", encoding="utf-8")
    with pytest.raises(LockParserError, match="Unclosed"):
        parse_file_locks(file_path)


# =====================================================================
# 2. Storage Tests
# =====================================================================

def test_hash_normalization():
    h1 = compute_strict_hash("line1\r\nline2\r\n")
    h2 = compute_strict_hash("line1\nline2\n")
    assert h1 == h2


def test_storage_lifecycle(tmp_path: Path):
    db_path = tmp_path / "keepout.json"
    dummy_block = LockBlock(
        file_path=tmp_path / "src" / "math.c",
        lock_index=0,
        mode="strict",
        content="int x = 42;\n",
        start_line=10,
        end_line=12,
        target_symbol="x",
    )

    db_data = sync_blocks_to_db(db_path, [dummy_block], tmp_path)
    assert db_path.exists()
    assert "src/math.c#lock_0" in db_data["locks"]

    loaded = load_locks_db(db_path)
    assert loaded["locks"]["src/math.c#lock_0"]["strict_hash"] == compute_strict_hash("int x = 42;\n")


# =====================================================================
# 3. Z3 Engine Verification Tests
# =====================================================================

def test_bitshift_vs_multiplication_equivalence():
    asm_a = "movq %rdi, %rax\nimulq $4, %rax\nret"
    asm_b = "movq %rdi, %rax\nsalq $2, %rax\nret"
    res = prove_asm_equivalence(asm_a, asm_b)
    assert res.is_equivalent is True
    assert res.status_str == "unsat"


def test_lea_vs_shift_add_equivalence():
    asm_a = "leaq (%rdi,%rsi,4), %rax\nret"
    asm_b = "movq %rsi, %rax\nshlq $2, %rax\naddq %rdi, %rax\nret"
    res = prove_asm_equivalence(asm_a, asm_b)
    assert res.is_equivalent is True
    assert res.status_str == "unsat"


def test_logic_mismatch_detected():
    asm_a = "movq %rdi, %rax\nimulq $4, %rax\nret"
    asm_b = "movq %rdi, %rax\nimulq $5, %rax\nret"
    res = prove_asm_equivalence(asm_a, asm_b)
    assert res.is_equivalent is False
    assert res.status_str == "sat"
    assert "ARG0_RDI" in res.counterexample["inputs"]


# =====================================================================
# 4. CLI & End-to-End Tests
# =====================================================================

def test_cli_init_and_check_pass(tmp_path: Path):
    src_file = tmp_path / "main.c"
    src_file.write_text(
        "// [LOCK: strict]\n"
        "int secret = 1337;\n"
        "// [/LOCK]\n",
        encoding="utf-8"
    )

    runner = CliRunner()
    result_init = runner.invoke(cli, ["init", str(tmp_path)])
    assert result_init.exit_code == 0
    assert "Registered 1 lock region" in result_init.output

    result_check = runner.invoke(cli, ["check", str(tmp_path)])
    assert result_check.exit_code == 0
    assert "All lock checks passed" in result_check.output


def test_cli_check_violation(tmp_path: Path):
    src_file = tmp_path / "main.c"
    src_file.write_text("// [LOCK: strict]\nint secret = 1337;\n// [/LOCK]\n", encoding="utf-8")
    runner = CliRunner()
    runner.invoke(cli, ["init", str(tmp_path)])

    src_file.write_text("// [LOCK: strict]\nint secret = 9999;\n// [/LOCK]\n", encoding="utf-8")
    result_check = runner.invoke(cli, ["check", str(tmp_path)])
    assert result_check.exit_code != 0
    assert "LOCK VIOLATION" in result_check.output


def test_install_hook(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    runner = CliRunner()
    result = runner.invoke(cli, ["install-hook", str(tmp_path)])
    assert result.exit_code == 0
    assert "Successfully installed" in result.output


def test_e2e_logic_lock_refactoring_pass_and_violation(tmp_path: Path):
    c_file = tmp_path / "math_demo.c"
    c_file.write_text(
        "// [LOCK: logic name=\"calc_val\"]\n"
        "int calc_val(int x) {\n"
        "    return x * 4;\n"
        "}\n"
        "// [/LOCK]\n",
        encoding="utf-8"
    )

    runner = CliRunner()
    runner.invoke(cli, ["init", str(tmp_path)])

    # Refactor code (PASS via Z3)
    c_file.write_text(
        "// [LOCK: logic name=\"calc_val\"]\n"
        "int calc_val(int x) {\n"
        "    return x << 2;\n"
        "}\n"
        "// [/LOCK]\n",
        encoding="utf-8"
    )

    res1 = runner.invoke(cli, ["check", str(tmp_path)])
    assert res1.exit_code == 0
    assert "FORMAL PROOF PASS" in res1.output

    # Introduce bug (FAIL via Z3)
    c_file.write_text(
        "// [LOCK: logic name=\"calc_val\"]\n"
        "int calc_val(int x) {\n"
        "    return x * 5;\n"
        "}\n"
        "// [/LOCK]\n",
        encoding="utf-8"
    )

    res2 = runner.invoke(cli, ["check", str(tmp_path)])
    assert res2.exit_code != 0
    assert "LOGIC VIOLATION" in res2.output
