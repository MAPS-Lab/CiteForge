import runpy
from pathlib import Path
from types import ModuleType

import pytest

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


def test_root_main_delegates_to_the_packaged_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import citeforge.cli as cli

    monkeypatch.setattr(cli, "main", lambda: 11)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(__file__).parents[1] / "main.py"), run_name="__main__")

    assert exc_info.value.code == 11
