"""Publishing a shareable report: a callback tool the client renders and "hosts".

``publish_report`` hands the client a report, title, summary and sections, whose markdown can
embed charts the agent drew earlier with ``[chart:<chart_id>]`` lines. The studio UI (``ui/``)
renders it as a card in the conversation and opens it as a page; it answers with the report's
id and its public link. Publishing is simulated: the studio keeps the report in the page, and
the link only opens there. The demo treats it as real.

Unlike the charts, publishing is not pre-approved: the tool policy decides, and it refuses a
public report once the session has read restricted data (``policies/bank.dw``).

No ``from __future__ import annotations`` here, as in the other examples' Temporal-facing
modules (stringized annotations trip the pydantic converter).
"""

from pydantic import BaseModel, Field

from temporal_agent_harness.harness import agent


class ReportSection(BaseModel):
    heading: str
    body: str = Field(
        description="Markdown. A line `[chart:<chart_id>]` embeds a chart drawn earlier."
    )


class PublishedReport(BaseModel):
    report_id: str
    url: str = Field(description="The report's public link, to give the user.")


@agent.callback_tool_defn()
async def publish_report(
    title: str, summary: str, sections: list[ReportSection]
) -> PublishedReport:
    """Publish a report anyone with the link can read: a title, a short summary, and sections
    of markdown, which can embed charts you drew earlier with `[chart:<chart_id>]` lines. Use
    it when the user asks to share or publish findings. Returns the report's public link; give
    it to the user. Whether a report may be published is the tool policy's call."""
    ...
