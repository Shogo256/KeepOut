"""Core module for KeepOut: Directive parsing, storage DB, assembly interpretation & Z3 formal verification."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Dict, Any, List, Optional, Set, Tuple
import z3


# =====================================================================
# 1. Comment Parser & Directive Data Structures
# =====================================================================

@dataclass
class LockBlock:
    file_path: Path
    lock_index: int
    mode: str  # "strict" or "logic"
    content: str
    start_line: int
    end_line: int
    target_symbol: Optional[str] = None
    unlock_reason: Optional[str] = None


LOCK_START_PATTERN = re.compile(
    r'(?P<comment_prefix>#|//|/\*|;)\s*\[LOCK(?::\s*(?P<mode>strict|logic))?(?P<attrs>[^\]]*)\](?P<comment_suffix>.*)'
)
LOCK_END_PATTERN = re.compile(r'(?:#|//|/\*|;)\s*\[/LOCK\]')
UNLOCK_REASON_PATTERN = re.compile(r'UNLOCK_REASON:\s*(?P<reason>.+)')
ATTR_NAME_PATTERN = re.compile(r'(?:name|target_symbol)\s*=\s*["\'](?P<val>[^"\']+)["\']')
FUNC_DECL_PATTERN = re.compile(
    r'^\s*(?:def|fn|int|void|float|double|char|long|unsigned|struct|class|auto)\s+(?P<func_name>[a-zA-Z_][a-zA-Z0-9_]*)\b'
)


class LockParserError(Exception):
    """Exception raised when lock directive comments are invalid."""
    pass


def parse_file_locks(file_path: Path) -> List[LockBlock]:
    """Scans a file for [LOCK] ... [/LOCK] directives."""
    if not file_path.exists():
        return []

    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []

    lines = raw_text.replace("\r\n", "\n").split("\n")
    blocks: List[LockBlock] = []

    in_lock = False
    current_mode = "strict"
    current_target_symbol = None
    current_unlock_reason = None
    current_start_line = 0
    current_lines: List[str] = []
    lock_counter = 0

    for idx, line in enumerate(lines, start=1):
        reason_match = UNLOCK_REASON_PATTERN.search(line)
        if reason_match and in_lock:
            current_unlock_reason = reason_match.group("reason").strip()

        start_match = LOCK_START_PATTERN.search(line)
        end_match = LOCK_END_PATTERN.search(line)

        if start_match:
            if in_lock:
                raise LockParserError(
                    f"Nested [LOCK] directive found at {file_path}:{idx}. "
                    f"Lock opened at line {current_start_line} must be closed with [/LOCK] first."
                )
            in_lock = True
            lock_mode = start_match.group("mode")
            current_mode = lock_mode.lower() if lock_mode else "strict"
            current_start_line = idx
            current_lines = []

            if reason_match:
                current_unlock_reason = reason_match.group("reason").strip()
            else:
                current_unlock_reason = None

            attrs_str = start_match.group("attrs") or ""
            attr_match = ATTR_NAME_PATTERN.search(attrs_str)
            current_target_symbol = attr_match.group("val") if attr_match else None

        elif end_match:
            if not in_lock:
                raise LockParserError(
                    f"Unmatched [/LOCK] directive found at {file_path}:{idx} without prior [LOCK]."
                )

            content_str = "\n".join(current_lines)

            if not current_target_symbol and current_lines:
                func_match = FUNC_DECL_PATTERN.search(current_lines[0])
                if func_match:
                    current_target_symbol = func_match.group("func_name")

            block = LockBlock(
                file_path=file_path,
                lock_index=lock_counter,
                mode=current_mode,
                content=content_str,
                start_line=current_start_line,
                end_line=idx,
                target_symbol=current_target_symbol,
                unlock_reason=current_unlock_reason,
            )
            blocks.append(block)
            lock_counter += 1
            in_lock = False

        elif in_lock:
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
VERSION = "1.0.0"


def compute_strict_hash(content: str) -> str:
    """Computes SHA-256 hash of normalized code content."""
    normalized = content.replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_locks_db(db_path: Path) -> Dict[str, Any]:
    """Loads keepout.json database."""
    if not db_path.exists():
        return {"version": VERSION, "locks": {}}
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"version": VERSION, "locks": {}}
    except Exception:
        return {"version": VERSION, "locks": {}}


def save_locks_db(db_path: Path, db_data: Dict[str, Any]) -> None:
    """Saves keepout.json database atomically."""
    tmp_path = db_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(db_path)


def make_lock_key(rel_file_path: str, lock_index: int) -> str:
    """Generates lock key e.g. 'src/main.c#lock_0'."""
    return f"{rel_file_path}#lock_{lock_index}"


def sync_blocks_to_db(db_path: Path, blocks: List[LockBlock], root_dir: Path) -> Dict[str, Any]:
    """Updates keepout.json with LockBlocks."""
    db_data = load_locks_db(db_path)
    new_locks: Dict[str, Any] = {}

    for block in blocks:
        try:
            rel_path = block.file_path.relative_to(root_dir).as_posix()
        except ValueError:
            rel_path = block.file_path.as_posix()

        key = make_lock_key(rel_path, block.lock_index)
        content_hash = compute_strict_hash(block.content)

        compiled_asm = None
        if block.mode == "logic":
            try:
                ext = block.file_path.suffix.lstrip(".")
                compiled_asm = compile_snippet_to_asm(block.content, lang=ext, symbol=block.target_symbol)
            except Exception:
                compiled_asm = None

        new_locks[key] = {
            "file_path": rel_path,
            "lock_index": block.lock_index,
            "mode": block.mode,
            "strict_hash": content_hash,
            "compiled_asm": compiled_asm,
            "target_symbol": block.target_symbol,
            "start_line": block.start_line,
            "end_line": block.end_line,
        }

    db_data["version"] = VERSION
    db_data["locks"] = new_locks
    save_locks_db(db_path, db_data)
    return db_data


# =====================================================================
# 3. Compiler Backend Bridge (C / Rust -> x86_64 Assembly)
# =====================================================================

class CompilerError(Exception):
    pass


def compile_snippet_to_asm(snippet: str, lang: str = "c", symbol: Optional[str] = None) -> str:
    """Compiles code snippet into clean x86_64 assembly code."""
    lang = lang.lower().lstrip(".")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        if lang in ["c", "h"]:
            src_file = tmp_path / "code.c"
            asm_file = tmp_path / "code.s"
            full_code = _wrap_c_code(snippet, symbol)
            src_file.write_text(full_code, encoding="utf-8")

            cmd = ["gcc", "-S", "-O2", str(src_file), "-o", str(asm_file)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                raise CompilerError(f"GCC compilation failed:\n{res.stderr}")
            return _clean_asm(asm_file.read_text(encoding="utf-8"))

        elif lang in ["rs", "rust"]:
            src_file = tmp_path / "code.rs"
            asm_file = tmp_path / "code.s"
            full_code = _wrap_rust_code(snippet, symbol)
            src_file.write_text(full_code, encoding="utf-8")

            cmd = ["rustc", "--emit", "asm", "-O", str(src_file), "-o", str(asm_file)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                raise CompilerError(f"Rustc compilation failed:\n{res.stderr}")
            return _clean_asm(asm_file.read_text(encoding="utf-8"))

        else:
            raise CompilerError(f"Unsupported language: '{lang}'")


def _wrap_c_code(snippet: str, symbol: Optional[str]) -> str:
    if re.search(r'\b[a-zA-Z_][a-zA-Z0-9_]*\s*\([^)]*\)\s*\{', snippet):
        return f"#include <stdint.h>\n#include <stddef.h>\n{snippet}\n"
    func_name = symbol if symbol else "keepout_target"
    return f"#include <stdint.h>\n#include <stddef.h>\nint64_t {func_name}(int64_t rdi, int64_t rsi, int64_t rdx, int64_t rcx) {{\n{snippet}\n}}\n"


def _wrap_rust_code(snippet: str, symbol: Optional[str]) -> str:
    if "fn " in snippet and "{" in snippet:
        return f"#![crate_type = \"lib\"]\n{snippet}\n"
    func_name = symbol if symbol else "keepout_target"
    return f"#![crate_type = \"lib\"]\n#[no_mangle]\npub extern \"C\" fn {func_name}(rdi: i64, rsi: i64, rdx: i64, rcx: i64) -> i64 {{\n{snippet}\n}}\n"


def _clean_asm(raw_asm: str) -> str:
    clean = []
    for line in raw_asm.splitlines():
        stripped = line.strip()
        if stripped.startswith(".") and not stripped.endswith(":"):
            if not (stripped.startswith(".L") and ":" not in stripped):
                continue
        if stripped.startswith("#") or stripped.startswith(";") or not stripped:
            continue
        clean.append(line)
    return "\n".join(clean)


# =====================================================================
# 4. Z3 Register State & Assembly Interpreter Engine
# =====================================================================

REGISTER_NAMES_64 = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
ARG_REGISTERS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]

LEA_ATT_PATTERN = re.compile(r'^(?P<disp>-?\d+)?\((?:%(?P<base>[a-z0-9]+))?(?:,\s*%(?P<index>[a-z0-9]+)(?:,\s*(?P<scale>\d+))?)?\)$')
LEA_INTEL_PATTERN = re.compile(r'^\[\s*(?:(?P<base>[a-z0-9]+)\s*)?(?:\+\s*(?P<index>[a-z0-9]+)\s*\*\s*(?P<scale>\d+)\s*)?(?:\+\s*(?P<disp>-?\d+)\s*)?\]$')


class RegisterState:
    """Manages Z3 BitVector expressions for 64-bit CPU registers."""
    def __init__(self, init_free_inputs: bool = True):
        self.regs: Dict[str, z3.ExprRef] = {}
        for name in REGISTER_NAMES_64:
            if init_free_inputs and name in ARG_REGISTERS:
                idx = ARG_REGISTERS.index(name)
                self.regs[name] = z3.BitVec(f"ARG{idx}_{name.upper()}", 64)
            else:
                self.regs[name] = z3.BitVecVal(0, 64)

    def get_reg64(self, name: str) -> z3.ExprRef:
        canonical = name.lower().lstrip("%")
        if canonical in self.regs:
            return self.regs[canonical]
        if canonical.startswith("e") and canonical[1:] in ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp"]:
            return z3.Extract(31, 0, self.regs["r" + canonical[1:]])
        if canonical in [f"r{i}d" for i in range(8, 16)]:
            return z3.Extract(31, 0, self.regs[canonical[:-1]])
        if canonical in ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp"]:
            return z3.Extract(15, 0, self.regs["r" + canonical])
        if canonical in ["al", "bl", "cl", "dl", "sil", "dil", "bpl", "spl"]:
            base = canonical[:-1] if canonical.endswith("l") else canonical
            parent = "r" + base
            if parent not in self.regs: parent = "r" + canonical[0] + "x"
            return z3.Extract(7, 0, self.regs[parent])
        raise ValueError(f"Unknown register: '{name}'")

    def set_reg64(self, name: str, val: z3.ExprRef) -> None:
        canonical = name.lower().lstrip("%")
        if val.size() != 64:
            val = z3.ZeroExt(64 - val.size(), val) if val.size() < 64 else z3.Extract(63, 0, val)
        if canonical in self.regs:
            self.regs[canonical] = val
            return
        if canonical.startswith("e") and canonical[1:] in ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp"]:
            self.regs["r" + canonical[1:]] = z3.ZeroExt(32, z3.Extract(31, 0, val))
            return
        if canonical in [f"r{i}d" for i in range(8, 16)]:
            self.regs[canonical[:-1]] = z3.ZeroExt(32, z3.Extract(31, 0, val))
            return
        raise ValueError(f"Write to unknown register: '{name}'")


class AsmInterpreter:
    """Parses x86_64 assembly code and interprets state transitions into Z3 BitVectors."""
    def __init__(self, state: Optional[RegisterState] = None):
        self.state = state if state is not None else RegisterState()

    def parse_operand(self, op_str: str) -> z3.ExprRef:
        op_str = op_str.strip()
        if op_str.startswith("$"):
            return z3.BitVecVal(int(op_str[1:]), 64)
        if op_str.lstrip("-").isdigit():
            return z3.BitVecVal(int(op_str), 64)

        att_match = LEA_ATT_PATTERN.match(op_str)
        if att_match:
            d, b, i, s = att_match.group("disp"), att_match.group("base"), att_match.group("index"), att_match.group("scale")
            expr = z3.BitVecVal(0, 64)
            if b: expr = expr + self.state.get_reg64(b)
            if i: expr = expr + (self.state.get_reg64(i) * z3.BitVecVal(int(s) if s else 1, 64))
            if d: expr = expr + z3.BitVecVal(int(d), 64)
            return expr

        intel_match = LEA_INTEL_PATTERN.match(op_str)
        if intel_match:
            b, i, s, d = intel_match.group("base"), intel_match.group("index"), intel_match.group("scale"), intel_match.group("disp")
            expr = z3.BitVecVal(0, 64)
            if b: expr = expr + self.state.get_reg64(b)
            if i: expr = expr + (self.state.get_reg64(i) * z3.BitVecVal(int(s) if s else 1, 64))
            if d: expr = expr + z3.BitVecVal(int(d), 64)
            return expr

        return self.state.get_reg64(op_str)

    def execute_line(self, line: str) -> None:
        line = line.split("#")[0].split(";")[0].strip()
        if not line or line.endswith(":") or line.startswith("."):
            return
        parts = line.split(None, 1)
        mnemonic = parts[0].lower()
        operands = self._split_operands(parts[1] if len(parts) > 1 else "")

        if mnemonic in ["ret", "retq"]: return

        if mnemonic in ["mov", "movq", "movl", "movabsq"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[0]))
        elif mnemonic in ["add", "addq", "addl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) + self.parse_operand(operands[0]))
        elif mnemonic in ["sub", "subq", "subl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) - self.parse_operand(operands[0]))
        elif mnemonic in ["imul", "imulq", "imull"]:
            if len(operands) == 2:
                self.state.set_reg64(operands[1], self.parse_operand(operands[1]) * self.parse_operand(operands[0]))
            elif len(operands) == 3:
                self.state.set_reg64(operands[2], self.parse_operand(operands[0]) * self.parse_operand(operands[1]))
        elif mnemonic in ["sal", "shl", "salq", "shlq", "shll"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) << self.parse_operand(operands[0]))
        elif mnemonic in ["sar", "sarq", "sarl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) >> self.parse_operand(operands[0]))
        elif mnemonic in ["shr", "shrq", "shrl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], z3.LShR(self.parse_operand(operands[1]), self.parse_operand(operands[0])))
        elif mnemonic in ["xor", "xorq", "xorl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) ^ self.parse_operand(operands[0]))
        elif mnemonic in ["and", "andq", "andl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) & self.parse_operand(operands[0]))
        elif mnemonic in ["or", "orq", "orl"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[1]) | self.parse_operand(operands[0]))
        elif mnemonic in ["lea", "leaq", "leal"] and len(operands) == 2:
            self.state.set_reg64(operands[1], self.parse_operand(operands[0]))
        elif mnemonic in ["neg", "negq", "negl"] and len(operands) == 1:
            self.state.set_reg64(operands[0], -self.parse_operand(operands[0]))
        elif mnemonic in ["not", "notq", "notl"] and len(operands) == 1:
            self.state.set_reg64(operands[0], ~self.parse_operand(operands[0]))

    def execute_asm(self, asm_code: str) -> z3.ExprRef:
        for line in asm_code.splitlines():
            self.execute_line(line)
        return self.state.get_reg64("rax")

    def _split_operands(self, args_str: str) -> List[str]:
        operands, current, depth = [], [], 0
        for char in args_str:
            if char in "([": depth += 1; current.append(char)
            elif char in ")]": depth -= 1; current.append(char)
            elif char == "," and depth == 0: operands.append("".join(current).strip()); current = []
            else: current.append(char)
        if current: operands.append("".join(current).strip())
        return [op for op in operands if op]


# =====================================================================
# 5. Formally Proving Equivalence via Z3 Solver
# =====================================================================

@dataclass
class EquivalenceResult:
    is_equivalent: bool
    status_str: str  # "unsat", "sat", "unknown"
    message: str
    counterexample: Optional[Dict[str, Any]] = None


def prove_asm_equivalence(asm_a: str, asm_b: str) -> EquivalenceResult:
    """Proves if assembly block A and block B produce identical RAX output for all inputs."""
    state_a = RegisterState(init_free_inputs=True)
    state_b = RegisterState(init_free_inputs=False)
    for reg_name in ARG_REGISTERS:
        state_b.set_reg64(reg_name, state_a.get_reg64(reg_name))

    rax_a = AsmInterpreter(state_a).execute_asm(asm_a)
    rax_b = AsmInterpreter(state_b).execute_asm(asm_b)

    solver = z3.Solver()
    solver.add(rax_a != rax_b)
    check_res = solver.check()

    if check_res == z3.unsat:
        return EquivalenceResult(
            is_equivalent=True,
            status_str="unsat",
            message="✨ Formally proven equivalent by Z3 solver (UNSAT: no counterexample exists)."
        )
    elif check_res == z3.sat:
        model = solver.model()
        ce_inputs = {decl.name(): model[decl].as_long() for decl in model.decls()}
        val_a = model.eval(rax_a, model_completion=True).as_long()
        val_b = model.eval(rax_b, model_completion=True).as_long()
        return EquivalenceResult(
            is_equivalent=False,
            status_str="sat",
            message=(
                f"🚨 Logic Mismatch Detected (SAT)! Counterexample found:\n"
                f"  Inputs: {ce_inputs}\n"
                f"  Expected Output (Original): {val_a}\n"
                f"  Actual Output (Modified): {val_b}"
            ),
            counterexample={"inputs": ce_inputs, "output_a": val_a, "output_b": val_b}
        )
    else:
        return EquivalenceResult(
            is_equivalent=False,
            status_str="unknown",
            message="⚠️ Z3 Solver returned UNKNOWN."
        )
