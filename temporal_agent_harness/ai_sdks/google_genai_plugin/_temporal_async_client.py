"""Temporal-aware AsyncClient shim.

``TemporalAsyncClient`` is an ``AsyncClient`` subclass that wires up
Temporal-aware replacements for modules that need special handling
(files, file search stores, interactions).
"""

from __future__ import annotations

from google.genai.client import AsyncClient

from ._temporal_api_client import (
    TemporalApiClient,
)
from ._temporal_file_search_stores import (
    TemporalAsyncFileSearchStores,
)
from ._temporal_files import (
    TemporalAsyncFiles,
)
from ._temporal_interactions import (
    TemporalAsyncInteractions,
)
from temporalio.workflow import ActivityConfig


class TemporalAsyncClient(AsyncClient):
    """``AsyncClient`` subclass that uses Temporal-aware modules.

    Replaces ``AsyncFiles`` with ``TemporalAsyncFiles``,
    ``AsyncFileSearchStores`` with ``TemporalAsyncFileSearchStores``, and
    the ``interactions`` property with :class:`TemporalAsyncInteractions`
    so that file upload/download operations, file search store uploads,
    and Interactions-API calls all run entirely inside Temporal activities.

    Other modules (models, tunings, caches, batches, live, tokens,
    operations) are inherited unchanged and work through
    ``TemporalApiClient``'s activity-backed HTTP methods.
    """

    def __init__(
        self,
        api_client: TemporalApiClient,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize with Temporal-aware files, file search stores, and interactions."""
        super().__init__(api_client)
        self._files = TemporalAsyncFiles(api_client, activity_config)
        self._file_search_stores = TemporalAsyncFileSearchStores(
            api_client, activity_config
        )
        self._temporal_interactions = TemporalAsyncInteractions(
            api_client, activity_config
        )

    @property
    def interactions(self) -> TemporalAsyncInteractions:
        """Return the Temporal-aware ``interactions`` shim.

        Overrides the base ``AsyncClient.interactions`` property, which
        would otherwise try to construct a Stainless ``_nextgen_client``
        using credentials the workflow doesn't (and shouldn't) hold.
        """
        return self._temporal_interactions
