from __future__ import annotations

import psycopg
import pytest

from scripts import verify_m2_migration as verifier


ADMIN_URL = "postgresql://postgres:secret@127.0.0.1:5432/postgres"
APP_URL = "postgresql+psycopg://mockmate:secret@127.0.0.1:5432/mockmate_m2_dev"
TEST_URL = "postgresql+psycopg://mockmate:secret@127.0.0.1:5432/mockmate_m2_test"


def _set_env(monkeypatch) -> None:
    monkeypatch.setenv("MOCKMATE_ADMIN_DATABASE_URL", ADMIN_URL)
    monkeypatch.setenv("DATABASE_URL", APP_URL)
    monkeypatch.setenv("MOCKMATE_TEST_DATABASE_URL", TEST_URL)


def test_explicit_database_must_use_migration_prefix() -> None:
    generated = verifier._resolve_database_name(None)
    assert generated.startswith(verifier.MIGRATION_DB_PREFIX)

    accepted = verifier._resolve_database_name("mockmate_m2_migration_abc123")
    assert accepted == "mockmate_m2_migration_abc123"

    with pytest.raises(SystemExit):
        verifier._resolve_database_name("mockmate_m2_dev", dev_db="mockmate_m2_dev")
    with pytest.raises(SystemExit):
        verifier._resolve_database_name("mockmate_m2_test", test_db="mockmate_m2_test")
    with pytest.raises(SystemExit):
        verifier._resolve_database_name("postgres")
    with pytest.raises(SystemExit):
        verifier._resolve_database_name("mockmate_m2_migration_ab")
    with pytest.raises(SystemExit):
        verifier._resolve_database_name("mockmate_m2_migration_" + "x" * 60)


def test_explicit_development_database_runs_no_ddl(monkeypatch) -> None:
    _set_env(monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_create(admin: str, name: str, owner: str) -> None:
        calls.append(("create", name))

    monkeypatch.setattr(verifier, "_create_database", fake_create)
    monkeypatch.setattr(
        verifier, "_run_verification", lambda app_url, database: calls.append(("run", database))
    )
    monkeypatch.setattr(
        verifier, "_drop_database", lambda admin, name: calls.append(("drop", name))
    )

    assert verifier.main(["--database", "mockmate_m2_dev"]) != 0
    assert calls == []


def test_create_database_uses_identifier_and_only_create(monkeypatch) -> None:
    statements: list[str] = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            statements.append(query.as_string(None))
            raise psycopg.errors.DuplicateDatabase("already exists")

    def fake_connect(dsn: str, autocommit: bool = False):
        assert autocommit is True
        return FakeConnection()

    monkeypatch.setattr(psycopg, "connect", fake_connect)

    with pytest.raises(psycopg.errors.DuplicateDatabase):
        verifier._create_database(ADMIN_URL, "mockmate_m2_migration_abc123", "mockmate")

    assert len(statements) == 1
    assert "CREATE DATABASE" in statements[0]
    assert "DROP DATABASE" not in statements[0]
    assert "pg_terminate_backend" not in statements[0]


def test_duplicate_database_exits_without_drop_or_work(monkeypatch) -> None:
    _set_env(monkeypatch)
    calls: list[tuple[str, str]] = []

    def duplicate(admin: str, name: str, owner: str) -> None:
        raise psycopg.errors.DuplicateDatabase("already exists")

    monkeypatch.setattr(verifier, "_create_database", duplicate)
    monkeypatch.setattr(
        verifier, "_run_verification", lambda app_url, database: calls.append(("run", database))
    )
    monkeypatch.setattr(
        verifier, "_drop_database", lambda admin, name: calls.append(("drop", name))
    )

    result = verifier.main(["--database", "mockmate_m2_migration_existing1"])

    assert result == 2
    assert calls == []


def test_cleanup_only_touches_database_created_in_this_run(monkeypatch) -> None:
    _set_env(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        verifier, "_create_database", lambda admin, name, owner: events.append(("create", name))
    )
    monkeypatch.setattr(
        verifier, "_run_verification", lambda app_url, database: events.append(("run", database))
    )
    monkeypatch.setattr(
        verifier, "_drop_database", lambda admin, name: events.append(("drop", name))
    )

    assert verifier.main([]) == 0
    created = [name for kind, name in events if kind == "create"]
    dropped = [name for kind, name in events if kind == "drop"]
    assert len(created) == 1 and created[0].startswith(verifier.MIGRATION_DB_PREFIX)
    assert dropped == created
    assert all("mockmate_m2_dev" not in name and "mockmate_m2_test" not in name for name in dropped)


def test_cleanup_runs_after_midway_failure(monkeypatch) -> None:
    _set_env(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        verifier, "_create_database", lambda admin, name, owner: events.append(("create", name))
    )

    def fail(app_url: str, database: str) -> None:
        events.append(("run", database))
        raise RuntimeError("midway failure")

    monkeypatch.setattr(verifier, "_run_verification", fail)
    monkeypatch.setattr(
        verifier, "_drop_database", lambda admin, name: events.append(("drop", name))
    )

    with pytest.raises(RuntimeError, match="midway failure"):
        verifier.main([])
    created = [name for kind, name in events if kind == "create"]
    dropped = [name for kind, name in events if kind == "drop"]
    assert dropped == created


def test_keep_does_not_drop_temporary_database(monkeypatch) -> None:
    _set_env(monkeypatch)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        verifier, "_create_database", lambda admin, name, owner: events.append(("create", name))
    )
    monkeypatch.setattr(
        verifier, "_run_verification", lambda app_url, database: events.append(("run", database))
    )
    monkeypatch.setattr(
        verifier, "_drop_database", lambda admin, name: events.append(("drop", name))
    )

    assert verifier.main(["--keep"]) == 0
    assert [kind for kind, _ in events] == ["create", "run"]


def test_admin_and_template_must_point_to_same_local_instance(monkeypatch) -> None:
    monkeypatch.setenv("MOCKMATE_ADMIN_DATABASE_URL", ADMIN_URL)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://mockmate:secret@127.0.0.1:5433/mockmate_m2_dev"
    )
    with pytest.raises(SystemExit):
        verifier._validate_template_host(
            ADMIN_URL, "postgresql+psycopg://mockmate:secret@127.0.0.1:5433/mockmate"
        )
    with pytest.raises(SystemExit):
        verifier._validate_template_host(
            ADMIN_URL, "postgresql+psycopg://mockmate:secret@db.example.com:5432/mockmate"
        )
