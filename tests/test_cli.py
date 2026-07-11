from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ai_meter.cli import run

ROOT = Path(__file__).resolve().parents[1]


def test_cli_acceptance_flow(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "meter.db"
    assert run(["validate", str(ROOT / "examples/provider_api/usage.json")]) == 0
    assert (
        run(["ingest", str(ROOT / "examples/provider_api/usage.json"), "--database", str(db)]) == 0
    )
    assert run(["summarize", "--database", str(db)]) == 0
    assert run(["report", "cost", "--database", str(db)]) == 0
    assert run(["report", "outcomes", "--database", str(db)]) == 0
    assert run(["report", "cost-per-success", "--database", str(db)]) == 0
    assert run(["report", "providers", "--database", str(db)]) == 0
    assert run(["pricing", "validate", str(ROOT / "examples/provider_api/pricing.yaml")]) == 0
    assert (
        run(
            [
                "pricing",
                "calculate",
                str(ROOT / "examples/provider_api/usage.json"),
                "--pricing",
                str(ROOT / "examples/provider_api/pricing.yaml"),
            ]
        )
        == 0
    )
    assert (
        run(
            [
                "infrastructure",
                "calculate",
                str(ROOT / "examples/local_ollama/usage.json"),
                "--profile",
                str(ROOT / "examples/local_ollama/infrastructure.yaml"),
            ]
        )
        == 0
    )
    assert (
        run(
            [
                "budget",
                "evaluate",
                "--database",
                str(db),
                "--budget",
                str(ROOT / "examples/budgets/monthly.yaml"),
            ]
        )
        == 0
    )
    assert run(["report", "anomalies", "--database", str(db)]) == 0
    assert run(["report", "prometheus", "--database", str(db)]) == 0
    assert (
        run(["report", "html", "--database", str(db), "--output", str(tmp_path / "report.html")])
        == 0
    )
    assert run(["report", "reconciliation", "--database", str(db)]) == 0
    assert (
        run(
            [
                "export",
                "--database",
                str(db),
                "--format",
                "csv",
                "--output",
                str(tmp_path / "usage.csv"),
            ]
        )
        == 0
    )
    assert run(["schema", "list"]) == 0
    assert run(["schema", "export", "--output", str(tmp_path / "schemas")]) == 0
    assert run(["pricing", "sources", str(ROOT / "examples/provider_api/pricing.yaml")]) == 0
    assert run(["pricing", "warnings", str(ROOT / "examples/provider_api/pricing.yaml")]) == 0
    assert run(["infrastructure", "profiles"]) == 0
    assert (
        run(
            [
                "adapters",
                "audit-log-ingest",
                str(ROOT / "examples/audit_log/events.jsonl"),
                "--database",
                str(tmp_path / "audit.db"),
            ]
        )
        == 0
    )
    assert run(["modelswap", "project", str(ROOT / "examples/modelswap/projection.yaml")]) == 0
    assert (tmp_path / "schemas" / "usage-record-v1.schema.json").exists()
    assert "valid" in capsys.readouterr().out


def test_cli_error_path() -> None:
    assert run(["validate", str(ROOT / "examples/invalid/negative_tokens.json")]) == 2


def test_cli_missing_and_malformed_files_are_controlled(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert run(["validate", str(tmp_path / "missing.json")]) == 2
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    assert run(["validate", str(malformed)]) == 2
    assert "aimeter: error:" in capsys.readouterr().err


def test_deprecated_cli_alias_warns(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from ai_meter.cli import deprecated_app

    monkeypatch.setattr(sys, "argv", ["openaimeter", "--help"])
    with (
        pytest.warns(DeprecationWarning, match="openaimeter has been renamed to aimeter"),
        pytest.raises(SystemExit) as exc_info,
    ):
        deprecated_app()
    assert exc_info.value.code == 0
    assert "openaimeter: warning:" in capsys.readouterr().err
