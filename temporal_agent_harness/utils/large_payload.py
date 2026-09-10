"""Large-payload offloading via Temporal's official external-storage support
(https://docs.temporal.io/develop/python/best-practices/data-handling/external-storage).

Temporal caps a single payload at ~2 MB (activity results, workflow /
continue-as-new inputs, signals, query results). Some harness data doesn't fit —
Code Mode sandbox snapshots most of all — so oversized payloads are offloaded to
external storage.

The SDK implements the "claim check" pattern natively: a ``StorageDriver``
registered on the ``DataConverter`` via ``ExternalStorage`` offloads any payload
over ``payload_size_threshold`` to external storage, replacing it on the wire
with a small reference; retrieval is automatic on decode. This is transparent to
workflow/activity code — they just pass the data.

This module reads NO environment variables: which backend to use is deployment
configuration, so it is passed in. Build an ``ExternalStorage`` with one of the two
factories here and hand it to ``AgentHarnessPlugin(large_payload_offload=...)``::

    # single-host dev (the plugin's default)
    plugin = AgentHarnessPlugin(large_payload_offload=local_payload_storage())

    # multi-host deploy
    plugin = AgentHarnessPlugin(
        large_payload_offload=await s3_payload_storage(bucket=os.environ["MY_BUCKET"]),
    )

Read whatever env vars your deployment uses in your own worker/server startup and
pick the factory there. Every client, worker, and server in a deployment must use
the SAME backend, or a payload offloaded by one is unreadable by another.

# ===========================================================================
# DEPLOY NOTE: LocalFileStorageDriver writes to the LOCAL FILESYSTEM, so an
# offloaded payload can only be retrieved by a process sharing that filesystem —
# fine for local dev (single host: launchers, agent worker, and server all see the
# same /tmp), but NOT across hosts. For multi-host deploys use
# s3_payload_storage(). Operational checklist:
#   * Provision an S3 bucket + IAM for the workers; pass the bucket name to
#     s3_payload_storage() and supply region/credentials via the standard AWS chain.
#   * Set a bucket lifecycle/TTL policy to GC offloaded objects — they are keyed
#     by content hash and never deleted by the driver (the local driver has no
#     GC either; offloaded files just accumulate).
#   * Every worker/launcher/server must use the same bucket, so any offloaded
#     payload is retrievable wherever it's consumed (the cross-host gap the local
#     driver can't span).
# ===========================================================================
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
)

# Where the local driver keeps payloads when no directory is given. A dev default:
# see the DEPLOY NOTE above before relying on it beyond one host.
DEFAULT_LOCAL_PAYLOAD_DIR = Path("/tmp/temporal-large-payloads")

# Offload anything at/above this size. Kept just under Temporal's ~2 MB hard
# limit so only genuinely oversized payloads leave the Event History;
# ordinary payloads (configs, query results) stay inline.
DEFAULT_PAYLOAD_SIZE_THRESHOLD = 1_500_000


def _safe_key(key: str) -> str:
    """Claim keys are flat filenames — reject anything that could escape base_dir."""
    if not key or "/" in key or "\\" in key or ".." in key:
        raise ValueError(f"invalid storage claim key: {key!r}")
    return key


class LocalFileStorageDriver(StorageDriver):
    """Filesystem-backed ``StorageDriver``. Payloads are keyed by a SHA-256 of
    their serialized bytes (so identical bytes dedupe to one file), mirroring the
    SDK's S3 driver. Intended for single-host development — see the module's
    DEPLOY NOTE. Prefer :func:`local_payload_storage`, which wraps this in the
    ``ExternalStorage`` the plugin wants."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        driver_name: str = "local-file",
    ) -> None:
        self._base = (
            Path(base_dir) if base_dir is not None else DEFAULT_LOCAL_PAYLOAD_DIR
        )
        self._name = driver_name

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        # Stable, language-agnostic identifier (the default would be the class name).
        return "local.filedriver"

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        self._base.mkdir(parents=True, exist_ok=True)
        claims: list[StorageDriverClaim] = []
        for payload in payloads:
            raw = payload.SerializeToString()
            digest = hashlib.sha256(raw).hexdigest()
            key = f"{digest}.bin"
            path = self._base / key
            if not path.exists():
                # Write to a temp file then atomically rename so a reader never
                # sees a partially-written blob.
                tmp = path.with_name(f"{key}.{os.getpid()}.tmp")
                tmp.write_bytes(raw)
                tmp.replace(path)
            claims.append(
                StorageDriverClaim(
                    claim_data={
                        "key": key,
                        "hash_algorithm": "sha256",
                        "hash_value": digest,
                    }
                )
            )
        return claims

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        out: list[Payload] = []
        for claim in claims:
            key = _safe_key(claim.claim_data["key"])
            raw = (self._base / key).read_bytes()
            expected = claim.claim_data.get("hash_value")
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"integrity check failed for stored payload {key!r}")
            payload = Payload()
            payload.ParseFromString(raw)
            out.append(payload)
        return out


def local_payload_storage(
    *,
    base_dir: Path | str | None = None,
    payload_size_threshold: int = DEFAULT_PAYLOAD_SIZE_THRESHOLD,
) -> ExternalStorage:
    """Offload oversized payloads to the local filesystem. For SINGLE-HOST use only.

    The default for local development, and the harness plugin's default. Only a process
    sharing this filesystem can read back what it stores, so use
    :func:`s3_payload_storage` for anything multi-host (see the module's DEPLOY NOTE).

    Args:
        base_dir: Directory to write offloaded payloads to. Defaults to
            :data:`DEFAULT_LOCAL_PAYLOAD_DIR`.
        payload_size_threshold: Offload payloads at or above this many bytes.
    """
    return ExternalStorage(
        drivers=[LocalFileStorageDriver(base_dir)],
        payload_size_threshold=payload_size_threshold,
    )


# The default offload target, built once. An ``ExternalStorage`` is a frozen dataclass over a
# stateless driver, so one instance is safely shared by every plugin, client, worker, and
# server in a process — and sharing it is the point: they must all agree on the backend.
DEFAULT_PAYLOAD_STORAGE = local_payload_storage()


async def s3_payload_storage(
    *,
    bucket: str,
    payload_size_threshold: int = DEFAULT_PAYLOAD_SIZE_THRESHOLD,
) -> ExternalStorage:
    """Offload oversized payloads to an S3 bucket. The multi-host deploy target.

    Built on the SDK's out-of-the-box ``S3StorageDriver`` (it keys objects by content hash
    and integrity-checks on retrieve — no custom driver to write). Requires the harness's
    ``s3`` extra; region and credentials come from the standard AWS chain (and
    ``AWS_ENDPOINT_URL_S3`` overrides the endpoint, e.g. for MinIO in tests).

    Async because the aioboto3 client it wraps must be created from a running event loop —
    so call it from your ``async`` startup, before constructing the plugin.

    Args:
        bucket: Name of the S3 bucket to store offloaded payloads in. Every process in the
            deployment must use the same one.
        payload_size_threshold: Offload payloads at or above this many bytes.
    """
    import aioboto3
    from temporalio.contrib.aws.s3driver import S3StorageDriver
    from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client

    # aioboto3's client is an async context manager; enter it and hold it open
    # for the process lifetime (no matching __aexit__ — the connection pool is
    # reclaimed on process exit, mirroring a long-lived service client).
    s3_client = await aioboto3.Session().client("s3").__aenter__()
    return ExternalStorage(
        drivers=[S3StorageDriver(client=new_aioboto3_client(s3_client), bucket=bucket)],
        payload_size_threshold=payload_size_threshold,
    )


def with_large_payload_offload(
    base: DataConverter, storage: ExternalStorage
) -> DataConverter:
    """Return ``base`` configured to offload oversized payloads to ``storage``.

    For assembling a ``DataConverter`` by hand — a service that talks to harness agents but
    isn't one itself. Agent clients and workers should add ``AgentHarnessPlugin`` instead,
    which applies the same offload alongside the rest of the harness's wiring. Whichever you
    use, every process in the deployment must end up with the same backend.
    """
    if base.external_storage is not None:
        raise ValueError("data converter already has external_storage configured")
    return dataclasses.replace(base, external_storage=storage)
