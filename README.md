```
  🔒 _  _____ _____ _____   ____  _   _ _____ 
  | |/ / ____| ____|  _ \ / __ \| | | |_   _|
  | ' /|  _| |  _| | |_) | |  | | | | | | |  
  | . \| |___| |___|  __/| |__| | |_| | | |  
  |_|\_\_____|_____|_|    \____/ \___/  |_|  
```

# 🔒 KeepOut

> **Production-ready, lightweight code protection powered by Smart Syntax Locking (AST Alpha-Renaming) & Deterministic Property-Based Testing (PBT).**

**KeepOut** is a lightweight CLI tool designed to physically protect critical code sections from external changes—whether caused by future oversights, unintended edits by teammates, or automated AI refactorings—by marking regions with comment directives (`# [LOCK]` ... `# [/LOCK]`).

Refactored for maximum production usability, KeepOut features **Smart Syntax Locking** ($O(1)$ AST Structural Hash & Variable Alpha-Renaming) and **Deterministic Property-Based Testing (10,000+ Reproducible Fuzzing Samples + Counterexample Caching)**, ensuring instant verification, zero flaky CI builds, and zero freezes across large-scale repositories.

---

## 🌟 2 Core Production Engines

| Engine | Syntax Directive | How It Works | Performance & Reliability | Ideal Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Smart Syntax Lock** *(Default)* | `# [LOCK]` or `# [LOCK: ast]` | **AST Alpha-Renaming & Structural Hash**. Ignores comments, whitespace, formatting, indentation, and variable renames (`x` ➔ `val`). | ⚡ $O(1)$ (<1ms)<br>Zero freezes | Formatting & linting-heavy codebases where code structure must stay intact. |
| **Deterministic PBT Sub-Engine** | `# [LOCK: pbt]` or `# [LOCK: logic]` | **Deterministic Property-Based Testing (10,000 Samples)** + **Counterexample Auto-Caching**. Reproducible fixed seed (`seed=42`). | 🎯 Milliseconds<br>Zero Flaky CI Runs | Core algorithms, AI refactorings, functions with complex loops or external libraries. |
| **Strict Lock** | `# [LOCK: strict]` | SHA-256 exact byte hash matching. Rejects any edit including spaces or comments. | ⚡ $O(1)$ Instantaneous | Security constants, preventing re-occurrence of critical bugs. |

---

## 🧠 Deterministic PBT & Counterexample Caching

```
[ Original Code ]  ──┐
                     ├──> [ AST Alpha-Renaming Engine ] ──> Match? ➔ PASS ✨ (<0.001s)
[ Modified Code ]  ──┘                   │
 (Variable renames,                      └── Mismatch? (AST Structure Altered)
  comments, formatting                             │
  ignored automatically)                           ▼
                                     [ 1. Check Cached Counterexamples ] (keepout.json)
                                                   │
                                                   ├── Fail? ➔ Instant Regression Block 🚨 (0.0001s)
                                                   └── Pass? ➔ [ 2. Deterministic PBT Engine ]
                                                               (10,000 Reproducible Inputs, seed=42)
                                                                       │
                                                                       ├── Output Match ➔ PASS ✨
                                                                       └── Discrepancy ➔ BLOCK 🚨
                                                                           (Auto-cache Counterexample)
```

1. **Deterministic Seed (No Flaky Tests)**: Fixed PRNG seed guarantees 100% reproducible test vectors across local and CI runs.
2. **Counterexample Caching**: When a bug or logic mismatch is caught, KeepOut automatically caches the failing input parameter inside `keepout.json`. On subsequent checks, KeepOut checks cached counterexamples **first**, preventing regressions in 0.0001 seconds.

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

### 🧠 Deterministic Property-Based Testing Lock (10,000 Reproducible Samples)
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
