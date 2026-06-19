"""Large-payload offloading via Temporal's official external-storage support
(https://docs.temporal.io/develop/python/best-practices/data-handling/external-storage).

Temporal caps a single payload at ~2 MB (activity results, workflow /
continue-as-new inputs, signals, query results). Some of our data may not fit
within this limit, so we enable external storage offload.

The SDK implements the "claim check" pattern natively: a ``StorageDriver``
registered on the ``DataConverter`` via ``ExternalStorage`` offloads any payload
over ``payload_size_threshold`` to external storage, replacing it on the wire
with a small reference; retrieval is automatic on decode. This is transparent to
workflow/activity code — they just pass the data.

The backend is chosen by the ``LARGE_PAYLOAD_DRIVER`` env var ("local" — the
default, filesystem-backed for development — or "s3" for multi-host deploys).

# ===========================================================================
# DEPLOY NOTE: the default LocalFileStorageDriver writes to the LOCAL
# FILESYSTEM, so an offloaded payload can only be retrieved by a process sharing
# that filesystem — fine for local dev (single host: launchers, agent worker, 
# and server all see the same /tmp), but NOT across hosts. For multi-host deploys
# set LARGE_PAYLOAD_DRIVER=s3 (the SDK's built-in S3 driver, wired in 
# _s3_storage_driver() below). Operational checklist:
#   * Provision an S3 bucket + IAM for the workers; set LARGE_PAYLOAD_S3_BUCKET
#     and supply region/credentials via the standard AWS chain.
#   * Set a bucket lifecycle/TTL policy to GC offloaded objects — they are keyed
#     by content hash and never deleted by the driver (the local driver has no
#     GC either; offloaded files just accumulate).
#   * Every worker/launcher/server must use LARGE_PAYLOAD_DRIVER=s3 with access
#     to the same bucket, so any offloaded payload is retrievable wherever it's
#     consumed (the cross-host gap the local driver can't span).
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

# Where local payloads live; override via env for tests/deploys.
_BASE_DIR = Path(os.environ.get("LARGE_PAYLOAD_DIR", "/tmp/temporal-large-payloads"))

# Offload anything at/above this size. Kept just under Temporal's ~2 MB hard
# limit so only genuinely oversized payloads leave the Event History; 
# ordinary payloads (configs, query results) stay inline.
_THRESHOLD_BYTES = 1_500_000


def _safe_key(key: str) -> str:
    """Claim keys are flat filenames — reject anything that could escape base_dir."""
    if not key or "/" in key or "\\" in key or ".." in key:
        raise ValueError(f"invalid storage claim key: {key!r}")
    return key


class LocalFileStorageDriver(StorageDriver):
    """Filesystem-backed ``StorageDriver``. Payloads are keyed by a SHA-256 of
    their serialized bytes (so identical bytes dedupe to one file), mirroring the
    SDK's S3 driver. Intended for local development only — see the module TODO."""

    def __init__(
        self,
        base_dir: Path | None = None,
        driver_name: str = "local-file",
    ) -> None:
        self._base = Path(base_dir) if base_dir is not None else _BASE_DIR
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


# Selects the storage backend. "local" (default) writes to the local filesystem;
# "s3" is the multi-host deploy target (not yet wired — see _s3_storage_driver).
_DRIVER_KIND = os.environ.get("LARGE_PAYLOAD_DRIVER", "local").strip().lower()


async def _build_storage_driver() -> StorageDriver:
    """Return the active storage driver, selected by the ``LARGE_PAYLOAD_DRIVER``
    env var ("local" — default — or "s3"). Async because the s3 backend creates
    an aioboto3 client, which must be entered from an async context."""
    match _DRIVER_KIND:
        case "local":
            return LocalFileStorageDriver()
        case "s3":
            return await _s3_storage_driver()
        case _:
            raise ValueError(
                f"unknown LARGE_PAYLOAD_DRIVER={_DRIVER_KIND!r}; expected 'local' or 's3'"
            )


async def _s3_storage_driver() -> StorageDriver:
    """The multi-host S3 backend, built on the SDK's out-of-the-box
    ``S3StorageDriver`` (it keys objects by content hash and integrity-checks on
    retrieve — no custom driver to write). Requires the ``temporalio[aioboto3]``
    extra and ``LARGE_PAYLOAD_S3_BUCKET``; region/credentials come from the
    standard AWS chain (and ``AWS_ENDPOINT_URL_S3`` overrides the endpoint, e.g.
    for MinIO in tests)."""
    import aioboto3
    from temporalio.contrib.aws.s3driver import S3StorageDriver
    from temporalio.contrib.aws.s3driver.aioboto3 import new_aioboto3_client

    bucket = os.environ.get("LARGE_PAYLOAD_S3_BUCKET")
    if not bucket:
        raise RuntimeError(
            "LARGE_PAYLOAD_DRIVER=s3 requires LARGE_PAYLOAD_S3_BUCKET to be set"
        )
    # aioboto3's client is an async context manager; enter it and hold it open
    # for the process lifetime (no matching __aexit__ — the connection pool is
    # reclaimed on process exit, mirroring a long-lived service client).
    s3_client = await aioboto3.Session().client("s3").__aenter__()
    return S3StorageDriver(client=new_aioboto3_client(s3_client), bucket=bucket)


async def with_large_payload_offload(base: DataConverter) -> DataConverter:
    """Return ``base`` configured to offload oversized payloads to external
    storage. Apply at every Client.connect site so offloaded payloads can be
    read back wherever they're consumed. Async because the s3 backend builds an
    aioboto3-backed driver."""
    if base.external_storage is not None:
        raise ValueError("data converter already has external_storage configured")
    return dataclasses.replace(
        base,
        external_storage=ExternalStorage(
            drivers=[await _build_storage_driver()],
            payload_size_threshold=_THRESHOLD_BYTES,
        ),
    )
