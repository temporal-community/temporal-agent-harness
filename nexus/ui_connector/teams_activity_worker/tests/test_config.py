import pytest

from teams_activity_worker.worker import DEFAULT_SERVICE_URL, Settings


def test_settings_reads_existing_environment_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROSOFT_TENANT_ID", "tenant")
    monkeypatch.setenv("MICROSOFT_APP_ID", "app")
    monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "secret")
    monkeypatch.delenv("TEAMS_SERVICE_URL", raising=False)

    settings = Settings.from_env()

    assert settings.microsoft_tenant_id == "tenant"
    assert settings.microsoft_app_id == "app"
    assert settings.microsoft_app_password == "secret"
    assert settings.teams_service_url == DEFAULT_SERVICE_URL
    assert settings.task_queue == "nexus-connector-teams"


def test_settings_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MICROSOFT_TENANT_ID", raising=False)
    with pytest.raises(ValueError, match="MICROSOFT_TENANT_ID is required"):
        Settings.from_env()
