"""Code review input, reviewer reports, and message envelopes."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Angle = Literal["correctness", "security"]


class ReviewRequest(BaseModel):
    """Provider-independent review input; a GitHub adapter can produce this later."""

    diff: str = Field(min_length=1, max_length=120_000)
    instructions: str = Field(default="", max_length=8_000)

    @model_validator(mode="after")
    def require_unified_diff(self) -> "ReviewRequest":
        if not re.search(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", self.diff, re.M):
            raise ValueError(
                "Provide a unified text diff with file headers and @@ hunks"
            )
        if not re.search(r"^\+\+\+ ", self.diff, re.M):
            raise ValueError("The diff must include +++ file headers")
        return self


class Finding(BaseModel):
    """One actionable issue supported by the supplied diff."""

    model_config = ConfigDict(extra="forbid")
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: int = Field(ge=1)
    side: Literal["old", "new"]
    title: str
    explanation: str
    suggested_fix: str


class ReviewerDraft(BaseModel):
    """Structured model output, before the workflow stamps its identity."""

    model_config = ConfigDict(extra="forbid")
    summary: str
    findings: list[Finding]
    limitations: list[str]


class ReviewerAssignment(ReviewRequest):
    """A correlated reviewer turn carrying a diff and the selected model name."""

    review_id: str
    model: str
    revision: int = Field(default=1, ge=1)


class ReviewerReport(BaseModel):
    """A specialist report delivered to the coordinator by Signal."""

    review_id: str
    revision: int = 1
    angle: Angle
    harness: str
    status: Literal["completed", "failed"]
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ReviewMessage(BaseModel):
    """Structured data carried by the existing string-bodied Signal message."""

    type: Literal["review_guidance", "review_progress", "review_findings"]
    review_id: str
    text: str
    report: ReviewerReport | None = None


class ReviewAcknowledgement(BaseModel):
    """Turn replies acknowledge delivery; findings travel only through Signals."""

    review_id: str
    revision: int
    report_delivered: bool


class ModelNames(BaseModel):
    """Non-secret gateway model aliases selected by the worker."""

    coordinator: str
    correctness: str
    security: str


class ReviewResult(BaseModel):
    """The consolidated review plus the exact specialist reports received."""

    review_id: str
    text: str
    reports: list[ReviewerReport]
    findings: list[Finding]
    complete: bool


def diff_locations(diff: str) -> set[tuple[str, str, int]]:
    """Locations actually present in text hunks, including old-side deletions."""
    locations: set[tuple[str, str, int]] = set()
    old_file = new_file = ""
    old_line = new_line = 0
    old_remaining = new_remaining = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif not in_hunk and line.startswith("--- "):
            old_file = line[4:].split("\t", 1)[0].removeprefix("a/")
        elif not in_hunk and line.startswith("+++ "):
            new_file = line[4:].split("\t", 1)[0].removeprefix("b/")
        elif match := re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line):
            old_line, new_line = int(match[1]), int(match[3])
            old_remaining = int(match[2]) if match[2] is not None else 1
            new_remaining = int(match[4]) if match[4] is not None else 1
            in_hunk = old_remaining > 0 or new_remaining > 0
        elif in_hunk:
            if line.startswith(" "):
                locations.add((old_file, "old", old_line))
                locations.add((new_file, "new", new_line))
                old_line += 1
                new_line += 1
                old_remaining -= 1
                new_remaining -= 1
            elif line.startswith("-"):
                locations.add((old_file, "old", old_line))
                old_line += 1
                old_remaining -= 1
            elif line.startswith("+"):
                locations.add((new_file, "new", new_line))
                new_line += 1
                new_remaining -= 1
            elif not line.startswith("\\"):
                in_hunk = False
            if old_remaining <= 0 and new_remaining <= 0:
                in_hunk = False
    return locations


def make_report(
    assignment: ReviewerAssignment, angle: Angle, harness: str, draft: ReviewerDraft
) -> ReviewerReport:
    """Keep supported locations and explicitly report discarded citations."""
    locations = diff_locations(assignment.diff)
    supported = [f for f in draft.findings if (f.file, f.side, f.line) in locations]
    limitations = list(draft.limitations)
    discarded = len(draft.findings) - len(supported)
    if discarded:
        limitations.append(
            f"Discarded {discarded} finding(s) citing locations absent from the diff."
        )
    return ReviewerReport(
        review_id=assignment.review_id,
        revision=assignment.revision,
        angle=angle,
        harness=harness,
        status="completed",
        summary=draft.summary,
        findings=supported,
        limitations=limitations,
    )


def report_text(report: ReviewerReport) -> str:
    lines = [
        f"### {report.angle.title()} review · {report.harness}",
        f"Status: {report.status}",
        "",
        report.summary,
    ]
    for finding in report.findings:
        lines.extend(
            [
                "",
                f"**{finding.severity.upper()}: {finding.title}**",
                f"`{finding.file}:{finding.line}` ({finding.side} side)",
                finding.explanation,
                f"Suggested fix: {finding.suggested_fix}",
            ]
        )
    if report.status == "completed" and not report.findings:
        lines.extend(["", "No actionable findings reported."])
    if report.limitations:
        lines.extend(
            ["", "Limitations:", *[f"- {item}" for item in report.limitations]]
        )
    return "\n".join(lines)


def consolidate(reports: list[ReviewerReport]) -> list[Finding]:
    """Deduplicate matching location/title findings; preserve the highest severity."""
    severity = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings: dict[tuple[str, str, int, str], Finding] = {}
    for report in reports:
        for finding in report.findings:
            key = (
                finding.file,
                finding.side,
                finding.line,
                finding.title.casefold().strip(),
            )
            existing = findings.get(key)
            if (
                existing is None
                or severity[finding.severity] < severity[existing.severity]
            ):
                findings[key] = finding
    return sorted(
        findings.values(), key=lambda f: (severity[f.severity], f.file, f.line)
    )
