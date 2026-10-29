"""CLI interface for KeepOut."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import List, Set
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
    load_locks_db,
    python_ast_hash,
    save_locks_db,
    sync_blocks_to_db,
    parse_file_locks,
    make_lock_key,
    property_based_test_equivalence,
)

KEEPOUT_BANNER = r"""
  🔒 _  _____ _____ _____   ____  _   _ _____ 
  | |/ / ____| ____|  _ \ / __ \| | | |_   _|
  | ' /|  _| |  _| | |_) | |  | | | | | | |  
  | . \| |___| |___|  __/| |__| | |_| | | |  
  |_|\_\_____|_____|_|    \____/ \___/  |_|  
"""

IGNORED_DIRS: Set[str] = {
    ".git", ".venv", ".pixi", "__pycache__", "node_modules", "build", "dist", "target", ".idea", ".vscode"
}

SUPPORTED_EXTENSIONS: Set[str] = {
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".rs", ".py", ".go", ".java", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bash", ".sac", ".mojo", ".s", ".asm", ".ll", ".llvm",
    ".scm", ".ss", ".lisp", ".clj", ".apl", ".ijs", ".v", ".sv", ".sol", ".🔥"
}


def find_source_files(root_dir: Path) -> List[Path]:
    """Finds all source code files recursively under root_dir."""
    source_files: List[Path] = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for file in files:
            file_path = Path(root) / file
            if file_path.name == DEFAULT_DB_NAME:
                continue
            if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                source_files.append(file_path)
    return sorted(source_files)


@click.group()
@click.version_option(version=__version__, prog_name="keepout")
def cli():
    """KeepOut - Smart Syntax Locking & Deterministic Property-Based Testing Sub-Engine."""
    pass


@cli.command(name="init")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
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


def export_staged_files(repo_dir: Path, dest: Path) -> None:
    """Writes the staged (index) version of every file keepout reads into dest.

    Paths stay relative to repo_dir, so lock keys match the working tree's.
    """
    listing = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-files", "-z", "--cached"],
        capture_output=True, check=True,
    ).stdout
    wanted = []
    for raw in listing.split(b"\0"):
        name = Path(os.fsdecode(raw))
        if raw and (name.suffix.lower() in SUPPORTED_EXTENSIONS or name.name == DEFAULT_DB_NAME):
            wanted.append(raw)
    if wanted:
        subprocess.run(
            ["git", "-C", str(repo_dir), "checkout-index", "-z", "--stdin", f"--prefix={dest}{os.sep}"],
            input=b"\0".join(wanted), capture_output=True, check=True,
        )


@cli.command(name="check")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
@click.option(
    "--staged", is_flag=True,
    help="Check what is staged for commit (the git index), not the working tree. The pre-commit hook uses this.",
)
@click.option(
    "--no-unlock", "no_unlock", is_flag=True,
    help="Ignore UNLOCK_REASON comments, so an unlocked edit is checked like any other. For CI where automated agents can commit.",
)
def check_cmd(target_dir: Path, staged: bool, no_unlock: bool):
    """Check current source files against keepout.json."""
    click.echo(KEEPOUT_BANNER)
    if not staged:
        run_check(target_dir.resolve(), allow_unlock=not no_unlock)

    # Checking the working tree in a pre-commit hook let a staged violation
    # through whenever the file on disk had been put back: what gets
    # committed is the index, so that is what is checked.
    with tempfile.TemporaryDirectory(prefix="keepout-staged-") as tmp:
        try:
            export_staged_files(target_dir.resolve(), Path(tmp))
        except (OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", b"") or b""
            click.echo(f"🚨 Error: could not read the staged files with git: {e} {detail.decode(errors='replace').strip()}", err=True)
            sys.exit(1)
        run_check(Path(tmp).resolve(), allow_unlock=not no_unlock)


def run_check(root_dir: Path, allow_unlock: bool = True) -> None:
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

    source_files = find_source_files(root_dir)
    current_blocks: List[LockBlock] = []

    for sf in source_files:
        try:
            blocks = parse_file_locks(sf)
            current_blocks.extend(blocks)
        except LockParserError as e:
            click.echo(f"🚨 Parse Error: {e}", err=True)
            sys.exit(1)

    violation_count = 0
    checked_keys: Set[str] = set()

    for block in current_blocks:
        try:
            rel_path = block.file_path.relative_to(root_dir).as_posix()
        except ValueError:
            rel_path = block.file_path.as_posix()

        key = make_lock_key(rel_path, block.lock_id)
        checked_keys.add(key)

        # A lock with no record was only a warning, so deleting entries from
        # keepout.json (or emptying it to {"locks": {}}) switched those locks
        # off while the check stayed green.
        if key not in saved_locks:
            click.echo(f"🚨 [UNREGISTERED LOCK] {key} ({rel_path}:{block.start_line}) - No record in {DEFAULT_DB_NAME}, so nothing was checked. Run 'keepout init' to register it.", err=True)
            violation_count += 1
            continue

        saved_info = saved_locks[key]

        # The check runs the mode recorded in keepout.json; one edit there
        # could turn a strict lock into an ast lock without touching source.
        if canonical_mode(saved_info["mode"]) != canonical_mode(block.mode):
            click.echo(f"🚨 [MODE MISMATCH] {key} ({rel_path}:{block.start_line}) - Source says '{block.mode}' but {DEFAULT_DB_NAME} records '{saved_info['mode']}'. Run 'keepout init' if the change is intended.", err=True)
            violation_count += 1
            continue

        current_hash = compute_strict_hash(block.content)
        ext = block.file_path.suffix.lstrip(".")
        current_ast_hash = compute_ast_hash(block.content, lang=ext)

        unlock_reason = block.unlock_reason if allow_unlock else None
        if block.unlock_reason and not allow_unlock:
            click.echo(f"ℹ️ {key}: UNLOCK_REASON \"{block.unlock_reason}\" ignored (--no-unlock).")

        # 1. Strict Lock Mode (Byte/Text SHA-256)
        if saved_info["mode"] == "strict":
            if current_hash == saved_info["strict_hash"]:
                click.echo(f"✅ [PASS] {key} (strict lock intact)")
            else:
                if unlock_reason:
                    click.echo(f"⚠️ [UNLOCKED] {key} modified with reason: \"{unlock_reason}\"")
                else:
                    click.echo(f"🚨 [LOCK VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Strict lock region modified!", err=True)
                    violation_count += 1

        # 2. Smart Syntax Lock Mode (AST Structural & Variable Alpha-Rename Hash - Default)
        elif saved_info["mode"] in ["ast", "default"]:
            if current_ast_hash == saved_info["ast_hash"]:
                click.echo(f"✅ [PASS] {key} (Smart Syntax Lock - formatting, comments & variable renames ignored)")
            else:
                if unlock_reason:
                    click.echo(f"⚠️ [UNLOCKED] {key} modified with reason: \"{unlock_reason}\"")
                else:
                    click.echo(f"🚨 [AST STRUCTURAL VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Code AST structure modified!", err=True)
                    violation_count += 1

        # 3. Deterministic Property-Based Testing (PBT / Logic Lock)
        elif saved_info["mode"] in ["pbt", "logic"]:
            # An equal AST hash stands in for PBT only when it is a real
            # Python AST hash. The token fallback used for everything else
            # ignores '#' lines, '//' to end of line and whitespace, so
            # trusting it let `#[cfg(test)]` or `#if 0` through a logic lock
            # without PBT ever running.
            current_py_hash = python_ast_hash(block.content, ext)
            if current_hash == saved_info["strict_hash"]:
                click.echo(f"✅ [PASS] {key} (logic lock - unmodified text)")
            elif current_py_hash is not None and current_py_hash == saved_info["ast_hash"]:
                click.echo(f"✅ [PASS] {key} (logic lock - AST structural match)")
            elif unlock_reason:
                click.echo(f"⚠️ [UNLOCKED] {key} modified with reason: \"{unlock_reason}\"")
            else:
                # Falling back to the current code here compared the edit with
                # itself, so an entry without original_code always passed.
                orig_code = saved_info["original_code"]
                cached_ces = saved_info.get("cached_counterexamples", [])
                
                try:
                    res = property_based_test_equivalence(
                        orig_code,
                        block.content,
                        lang=ext,
                        num_samples=10000,
                        seed=42,
                        cached_counterexamples=cached_ces
                    )

                    if res.is_equivalent:
                        click.echo(f"✨ [PBT VERIFICATION PASS] {key} ({rel_path}:{block.start_line}) - {res.message}")
                    elif res.status_str == "pbt_unverifiable":
                        # Not a logic violation: the logic may be untouched.
                        # It is a lock that did not check what it guards, and
                        # saying so is the whole point -- a region reported
                        # green without being reached is worse than one with
                        # no lock on it.
                        click.echo(f"🔍 [PBT UNVERIFIABLE] {key} ({rel_path}:{block.start_line}-{block.end_line}) - This lock verified nothing.", err=True)
                        click.echo(f"  {res.message}", err=True)
                        violation_count += 1
                    else:
                        click.echo(f"🚨 [LOGIC VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Calculation logic modified!", err=True)
                        click.echo(f"  {res.message}", err=True)
                        violation_count += 1

                        # Cache newly discovered counterexample into keepout.json for future regression prevention
                        if res.counterexample and "input" in res.counterexample:
                            new_ce = res.counterexample["input"]
                            if new_ce not in cached_ces:
                                cached_ces.append(new_ce)
                                saved_info["cached_counterexamples"] = cached_ces
                                db_data["locks"][key] = saved_info
                                save_locks_db(db_path, db_data)
                                click.echo(f"  📌 Automatically cached counterexample input ({new_ce}) into keepout.json for regression prevention.")

                except Exception as err:
                    click.echo(f"🚨 [PBT ENGINE ERROR] {key} - Unable to perform property-based verification: {err}", err=True)
                    violation_count += 1

    for key, saved_info in saved_locks.items():
        if key not in checked_keys:
            click.echo(f"🚨 [MISSING LOCK] Lock region [{key}] found in {DEFAULT_DB_NAME} but missing from source code!", err=True)
            violation_count += 1

    if violation_count > 0:
        click.echo(f"\n🚨 KeepOut check failed with {violation_count} violation(s).", err=True)
        sys.exit(1)
    else:
        click.echo(f"\n✨ All lock checks passed successfully!")
        sys.exit(0)


PRE_COMMIT_MARKER = "# KEEPOUT_PRE_COMMIT_HOOK"

# The check runs on the staged content, and a missing keepout blocks the
# commit: the old block checked the working tree and, without keepout on
# PATH, printed "Skipping check" and let the commit through.
PRE_COMMIT_SCRIPT_BLOCK = f"""{PRE_COMMIT_MARKER}
echo "🔒 Running KeepOut pre-commit check on the staged content..."
if command -v pixi >/dev/null 2>&1 && pixi run keepout --version >/dev/null 2>&1; then
    pixi run keepout check --staged . || {{ echo "🚨 KeepOut pre-commit check failed! Commit blocked."; exit 1; }}
elif command -v keepout >/dev/null 2>&1; then
    keepout check --staged . || {{ echo "🚨 KeepOut pre-commit check failed! Commit blocked."; exit 1; }}
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
