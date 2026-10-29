```
  🔒 _  _____ _____ _____   ____  _   _ _____ 
  | |/ / ____| ____|  _ \ / __ \| | | |_   _|
  | ' /|  _| |  _| | |_) | |  | | | | | | |  
  | . \| |___| |___|  __/| |__| | |_| | | |  
  |_|\_\_____|_____|_|    \____/ \___/  |_|  
```

# 🔒 KeepOut

> **Lightweight code protection powered by Smart Syntax Locking (AST Alpha-Renaming) & Deterministic Property-Based Testing (PBT).**

**KeepOut** is a lightweight CLI tool that makes changes to critical code sections visible and blocks them in CI or a pre-commit hook—whether caused by future oversights, unintended edits by teammates, or automated AI refactorings—by marking regions with comment directives (`# [LOCK]` ... `# [/LOCK]`).

KeepOut features **Smart Syntax Locking** (AST structural hash with alpha-renaming of local variables) and **Deterministic Property-Based Testing** (10,000 reproducible inputs, boundary values taken from the code itself, and counterexample caching). Read [What KeepOut does not catch](#-what-keepout-does-not-catch) before relying on it.

---

## 🌟 Lock Modes

| Engine | Syntax Directive | How It Works | Cost | Ideal Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Smart Syntax Lock** *(Default)* | `# [LOCK]` or `# [LOCK: ast]` | **AST Alpha-Renaming & Structural Hash**. Ignores comments, whitespace, formatting, and renames of parameters and local variables (`x` ➔ `val`). Names visible outside the function — callees, builtins, globals, and the functions, classes and constants the region defines — must stay as written. Renames are recognised in Python only; other languages get a token hash that ignores comments and formatting. | Linear in region size (about 7 ms per 100 lines of Python) | Formatting & linting-heavy codebases where code structure must stay intact. |
| **Deterministic PBT Sub-Engine** | `# [LOCK: pbt]` or `# [LOCK: logic]` | Calls **every top-level function** in the region with the same 10,000+ integers, in the original and the edited version, and compares return values *and* raised exceptions. Python and Mojo only. | About 0.2–0.5 s per edited region (a separate Python process); at most 1 s per call and 60 s per region | Pure functions of **one integer argument** whose implementation may be refactored. |
| **Strict Lock** | `# [LOCK: strict]` | SHA-256 exact byte hash matching. Rejects any edit including spaces or comments. | Linear in region size | Security constants, preventing re-occurrence of critical bugs. |

---

## 🧠 Deterministic PBT & Counterexample Caching

```
[ Original Code ]  ──┐
                     ├──> [ Python AST Alpha-Renaming ] ──> Match? ➔ PASS ✨ (Python regions only)
[ Modified Code ]  ──┘                   │
 (Local renames,                         └── Mismatch, or not Python
  comments, formatting                             │
  ignored automatically)                           ▼
                                     [ 1. Re-try Cached Counterexamples ] (keepout.json)
                                                   │
                                                   ├── Fail? ➔ Instant Regression Block 🚨
                                                   └── Pass? ➔ [ 2. Deterministic PBT Engine ]
                                                               (edge cases, every integer in -256..256,
                                                                c-1/c/c+1/-c for each literal c in the code,
                                                                random integers up to 10,000 inputs, seed=42)
                                                                       │
                                                                       ├── Same results & exceptions ➔ PASS ✨
                                                                       ├── Discrepancy ➔ BLOCK 🚨 (Auto-cache Counterexample)
                                                                       └── Nothing could be compared ➔ UNVERIFIABLE 🔍 (fails the check)
```

1. **Deterministic Inputs (No Flaky Tests)**: The inputs depend only on the seed and on the code, so local and CI runs test the same values.
2. **Boundaries from the code**: Each integer literal in either version contributes its neighbours, so `x >= 18` ➔ `x > 18` or a special case for `x == 123456789` is caught. Values the code does not mention, outside -256..256, are only sampled at random.
3. **Counterexample Caching**: When a mismatch is caught, KeepOut stores the failing input in `keepout.json` (so `check` writes to that file) and tries it first on later checks.
4. **UNVERIFIABLE is a failure**: A function the engine cannot call — several arguments, non-integer input, names defined outside the region — is reported as verifying nothing rather than passing. Use an ast or strict lock for it.

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

Directives must start a comment line (`#`, `//`, `/*`, `;` or `⍝`). The mode is one of `strict`, `ast`, `pbt` or `logic`; anything else is an error rather than a silent fallback to `ast`.

### 🌿 Smart Syntax Lock (Default: Structure Fixed, Formatting & Local Renames Ignored)
```python
# [LOCK: ast name="calc"]
def calculate_product(input_value):
    # Local variable renames, comments, and formatting edits are allowed
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
// [LOCK: strict name="max_retry"]
const int MAX_RETRY_COUNT = 5;
// [/LOCK]
```

### 🏷️ Naming locks
`name="..."` (letters, digits, `_`, `.`, `-`) is the lock's identity in `keepout.json`: `src/app.py#calc`. Unnamed locks are numbered among the unnamed locks of their file (`src/app.py#lock_0`), so adding an unnamed lock above them shifts their keys. Name every lock you expect to keep.

### 🔓 Unlocking on purpose
A comment line `UNLOCK_REASON: <why>` inside the region (or after the `[LOCK]` directive on its line) lets an edit pass, reported as `UNLOCKED`. Anyone who can edit the file can add one, an AI agent included. In CI where agents can commit, run `keepout check --no-unlock` so unlock comments are ignored. Afterwards, remove the comment and run `keepout init` to record the new code.

### 🪝 Pre-commit hook
`keepout install-hook` adds a block that runs `keepout check --staged` — the content being committed, not the files on disk — and blocks the commit when keepout cannot be found. `git commit --no-verify` skips it deliberately.

---

## ⚠️ What KeepOut does not catch

- **Anyone who can write to the repository can re-lock.** `keepout init` records whatever the code is now. KeepOut makes a change visible and fails a check; it does not stop a person or agent who also edits `keepout.json` or re-runs `init`. Review changes to `keepout.json` like code.
- **Code outside the region.** Redefining a locked function later in the file, or monkeypatching it elsewhere, does not touch the region.
- **PBT only sees what it can call.** Each top-level function is called with one integer. Behaviour for other inputs (floats, strings, large values the code never mentions) is not tested.
- **Renames outside Python.** For other languages the ast lock compares tokens: comments and formatting are ignored, renaming a variable is a change.
- **`keepout.json` from an older keepout.** A database written with a different hash format is refused; review the regions and run `keepout init`.

## 🛡️ Security notes

- **`pbt` locks execute code.** Checking an edited pbt region runs both the edited region and the original recorded in `keepout.json`, in a separate Python process with time limits but with your user's permissions: file writes and network calls in that code really happen. Do not put pbt locks on code with side effects, and run `keepout check` only where you would run the project's tests.
- `keepout check` refuses a `keepout.json` that is unreadable, malformed, or whose recorded `original_code` does not match its `strict_hash`, instead of treating it as empty.

---

## 🧪 Running Tests

```bash
pytest
```

---

## 📄 License

MIT License
