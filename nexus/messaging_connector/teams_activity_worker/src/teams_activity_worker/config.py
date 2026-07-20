"""Environment-backed worker configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    microsoft_tenant_id: str
    microsoft_app_id: str
    microsoft_app_password: str
    teams_service_url: str = DEFAULT_SERVICE_URL
    temporal_address: str = "localhost:7233"
    connector_namespace: str = "connector"
    task_queue: str = "nexus-connector-teams"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            microsoft_tenant_id=_required("MICROSOFT_TENANT_ID"),
            microsoft_app_id=_required("MICROSOFT_APP_ID"),
            microsoft_app_password=_required("MICROSOFT_APP_PASSWORD"),
            teams_service_url=os.getenv("TEAMS_SERVICE_URL", DEFAULT_SERVICE_URL).strip() or DEFAULT_SERVICE_URL,
            temporal_address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233").strip() or "localhost:7233",
            connector_namespace=os.getenv("CONNECTOR_NAMESPACE", "connector").strip() or "connector",
            task_queue=os.getenv("CONNECTOR_TASK_QUEUE", "nexus-connector-teams").strip()
            or "nexus-connector-teams",
        )

