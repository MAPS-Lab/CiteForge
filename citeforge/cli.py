"""Command-line interface for CiteForge."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from citeforge.config import (
    DEFAULT_GEMINI_KEY_FILE,
    DEFAULT_INPUT,
    DEFAULT_OR_KEY_FILE,
    DEFAULT_OUT_DIR,
    DEFAULT_S2_KEY_FILE,
    DEFAULT_SERPAPI_KEY_FILE,
    DEFAULT_SERPLY_KEY_FILE,
    MAX_PUBLICATIONS_PER_AUTHOR,
    get_min_year,
)
from citeforge.exceptions import FILE_IO_ERRORS, FILE_READ_ERRORS
from citeforge.http_utils import reset_api_call_counts
from citeforge.io_utils import (
    init_summary_csv,
    read_gemini_api_key,
    read_openreview_credentials,
    read_records,
    read_semantic_api_key,
    read_serpapi_api_key,
    read_serply_api_key,
)
from citeforge.log_utils import LogCategory, logger
from citeforge.pipeline.postrun import finalize_run
from citeforge.pipeline.scheduler import prioritize_records, run_all
from citeforge.refresh.census import load_census
from citeforge.refresh.checkpoint import CheckpointError, CheckpointStore
from citeforge.refresh.engine import RefreshEngine
from citeforge.refresh.inventory import InventoryPolicy, RefreshCredentials
from citeforge.refresh.ledger import Ledger, _digest
from citeforge.refresh.transport import LedgerTransport
from citeforge.refresh.types import GenerationSpec, RunStatus

T = TypeVar("T")

DEFAULT_LEDGER_NAME = "ledger.sqlite3"

# The inventory planner rejects any Scholar page topology that does not step by 100,
# so the page bound follows from the configured publication bound rather than a
# second, independently drifting constant.
_SCHOLAR_PAGE_SIZE = 100

# One bounded inventory generation needs the two seed adapters the engine preflights
# plus the two profile adapters the census can carry. A wider set would demand
# discovery preflight authority the CLI does not grant.
_REFRESH_ADAPTER_VERSIONS = {"dblp": "1", "doi_csl": "1", "s2": "1", "scholar": "1"}

_FAILED_STATUSES = frozenset({RunStatus.BLOCKED, RunStatus.INVALID_CONFIGURATION})


def _path_from_cwd(path: Path) -> Path:
    """Return an absolute path, interpreting relative paths from the invocation CWD."""
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options and resolve all data paths from the current directory."""
    parser = argparse.ArgumentParser(description="Enrich author BibTeX records from scholarly APIs.")
    parser.add_argument("--force", action="store_true", help="Re-enrich records even when their cache is complete.")
    parser.add_argument(
        "--input", type=Path, default=Path(DEFAULT_INPUT), help="Author CSV file (default: data/input.csv)."
    )
    parser.add_argument(
        "--output", type=Path, default=Path(DEFAULT_OUT_DIR), help="Output directory (default: output)."
    )
    subparsers = parser.add_subparsers(dest="command")
    refresh = subparsers.add_parser("refresh", help="Run one bounded durable refresh generation.")
    refresh.add_argument(
        "--input", type=Path, default=Path(DEFAULT_INPUT), help="Author census CSV (default: data/input.csv)."
    )
    refresh.add_argument("--state-dir", type=Path, required=True, help="Directory holding the durable refresh ledger.")
    refresh.add_argument(
        "--generation",
        default="",
        help="Reject the run unless the inputs derive this generation identifier.",
    )
    refresh.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=None,
        help="Directory of authenticated checkpoints. Requires CHECKPOINT_KEY in the environment.",
    )
    args = parser.parse_args(argv)
    args.input = _path_from_cwd(args.input)
    args.output = _path_from_cwd(args.output)
    if args.command == "refresh":
        args.state_dir = _path_from_cwd(args.state_dir)
        if args.checkpoint_dir is not None:
            args.checkpoint_dir = _path_from_cwd(args.checkpoint_dir)
    return args


def _read_serpapi_key() -> str | None:
    """Read the SerpAPI key file. Both the legacy run and refresh need it."""
    return read_serpapi_api_key(str(_path_from_cwd(Path(DEFAULT_SERPAPI_KEY_FILE))))


def _load_optional_key(reader: Callable[[], T], label: str, miss_note: str) -> T:
    """Load an optional credential, logging success or the degradation on a miss."""
    value = reader()
    if value:
        logger.success(f"{label} loaded", category=LogCategory.PLAN)
    else:
        logger.warn(f"{label} not found; {miss_note}", category=LogCategory.PLAN)
    return value


def run_pipeline(input_path: Path, output_path: Path, force_enrich: bool) -> int:
    """Load credentials and records, run enrichment, then finalize the result."""
    out_dir = str(output_path)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create output directory '{out_dir}': {e}", category=LogCategory.ERROR)
        return 2

    logger.set_log_file(os.path.join(out_dir, "run.log"))
    reset_api_call_counts()
    logger.step("CiteForge run started", category=LogCategory.PLAN)

    serpapi_credential = _read_serpapi_key()
    if not serpapi_credential:
        logger.error("SerpAPI key not found; cannot fetch author publications", category=LogCategory.PLAN)
        logger.close()
        return 2
    logger.success("SerpAPI key loaded", category=LogCategory.PLAN)

    serply_key = _load_optional_key(
        lambda: read_serply_api_key(str(_path_from_cwd(Path(DEFAULT_SERPLY_KEY_FILE)))),
        "Serply API key",
        "Scholar citation detail will be skipped",
    )
    s2_api_key = _load_optional_key(
        lambda: read_semantic_api_key(str(_path_from_cwd(Path(DEFAULT_S2_KEY_FILE)))),
        "Semantic Scholar key",
        "S2 enrichment disabled",
    )
    or_creds = _load_optional_key(
        lambda: read_openreview_credentials(str(_path_from_cwd(Path(DEFAULT_OR_KEY_FILE)))),
        "OpenReview credentials",
        "OpenReview enrichment may be limited",
    )
    gemini_api_key = _load_optional_key(
        lambda: read_gemini_api_key(str(_path_from_cwd(Path(DEFAULT_GEMINI_KEY_FILE)))),
        "Gemini API key",
        "short titles will use fallback algorithm",
    )

    try:
        records = read_records(str(input_path))
        logger.success(f"Input loaded: {len(records)} record(s)", category=LogCategory.PLAN)
    except FILE_READ_ERRORS as e:
        logger.error(f"Error reading input file: {e}", category=LogCategory.ERROR)
        logger.close()
        return 2

    records = prioritize_records(records, out_dir)

    csv_path = os.path.join(out_dir, "summary.csv")
    summary_csv_path: str | None = csv_path
    try:
        init_summary_csv(csv_path, preserve_existing=True)
        logger.success(f"Summary CSV initialized: {csv_path}", category=LogCategory.PLAN)
    except FILE_IO_ERRORS as e:
        logger.warn(f"Could not initialize summary CSV: {e}", category=LogCategory.ERROR)
        summary_csv_path = None

    total_saved, processed = run_all(
        serpapi_credential,
        serply_key,
        s2_api_key,
        or_creds,
        gemini_api_key,
        records,
        out_dir,
        summary_csv_path,
        force_enrich,
    )

    try:
        finalize_run(out_dir, records, total_saved, processed, summary_csv_path)
    finally:
        logger.close()

    return 0


def _inventory_policy() -> InventoryPolicy:
    """Bind the durable inventory policy to the single configured corpus window."""
    pages = -(-MAX_PUBLICATIONS_PER_AUTHOR // _SCHOLAR_PAGE_SIZE)
    return InventoryPolicy(get_min_year(), MAX_PUBLICATIONS_PER_AUTHOR, pages)


def _policy_version(policy: InventoryPolicy) -> str:
    """Derive the refresh policy identity so a bound change forces a new generation."""
    return f"inventory-{policy.min_year}-{policy.max_publications}-{policy.max_scholar_pages}"


def _head_commit(repo_root: Path) -> str:
    """Resolve the committed base the generation identity binds to."""
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("git is required to identify the refresh base commit")
    try:
        completed = subprocess.run(  # noqa: S603
            (executable, "--no-replace-objects", "rev-parse", "--verify", "--end-of-options", "HEAD"),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot resolve the current Git HEAD commit") from exc
    commit = completed.stdout.strip()
    if not commit:
        raise ValueError("current Git HEAD commit is empty")
    return commit


def _build_spec(input_path: Path, repo_root: Path, policy: InventoryPolicy) -> GenerationSpec:
    """Derive the generation identity from the census, the policy, and the committed base."""
    census = load_census(input_path)
    return GenerationSpec(census, _policy_version(policy), _REFRESH_ADAPTER_VERSIONS, _head_commit(repo_root))


def run_refresh(
    input_path: Path, state_dir: Path, expected_generation: str, checkpoint_dir: Path | None = None
) -> int:
    """Drive one bounded refresh generation against the durable ledger under the state directory."""
    logger.set_log_file(str(state_dir / "refresh.log"))
    reset_api_call_counts()
    logger.step("CiteForge refresh started", category=LogCategory.PLAN)

    repo_root = Path.cwd()
    policy = _inventory_policy()
    try:
        spec = _build_spec(input_path, repo_root, policy)
    except (OSError, ValueError) as e:
        logger.error(f"Refresh inputs rejected: {e}", category=LogCategory.ERROR)
        logger.close()
        return 2

    # The identity is derived, so the flag pins an intended generation rather than
    # selecting one. A mismatch means the census, policy, or base commit moved.
    if expected_generation and expected_generation != spec.id:
        logger.error(
            f"Requested generation {expected_generation} does not match the current inputs ({spec.id})",
            category=LogCategory.ERROR,
        )
        logger.close()
        return 2

    # The checkpoint key is derived here rather than in the workflow, so the
    # derivation and the generation identity stay in one place. Asking for
    # checkpoints without a key is a configuration error, never a silent
    # downgrade to running unprotected.
    store: CheckpointStore | None = None
    if checkpoint_dir is not None:
        secret = os.environ.get("CHECKPOINT_KEY", "")
        if not secret:
            logger.error("--checkpoint-dir requires CHECKPOINT_KEY in the environment", category=LogCategory.ERROR)
            logger.close()
            return 2
        key = hashlib.sha256(secret.encode()).digest()
        key_id = hashlib.sha256(b"citeforge-checkpoint:" + key).hexdigest()[:16]
        store = CheckpointStore(checkpoint_dir, key, key_id)
        # Restore before the ledger is opened, because the sealed payload holds
        # the ledger file itself. A checkpoint that will not verify is an error
        # to escalate, never a reason to start the generation from zero, which
        # is the failure the whole mechanism exists to prevent.
        if store.available_sequences():
            try:
                restored = store.load_latest_valid(
                    generation_id=spec.id,
                    input_digest=_digest(spec.census.canonical_content()),
                    policy_digest=_digest(spec.refresh_policy_version),
                    destination=state_dir,
                )
            except CheckpointError as e:
                logger.error(f"Checkpoint restore failed: {e}", category=LogCategory.ERROR)
                logger.close()
                return 1
            logger.step(f"Restored checkpoint sequence {restored.sequence}", category=LogCategory.PLAN)

    credential = _read_serpapi_key() or None
    credentials = RefreshCredentials(serpapi_key=credential)

    try:
        with Ledger.open(state_dir / DEFAULT_LEDGER_NAME, corpus_repo_root=repo_root) as ledger:
            engine = RefreshEngine(ledger, policy, LedgerTransport(ledger), checkpoint_store=store)
            # One invocation runs one bounded segment to its own natural stop. A caller
            # that wants a shorter segment reruns the command, since the ledger resumes.
            result = engine.run(spec, credentials, lambda: False)
    except (OSError, ValueError) as e:
        logger.error(f"Refresh ledger rejected the generation: {e}", category=LogCategory.ERROR)
        logger.close()
        return 2

    logger.step(
        f"Refresh {result.status.value}: generation={result.generation_id} "
        f"completed_tasks={result.completed_tasks} remaining_tasks={result.remaining_tasks}",
        category=LogCategory.PLAN,
    )
    if result.detail:
        logger.info(f"Refresh detail: {result.detail}", category=LogCategory.PLAN)
    logger.close()
    return 1 if result.status in _FAILED_STATUSES else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run CiteForge with command-line options."""
    args = parse_args(argv)
    if args.command == "refresh":
        return run_refresh(args.input, args.state_dir, args.generation, args.checkpoint_dir)
    return run_pipeline(args.input, args.output, args.force)
