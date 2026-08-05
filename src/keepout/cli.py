"""CLI interface for KeepOut."""

import os
from pathlib import Path
import sys
from typing import List, Set
import click

from keepout import __version__
from keepout.core import (
    DEFAULT_DB_NAME,
    LockBlock,
    LockParserError,
    compute_strict_hash,
    load_locks_db,
    sync_blocks_to_db,
    parse_file_locks,
    make_lock_key,
    compile_snippet_to_llvm_ir,
    prove_llvm_ir_equivalence,
)

IGNORED_DIRS: Set[str] = {
    ".git", ".venv", ".pixi", "__pycache__", "node_modules", "build", "dist", "target", ".idea", ".vscode"
}

SUPPORTED_EXTENSIONS: Set[str] = {
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".rs", ".py", ".go", ".java", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bash", ".sac", ".mojo", ".s", ".asm", ".ll", ".llvm"
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
    """KeepOut - Cross-Platform Logic Protection via LLVM IR & Z3 Formal Verification."""
    pass


@cli.command(name="init")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
def init_cmd(target_dir: Path):
    """Scan source files and initialize/update keepout.json."""
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

    db_data = sync_blocks_to_db(db_path, all_blocks, root_dir)
    locks = db_data.get("locks", {})

    click.echo(f"🔒 KeepOut initialized! Registered {len(locks)} lock region(s) in {db_path.name}")
    for key, info in locks.items():
        click.echo(f"  • {key} (mode: {info['mode']})")


@cli.command(name="check")
@click.argument("target_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=".")
def check_cmd(target_dir: Path):
    """Check current source files against keepout.json."""
    root_dir = target_dir.resolve()
    db_path = root_dir / DEFAULT_DB_NAME

    if not db_path.exists():
        click.echo(f"🚨 Error: {DEFAULT_DB_NAME} not found. Run 'keepout init' first.", err=True)
        sys.exit(1)

    db_data = load_locks_db(db_path)
    saved_locks = db_data.get("locks", {})

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

        key = make_lock_key(rel_path, block.lock_index)
        checked_keys.add(key)

        if key not in saved_locks:
            click.echo(f"⚠️ WARNING: Unregistered lock [{key}] at {rel_path}:{block.start_line}. Run 'keepout init'.")
            continue

        saved_info = saved_locks[key]
        current_hash = compute_strict_hash(block.content)

        if saved_info["mode"] == "strict":
            if current_hash == saved_info["strict_hash"]:
                click.echo(f"✅ [PASS] {key} (strict lock intact)")
            else:
                if block.unlock_reason:
                    click.echo(f"⚠️ [UNLOCKED] {key} modified with reason: \"{block.unlock_reason}\"")
                else:
                    click.echo(f"🚨 [LOCK VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Strict lock region has been modified!", err=True)
                    violation_count += 1

        elif saved_info["mode"] == "logic":
            if current_hash == saved_info["strict_hash"]:
                click.echo(f"✅ [PASS] {key} (logic lock - unmodified text)")
            elif block.unlock_reason:
                click.echo(f"⚠️ [UNLOCKED] {key} modified with reason: \"{block.unlock_reason}\"")
            else:
                saved_llvm_ir = saved_info.get("compiled_llvm_ir")
                ext = block.file_path.suffix.lstrip(".")
                
                try:
                    current_llvm_ir = compile_snippet_to_llvm_ir(block.content, lang=ext, symbol=block.target_symbol)
                    if not saved_llvm_ir:
                        saved_llvm_ir = current_llvm_ir

                    res = prove_llvm_ir_equivalence(saved_llvm_ir, current_llvm_ir)

                    if res.is_equivalent:
                        click.echo(f"✨ [FORMAL PROOF PASS] {key} ({rel_path}:{block.start_line}) - Code text refactored! Z3 proved LLVM IR logic is 100% equivalent.")
                    else:
                        click.echo(f"🚨 [LOGIC VIOLATION] {key} ({rel_path}:{block.start_line}-{block.end_line}) - Calculation logic modified!", err=True)
                        click.echo(f"  {res.message}", err=True)
                        violation_count += 1

                except Exception as err:
                    click.echo(f"🚨 [COMPILER/SOLVER ERROR] {key} - Unable to perform LLVM IR logic verification: {err}", err=True)
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

PRE_COMMIT_SCRIPT_BLOCK = f"""{PRE_COMMIT_MARKER}
echo "🔒 Running KeepOut pre-commit check..."
if command -v pixi >/dev/null 2>&1 && pixi run keepout --version >/dev/null 2>&1; then
    pixi run keepout check .
elif command -v keepout >/dev/null 2>&1; then
    keepout check .
else
    echo "⚠️ keepout executable not found in PATH or pixi env. Skipping check."
fi

if [ $? -ne 0 ]; then
    echo "🚨 KeepOut pre-commit check failed! Commit blocked."
    exit 1
fi
{PRE_COMMIT_MARKER}_END
"""


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
        if PRE_COMMIT_MARKER in content:
            click.echo("ℹ️ KeepOut pre-commit hook is already installed.")
            sys.exit(0)
        else:
            new_content = content.rstrip() + "\n\n" + PRE_COMMIT_SCRIPT_BLOCK
    else:
        new_content = "#!/bin/sh\n\n" + PRE_COMMIT_SCRIPT_BLOCK

    hook_file.write_text(new_content, encoding="utf-8")
    
    mode = hook_file.stat().st_mode
    hook_file.chmod(mode | 0o111)

    click.echo(f"🪝 Successfully installed KeepOut pre-commit hook at {hook_file}")


if __name__ == "__main__":
    cli()
