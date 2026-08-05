# 🔒 KeepOut

> **Production-ready, lightweight code protection powered by Smart Syntax Locking (AST Alpha-Renaming) & Property-Based Testing (PBT).**

**KeepOut** is a lightweight CLI tool designed to physically protect critical code sections from external changes—whether caused by future oversights, unintended edits by teammates, or automated AI refactorings—by marking regions with comment directives (`# [LOCK]` ... `# [/LOCK]`).

Refactored for maximum production usability, KeepOut focuses on **Smart Syntax Locking** ($O(1)$ AST Structural Hash & Variable Alpha-Renaming) and **Smart Property-Based Testing (10,000+ Automated Fuzzing Samples)**, ensuring instant verification and zero freezes across large-scale repositories.

---

## 🌟 2 Core Production Engines

| Engine | Syntax Directive | How It Works | Performance | Ideal Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Smart Syntax Lock** *(Default)* | `# [LOCK]` or `# [LOCK: ast]` | **AST Alpha-Renaming & Structural Hash**. Ignores comments, whitespace, formatting, indentation, and variable renames (`x` ➔ `val`). | ⚡ $O(1)$ (<1ms)<br>Zero freezes | Formatting & linting-heavy codebases where code structure must stay intact. |
| **Smart Property-Based Sub-Engine** | `# [LOCK: pbt]` or `# [LOCK: logic]` | **Property-Based Testing (10,000+ Fuzzing Samples)**. Feeds identical generated inputs to original & modified code to verify output equivalence. | 🎯 Milliseconds<br>Supports complex loops & libraries | Core algorithms, AI refactorings, functions with complex loops or external libraries. |
| **Strict Lock** | `# [LOCK: strict]` | SHA-256 exact byte hash matching. Rejects any edit including spaces or comments. | ⚡ $O(1)$ Instantaneous | Security constants, preventing re-occurrence of critical bugs. |

---

## 🧠 How Smart Syntax Lock & PBT Work

```
[ Original Code ]  ──┐
                     ├──> [ AST Alpha-Renaming Engine ] ──> Match? ➔ PASS ✨ (<0.001s)
[ Modified Code ]  ──┘                   │
 (Variable renames,                      └── Mismatch? (AST Structure Altered)
  comments, formatting                             │
  ignored automatically)                           ▼
                                     [ Property-Based Test Sub-Engine ]
                                     (10,000+ Automated Sample Inputs)
                                                   │
                                                   ├── Output Match across 10,000 samples ➔ PASS ✨
                                                   └── Discrepancy Found ➔ BLOCK 🚨 (with Counterexample Input)
```

1. **AST Alpha-Renaming**: Variable names (`input_val` ➔ `x`), function headers, docstrings, comments, and formatting are normalized into canonical AST symbols.
2. **Property-Based Testing**: For modified AST structures, KeepOut executes 10,000+ automated test inputs (integers, edge cases $0, -1, 2^{31}-1$, floats, arrays).
3. **Black-Box Equivalence Verification**: Fully supports loops, recursion, external library calls (`numpy`, `pandas`, etc.), and objects without SMT solver limits or timeouts.

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

### 🌿 Smart Syntax Lock (Default: Structure Fixed, Formatting & Renames Ignored)
```python
# [LOCK: ast name="calc"]
def calculate_product(input_value):
    # Variable renames, comments, and formatting edits are allowed
    result_value = input_value * 4
    return result_value
# [/LOCK]
```

### 🧠 Property-Based Testing Lock (10,000 Automated Fuzzing Samples)
```mojo
# [LOCK: pbt name="fast_calc"]
fn fast_calc(x: Int) -> Int:
    return x * 4
# [/LOCK]
```

### 🔒 Strict Lock (Exact Byte Freeze)
```c
// [LOCK: strict]
const int MAX_RETRY_COUNT = 5;
// [/LOCK]
```

---

## 🧪 Running Tests

```bash
pytest
```

---

## 📄 License

MIT License
