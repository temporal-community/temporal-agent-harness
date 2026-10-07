# ABOUTME: Unit tests for reading and writing OKF bundles: frontmatter parsing that never rejects
# a file, link extraction (relative, bundle-absolute, anchors, URLs, code fences), the bundle
# walk with its limits, and rendering a concept's text.

from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath

import pytest
import yaml

from temporal_agent_harness.harness.code_mode import okf
from temporal_agent_harness.harness.code_mode.okf import (
    build_okf_graph,
    link_targets,
    parse_concept,
    render_concept,
)
from temporal_agent_harness.harness.code_mode.vfs import FileStat

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


class Bundle:
    """An in-process filesystem with the ActivityFileSystem read methods, counting reads."""

    def __init__(self, files: dict[str, str | bytes]) -> None:
        self.files = {k: v.encode() if isinstance(v, str) else v for k, v in files.items()}
        self.reads: list[tuple[str, int | None]] = []

    def _dirs(self) -> set[str]:
        out = {"."}
        for key in self.files:
            parent = PurePosixPath(key).parent
            while str(parent) != ".":
                out.add(str(parent))
                parent = parent.parent
        return out

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        if str(path) in self._dirs():
            return FileStat(is_dir=True)
        data = self.files.get(str(path))
        return None if data is None else FileStat(is_dir=False, size=len(data))

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        self.reads.append((str(path), length))
        data = self.files.get(str(path))
        if data is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        return data[offset : None if length is None else offset + length]

    async def list(self, path: PurePosixPath) -> list[str]:
        prefix = "" if str(path) == "." else f"{path}/"
        return sorted(
            {k[len(prefix) :].split("/")[0] for k in self.files if k.startswith(prefix)}
        )


def concept(fields: str, body: str = "") -> str:
    return f"---\n{fields}\n---\n\n{body}"


def parse(text: str, path: str = "a.md"):
    return parse_concept(path.removesuffix(".md"), path, text, size=len(text), now=NOW)[0]


# ---------------------------------------------------------------- one concept


def test_a_concept_reads_its_frontmatter():
    found = parse(
        concept(
            "type: Preference\ntitle: Coffee\ndescription: A flat white.\n"
            "tags: [drinks, morning]\nstatus: draft"
        ),
        "prefs/coffee.md",
    )
    assert (found.id, found.path, found.type, found.title) == (
        "prefs/coffee",
        "prefs/coffee.md",
        "Preference",
        "Coffee",
    )
    assert (found.description, found.tags, found.status, found.problem) == (
        "A flat white.",
        ["drinks", "morning"],
        "draft",
        None,
    )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("# Just a heading\n", "no frontmatter"),
        ("---\ntype: Fact\n", "never closed"),
        ("---\ntype: [unclosed\n---\n", "not valid YAML"),
        ("---\n- a\n- b\n---\n", "not a mapping"),
        ("---\n---\n", "empty"),
        ("---\nnote: no type here\n---\n", "no `type`"),
    ],
)
def test_a_broken_concept_is_still_a_concept(text, problem):
    found = parse(text, "notes/odd.md")
    assert found.type == "Unknown"
    assert found.title == "odd"
    assert problem in (found.problem or "")


def test_a_concept_with_a_type_but_other_problems_keeps_its_type():
    assert parse(concept("type: Fact\ntags: one, two")).tags == ["one", "two"]


def test_tags_are_listed_once_each():
    assert parse(concept("type: Fact\ntags: [a, b, a, '', b]")).tags == ["a", "b"]
    assert parse(concept("type: Fact\ntags: a, a")).tags == ["a"]


@pytest.mark.parametrize(
    ("verified", "tier"),
    [
        ("", "unverified"),
        ("verified: {by: 'human:user', at: 2026-01-01T00:00:00Z}", "human-reviewed"),
        ("verified:\n  - {by: process:nightly, at: 2026-01-01T00:00:00Z}", "machine-confirmed"),
        (
            "verified:\n  - {by: process:nightly}\n  - {by: 'human:ana'}",
            "human-reviewed",
        ),
    ],
)
def test_trust_tier_comes_from_verified(verified, tier):
    assert parse(concept(f"type: Fact\n{verified}")).trust_tier == tier


@pytest.mark.parametrize(
    ("stale_after", "stale"),
    [
        ("2026-10-01T00:00:00Z", True),
        ("2026-10-06T00:00:00Z", True),
        ("2026-12-31", False),
        ("'2027-01-01T00:00:00+00:00'", False),
        ("not a date", False),
    ],
)
def test_staleness_compares_stale_after_with_now(stale_after, stale):
    assert parse(concept(f"type: Plan\nstale_after: {stale_after}")).stale is stale


# ---------------------------------------------------------------- links


def test_links_resolve_relative_to_the_file_or_the_bundle_root():
    body = (
        "See [Ana](../people/ana.md), [the trip](denver.md#dates) and [x](/projects/x.md).\n"
        "Again [Ana](../people/ana.md) and [pic](photo.png) and [web](https://e.com/a.md).\n"
        "Also [mail](mailto:a@b.md), [up and out](../../outside.md), [index](index.md).\n"
        "And [me](trip.md) and [spaced](<my%20notes.md>).\n"
    )
    assert link_targets(body, "trips/trip.md") == [
        "people/ana",
        "trips/denver",
        "projects/x",
        "trips/my notes",
    ]


def test_links_inside_code_fences_are_not_links():
    body = "```\n[no](a.md)\n```\n[yes](b.md)\n~~~python\n[no](c.md)\n```\nstill fenced\n~~~\n"
    assert link_targets(body, "x.md") == ["b"]


# ---------------------------------------------------------------- the walk


async def test_a_walk_lists_every_concept_and_the_links_between_them():
    fs = Bundle(
        {
            "index.md": "# Memory\n* [Coffee](prefs/coffee.md)\n",
            "log.md": "# Log\n",
            "prefs/coffee.md": concept("type: Preference", "Goes with [Ana](../people/ana.md)."),
            "people/ana.md": concept("type: Person", "Likes [coffee](../prefs/coffee.md), "
                                     "[tea](../prefs/tea.md)."),
            "people/index.md": "* [Ana](ana.md)\n",
            "scratch.txt": "not markdown",
            "broken.md": "no frontmatter at all",
        }
    )
    graph = await build_okf_graph(fs, prefix=PurePosixPath("."), links=True, now=NOW)
    assert [c.id for c in graph.concepts] == ["broken", "people/ana", "prefs/coffee"]
    assert graph.concepts[0].type == "Unknown" and graph.concepts[0].problem
    assert [(l.source, l.target, l.dangling) for l in graph.links] == [
        ("people/ana", "prefs/coffee", False),
        ("people/ana", "prefs/tea", True),
        ("prefs/coffee", "people/ana", False),
    ]
    assert graph.truncated is False
    assert graph.walked_at == NOW.timestamp()


async def test_a_walk_can_start_below_the_root():
    fs = Bundle({"a.md": concept("type: A"), "sub/b.md": concept("type: B")})
    graph = await build_okf_graph(fs, prefix=PurePosixPath("sub"), links=False, now=NOW)
    assert [c.id for c in graph.concepts] == ["sub/b"]


async def test_without_links_only_each_files_head_is_read():
    big = concept("type: Fact", "x" * 50_000)
    long_frontmatter = "---\ntype: Fact\nnote: " + "y" * 10_000 + "\n---\nbody\n"
    fs = Bundle({"big.md": big, "long.md": long_frontmatter})
    graph = await build_okf_graph(fs, prefix=PurePosixPath("."), links=False, now=NOW)
    assert [c.type for c in graph.concepts] == ["Fact", "Fact"]
    assert graph.links == []
    # One head read for big.md; long.md's frontmatter outran the head, so it was read again.
    assert fs.reads == [
        ("big.md", okf.OKF_HEAD_BYTES),
        ("long.md", okf.OKF_HEAD_BYTES),
        ("long.md", okf.MAX_OKF_FILE_BYTES),
    ]


async def test_a_walk_stops_at_its_limits(monkeypatch):
    monkeypatch.setattr(okf, "MAX_OKF_CONCEPTS", 2)
    fs = Bundle({f"c{i}.md": concept("type: Fact") for i in range(4)})
    graph = await build_okf_graph(fs, prefix=PurePosixPath("."), links=True, now=NOW)
    assert [c.id for c in graph.concepts] == ["c0", "c1"]
    assert graph.truncated

    monkeypatch.setattr(okf, "MAX_OKF_CONCEPTS", 100)
    monkeypatch.setattr(okf, "MAX_OKF_TOTAL_BYTES", len(concept("type: Fact")) + 1)
    graph = await build_okf_graph(fs, prefix=PurePosixPath("."), links=True, now=NOW)
    assert len(graph.concepts) == 2  # the second read got the last byte of the budget
    assert graph.truncated


async def test_an_unreadable_concept_is_listed_with_its_problem():
    class Flaky(Bundle):
        async def read(self, path, offset=0, length=None):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))

    graph = await build_okf_graph(
        Flaky({"a.md": "x"}), prefix=PurePosixPath("."), links=True, now=NOW
    )
    assert graph.concepts[0].problem == "unreadable: Permission denied"


async def test_what_cant_be_listed_or_stated_is_skipped_and_the_walk_goes_on():
    class Patchy(Bundle):
        async def list(self, path):
            if str(path) == "locked":
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
            return await super().list(path)

        async def stat(self, path):
            if str(path) == "vanished.md":
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
            return await super().stat(path)

    fs = Patchy(
        {
            "a.md": concept("type: Fact"),
            "locked/b.md": concept("type: Fact"),
            "vanished.md": concept("type: Fact"),
            "z.md": concept("type: Fact"),
        }
    )
    graph = await build_okf_graph(fs, prefix=PurePosixPath("."), links=True, now=NOW)
    assert [c.id for c in graph.concepts] == ["a", "z"]
    assert not graph.truncated
    missing = await build_okf_graph(
        Patchy({}), prefix=PurePosixPath("locked"), links=False, now=NOW
    )
    assert missing.concepts == []


# ---------------------------------------------------------------- writing


def test_render_writes_frontmatter_and_body_and_records_who_generated_it():
    text = render_concept(
        {"type": "Plan", "title": "Denver", "tags": ["travel"], "stale_after": "2026-10-15"},
        "\n\nFlying on the 14th.",
        generated_by="CodeModeMemoryAgent",
        at=datetime(2026, 10, 6, 12, 30, 5, 123, tzinfo=timezone.utc),
    )
    assert text.startswith("---\ntype: Plan\ntitle: Denver\n")
    assert text.endswith("---\n\nFlying on the 14th.\n")
    found = parse(text, "trips/denver.md")
    assert (found.type, found.title, found.problem) == ("Plan", "Denver", None)
    fields = yaml.safe_load(text.split("---")[1])
    assert fields["generated"] == {"by": "CodeModeMemoryAgent", "at": "2026-10-06T12:30:05Z"}


def test_render_keeps_a_given_generated_and_renders_whatever_it_is_given():
    text = render_concept(
        {"generated": {"by": "human:ana"}, "note": "no type"},
        "body",
        generated_by="Agent",
        at=NOW,
    )
    assert yaml.safe_load(text.split("---")[1]) == {"generated": {"by": "human:ana"}, "note": "no type"}
    assert parse(text).problem == "frontmatter has no `type`"


def test_render_stamps_a_verified_entry_that_has_no_time():
    text = render_concept(
        {"type": "Fact", "verified": [{"by": "human:user"}, {"by": "process:x", "at": "then"}]},
        "",
        generated_by="Agent",
        at=NOW,
    )
    fields = yaml.safe_load(text.split("---")[1])
    assert fields["verified"] == [
        {"by": "human:user", "at": "2026-10-06T00:00:00Z"},
        {"by": "process:x", "at": "then"},
    ]
    assert parse(text).trust_tier == "human-reviewed"
