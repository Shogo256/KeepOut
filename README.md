# 🔒 KeepOut

> **Production-ready code protection powered by Strict Hashing, AST Structural Locking, Property-Based Testing (PBT), and Z3 Formal Verification.**

**KeepOut** is a lightweight CLI tool designed to physically protect critical code sections from external changes—whether caused by future oversights, unintended edits by teammates, or automated AI refactorings—by marking regions with comment directives (`# [LOCK]` ... `# [/LOCK]`).

Designed for modern production engineering, KeepOut combines **AST Structural Hash Locking**, **Property-Based Testing (10,000+ Fuzzing Samples)**, and **Z3 Formal Solver Verification** to deliver instantaneous, zero-hang protection across large-scale repositories.

---

## 🌟 3 Production-Ready Lock Modes

KeepOut offers 3 distinct locking modes tailored for real-world development:

| Lock Mode | Syntax Directive | How It Works | Performance & Scalability | Ideal Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Strict Lock** | `# [LOCK: strict]` | SHA-256 exact byte hash matching. Rejects any edit including spaces or comments. | ⚡ $O(1)$ Instantaneous | Security constants, preventing re-occurrence of critical bugs. |
| **AST Lock** | `# [LOCK: ast]` | Hashes normalized Abstract Syntax Tree (AST). **Ignores formatting, whitespace, indentation, and comments**. Blocks structural AST changes. | 🚀 $O(1)$ Zero-solver overhead | Code formatting / linting-heavy repositories where code structure must stay fixed. |
| **PBT / Logic Lock** | `# [LOCK: pbt]` or `# [LOCK: logic]` | **Property-Based Testing (10,000+ Fuzzing Samples) + Z3 Solver**. Runs automated input vectors to ensure 100% output equivalence. | 🎯 Fast & robust for complex loops, objects & libraries | Core algorithms, data processing functions, AI-assisted refactoring verification. |

---

## 🧠 How Property-Based Testing (PBT) & AST Verification Work

```
[ Original Code ]  ──┐
                     ├──> [ AST Structural Hash Engine ] ──> Match? ➔ PASS ✨ (0.0001s)
[ Modified Code ]  ──┘                   │
                                         └── Mismatch?
                                               │
                                               ▼
                                 [ Property-Based Test Engine ]
                                 (10,000+ Automated Sample Inputs)
                                               │
                                               ├── Output Match across 10,000 samples ➔ PASS ✨
                                               └── Discrepancy Found ➔ BLOCK 🚨 (with Counterexample Input)
```

1. **AST Structural Hash Check**: In $O(1)$ time, KeepOut verifies whether AST syntax nodes match. Formatting and comment edits pass instantly without solver overhead.
2. **Property-Based Fuzzing**: For modified ASTs, KeepOut executes 10,000+ automated test inputs (integers, edge-cases $0, -1, 2^{31}-1$, floats, random distributions).
3. **Hybrid Z3 Formal Theorem Solver**: Runs linear BitVector formal equivalence proofs for additional mathematical certainty.

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

### 🌿 AST Lock (Structure Fixed, Formatting Ignored)
```python
# [LOCK: ast name="calc"]
def calc(x):
    # Formatting and indentation changes here are allowed
    return x * 4
# [/LOCK]
```

### 🧠 PBT / Logic Lock (Property-Based Verification across 10,000 Samples)
```mojo
# [LOCK: pbt name="fast_calc"]
fn fast_calc(x: Int) -> Int:
    return x * 4
# [/LOCK]
```

---

## 🧪 Running Tests

```bash
pytest
```

---

## 📄 License

MIT License
