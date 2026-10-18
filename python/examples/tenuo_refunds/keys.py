"""The example's keys, and the Tenuo configuration the worker builds from them.

Three keys, created on first use under ``.keys/`` (gitignored) by ``issue_warrant.py``:

  * the issuer's root, which signs warrants;
  * the agent's holder key, which the warrants are issued to;
  * the support lead's approver key, which signs approvals for refunds the warrant gates.

The worker calls :func:`load` at startup to build :data:`CONFIG`, a ``TenuoPluginConfig`` like
the one Tenuo's Temporal integration uses. The workflow reads :data:`CONFIG` from this module,
which the worker passes through the sandbox, so keys come from the worker's configuration and
never pass through workflow history.

The approval handler, :func:`ask_support_lead`, asks in the worker's output and signs with the
support lead's key, like Tenuo's ``cli_prompt`` but readable when ``just demo`` runs every
program in one terminal. That's for local development, and it's why this worker holds the
approver key. In production the approver key stays with the approver: with Tenuo Cloud, build the
config from ``tenuo_cloud.temporal`` instead, and approvers sign in the dashboard or Slack.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

from tenuo import PublicKey, SignedApproval, SigningKey
from tenuo.approval import ApprovalDenied, ApprovalRequest, sign_approval
from tenuo.temporal import TenuoPluginConfig

# Under `bazel run`, __file__ is in Bazel's runfiles tree; keep the keys in the source tree.
_WORKSPACE = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
KEY_DIR = (
    Path(_WORKSPACE) / "python" / "examples" / "tenuo_refunds"
    if _WORKSPACE
    else Path(__file__).parent
) / ".keys"

CONFIG: TenuoPluginConfig | None = None
#: Whether refunds the warrant leaves to the agent's discretion above the autonomous limit are
#: confirmed by a click in the harness UI rather than signed by the support lead
#: (``just demo-confirm``, which sets ``TENUO_REFUNDS_CONFIRM=1``).
CONFIRM_IN_CHAT = False


def _signing_key(name: str) -> SigningKey:
    path = KEY_DIR / f"{name}.key"
    if not path.exists():
        KEY_DIR.mkdir(exist_ok=True)
        key = SigningKey.generate()
        path.write_text(bytes(key.secret_key_bytes()).hex())
        path.chmod(0o600)
        (KEY_DIR / f"{name}.pub").write_text(bytes(key.public_key_bytes()).hex())
    return SigningKey.from_bytes(bytes.fromhex(path.read_text().strip()))


def _public_key(name: str) -> PublicKey:
    path = KEY_DIR / f"{name}.pub"
    if not path.exists():
        raise SystemExit(f"{path} not found: run `just session A-1001` once to create the keys")
    return PublicKey.from_bytes(bytes.fromhex(path.read_text().strip()))


def root() -> SigningKey:
    """The issuer's root key. Only ``issue_warrant.py`` uses it."""
    return _signing_key("root")


def holder() -> SigningKey:
    return _signing_key("holder")


def approver() -> SigningKey:
    """The support lead's key."""
    return _signing_key("support-lead")


_ASKING = threading.Lock()


def ask_support_lead(request: ApprovalRequest) -> SignedApproval:
    """Put a gated refund to the support lead in this process's output and sign their answer.

    Every line ends in a newline, so the question shows up when ``just demo`` relays the
    worker's output line by line. Logging is held while the question is open so nothing prints
    over it, and one question is asked at a time.
    """
    args = request.arguments or {}
    amount = args.get("amount_cents")
    what = f"refund ${amount / 100:.2f} on order {args.get('order_id')}" if amount else request.tool
    with _ASKING:
        logging.disable(logging.CRITICAL)
        try:
            print(
                f"\n  The agent wants to {what}.\n"
                "  The warrant needs the support lead's approval for this.\n"
                "  Approve as the support lead? Type y and press Enter (anything else refuses):",
                flush=True,
            )
            answer = sys.stdin.readline().strip().lower()
        finally:
            logging.disable(logging.NOTSET)
    if answer not in ("y", "yes"):
        print("  Refused.", flush=True)
        raise ApprovalDenied(request, reason="the support lead refused")
    print("  Approved and signed with the support lead's key.", flush=True)
    return sign_approval(request, approver(), external_id="terminal")


def confirm_in_chat() -> bool:
    """Whether ``TENUO_REFUNDS_CONFIRM`` is set."""
    return os.environ.get("TENUO_REFUNDS_CONFIRM", "") not in ("", "0")


def load() -> TenuoPluginConfig:
    """Build the worker's Tenuo configuration: the agent's holder key, the issuer's public key as
    the only trusted root, and the support lead answering approvals in this terminal."""
    global CONFIG, CONFIRM_IN_CHAT
    CONFIRM_IN_CHAT = confirm_in_chat()
    if not (KEY_DIR / "holder.key").exists():
        raise SystemExit(
            f"{KEY_DIR}/holder.key not found: run `just session A-1001` once to create the keys"
        )
    CONFIG = TenuoPluginConfig(
        signing_key=holder(),
        trusted_roots=[_public_key("root")],
        approval_handler=ask_support_lead,
        # Room for a gated call to wait on the support lead. Set explicitly so Tenuo doesn't
        # log that it widened the default.
        retry_pop_max_windows=240,
    )
    return CONFIG
