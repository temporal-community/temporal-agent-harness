"""Where the Temporal Web UI shows this server's workflows, for the console's "Open in Temporal".

The server already knows which Temporal it is connected to, so it tells the console rather than
every page assuming the local dev server: ``TEMPORAL_UI_URL`` when set (a self-hosted UI), else
Temporal Cloud's UI for a Cloud address, else the dev server's UI for a local one. Anything else
is left unknown, and the console falls back to its own default.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlsplit

TEMPORAL_UI_URL_ENV = "TEMPORAL_UI_URL"
TEMPORAL_CLOUD_UI = "https://cloud.temporal.io"
DEV_SERVER_UI = "http://localhost:8233"
_CLOUD_HOST_SUFFIXES = (".tmprl.cloud", ".api.temporal.io")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def temporal_workflows_url(target_host: str | None, namespace: str | None) -> str | None:
    """The Temporal Web UI's workflows page for ``namespace``, or ``None`` when it is unknown.

    A workflow's page is this URL followed by ``/<workflow id>``.
    """
    ui = os.environ.get(TEMPORAL_UI_URL_ENV) or _ui_for_host(target_host)
    if ui is None:
        return None
    return f"{ui.rstrip('/')}/namespaces/{quote(namespace or 'default', safe='')}/workflows"


def _ui_for_host(target_host: str | None) -> str | None:
    if not target_host:
        return None
    # A target is ``host:port`` or a URL; either way only the host decides.
    target = target_host if "://" in target_host else f"//{target_host}"
    try:
        host = urlsplit(target).hostname
    except ValueError:
        return None
    if host is None:
        return None
    if host.endswith(_CLOUD_HOST_SUFFIXES):
        return TEMPORAL_CLOUD_UI
    if host in _LOCAL_HOSTS:
        return DEV_SERVER_UI
    return None
