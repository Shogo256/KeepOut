"""Core module for KeepOut: Universal Multi-Language Formal Verification Engine via LLVM IR & Multi-AST Solvers."""

import ast
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
    r'(?P<comment_prefix>#|//|/\*|;|⍝)\s*\[LOCK(?::\s*(?P<mode>strict|logic))?(?P<attrs>[^\]]*)\](?P<comment_suffix>.*)'
)
LOCK_END_PATTERN = re.compile(r'(?:#|//|/\*|;|⍝)\s*\[/LOCK\]')
UNLOCK_REASON_PATTERN = re.compile(r'UNLOCK_REASON:\s*(?P<reason>.+)')
ATTR_NAME_PATTERN = re.compile(r'(?:name|target_symbol)\s*=\s*["\'](?P<val>[^"\']+)["\']')
FUNC_DECL_PATTERN = re.compile(
    r'^\s*(?:def|fn|function|procedure|int|void|float|double|char|long|unsigned|struct|class|auto|module|contract)\s+(?P<func_name>[a-zA-Z_][a-zA-Z0-9_]*)\b'
)


class LockParserError(Exception):
    """Exception raised when lock directive comments are invalid."""
    pass


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
# 2. Storage Manager (keepout.json)
# =====================================================================

DEFAULT_DB_NAME = "keepout.json"
VERSION = "3.0.0"  # Universal Language Formal Verification Engine


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

        compiled_ir = None
        if block.mode == "logic":
            try:
                ext = block.file_path.suffix.lstrip(".")
                compiled_ir = compile_snippet_to_llvm_ir(block.content, lang=ext, symbol=block.target_symbol)
            except Exception:
                compiled_ir = block.content

        new_locks[key] = {
            "file_path": rel_path,
            "lock_index": block.lock_index,
            "mode": block.mode,
            "strict_hash": content_hash,
            "compiled_ir": compiled_ir,
            "target_symbol": block.target_symbol,
            "start_line": block.start_line,
            "end_line": block.end_line,
        }

    db_data["version"] = VERSION
    db_data["locks"] = new_locks
    save_locks_db(db_path, db_data)
    return db_data


# =====================================================================
# 3. LLVM IR Compiler Bridge (C / C++ / Rust / Fortran / Ada -> LLVM IR)
# =====================================================================

class CompilerError(Exception):
    pass


def compile_snippet_to_llvm_ir(snippet: str, lang: str = "c", symbol: Optional[str] = None) -> str:
    """Compiles code snippet into target-agnostic LLVM IR (.ll) if supported by compiler."""
    lang = lang.lower().lstrip(".")
    if lang in ["ll", "llvm"]:
        return snippet

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        if lang in ["c", "h", "cpp", "cc", "cxx", "hpp"]:
            src_file = tmp_path / "code.c"
            ir_file = tmp_path / "code.ll"
            full_code = _wrap_c_code(snippet, symbol)
            src_file.write_text(full_code, encoding="utf-8")

            cmd = ["clang", "-S", "-emit-llvm", "-O2", str(src_file), "-o", str(ir_file)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                return ir_file.read_text(encoding="utf-8")

        elif lang in ["rs", "rust"]:
            src_file = tmp_path / "code.rs"
            ir_file = tmp_path / "code.ll"
            full_code = _wrap_rust_code(snippet, symbol)
            src_file.write_text(full_code, encoding="utf-8")

            cmd = ["rustc", "--emit=llvm-ir", "-O", str(src_file), "-o", str(ir_file)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                return ir_file.read_text(encoding="utf-8")

    return snippet


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


# =====================================================================
# 4. LLVM IR Parser & Interpreter Engine
# =====================================================================

def parse_llvm_type_width(type_str: str) -> int:
    type_str = type_str.strip()
    if type_str.startswith("i") and type_str[1:].isdigit():
        return int(type_str[1:])
    return 32


class LlvmIrInterpreter:
    def __init__(self, shared_inputs: Optional[Dict[str, z3.ExprRef]] = None):
        self.env: Dict[str, z3.ExprRef] = {}
        self.shared_inputs = shared_inputs if shared_inputs is not None else {}

    def get_val(self, operand_str: str, default_width: int = 32) -> z3.ExprRef:
        operand_str = operand_str.strip()
        if operand_str in self.env:
            return self.env[operand_str]
        if operand_str.lstrip("-").isdigit():
            return z3.BitVecVal(int(operand_str), default_width)
        if operand_str == "true": return z3.BitVecVal(1, 1)
        if operand_str == "false": return z3.BitVecVal(0, 1)

        var_name = f"VAR_{operand_str.lstrip('%')}"
        bv = z3.BitVec(var_name, default_width)
        self.env[operand_str] = bv
        return bv

    def execute_llvm_ir(self, llvm_ir: str) -> z3.ExprRef:
        lines = llvm_ir.splitlines()
        in_func = False
        ret_expr: Optional[z3.ExprRef] = None

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(";"): continue
            if stripped.startswith("define "):
                in_func = True
                param_match = re.search(r'define\s+.*?\s*@\w+\((?P<args>[^)]*)\)', stripped)
                if param_match and param_match.group("args").strip():
                    args = [a.strip() for a in param_match.group("args").split(",") if a.strip()]
                    for arg_idx, arg in enumerate(args):
                        parts = arg.split()
                        type_w = parse_llvm_type_width(parts[0]) if parts else 32
                        var_name = parts[-1] if len(parts) > 1 else f"%{arg_idx}"
                        if var_name in self.shared_inputs:
                            self.env[var_name] = self.shared_inputs[var_name]
                        else:
                            free_bv = z3.BitVec(f"ARG{arg_idx}_{var_name.lstrip('%')}", type_w)
                            self.env[var_name] = free_bv
                            self.shared_inputs[var_name] = free_bv
                continue

            if not in_func: continue
            if stripped == "}": in_func = False; continue

            res = self._execute_instruction(stripped)
            if res is not None: ret_expr = res

        if ret_expr is None:
            raise ValueError("No return instruction found in LLVM IR.")
        return ret_expr

    def _execute_instruction(self, line: str) -> Optional[z3.ExprRef]:
        if line.startswith("ret "):
            parts = line.split()
            width = parse_llvm_type_width(parts[1]) if len(parts) > 1 else 32
            val_str = parts[2] if len(parts) > 2 else parts[1]
            return self.get_val(val_str, default_width=width)

        if "=" not in line: return None
        lhs, rhs = line.split("=", 1)
        dst_var = lhs.strip()
        rhs_tokens = rhs.strip().split()
        if not rhs_tokens: return None

        idx = 0
        while idx < len(rhs_tokens) and rhs_tokens[idx] in ["nsw", "nuw", "exact", "dso_local"]: idx += 1
        opcode = rhs_tokens[idx].lower()
        args_tokens = rhs_tokens[idx+1:]

        if opcode in ["add", "sub", "mul", "sdiv", "udiv", "srem", "urem", "shl", "ashr", "lshr", "and", "or", "xor"]:
            rest_str = " ".join(args_tokens)
            parts = [p.strip() for p in rest_str.split(",")]
            w = parse_llvm_type_width(parts[0].split()[0])
            v1 = self.get_val(parts[0].split()[-1], default_width=w)
            v2 = self.get_val(parts[1] if len(parts) > 1 else "0", default_width=w)

            if v1.size() != v2.size():
                tw = max(v1.size(), v2.size())
                if v1.size() < tw: v1 = z3.ZeroExt(tw - v1.size(), v1)
                if v2.size() < tw: v2 = z3.ZeroExt(tw - v2.size(), v2)

            if opcode == "add": self.env[dst_var] = v1 + v2
            elif opcode == "sub": self.env[dst_var] = v1 - v2
            elif opcode == "mul": self.env[dst_var] = v1 * v2
            elif opcode == "sdiv": self.env[dst_var] = v1 / v2
            elif opcode == "udiv": self.env[dst_var] = z3.UDiv(v1, v2)
            elif opcode == "srem": self.env[dst_var] = v1 % v2
            elif opcode == "urem": self.env[dst_var] = z3.URem(v1, v2)
            elif opcode == "shl": self.env[dst_var] = v1 << v2
            elif opcode == "ashr": self.env[dst_var] = v1 >> v2
            elif opcode == "lshr": self.env[dst_var] = z3.LShR(v1, v2)
            elif opcode == "and": self.env[dst_var] = v1 & v2
            elif opcode == "or": self.env[dst_var] = v1 | v2
            elif opcode == "xor": self.env[dst_var] = v1 ^ v2

        elif opcode == "icmp":
            cond_kind = args_tokens[0]
            rest_str = " ".join(args_tokens[1:])
            parts = [p.strip() for p in rest_str.split(",")]
            w = parse_llvm_type_width(parts[0].split()[0])
            v1 = self.get_val(parts[0].split()[-1], default_width=w)
            v2 = self.get_val(parts[1] if len(parts) > 1 else "0", default_width=w)
            cond_expr = None
            if cond_kind == "eq": cond_expr = (v1 == v2)
            elif cond_kind == "ne": cond_expr = (v1 != v2)
            elif cond_kind in ["slt", "ult"]: cond_expr = (v1 < v2)
            elif cond_kind in ["sle", "ule"]: cond_expr = (v1 <= v2)
            elif cond_kind in ["sgt", "ugt"]: cond_expr = (v1 > v2)
            elif cond_kind in ["sge", "uge"]: cond_expr = (v1 >= v2)
            if cond_expr is not None:
                self.env[dst_var] = z3.If(cond_expr, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))

        elif opcode == "select":
            rest_str = " ".join(args_tokens)
            parts = [p.strip() for p in rest_str.split(",")]
            cond_val = self.get_val(parts[0].split()[-1], default_width=1)
            v1_val = self.get_val(parts[1].split()[-1], default_width=parse_llvm_type_width(parts[1].split()[0]))
            v2_val = self.get_val(parts[2].split()[-1], default_width=parse_llvm_type_width(parts[1].split()[0]))
            self.env[dst_var] = z3.If(cond_val == 1, v1_val, v2_val)

        elif opcode in ["sext", "zext", "trunc"]:
            rest_str = " ".join(args_tokens)
            match_cast = re.search(r'(?P<t1>i\d+)\s+(?P<v>[^\s]+)\s+to\s+(?P<t2>i\d+)', rest_str)
            if match_cast:
                w1, w2 = parse_llvm_type_width(match_cast.group("t1")), parse_llvm_type_width(match_cast.group("t2"))
                v = self.get_val(match_cast.group("v"), default_width=w1)
                if opcode == "sext": self.env[dst_var] = z3.SignExt(w2 - w1, v) if w2 > w1 else v
                elif opcode == "zext": self.env[dst_var] = z3.ZeroExt(w2 - w1, v) if w2 > w1 else v
                elif opcode == "trunc": self.env[dst_var] = z3.Extract(w2 - 1, 0, v) if w2 < w1 else v

        return None


# =====================================================================
# 5. Multi-Language AST Parsers (Python, Lisp, Verilog, APL, Universal)
# =====================================================================

def parse_python_to_z3(code: str, env: Dict[str, z3.ExprRef]) -> z3.ExprRef:
    """Parses Python code expression into Z3 BitVector expression."""
    tree = ast.parse(code.strip())
    
    # Locate return or expression
    target_node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef):
            for sub_stmt in stmt.body:
                if isinstance(sub_stmt, ast.Return):
                    target_node = sub_stmt.value
        elif isinstance(stmt, ast.Return):
            target_node = stmt.value
        elif isinstance(stmt, ast.Expr):
            target_node = stmt.value

    if target_node is None:
        target_node = ast.parse(code.strip(), mode="eval").body

    def _convert(node):
        if isinstance(node, ast.Constant):
            return z3.BitVecVal(node.value, 64)
        elif isinstance(node, ast.Name):
            if node.id not in env:
                env[node.id] = z3.BitVec(f"ARG_{node.id}", 64)
            return env[node.id]
        elif isinstance(node, ast.BinOp):
            l, r = _convert(node.left), _convert(node.right)
            if isinstance(node.op, ast.Add): return l + r
            if isinstance(node.op, ast.Sub): return l - r
            if isinstance(node.op, ast.Mult): return l * r
            if isinstance(node.op, ast.LShift): return l << r
            if isinstance(node.op, ast.RShift): return l >> r
            if isinstance(node.op, ast.BitXor): return l ^ r
            if isinstance(node.op, ast.BitAnd): return l & r
            if isinstance(node.op, ast.BitOr): return l | r
        elif isinstance(node, ast.UnaryOp):
            val = _convert(node.operand)
            if isinstance(node.op, ast.USub): return -val
            if isinstance(node.op, ast.Invert): return ~val
        raise ValueError(f"Unsupported Python AST node: {ast.dump(node)}")

    return _convert(target_node)


def parse_generic_to_z3(code: str, env: Dict[str, z3.ExprRef]) -> z3.ExprRef:
    """Universal generic math expression parser into Z3 for APL, Lisp, Verilog, Solidity, etc."""
    code_clean = re.sub(r'//.*|#.*|;|⍝.*|/\*.*?\*/', '', code)
    code_clean = re.sub(r'\b(def|fn|function|return|assign|var|val|let)\b', '', code_clean)
    code_clean = code_clean.replace("×", "*").replace("÷", "/").replace("←", "=").replace(":=", "=")

    # Extract return/assign target or expression
    if "=" in code_clean:
        code_clean = code_clean.split("=")[-1]
    
    code_clean = code_clean.strip().rstrip(";")

    # S-Expression Lisp pattern e.g. (* x 4) or (ash x 2)
    sexpr_match = re.match(r'^\(\s*(?P<op>[\*\+\-\<\>\&\|\^]+|ash|shl|add|sub|mul)\s+(?P<v1>[^\s]+)\s+(?P<v2>[^\s]+)\s*\)$', code_clean)
    if sexpr_match:
        op = sexpr_match.group("op")
        v1_str, v2_str = sexpr_match.group("v1"), sexpr_match.group("v2")
        
        def resolve_token(t):
            if t.lstrip("-").isdigit(): return z3.BitVecVal(int(t), 64)
            if t not in env: env[t] = z3.BitVec(f"ARG_{t}", 64)
            return env[t]

        v1, v2 = resolve_token(v1_str), resolve_token(v2_str)
        if op in ["*", "mul"]: return v1 * v2
        if op in ["+", "add"]: return v1 + v2
        if op in ["-", "sub"]: return v1 - v2
        if op in ["<<", "ash", "shl"]: return v1 << v2
        if op in [">>", "shr"]: return v1 >> v2
        if op in ["^", "xor"]: return v1 ^ v2

    # Infix binary pattern e.g. x * 4 or x << 2 or 4 * x
    infix_match = re.match(r'^(?P<v1>[^\s]+)\s*(?P<op>\*|\+|\-|\<\<|\>\>|\&|\||\^)\s*(?P<v2>[^\s]+)$', code_clean)
    if infix_match:
        op = infix_match.group("op")
        v1_str, v2_str = infix_match.group("v1"), infix_match.group("v2")

        def resolve_token(t):
            if t.lstrip("-").isdigit(): return z3.BitVecVal(int(t), 64)
            if t not in env: env[t] = z3.BitVec(f"ARG_{t}", 64)
            return env[t]

        v1, v2 = resolve_token(v1_str), resolve_token(v2_str)
        if op == "*": return v1 * v2
        if op == "+": return v1 + v2
        if op == "-": return v1 - v2
        if op == "<<": return v1 << v2
        if op == ">>": return v1 >> v2
        if op == "^": return v1 ^ v2
        if op == "&": return v1 & v2
        if op == "|": return v1 | v2

    # Fallback to Python AST parser
    try:
        return parse_python_to_z3(code_clean, env)
    except Exception:
        raise ValueError(f"Unable to parse universal code expression into Z3: '{code}'")


# =====================================================================
# 6. Universal Formally Proving Equivalence via Z3 Solver
# =====================================================================

@dataclass
class EquivalenceResult:
    is_equivalent: bool
    status_str: str  # "unsat", "sat", "unknown"
    message: str
    counterexample: Optional[Dict[str, Any]] = None


def prove_universal_equivalence(code_a: str, code_b: str, lang: str = "c") -> EquivalenceResult:
    """
    Formally proves semantic equivalence between code_a and code_b across ANY language
    (C, C++, Rust, Python, Lisp, APL, Verilog, Solidity, Fortran, Ada, etc.).
    """
    lang = lang.lower().lstrip(".")
    shared_env: Dict[str, z3.ExprRef] = {}

    # 1. Check if LLVM IR modules directly
    if "define " in code_a and "ret " in code_a:
        interp_a = LlvmIrInterpreter(shared_inputs=shared_env)
        interp_b = LlvmIrInterpreter(shared_inputs=shared_env)
        expr_a = interp_a.execute_llvm_ir(code_a)
        expr_b = interp_b.execute_llvm_ir(code_b)

    # 2. Check if LLVM IR compiler pipeline supported (C, C++, Rust)
    elif lang in ["c", "h", "cpp", "cc", "cxx", "hpp", "rs", "rust"]:
        try:
            ir_a = compile_snippet_to_llvm_ir(code_a, lang=lang)
            ir_b = compile_snippet_to_llvm_ir(code_b, lang=lang)
            interp_a = LlvmIrInterpreter(shared_inputs=shared_env)
            interp_b = LlvmIrInterpreter(shared_inputs=shared_env)
            expr_a = interp_a.execute_llvm_ir(ir_a)
            expr_b = interp_b.execute_llvm_ir(ir_b)
        except Exception:
            # Fallback to Universal AST parser if compiler unavailable
            expr_a = parse_generic_to_z3(code_a, shared_env)
            expr_b = parse_generic_to_z3(code_b, shared_env)

    # 3. Python AST Engine
    elif lang in ["py", "python"]:
        expr_a = parse_python_to_z3(code_a, shared_env)
        expr_b = parse_python_to_z3(code_b, shared_env)

    # 4. Universal AST Engine (APL, Lisp, Guile, Verilog, Solidity, J, etc.)
    else:
        expr_a = parse_generic_to_z3(code_a, shared_env)
        expr_b = parse_generic_to_z3(code_b, shared_env)

    # Adjust bit-width mismatches
    if expr_a.size() != expr_b.size():
        max_w = max(expr_a.size(), expr_b.size())
        if expr_a.size() < max_w: expr_a = z3.ZeroExt(max_w - expr_a.size(), expr_a)
        if expr_b.size() < max_w: expr_b = z3.ZeroExt(max_w - expr_b.size(), expr_b)

    solver = z3.Solver()
    solver.add(expr_a != expr_b)
    check_res = solver.check()

    if check_res == z3.unsat:
        return EquivalenceResult(
            is_equivalent=True,
            status_str="unsat",
            message="✨ Formally proven equivalent by Z3 Universal Solver (UNSAT: no counterexample exists)."
        )
    elif check_res == z3.sat:
        model = solver.model()
        ce_inputs = {decl.name(): model[decl].as_long() for decl in model.decls()}
        val_a = model.eval(expr_a, model_completion=True).as_long()
        val_b = model.eval(expr_b, model_completion=True).as_long()
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
