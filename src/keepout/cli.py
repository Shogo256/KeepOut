"""CLI interface for KeepOut."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple
import functools

import click

from keepout import __version__
from keepout.core import (
    DEFAULT_DB_NAME,
    VERSION,
    LockBlock,
    LockDatabaseError,
    LockParserError,
    canonical_mode,
    compute_strict_hash,
    compute_ast_hash,
    language_of,
    load_locks_db,
    structural_hash,
    save_locks_db,
    sync_blocks_to_db,
    parse_file_locks,
    make_lock_key,
    property_based_test_equivalence,
)
from keepout.tsast import TreeSitterUnavailable

KEEPOUT_BANNER = r"""
  🔒 _  _____ _____ _____   ____  _   _ _____ 
  | |/ / ____| ____|  _ \ / __ \| | | |_   _|
  | ' /|  _| |  _| | |_) | |  | | | | | | |  
  | . \| |___| |___|  __/| |__| | |_| | | |  
  |_|\_\_____|_____|_|    \____/ \___/  |_|  
"""

# Skipped by the directory walk used outside git. Inside a git work tree
# .gitignore decides instead.
IGNORED_DIRS: Set[str] = {
    ".git", ".venv", "venv", ".pixi", "__pycache__", "node_modules", "build", "dist", "target",
    "vendor", "coverage", ".idea", ".vscode",
}

SUPPORTED_EXTENSIONS: Set[str] = {
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".rs", ".py", ".go", ".java", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bash", ".sac", ".mojo", ".s", ".asm", ".ll", ".llvm",
    ".scm", ".ss", ".lisp", ".clj", ".apl", ".ijs", ".v", ".sv", ".sol", ".🔥",
    ".cs", ".proto", ".kt", ".kts", ".swift", ".scala",
    ".yaml", ".yml", ".toml", ".tf", ".tfvars", ".hcl", ".sql", ".mk",
}
# Files keepout reads by name rather than extension (see language_of).
SUPPORTED_LANGUAGES_BY_NAME: Set[str] = {"make", "dockerfile"}


def is_lockable(name: str) -> bool:
    """Whether a file of this name can hold locks."""
    path = Path(name)
    if path.name == DEFAULT_DB_NAME:
        return False
    return path.suffix.lower() in SUPPORTED_EXTENSIONS or language_of(path) in SUPPORTED_LANGUAGES_BY_NAME


def find_source_files(root_dir: Path, use_git: bool = True) -> List[Path]:
    """Files under root_dir that can hold locks.

    In a git work tree: the tracked files plus untracked ones .gitignore does
    not exclude, so generated code and dependencies are not read. Elsewhere
    (or with use_git=False, for a tree exported from git): a directory walk
    that skips IGNORED_DIRS.
    """
    if use_git:
        try:
            listing = subprocess.run(
                ["git", "-C", str(root_dir), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True,
            ).stdout
            files = {root_dir / os.fsdecode(raw) for raw in listing.split(b"\0") if raw}
            return sorted(f for f in files if is_lockable(f.name) and f.is_file())
        except (OSError, subprocess.CalledProcessError):
            pass
    source_files: List[Path] = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for file in files:
            if is_lockable(file):
                source_files.append(Path(root) / file)
    return sorted(source_files)


@click.group()
@click.version_option(version=__version__, prog_name="keepout")
def cli():
    """KeepOut - Smart Syntax Locking & Deterministic Property-Based Testing Sub-Engine."""
    pass


def _stops_without_tree_sitter(command):
    """Ends the command with a message, not a traceback, when a region needs
    a tree-sitter grammar that is not installed. Falling back to another
    hash instead would record or compare hashes no full install produces."""
    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except TreeSitterUnavailable as e:
            click.echo(f"🚨 Error: {e}", err=True)
            sys.exit(1)
    return wrapper


@cli.command(name="init")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
@_stops_without_tree_sitter
def init_cmd(target_dir: Path):
    """Scan source files and initialize/update keepout.json."""
    click.echo(KEEPOUT_BANNER)
    root_dir = target_dir.resolve()
    db_path = root_dir / DEFAULT_DB_NAME
    
    source_files = find_source_files(root_dir)
    all_blocks: List[LockBlock] = []

    for sf in source_files:
        try:
            blocks = parse_file_locks(sf)
            all_blocks.extend(blocks)
        except LockParserError as e:
            click.echo(f"🚨 Parse Error: {e}", err=True)
            sys.exit(1)

    try:
        db_data = sync_blocks_to_db(db_path, all_blocks, root_dir)
    except LockDatabaseError as e:
        click.echo(f"🚨 Error: {e}", err=True)
        click.echo(
            f"  init does not overwrite a {DEFAULT_DB_NAME} it cannot read. Restore it from "
            f"version control, or delete it and run init again to rebuild it from the "
            f"current source (its cached counterexamples are lost).",
            err=True,
        )
        sys.exit(1)
    locks = db_data.get("locks", {})

    click.echo(f"🔒 KeepOut initialized! Registered {len(locks)} lock region(s) in {db_path.name}")
    for key, info in locks.items():
        click.echo(f"  • {key} (mode: {info['mode']})")


def _git(repo_dir: Path, *args: str, input: Optional[bytes] = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args], input=input, capture_output=True, check=True,
    ).stdout


def _keepout_reads(name: str) -> bool:
    return is_lockable(name) or Path(name).name == DEFAULT_DB_NAME


def export_staged_files(repo_dir: Path, dest: Path) -> None:
    """Writes the staged (index) version of every file keepout reads into dest.

    Paths stay relative to repo_dir, so lock keys match the working tree's.
    """
    listing = _git(repo_dir, "ls-files", "-z", "--cached")
    wanted = [raw for raw in listing.split(b"\0") if raw and _keepout_reads(os.fsdecode(raw))]
    if wanted:
        _git(repo_dir, "checkout-index", "-z", "--stdin", f"--prefix={dest}{os.sep}", input=b"\0".join(wanted))


def export_tree_files(repo_dir: Path, rev: str, dest: Path) -> None:
    """Writes the files keepout reads, as they are at rev, into dest.

    Read from git objects directly, so nothing in the working tree -- and
    nothing in keepout.json -- decides what the baseline was.
    """
    listing = _git(repo_dir, "ls-tree", "-r", "-z", rev)
    entries = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw_name = record.partition(b"\t")
        mode, kind, oid = meta.split()
        name = os.fsdecode(raw_name)
        if kind == b"blob" and mode != b"120000" and _keepout_reads(name):
            entries.append((oid, name))
    if not entries:
        return
    out = _git(repo_dir, "cat-file", "--batch", input=b"".join(oid + b"\n" for oid, _ in entries))
    pos = 0
    for _, name in entries:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        data = out[header_end + 1:header_end + 1 + size]
        pos = header_end + 1 + size + 1
        if b"[LOCK" in data or b"[/LOCK" in data or Path(name).name == DEFAULT_DB_NAME:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


class BaseRefError(Exception):
    """The --base ref cannot be used as a baseline."""


def resolve_base(repo_dir: Path, ref: str) -> Optional[str]:
    """The merge base of ref and HEAD, or None for HEAD in a repository with
    no commits yet (everything is new)."""
    try:
        _git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except subprocess.CalledProcessError:
        if ref == "HEAD":
            return None
        raise BaseRefError("the repository has no commits yet")
    try:
        target = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except subprocess.CalledProcessError:
        raise BaseRefError(f"git cannot find '{ref}'")
    try:
        return _git(repo_dir, "merge-base", target, "HEAD").decode().strip()
    except subprocess.CalledProcessError:
        raise BaseRefError(f"'{ref}' and HEAD have no common history here")


def collect_blocks(root_dir: Path, use_git: bool = True) -> Dict[str, Tuple[str, LockBlock]]:
    """Every lock under root_dir by key; exits on a malformed directive."""
    found: Dict[str, Tuple[str, LockBlock]] = {}
    for sf in find_source_files(root_dir, use_git):
        try:
            blocks = parse_file_locks(sf)
        except LockParserError as e:
            click.echo(f"🚨 Parse Error: {e}", err=True)
            sys.exit(1)
        for block in blocks:
            try:
                rel_path = block.file_path.relative_to(root_dir).as_posix()
            except ValueError:
                rel_path = block.file_path.as_posix()
            found[make_lock_key(rel_path, block.lock_id)] = (rel_path, block)
    return found


@cli.command(name="check")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
@click.option(
    "--staged", is_flag=True,
    help="Check what is staged for commit (the git index), not the working tree. The pre-commit hook uses this.",
)
@click.option(
    "--base", "base_ref", metavar="REF",
    help="Compare each locked region with the same region at the merge base of REF and HEAD, "
         "read from git, instead of with keepout.json. For CI (--base origin/main) and the "
         "pre-commit hook (--base HEAD).",
)
@click.option(
    "--no-unlock", "no_unlock", is_flag=True,
    help="Ignore UNLOCK_REASON comments, so an unlocked edit is checked like any other. For CI where automated agents can commit.",
)
@_stops_without_tree_sitter
def check_cmd(target_dir: Path, staged: bool, base_ref: Optional[str], no_unlock: bool):
    """Check locked regions against keepout.json, or against git with --base."""
    click.echo(KEEPOUT_BANNER)
    root_dir = target_dir.resolve()
    allow_unlock = not no_unlock
    if not staged and not base_ref:
        run_check(root_dir, allow_unlock)

    with tempfile.TemporaryDirectory(prefix="keepout-") as tmp:
        current_root = root_dir
        try:
            # Checking the working tree in a pre-commit hook let a staged
            # violation through whenever the file on disk had been put back:
            # what gets committed is the index, so that is what is checked.
            if staged:
                current_root = Path(tmp).resolve() / "staged"
                current_root.mkdir()
                export_staged_files(root_dir, current_root)
            if not base_ref:
                run_check(current_root, allow_unlock, use_git=not staged)
            base_sha = resolve_base(root_dir, base_ref)
            base_root = Path(tmp).resolve() / "base"
            base_root.mkdir()
            if base_sha:
                export_tree_files(root_dir, base_sha, base_root)
        except BaseRefError as e:
            click.echo(
                f"🚨 Error: cannot use --base {base_ref}: {e}. In CI, fetch the base branch with "
                f"its history first (actions/checkout with fetch-depth: 0).",
                err=True,
            )
            sys.exit(1)
        except (OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", b"") or b""
            click.echo(f"🚨 Error: could not read the files from git: {e} {detail.decode(errors='replace').strip()}", err=True)
            sys.exit(1)
        label = f"{base_ref} (merge base {base_sha[:7]})" if base_sha else f"{base_ref} (no commits yet)"
        run_base_check(current_root, base_root, label, allow_unlock, current_in_git=not staged)


def _finish(violation_count: int) -> None:
    if violation_count > 0:
        click.echo(f"\n🚨 KeepOut check failed with {violation_count} violation(s).", err=True)
        sys.exit(1)
    click.echo(f"\n✨ All lock checks passed successfully!")
    sys.exit(0)


def check_block(
    key: str,
    rel_path: str,
    block: LockBlock,
    baseline: Dict[str, Any],
    baseline_name: str,
    allow_unlock: bool,
    on_counterexample=None,
) -> int:
    """Compares one locked region with its baseline and reports it.

    baseline has the shape of a keepout.json entry: mode, strict_hash,
    ast_hash, original_code, and optionally cached_counterexamples and
    unlock_reason. Returns the number of violations (0 or 1).
    """
    # The check runs the baseline's mode; changing the directive alone
    # (strict -> ast) must not weaken it.
    if canonical_mode(baseline["mode"]) != canonical_mode(block.mode):
        click.echo(f"🚨 [MODE MISMATCH] {key} ({rel_path}:{block.start_line}) - Source says '{block.mode}' but {baseline_name} has '{baseline['mode']}'.", err=True)
        return 1

    current_hash = compute_strict_hash(block.content)
    ext = language_of(block.file_path)
    current_ast_hash = compute_ast_hash(block.content, lang=ext)

    # An unlock is for one change. A reason the baseline already carries
    # left the region unlocked for every later edit too.
    unlock_reason = block.unlock_reason if allow_unlock else None
    if block.unlock_reason and not allow_unlock:
        click.echo(f"ℹ️ {key}: UNLOCK_REASON \"{block.unlock_reason}\" ignored (--no-unlock).")
    elif unlock_reason and unlock_reason == baseline.get("unlock_reason"):
        click.echo(f"ℹ️ {key}: UNLOCK_REASON \"{unlock_reason}\" is already in {baseline_name}; write a new reason for a new change.")
        unlock_reason = None

    def unlocked() -> int:
        click.echo(f"⚠️ [UNLOCKED] {key} modified with reason: \"{unlock_reason}\"")
        return 0

    mode = canonical_mode(baseline["mode"])
    if mode == "strict":
        if current_hash == baseline["strict_hash"]:
            click.echo(f"✅ [PASS] {key} (strict lock intact)")
            return 0
        if unlock_reason:
            return unlocked()
        click.echo(f"🚨 [LOCK VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Strict lock region modified!", err=True)
        return 1

    if mode == "ast":
        if current_ast_hash == baseline["ast_hash"]:
            click.echo(f"✅ [PASS] {key} (Smart Syntax Lock - formatting, comments & variable renames ignored)")
            return 0
        if unlock_reason:
            return unlocked()
        click.echo(f"🚨 [AST STRUCTURAL VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Code AST structure modified!", err=True)
        return 1

    # Logic lock. An equal AST hash stands in for PBT only when it is a real
    # structural hash (Python, or tree-sitter for Go/JS/TS/Rust): the token
    # hash used for everything else is no proof of equal behaviour.
    current_structural = structural_hash(block.content, ext)
    if current_hash == baseline["strict_hash"]:
        click.echo(f"✅ [PASS] {key} (logic lock - unmodified text)")
        return 0
    if current_structural is not None and current_structural == baseline["ast_hash"]:
        click.echo(f"✅ [PASS] {key} (logic lock - AST structural match)")
        return 0
    if unlock_reason:
        return unlocked()

    cached_ces = list(baseline.get("cached_counterexamples", []))
    try:
        res = property_based_test_equivalence(
            baseline["original_code"],
            block.content,
            lang=ext,
            num_samples=10000,
            seed=42,
            cached_counterexamples=cached_ces,
        )
    except Exception as err:
        click.echo(f"🚨 [PBT ENGINE ERROR] {key} - Unable to perform property-based verification: {err}", err=True)
        return 1

    if res.is_equivalent:
        click.echo(f"✨ [PBT VERIFICATION PASS] {key} ({rel_path}:{block.start_line}) - {res.message}")
        return 0
    if res.status_str == "pbt_unverifiable":
        # Not a logic violation -- the logic may be untouched -- but a lock
        # that did not check what it guards, which is the finding.
        click.echo(f"🔍 [PBT UNVERIFIABLE] {key} ({rel_path}:{block.start_line}-{block.end_line}) - This lock verified nothing.", err=True)
        click.echo(f"  {res.message}", err=True)
        return 1
    click.echo(f"🚨 [LOGIC VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Calculation logic modified!", err=True)
    click.echo(f"  {res.message}", err=True)
    if on_counterexample and res.counterexample and "input" in res.counterexample:
        on_counterexample(res.counterexample["input"])
    return 1


def run_check(root_dir: Path, allow_unlock: bool = True, use_git: bool = True) -> None:
    """Checks root_dir against its keepout.json and exits with the result."""
    db_path = root_dir / DEFAULT_DB_NAME

    if not db_path.exists():
        click.echo(f"🚨 Error: {DEFAULT_DB_NAME} not found. Run 'keepout init' first.", err=True)
        sys.exit(1)

    try:
        db_data = load_locks_db(db_path)
    except LockDatabaseError as e:
        click.echo(f"🚨 Error: {e}", err=True)
        click.echo(
            f"  A lock record that cannot be read verifies nothing, so the check fails. "
            f"Restore {DEFAULT_DB_NAME} from version control, or delete it and run "
            f"'keepout init' to re-register every lock from the current source.",
            err=True,
        )
        sys.exit(1)

    if db_data.get("version") != VERSION:
        click.echo(
            f"🚨 Error: {DEFAULT_DB_NAME} was written with hash format "
            f"{db_data.get('version')!r}; this keepout uses {VERSION!r}, and the two "
            f"AST hashes are not comparable. Review the locked regions, then run "
            f"'keepout init' to re-register them.",
            err=True,
        )
        sys.exit(1)

    saved_locks = db_data["locks"]
    current = collect_blocks(root_dir, use_git)
    violation_count = 0

    for key, (rel_path, block) in current.items():
        # A lock with no record was only a warning, so deleting entries from
        # keepout.json (or emptying it to {"locks": {}}) switched those locks
        # off while the check stayed green.
        if key not in saved_locks:
            click.echo(f"🚨 [UNREGISTERED LOCK] {key} ({rel_path}:{block.start_line}) - No record in {DEFAULT_DB_NAME}, so nothing was checked. Run 'keepout init' to register it.", err=True)
            violation_count += 1
            continue

        saved_info = saved_locks[key]

        def cache(new_ce, key=key, saved_info=saved_info):
            cached = saved_info.setdefault("cached_counterexamples", [])
            if new_ce not in cached:
                cached.append(new_ce)
                save_locks_db(db_path, db_data)
                click.echo(f"  📌 Automatically cached counterexample input ({new_ce}) into keepout.json for regression prevention.")

        violation_count += check_block(key, rel_path, block, saved_info, DEFAULT_DB_NAME, allow_unlock, cache)

    for key in saved_locks:
        if key not in current:
            click.echo(f"🚨 [MISSING LOCK] Lock region [{key}] found in {DEFAULT_DB_NAME} but missing from source code!", err=True)
            violation_count += 1

    _finish(violation_count)


def run_base_check(current_root: Path, base_root: Path, base_label: str, allow_unlock: bool = True,
                   current_in_git: bool = True) -> None:
    """Checks the locks under current_root against the same locks as they are
    in base_root (exported from git), and exits with the result.

    keepout.json decides nothing here. Compared with keepout.json, a change
    plus `keepout init` -- or a hand-edited record -- passed; compared with
    the base commit, the change is a change. The logic lock's original is
    the base commit's code, not keepout.json's original_code.
    """
    click.echo(f"🔎 Comparing locked regions with {base_label}")
    base = collect_blocks(base_root, use_git=False)
    current = collect_blocks(current_root, use_git=current_in_git)
    base_db = _load_db_quietly(base_root / DEFAULT_DB_NAME)
    violation_count = 0

    for key, (rel_path, block) in current.items():
        if key not in base:
            click.echo(f"ℹ️ [NEW LOCK] {key} ({rel_path}:{block.start_line}) - Not in {base_label}; checked from the next change on.")
            continue
        _, base_block = base[key]
        base_ext = language_of(base_block.file_path)
        baseline = {
            "mode": base_block.mode,
            "strict_hash": compute_strict_hash(base_block.content),
            "ast_hash": compute_ast_hash(base_block.content, lang=base_ext),
            "original_code": base_block.content,
            "unlock_reason": base_block.unlock_reason,
            # Inputs only, never code: they are passed to the functions.
            "cached_counterexamples": base_db.get(key, {}).get("cached_counterexamples", []),
        }
        violation_count += check_block(key, rel_path, block, baseline, base_label, allow_unlock)

    for key, (rel_path, base_block) in base.items():
        if key not in current:
            click.echo(f"🚨 [LOCK REMOVED] {key} ({rel_path}:{base_block.start_line} in {base_label}) - The lock is gone from the source.", err=True)
            violation_count += 1

    current_db = _load_db_quietly(current_root / DEFAULT_DB_NAME)
    rerecorded = sorted(
        key for key, entry in current_db.items()
        if key in base_db and entry.get("strict_hash") != base_db[key].get("strict_hash")
    )
    if rerecorded:
        click.echo(f"ℹ️ {DEFAULT_DB_NAME} re-records {', '.join(rerecorded)}; the comparison above used the code in {base_label}, not the record.")

    _finish(violation_count)


def _load_db_quietly(db_path: Path) -> Dict[str, Any]:
    """The locks in a keepout.json used only for information; {} if absent or unreadable."""
    if not db_path.exists():
        return {}
    try:
        return load_locks_db(db_path)["locks"]
    except LockDatabaseError:
        return {}


PRE_COMMIT_MARKER = "# KEEPOUT_PRE_COMMIT_HOOK"

# The hook compares what is staged with HEAD, read from git: a locked region
# changed in this commit fails even when the same commit re-records it in
# keepout.json. The first hook checked the working tree against keepout.json
# and, without keepout on PATH, printed "Skipping check" and let the commit
# through; a missing keepout now blocks it.
PRE_COMMIT_SCRIPT_BLOCK = f"""{PRE_COMMIT_MARKER}
echo "🔒 Running KeepOut pre-commit check on the staged content..."
if command -v pixi >/dev/null 2>&1 && pixi run keepout --version >/dev/null 2>&1; then
    pixi run keepout check --staged --base HEAD . || {{ echo "🚨 KeepOut pre-commit check failed! Commit blocked."; exit 1; }}
elif command -v keepout >/dev/null 2>&1; then
    keepout check --staged --base HEAD . || {{ echo "🚨 KeepOut pre-commit check failed! Commit blocked."; exit 1; }}
else
    echo "🚨 keepout was not found in PATH or the pixi environment, so the locks were not checked. Commit blocked."
    echo "   Install keepout, or run 'git commit --no-verify' to skip the check on purpose."
    exit 1
fi
{PRE_COMMIT_MARKER}_END
"""

_HOOK_BLOCK_PATTERN = re.compile(
    re.escape(PRE_COMMIT_MARKER) + r"\n.*?" + re.escape(f"{PRE_COMMIT_MARKER}_END") + r"\n?",
    re.DOTALL,
)


@cli.command(name="install-hook")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
def install_hook_cmd(target_dir: Path):
    """Install KeepOut pre-commit hook into .git/hooks."""
    root_dir = target_dir.resolve()
    git_dir = root_dir / ".git"

    if not git_dir.exists() or not git_dir.is_dir():
        click.echo(f"🚨 Error: {root_dir} is not a git repository root (.git directory missing).", err=True)
        sys.exit(1)

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "pre-commit"

    if hook_file.exists():
        content = hook_file.read_text(encoding="utf-8")
        if PRE_COMMIT_SCRIPT_BLOCK in content:
            click.echo("ℹ️ KeepOut pre-commit hook is already installed.")
            sys.exit(0)
        if _HOOK_BLOCK_PATTERN.search(content):
            # An older KeepOut block: replace it where it stands.
            new_content = _HOOK_BLOCK_PATTERN.sub(lambda _: PRE_COMMIT_SCRIPT_BLOCK, content, count=1)
            action = "Updated"
        else:
            # First, right after the shebang: appended at the end, the block
            # never ran when the existing hook ended with `exit 0` or `exec`.
            first, sep, rest = content.partition("\n")
            if first.startswith("#!"):
                new_content = first + "\n\n" + PRE_COMMIT_SCRIPT_BLOCK + "\n" + rest
            else:
                new_content = PRE_COMMIT_SCRIPT_BLOCK + "\n" + content
            action = "Installed"
    else:
        new_content = "#!/bin/sh\n\n" + PRE_COMMIT_SCRIPT_BLOCK
        action = "Installed"

    hook_file.write_text(new_content, encoding="utf-8")

    mode = hook_file.stat().st_mode
    hook_file.chmod(mode | 0o111)

    click.echo(f"🪝 {action} KeepOut pre-commit hook at {hook_file}")

    hooks_path = subprocess.run(
        ["git", "-C", str(root_dir), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True,
    ).stdout.strip() if _git_available() else ""
    if hooks_path:
        click.echo(
            f"⚠️ core.hooksPath is set to '{hooks_path}', so git will not run {hook_file}. "
            f"Add the KeepOut block to the pre-commit hook there.",
            err=True,
        )


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


if __name__ == "__main__":
    cli()
