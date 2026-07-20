"""Diagnostic models for OpenCode's LSP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


SeverityLevel = Literal[1, 2, 3, 4]


class DiagnosticPosition(BaseModel):
    """Position in a text document."""

    line: int
    character: int


class DiagnosticRange(BaseModel):
    """Range in a text document."""

    start: DiagnosticPosition
    end: DiagnosticPosition

    @classmethod
    def create(
        cls,
        start_line: int,
        start_char: int,
        end_line: int,
        end_char: int,
    ) -> DiagnosticRange:
        return cls(
            start=DiagnosticPosition(line=start_line, character=start_char),
            end=DiagnosticPosition(line=end_line, character=end_char),
        )


class Diagnostic(BaseModel):
    """LSP Diagnostic matching vscode-languageserver-types format."""

    range: DiagnosticRange
    message: str
    severity: SeverityLevel | None = None
    """1=Error, 2=Warning, 3=Info, 4=Hint"""
    code: str | int | None = None
    source: str | None = None


class FormatterStatus(BaseModel):
    """Formatter status information."""

    name: str
    """Formatter name."""

    extensions: list[str]
    """File extensions this formatter handles."""

    enabled: bool
    """Whether this formatter is currently enabled."""
