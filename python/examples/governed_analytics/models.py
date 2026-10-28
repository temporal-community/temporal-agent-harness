"""What the analytics tools return, and the facts about a query that the policy sees.

Plain pydantic models, safe to import from workflow code.
"""

from typing import Literal

from pydantic import BaseModel, Field, JsonValue

# The tables a person can open. A choice rather than free text, so a grant always names the
# table exactly as the policy's read_table check does.
RestrictedTable = Literal["risk.loan_applications", "risk.credit_scores"]

# The answer that opens a restricted table: the policy's risk-access-requested rule matches it
# in a recorded request_access result.
GRANT_FOR_THIS_SESSION = "Grant for this session"


class SqlFacts(BaseModel):
    """What a query does, as the tool policy reads it: ``context.input.arg0`` of a SQL tool's
    call. ``analyze_sql`` reads these off DuckDB's own parser and planner, so they describe the
    query DuckDB would run, whatever CTEs, aliases or subqueries hide its tables.
    """

    statement: str = Field(
        description='"SELECT" for a single SELECT; another statement\'s type, "MULTIPLE" for '
        'more than one statement, or "INVALID" when the text does not parse.'
    )
    tables: list[str] = Field(
        default_factory=list,
        description='Every table the query reads, as "<database>.<table>".',
    )
    table_functions: list[str] = Field(
        default_factory=list,
        description="Table functions the query calls, such as read_csv or duckdb_tables.",
    )
    error: str | None = Field(
        default=None, description="Why the query does not parse or plan, when it doesn't."
    )


class TableRead(BaseModel):
    """One ``read_table`` check: a table a SQL tool's query reads, and the tool reading it."""

    table: str
    tool: str


class TableInfo(BaseModel):
    database: str
    table: str
    rows: int


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool


class TableDescription(BaseModel):
    table: str
    columns: list[ColumnInfo]


class DataResult(BaseModel):
    """What every SQL tool's result says about the data it came from. Results are recorded
    with the tool policy, so the policy can look back on what the session actually read."""

    tables_read: list[str] = Field(
        default_factory=list, description='The tables the query read, as "<database>.<table>".'
    )
    restricted_data: bool = Field(
        default=False, description="True when any of them is outside the analytics database."
    )


class QueryResult(DataResult):
    columns: list[str]
    rows: list[list[JsonValue]]
    truncated: bool = Field(description="True when the query returned more rows than shown.")


class SummaryStats(DataResult):
    column: str
    count: int
    nulls: int
    mean: float | None
    std: float | None
    min: float | None
    p25: float | None
    median: float | None
    p75: float | None
    max: float | None


class HistogramBin(BaseModel):
    lower: float
    upper: float
    count: int


class Distribution(DataResult):
    column: str
    count: int
    bins: list[HistogramBin]


class Correlation(DataResult):
    columns: list[str]
    matrix: list[list[float | None]] = Field(
        description="Pearson correlation of each pair of columns, in the order of `columns`."
    )


class GroupStats(BaseModel):
    group: JsonValue
    count: int
    mean: float | None
    std: float | None


class GroupComparison(DataResult):
    group_column: str
    value_column: str
    groups: list[GroupStats]
    welch_t: float | None = Field(
        default=None,
        description="Welch's t statistic for the first two groups, when there are exactly two. "
        "|t| above about 2 suggests a real difference.",
    )
    welch_df: float | None = None


class Outliers(DataResult):
    column: str
    method: str
    lower_bound: float | None
    upper_bound: float | None
    columns: list[str]
    rows: list[list[JsonValue]]
    truncated: bool


class ReportSection(BaseModel):
    heading: str
    body: str = Field(
        description="Markdown. A line `[chart:<chart_id>]` embeds a chart drawn earlier."
    )


class PublishedReport(BaseModel):
    report_id: str
    url: str = Field(description="The report's public link, to give the user.")
