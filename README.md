# 🔒 KeepOut

> **Cross-platform physical and logic-semantic protection for code regions powered by LLVM IR & Z3 Formal Verification.**

**KeepOut** is a lightweight CLI tool designed to physically protect critical code sections from external changes—whether caused by future oversights, unintended edits by teammates, or automated AI refactorings—by marking regions with comment directives (`# [LOCK]` ... `# [/LOCK]`).

Powered by **LLVM IR (Intermediate Representation)** parsing and Microsoft Research's **Z3 Solver**, KeepOut features **Cross-Platform Differential Formal Verification**, mathematically proving whether code refactorings preserve exact output semantics across all possible inputs on Apple Silicon (M1/M2/M3/M4), x86_64, ARM64, and RISC-V architectures.

---

## 🌟 Key Features

- 🔒 **Two-Tier Locking System**:
  - **Strict Lock (`[LOCK: strict]`)**: Completely freezes exact code lines using SHA-256 hashing. Rejects even a single whitespace or comment change.
  - **Logic-only Lock (`[LOCK: logic]`)**: Permits logic-preserving refactorings (e.g. variable renames, optimization like `x * 4` ➔ `x << 2`), while formally proving semantic equivalence across all $2^{64}$ possible inputs. Rejects any alteration that shifts calculation outputs (e.g., `x * 5`).
- ⚡ **LLVM IR Powered & Architecture Agnostic**:
  - Leverages target-independent LLVM IR (`.ll`). Works seamlessly across Apple Silicon Macs, Linux, Windows, x86_64, and ARM64. Supports C, C++, Rust (`rustc --emit=llvm-ir`), Swift, Zig, and raw `.ll` LLVM IR files.
- 🤖 **Safety Net for AI-Assisted Coding**:
  - Harness the speed of AI coding assistants (Copilot, Claude, Cursor) without worrying about introduced off-by-one errors or subtle logic regressions.
- 🪝 **Git Pre-commit Integration**:
  - Automatically verifies locked regions before commits, physically blocking non-compliant code.
- 🐳 **100% Dockerized**:
  - Zero local setup required. Run KeepOut instantly anywhere using Docker or Docker Compose.

---

## 🧠 How LLVM IR Differential Formal Verification Works

```
[ Original Code ] ──( Clang / Rustc )──> [ LLVM IR A (.ll) ] ──┐
                                                                ├──> [ Z3 LLVM SSA Interpreter ] ──> [ Z3 Solver: A != B ]
[ Modified Code ] ──( Clang / Rustc )──> [ LLVM IR B (.ll) ] ──┘                                           │
                                                                                                           ├──> UNSAT (No counterexample) ➔ PASS ✨
                                                                                                           └──> SAT (Counterexample found) ➔ BLOCK 🚨
```

1. **Target-Agnostic LLVM IR Emission**: High-level code (C, C++, Rust) is compiled via Clang or Rustc into SSA-form LLVM IR (`.ll`).
2. **Z3 SSA Environment Simulation**: LLVM SSA registers and types (`i32`, `i64`, `i1`, etc.) are mapped to Z3 BitVectors.
3. **Cross-Platform Equivalence Proof**: Z3 asserts `Output_A != Output_B` for free input parameters.
   - **`UNSAT`**: No counterexample exists across all $2^{64}$ inputs ➔ **Logic is 100% identical (Pass)**.
   - **`SAT`**: Counterexample found ➔ **Logic violation detected (Block with concrete counterexample input)**.

---

## 🐳 1. Running with Docker (Recommended)

No local Python, Clang, or LLVM installation required.

### Build Docker Image
```bash
docker build -t keepout .
```

### Shell Alias Setup (Optional)
Add this alias to your shell profile (`.bashrc` or `.zshrc`) to run `keepout` like a native binary:
```bash
alias keepout="docker run --rm -v \$(pwd):/workspace keepout"
```

### Docker Usage Examples
```bash
# Initialize locks in keepout.json
keepout init .

# Check lock integrity
keepout check .
```

### Using Docker Compose
```bash
# Initialize locks
docker compose run --rm keepout init .

# Verify locks
docker compose run --rm keepout check .
```

---

## 💻 2. Local Installation & Usage (Pip / Pixi)

### Installation
```bash
# Install via pip
pip install -e .

# Or using Pixi
pixi add z3-solver click pytest clang llvm
pixi run pip install -e .
```

### CLI Commands

#### 1. Initialize Locks (`keepout init`)
Scan source files and save lock hashes/LLVM IR to `keepout.json`:
```bash
keepout init
```

#### 2. Verify Locks (`keepout check`)
Verify source code integrity against `keepout.json`:
```bash
keepout check
```

#### 3. Install Git Pre-commit Hook (`keepout install-hook`)
Install pre-commit hook into `.git/hooks/pre-commit`:
```bash
keepout install-hook
```

---

## 📜 Directive Syntax Protocol

Supports standard comment syntax across multiple languages (`//`, `#`, `/* ... */`).

### 🔒 Strict Lock (Exact Byte Freeze)
```c
// [LOCK: strict]
const int MAX_RETRY_COUNT = 5;
const char* API_ENDPOINT = "https://api.internal/v1";
// [/LOCK]
```

### 🧠 Logic-only Lock (Semantic Equivalence)
```c
// [LOCK: logic name="fast_multiply"]
int fast_multiply(int val) {
    return val * 4;
}
// [/LOCK]
```

### 🔓 UNLOCK_REASON (Bypass Protection)
To intentionally modify a locked region, attach an `UNLOCK_REASON` comment:
```c
// [LOCK: strict] // UNLOCK_REASON: [JIRA-101] Updating retry configuration
const int MAX_RETRY_COUNT = 10;
// [/LOCK]
```

---

## 🧪 Running Tests

```bash
# Run unit & E2E tests
pytest

# Run tests in Docker container
docker run --rm keepout pytest
```

---

## 📄 License

MIT License
