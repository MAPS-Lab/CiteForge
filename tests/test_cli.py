import hashlib
import runpy
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, TracebackType

import pytest

from citeforge.cli import DEFAULT_LEDGER_NAME
from citeforge.refresh.checkpoint import CheckpointError, CheckpointStore
from citeforge.refresh.ledger import _digest
from citeforge.refresh.types import GenerationSpec, RunResult, RunStatus

CREDENTIALS = ("serpapi", "serply", "s2", ("user", "pass"), "gemini")
KEY_NAMES = ("serpapi_key", "serply_key", "s2_key", "or_key", "gemini_key")
KEY_FILENAMES = ("SerpAPI.key", "Serply.key", "Semantic.key", "OpenReview.key", "Gemini.key")


def _silence_logger(monkeypatch: pytest.MonkeyPatch, cli: ModuleType) -> None:
    for method in ("set_log_file", "step", "success", "warn", "error", "close"):
        monkeypatch.setattr(cli.logger, method, lambda *_args, **_kwargs: None)


def _stub_pipeline_boundaries(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    import citeforge.cli as cli

    records = captured["records"] = [object()]
    capture = captured.update
    patch = monkeypatch.setattr

    patch(cli, "reset_api_call_counts", lambda: None)
    _silence_logger(monkeypatch, cli)
    for name, key, value in (
        ("read_serpapi_api_key", "serpapi_key", "serpapi"),
        ("read_serply_api_key", "serply_key", "serply"),
        ("read_semantic_api_key", "s2_key", "s2"),
        ("read_openreview_credentials", "or_key", ("user", "pass")),
        ("read_gemini_api_key", "gemini_key", "gemini"),
    ):
        patch(cli, name, lambda path, key=key, value=value: capture(**{key: path}) or value)
    patch(cli, "read_records", lambda path: capture(read_records=path) or records)
    patch(cli, "prioritize_records", lambda values, out_dir: capture(prioritize=(values, out_dir)) or values)
    patch(cli, "init_summary_csv", lambda path, preserve_existing: capture(summary=(path, preserve_existing)))
    patch(cli, "run_all", lambda *args: capture(run_all=args) or (3, 4))
    patch(cli, "finalize_run", lambda *args: capture(finalize_run=args))


def _assert_successful_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str], input_path: str, output_path: str, force: bool
) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _stub_pipeline_boundaries(monkeypatch, captured)
    monkeypatch.chdir(tmp_path)

    assert cli.main(args) == 0

    expected_output = str(tmp_path / output_path)
    expected_summary = str(Path(expected_output) / "summary.csv")
    assert captured["read_records"] == str(tmp_path / input_path)
    assert captured["prioritize"] == (captured["records"], expected_output)
    assert captured["summary"] == (expected_summary, True)
    assert captured["run_all"] == (*CREDENTIALS, captured["records"], expected_output, expected_summary, force)
    assert captured["finalize_run"] == (expected_output, captured["records"], 3, 4, expected_summary)
    assert tuple(captured[key] for key in KEY_NAMES) == tuple(str(tmp_path / "keys" / path) for path in KEY_FILENAMES)


def test_cli_defaults_are_cwd_relative(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _assert_successful_run(monkeypatch, tmp_path, [], "data/input.csv", "output", False)


def test_cli_forwards_explicit_paths_and_force_to_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _assert_successful_run(
        monkeypatch,
        tmp_path,
        ["--force", "--input", "records/authors.csv", "--output", "results/bibtex"],
        "records/authors.csv",
        "results/bibtex",
        True,
    )


def test_cli_missing_required_key_returns_two_without_reaching_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import citeforge.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "reset_api_call_counts", lambda: None)
    _silence_logger(monkeypatch, cli)
    monkeypatch.setattr(cli, "read_serpapi_api_key", lambda _path: None)
    monkeypatch.setattr(cli, "read_records", lambda _path: pytest.fail("input reader must not run without a key"))
    monkeypatch.setattr(cli, "run_all", lambda *_args: pytest.fail("scheduler must not run without a key"))
    monkeypatch.setattr(cli, "finalize_run", lambda *_args: pytest.fail("finalizer must not run without a key"))

    assert cli.main([]) == 2


CENSUS = (
    "Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason\n"
    "Ada Lovelace,https://scholar.google.com/citations?user=Scholar123,,true,\n"
    "Excluded Author,,,false,No profile configured\n"
)
HEAD_COMMIT = "f" * 40


class _FakeLedger:
    """Stand in for the durable ledger without touching SQLite."""

    def __init__(self, path: Path, corpus_repo_root: Path | None) -> None:
        self.path = path
        self.corpus_repo_root = corpus_repo_root
        self.closed = False

    @classmethod
    def open(cls, path: Path, *, corpus_repo_root: Path | None = None) -> "_FakeLedger":
        return cls(path, corpus_repo_root)

    def __enter__(self) -> "_FakeLedger":
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.closed = True


def _install_refresh_doubles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured: dict[str, object], status: RunStatus
) -> None:
    import citeforge.cli as cli

    _silence_logger(monkeypatch, cli)
    monkeypatch.setattr(cli, "reset_api_call_counts", lambda: None)
    monkeypatch.setattr(cli, "read_serpapi_api_key", lambda path: captured.update(serpapi_key=path) or "serpapi")
    monkeypatch.setattr(cli, "_head_commit", lambda repo_root: captured.update(repo_root=repo_root) or HEAD_COMMIT)
    monkeypatch.setattr(cli, "Ledger", _FakeLedger)
    monkeypatch.setattr(cli, "LedgerTransport", lambda ledger: captured.update(transport_ledger=ledger) or "transport")

    class FakeEngine:
        def __init__(
            self, ledger: object, policy: object, transport: object, *, checkpoint_store: object = None
        ) -> None:
            captured.update(engine=(ledger, policy, transport), checkpoint_store=checkpoint_store)

        def run(self, spec: GenerationSpec, credentials: object, stop_requested: Callable[[], bool]) -> RunResult:
            captured.update(spec=spec, credentials=credentials, stopped=stop_requested())
            return RunResult(status, spec.id, completed_tasks=2, remaining_tasks=3, detail="engine detail")

    monkeypatch.setattr(cli, "RefreshEngine", FakeEngine)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "input.csv").write_text(CENSUS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_refresh_opens_the_ledger_and_drives_the_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _install_refresh_doubles(monkeypatch, tmp_path, captured, RunStatus.CONTINUATION)

    assert cli.main(["refresh", "--state-dir", "state"]) == 0

    ledger = captured["engine"][0]  # type: ignore[index]
    assert isinstance(ledger, _FakeLedger)
    assert ledger.path == tmp_path / "state" / cli.DEFAULT_LEDGER_NAME
    assert ledger.corpus_repo_root == tmp_path
    assert ledger.closed is True
    assert captured["transport_ledger"] is ledger
    assert captured["engine"][2] == "transport"  # type: ignore[index]
    assert captured["credentials"] == cli.RefreshCredentials(serpapi_key="serpapi")
    assert captured["stopped"] is False


def test_refresh_derives_the_generation_from_census_policy_and_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _install_refresh_doubles(monkeypatch, tmp_path, captured, RunStatus.CONTINUATION)

    assert cli.main(["refresh", "--state-dir", "state"]) == 0

    spec = captured["spec"]
    assert isinstance(spec, GenerationSpec)
    assert spec.base_commit == HEAD_COMMIT
    assert captured["repo_root"] == tmp_path
    assert dict(spec.adapter_versions) == {"dblp": "1", "doi_csl": "1", "s2": "1", "scholar": "1"}
    assert spec.refresh_policy_version == cli._policy_version(cli._inventory_policy())
    assert [row.scholar_id for row in spec.census.enabled_rows] == ["Scholar123"]
    assert captured["engine"][1] == cli._inventory_policy()  # type: ignore[index]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.COMPLETE, 0),
        (RunStatus.CONTINUATION, 0),
        (RunStatus.BLOCKED, 1),
        (RunStatus.INVALID_CONFIGURATION, 1),
    ],
)
def test_refresh_exit_status_follows_the_run_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: RunStatus, expected: int
) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _install_refresh_doubles(monkeypatch, tmp_path, captured, status)

    assert cli.main(["refresh", "--state-dir", "state"]) == expected


def test_refresh_accepts_a_matching_requested_generation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _install_refresh_doubles(monkeypatch, tmp_path, captured, RunStatus.CONTINUATION)
    expected = cli._build_spec(tmp_path / "data" / "input.csv", tmp_path, cli._inventory_policy()).id

    assert cli.main(["refresh", "--state-dir", "state", "--generation", expected]) == 0
    assert captured["spec"].id == expected  # type: ignore[union-attr]


def test_refresh_rejects_a_mismatched_generation_before_opening_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _install_refresh_doubles(monkeypatch, tmp_path, captured, RunStatus.CONTINUATION)

    assert cli.main(["refresh", "--state-dir", "state", "--generation", "a" * 64]) == 2
    assert "engine" not in captured


def test_refresh_rejects_an_unreadable_census_before_opening_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import citeforge.cli as cli

    captured: dict[str, object] = {}
    _install_refresh_doubles(monkeypatch, tmp_path, captured, RunStatus.CONTINUATION)

    assert cli.main(["refresh", "--state-dir", "state", "--input", "data/absent.csv"]) == 2
    assert "engine" not in captured


def test_bare_invocation_still_selects_the_legacy_pipeline() -> None:
    import citeforge.cli as cli

    assert cli.parse_args([]).command is None
    assert cli.parse_args(["--force"]).command is None


def test_root_main_delegates_to_the_packaged_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import citeforge.cli as cli

    monkeypatch.setattr(cli, "main", lambda: 11)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(__file__).parents[1] / "main.py"), run_name="__main__")

    assert exc_info.value.code == 11


def test_checkpoint_restore_uses_the_same_digests_the_save_side_wrote(tmp_path: Path) -> None:
    """The restore identity must match what the engine sealed, digest for digest.

    The ledger stores policy_digest as _digest(policy_version) and the engine
    seals with the value read back from the manifest, so restoring with the raw
    version string can never match. Nothing caught that: the gates were green
    and every continuation segment would have exited 1 on a checkpoint it had
    just written. Only a round trip finds it.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / DEFAULT_LEDGER_NAME).write_bytes(b"ledger-state")

    key = hashlib.sha256(b"secret").digest()
    store = CheckpointStore(
        tmp_path / "cp", key, hashlib.sha256(b"citeforge-checkpoint:" + key).hexdigest()[:16]
    )
    generation_id = "a" * 64
    policy_version = "inventory-2020-350-4"
    census_content: dict[str, object] = {"rows": [{"name": "Ada"}]}

    store.save(
        generation_id=generation_id,
        input_digest=_digest(census_content),
        policy_digest=_digest(policy_version),
        sequence=1,
        created_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        state_dir=state,
    )

    restored = store.load_latest_valid(
        generation_id=generation_id,
        input_digest=_digest(census_content),
        policy_digest=_digest(policy_version),
        destination=tmp_path / "restored",
    )
    assert restored.sequence == 1
    assert (tmp_path / "restored" / DEFAULT_LEDGER_NAME).read_bytes() == b"ledger-state"

    # A mismatched policy digest must NOT restore, or the assertion above is vacuous.
    with pytest.raises(CheckpointError, match="different input census or policy"):
        store.load_latest_valid(
            generation_id=generation_id,
            input_digest=_digest(census_content),
            policy_digest=policy_version,
            destination=tmp_path / "raw",
        )
