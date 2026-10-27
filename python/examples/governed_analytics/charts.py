"""Charts the agent draws for the user: callback tools a client renders natively.

A script calls ``render_bar_chart`` and the rest like any other host function, with data it
has already read. Each is a callback tool, so the workflow hands the call to the attached
client, which draws the chart and answers with a ``chart_id``. The agent then places the chart
in its answer by writing ``[chart:<chart_id>]`` on a line of its own, and the client renders it
there.

The studio UI (``ui/``) renders and answers these on its own. The generic console shows them
as forms asking for a ``chart_id``; any text answers them.

They draw only what the agent was allowed to read, so they are pre-approved rather than put to
the tool policy.

No ``from __future__ import annotations`` here, as in the other examples' Temporal-facing
modules (stringized annotations trip the pydantic converter).
"""

from pydantic import BaseModel, Field

from temporal_agent_harness.harness import agent


class Series(BaseModel):
    """One named series of values, one per category or x value."""

    name: str
    values: list[float]


class Point(BaseModel):
    x: float
    y: float
    label: str | None = Field(default=None, description="Shown when the point is hovered.")


class ChartRendered(BaseModel):
    chart_id: str = Field(
        description="Put `[chart:<chart_id>]` on its own line in your answer to show the chart."
    )


@agent.callback_tool_defn()
async def render_bar_chart(
    title: str, categories: list[str], series: list[Series], y_label: str | None = None
) -> ChartRendered:
    """Draw a bar chart for the user: one group of bars per category, one bar per series.
    Returns the chart's id; write `[chart:<chart_id>]` on its own line in your answer where
    the chart belongs."""
    ...


@agent.callback_tool_defn()
async def render_line_chart(
    title: str, x_values: list[str], series: list[Series], y_label: str | None = None
) -> ChartRendered:
    """Draw a line chart for the user: one line per series over `x_values` (dates, quarters,
    ...). Returns the chart's id; write `[chart:<chart_id>]` on its own line in your answer
    where the chart belongs."""
    ...


@agent.callback_tool_defn()
async def render_pie_chart(title: str, labels: list[str], values: list[float]) -> ChartRendered:
    """Draw a pie chart for the user: one slice per label. Returns the chart's id; write
    `[chart:<chart_id>]` on its own line in your answer where the chart belongs."""
    ...


@agent.callback_tool_defn()
async def render_scatter_chart(
    title: str, points: list[Point], x_label: str, y_label: str
) -> ChartRendered:
    """Draw a scatter plot for the user. Returns the chart's id; write `[chart:<chart_id>]` on
    its own line in your answer where the chart belongs."""
    ...


@agent.callback_tool_defn()
async def render_heatmap_chart(
    title: str,
    row_labels: list[str],
    column_labels: list[str],
    values: list[list[float]],
    value_label: str | None = None,
) -> ChartRendered:
    """Draw a heatmap for the user: a grid of cells, one row of `values` per row label and one
    value per column label, each cell shaded by its value (two hues when values run both sides
    of zero). A `correlate` result draws as one: its `columns` as both labels, its `matrix` as
    `values`. Returns the chart's id; write `[chart:<chart_id>]` on its own line in your answer
    where the chart belongs."""
    ...


CHART_TOOLS = [
    render_bar_chart,
    render_line_chart,
    render_pie_chart,
    render_scatter_chart,
    render_heatmap_chart,
]
