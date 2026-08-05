# 🔒 KeepOut

> **Universal physical and logic-semantic protection for code regions powered by LLVM IR, Multi-AST, and Z3 Formal Verification.**

**KeepOut** is a lightweight CLI tool designed to physically protect critical code sections from external changes—whether caused by future oversights, unintended edits by teammates, or automated AI refactorings—by marking regions with comment directives (`# [LOCK]` ... `# [/LOCK]`).

Powered by **Universal Multi-Language Verification Engines** (LLVM IR, Python AST, Lisp S-Expressions, APL Array Syntax, Verilog HDL, Solidity, C, C++, Rust), KeepOut mathematically proves whether code refactorings preserve exact output semantics across all possible inputs on Apple Silicon (M1/M2/M3/M4), x86_64, ARM64, and RISC-V architectures.

---

## 🌟 Universal Multi-Language Support

KeepOut brings **Formal Logic Verification** to virtually ANY programming language:

- 🔒 **Strict Lock (Exact Text Freeze)**: Works on **100% of programming & configuration languages** (Python, JS, TS, Rust, C, Go, Java, Ruby, PHP, Shell, SQL, YAML, etc.).
- 🧠 **Logic-only Lock (Formal Equivalence Proof)**:
  - **LLVM Systems Languages**: C, C++, Rust, Fortran, Ada, Swift, Zig, LLVM IR (`.ll`).
  - **Scripting & Dynamic**: Python (`ast.parse` ➔ Z3 AST), JavaScript/TypeScript.
  - **Array & Symbolic**: APL (`y ← 4 × x`), J Language, Chapel, SaC.
  - **Lisp & Functional**: Guile Scheme / Lisp (`(* x 4)` ➔ Z3 AST), Clojure.
  - **Hardware Description**: Verilog / SystemVerilog (`assign b = a * 4;` ➔ Z3 AST).
  - **Smart Contracts**: Solidity (`return x * 4;` ➔ Z3 AST).

---

## 🧠 How Universal Formal Verification Works

```
[ Code (C, Rust, Python, Lisp, APL, Verilog, etc.) ] ──( LLVM / AST Engine )──> [ Z3 Symbolic Expression ]
                                                                                         │
                                                                                 [ Z3 Solver: A != B ]
                                                                                         │
                                                                                         ├──> UNSAT (No counterexample) ➔ PASS ✨
                                                                                         └──> SAT (Counterexample found) ➔ BLOCK 🚨
```

1. **Multi-Engine Parsing**: Source code is compiled to LLVM IR (for compiled languages) or parsed via AST engines (for Python, Lisp, APL, Verilog, Solidity).
2. **Z3 Expression Mapping**: Operands and operations are converted directly into Z3 BitVectors and Ints.
3. **Equivalence Theorem Proof**: Z3 asserts `Output_A != Output_B` for free input parameters across all $2^{64}$ inputs.
   - **`UNSAT`**: No counterexample exists ➔ **Logic is 100% identical (Pass)**.
   - **`SAT`**: Counterexample found ➔ **Logic violation detected (Block with concrete counterexample input)**.

---

## 🐳 1. Running with Docker (Recommended)

No local setup required.

```bash
# Build Docker image
docker build -t keepout .

# Alias for native CLI execution
alias keepout="docker run --rm -v \$(pwd):/workspace keepout"

# Usage
keepout init .
keepout check .
```

---

## 💻 2. Local Installation & Usage (Pip / Pixi)

```bash
# Install via pip
pip install -e .

# Or using Pixi
pixi add z3-solver click pytest clang llvm
pixi run pip install -e .
```

### CLI Commands

```bash
# Initialize locks in keepout.json
keepout init

# Verify lock integrity
keepout check

# Install Git Pre-commit hook
keepout install-hook
```

---

## 📜 Directive Syntax Protocol

### 🔒 Strict Lock (Exact Byte Freeze)
```c
// [LOCK: strict]
const int MAX_RETRY_COUNT = 5;
// [/LOCK]
```

### 🧠 Logic-only Lock (Universal Formal Equivalence)

**Python (`.py`)**:
```python
# [LOCK: logic name="calc"]
def calc(x):
    return x * 4
# [/LOCK]
```

**SystemVerilog (`.sv`)**:
```verilog
// [LOCK: logic]
assign b = a * 4;
// [/LOCK]
```

**GNU APL (`.apl`)**:
```apl
⍝ [LOCK: logic]
y ← 4 × x
⍝ [/LOCK]
```

---

## 🧪 Running Tests

```bash
pytest
```

---

## 📄 License

MIT License
