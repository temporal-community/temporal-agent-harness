"""Submit a diff file to the running code review example and print the final review."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .models import ReviewMessage, ReviewRequest


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", type=Path, required=True)
    parser.add_argument("--instructions", default="")
    parser.add_argument("--ui-url", default="http://localhost:8002")
    parser.add_argument(
        "--timeout", type=float, default=1200, help="HTTP stream timeout in seconds"
    )
    args = parser.parse_args()
    request = ReviewRequest(diff=args.diff.read_text(), instructions=args.instructions)
    async with httpx.AsyncClient(base_url=args.ui_url, timeout=args.timeout) as client:
        response = await client.post(
            "/api/sessions", json={"agent_workflow_type": "CodeReviewCoordinator"}
        )
        response.raise_for_status()
        session_id = response.json()["workflow_id"]
        print(
            f"Watch the review: {args.ui_url}/?s={session_id}",
            file=sys.stderr,
            flush=True,
        )
        final = None
        event = None
        async with client.stream(
            "POST",
            "/api/chat",
            json={
                "session_id": session_id,
                "message": {
                    "type": "review",
                    "payload": request.model_dump(mode="json"),
                },
                "expected_turn": 1,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if event == "agent_message_sent":
                        message = ReviewMessage.model_validate_json(data["body"])
                        direction = (
                            "Subagent → Parent"
                            if data["recipient"] == "parent"
                            else "Parent → Subagent"
                        )
                        print(
                            f"[{direction}] {message.type}: {message.text.splitlines()[0]}",
                            file=sys.stderr,
                            flush=True,
                        )
                    elif event == "reply" and isinstance(data.get("output"), dict):
                        if (
                            "reports" in data["output"]
                            and "review_id" in data["output"]
                        ):
                            final = data["output"]
                    elif event == "error":
                        raise RuntimeError(data.get("message", "Review stream failed"))
        if final is None:
            raise RuntimeError(
                "The review stream ended without a coordinator report; check worker logs and the Temporal UI."
            )
        print(final["text"])
        if not final["complete"]:
            raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
