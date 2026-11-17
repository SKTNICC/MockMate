from __future__ import annotations

import pytest

from tests import conftest


def _resolve(monkeypatch, explicit: str, dev: str = "") -> tuple[str, str, str | None, str | None]:
    monkeypatch.setenv("MOCKMATE_TEST_DATABASE_URL", explicit)
    if dev:
        monkeypatch.setenv("DATABASE_URL", dev)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    return conftest._resolve_test_database()


def test_explicit_test_dsn_derives_app_and_maintenance_urls(monkeypatch) -> None:
    app_url, name, maintenance, dev = _resolve(
        monkeypatch,
        "postgresql://mockmate:pw@127.0.0.1:5432/mockmate_m2_test",
        "postgresql+psycopg://mockmate:pw@127.0.0.1:5432/mockmate_m2_dev",
    )
    assert name == "mockmate_m2_test"
    assert app_url.startswith("postgresql+psycopg://")
    assert maintenance is not None and maintenance.endswith("/postgres")
    assert dev == "mockmate_m2_dev"


@pytest.mark.parametrize(
    "explicit",
    [
        "postgresql://mockmate:pw@127.0.0.1:5432/mockmate_m2_dev",
        "postgresql://mockmate:pw@10.0.0.5:5432/mockmate_m2_test",
        "sqlite:///mockmate_m2_test",
        "postgresql://mockmate:pw@127.0.0.1:5432/",
    ],
)
def test_explicit_test_dsn_rejects_unsafe_targets(monkeypatch, explicit) -> None:
    with pytest.raises(RuntimeError):
        _resolve(monkeypatch, explicit)


def test_explicit_test_dsn_rejects_same_database_as_dev(monkeypatch) -> None:
    with pytest.raises(RuntimeError):
        _resolve(
            monkeypatch,
            "postgresql://mockmate:pw@127.0.0.1:5432/mockmate_m2_test",
            "postgresql+psycopg://mockmate:pw@127.0.0.1:5432/mockmate_m2_test",
        )
