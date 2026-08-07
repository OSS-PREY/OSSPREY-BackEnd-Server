"""Regression tests for run_pipeline's forecast result reporting.

These lock in the behaviour that a forecast which fails or produces no output
FAILS the job with a specific, human-readable reason, instead of silently
returning a "completed" job with zero months. They also cover the optional
large-repository size guard.

The tests drive the REAL orchestrator.run_pipeline and stub only the external
and heavy stages (GitHub metadata, the Rust scraper, the forecaster, and the
ReACT extractor), so no scraping/forecasting actually runs.
"""

import os
import sys
import json
import types
import importlib

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

GIT = "https://github.com/org/RegressionRepo.git"
PROJECT = "RegressionRepo"  # extract_project_name(GIT) -> bare repo name


def _real_orchestrator():
    """Return the real orchestrator module, reimporting it if another test has
    replaced the sys.modules entry with a stub (as several suites do)."""
    name = "app.pipeline.orchestrator"
    mod = sys.modules.get(name)
    if mod is None or isinstance(mod, types.SimpleNamespace) \
            or not callable(getattr(mod, "run_pipeline", None)):
        sys.modules.pop(name, None)
        mod = importlib.import_module(name)
    return mod


def _make_output_dir(tmp_path, commit_bytes=b"project,date\nRegressionRepo,2020-01-01\n"):
    out = tmp_path / "scrape_out"
    out.mkdir()
    (out / f"{PROJECT}-commit-file-dev.csv").write_bytes(commit_bytes)
    (out / f"{PROJECT}_issues.csv").write_text("repo_name\nRegressionRepo\n")
    return str(out)


def _prime(monkeypatch, tmp_path, output_dir):
    """Force a cache miss (fresh PEX dir) and stub the external/heavy stages so
    only run_pipeline's own control flow executes. Returns the temp PEX dir."""
    orch = _real_orchestrator()
    pex = tmp_path / "pex"
    (pex / "net-vis").mkdir(parents=True)
    (pex / "forecasts").mkdir(parents=True)
    monkeypatch.setenv("PEX_GENERATOR_DIR", str(pex))
    monkeypatch.delenv("MAX_COMMIT_CSV_MB", raising=False)
    monkeypatch.setattr(orch, "get_github_metadata", lambda *a, **k: {})
    monkeypatch.setattr(orch, "run_rust_code", lambda *a, **k: {"output_dir": output_dir})
    return orch, pex


def test_forecast_success_reports_months(monkeypatch, tmp_path):
    output_dir = _make_output_dir(tmp_path)
    orch, pex = _prime(monkeypatch, tmp_path, output_dir)

    def fake_forecast(tech_csv, social_csv, project, tasks, month_range):
        (pex / "net-vis" / f"{PROJECT}.json").write_text(json.dumps({"tech": {}, "social": {}}))
        (pex / "forecasts" / f"{PROJECT}.json").write_text(json.dumps({"0": 0.4, "1": 0.55}))
        return {}
    monkeypatch.setattr(orch, "run_forecast", fake_forecast)

    result = orch.run_pipeline(GIT)

    assert "error" not in result
    # forecast_json is calibrated now (see app/pipeline/calibration.py); the
    # untouched model output is preserved alongside it.
    assert result["forecast_json_raw"] == {"0": 0.4, "1": 0.55}
    assert set(result["forecast_json"]) == {"0", "1"}
    assert all(0.0 < v < 1.0 for v in result["forecast_json"].values())


def test_forecast_error_result_surfaces_reason(monkeypatch, tmp_path):
    output_dir = _make_output_dir(tmp_path)
    orch, _ = _prime(monkeypatch, tmp_path, output_dir)
    monkeypatch.setattr(
        orch, "run_forecast",
        lambda *a, **k: {"error": "out of memory while processing", "error_type": "MemoryError"},
    )

    result = orch.run_pipeline(GIT)

    # No output produced -> job must fail with the real reason, not report a
    # completed run with zero months.
    assert "forecast_json" not in result
    assert "error" in result
    assert "out of memory while processing" in result["error"]
    assert PROJECT in result["error"]


def test_forecast_crash_surfaces_exception(monkeypatch, tmp_path):
    output_dir = _make_output_dir(tmp_path)
    orch, _ = _prime(monkeypatch, tmp_path, output_dir)

    def boom(*a, **k):
        raise RuntimeError("boom in forecaster")
    monkeypatch.setattr(orch, "run_forecast", boom)

    result = orch.run_pipeline(GIT)

    assert "forecast_json" not in result
    assert "error" in result
    assert "boom in forecaster" in result["error"]


def test_missing_output_without_error_still_fails(monkeypatch, tmp_path):
    output_dir = _make_output_dir(tmp_path)
    orch, _ = _prime(monkeypatch, tmp_path, output_dir)
    # Forecaster "succeeds" (no error) but writes nothing.
    monkeypatch.setattr(orch, "run_forecast", lambda *a, **k: {})

    result = orch.run_pipeline(GIT)

    assert "forecast_json" not in result
    assert "error" in result
    assert PROJECT in result["error"]


def test_large_repo_guard_short_circuits(monkeypatch, tmp_path):
    output_dir = _make_output_dir(tmp_path, commit_bytes=b"x" * 4096)
    orch, _ = _prime(monkeypatch, tmp_path, output_dir)
    monkeypatch.setenv("MAX_COMMIT_CSV_MB", "0.001")  # ~1 KB limit < 4 KB commit file

    calls = {"n": 0}

    def should_not_run(*a, **k):
        calls["n"] += 1
        return {}
    monkeypatch.setattr(orch, "run_forecast", should_not_run)

    result = orch.run_pipeline(GIT)

    assert calls["n"] == 0
    assert "error" in result
    assert "too large" in result["error"].lower()
