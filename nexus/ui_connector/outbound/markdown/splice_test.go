package markdown

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func mrkdwnLink(url, title string) string {
	return "<" + url + "|" + title + ">"
}

func TestSplice(t *testing.T) {
	cases := []struct {
		name string
		text string
		cs   []protocol.Citation
		link func(url, title string) string
		want string
	}{
		{"no citations", "hello world", nil, mrkdwnLink, "hello world"},
		{
			// The visible marker is always the bare numeric label, matching the web UI
			// (ui/src/lib/components/chat/MarkdownMessage.svelte's markerForCitation) -
			// Title is not rendered inline there either, only used for a hover tooltip
			// Slack/Teams have no equivalent for.
			"marker inserted at EndIndex, title is not rendered inline",
			"hello world",
			[]protocol.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}},
			mrkdwnLink,
			"hello <https://example.com/doc|[1]> world",
		},
		{
			"commonmark marker inserted at EndIndex",
			"hello world",
			[]protocol.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}},
			CommonMarkLink,
			"hello [[1]](https://example.com/doc) world",
		},
		{
			"empty URL falls back to # instead of an empty href",
			"hello world",
			[]protocol.Citation{{URL: "", Title: "notes.txt", EndIndex: 5}},
			mrkdwnLink,
			"hello <#|[1]> world",
		},
		{
			"negative EndIndex appends at the end",
			"hello world",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: -1}},
			mrkdwnLink,
			"hello world <https://example.com/doc|[1]>",
		},
		{
			"EndIndex past the end of the text clamps to the end",
			"hello",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: 999}},
			mrkdwnLink,
			"hello <https://example.com/doc|[1]>",
		},
		{
			"citations at the same index coalesce adjacent, numbered by array order",
			"hello world",
			[]protocol.Citation{
				{URL: "https://example.com/a", EndIndex: 5},
				{URL: "https://example.com/b", EndIndex: 5},
			},
			mrkdwnLink,
			"hello <https://example.com/a|[1]> <https://example.com/b|[2]> world",
		},
		{
			"repeated source is not deduped - gets a new number each time",
			"a b",
			[]protocol.Citation{
				{URL: "https://example.com/doc", EndIndex: 1},
				{URL: "https://example.com/doc", EndIndex: 3},
			},
			mrkdwnLink,
			"a <https://example.com/doc|[1]> b <https://example.com/doc|[2]>",
		},
		{
			// Reproduces the collision bug: the reply text already contains a
			// citation-shaped substring "[1]" before any real citation is spliced in.
			// The internal sentinel marker must not be confused with it.
			"pre-existing bracket-number text is not mistaken for the real marker",
			"See item [1] in the list. More text here.",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: len("See item [1] in the list. More text here.")}},
			mrkdwnLink,
			"See item [1] in the list. More text here. <https://example.com/doc|[1]>",
		},
		{
			// EndIndex 14 lands between "st" and "ep" of "step" (index 12-15). The
			// marker must snap past the rest of the word rather than splitting it.
			"EndIndex mid-word snaps to the end of the word",
			"downscaling step regardless",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: 14}},
			mrkdwnLink,
			"downscaling step <https://example.com/doc|[1]> regardless",
		},
		{
			// EndIndex 6 lands exactly on the existing space between "these" and
			// "operations" - the result must have exactly one space on each side of
			// the marker, not the original spacing preserved verbatim.
			"marker gets exactly one space on each side regardless of existing spacing",
			"these operations",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: 6}},
			mrkdwnLink,
			"these <https://example.com/doc|[1]> operations",
		},
		{
			// EndIndex 21 lands right after "Commands" in the "### Step 1: Commands"
			// heading. Slack/Teams don't parse links inside headings, so the marker
			// must move past the heading into the body text.
			"EndIndex inside a heading moves past it into body text, trailing that line",
			"### Step 1: Commands\nDo the thing.",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: len("### Step 1: Commands")}},
			mrkdwnLink,
			"### Step 1: Commands\n Do the thing. <https://example.com/doc|[1]>",
		},
		{
			// Reproduces the real bug: a citation's EndIndex lands right at the start of
			// list item "2.", which used to render as "[3]2. Durable Sleep..." and broke
			// Slack's ordered-list parsing. It must move to the end of item 2 instead.
			"EndIndex at the start of a list item moves to the end of that item",
			"1. Scale Up.\n2. Durable Sleep.\n3. Scale Down.",
			[]protocol.Citation{{URL: "https://example.com/doc", EndIndex: len("1. Scale Up.\n")}},
			mrkdwnLink,
			// A leading space before "2." is expected and harmless - CommonMark allows
			// up to 3 spaces of indentation before a list marker.
			"1. Scale Up.\n 2. Durable Sleep. <https://example.com/doc|[1]>\n3. Scale Down.",
		},
		{
			// EndIndex lands right before the "*" that closes an italicized span. A
			// trailing space there (e.g. "text [1] *") stops the "*" from closing the
			// span at all - CommonMark requires no whitespace right before it.
			"EndIndex right before a closing italics marker doesn't break the italics",
			"*(For example, this allocates capacity).*",
			[]protocol.Citation{
				{URL: "https://example.com/a", EndIndex: len("*(For example, this allocates capacity).")},
				{URL: "https://example.com/b", EndIndex: len("*(For example, this allocates capacity).")},
			},
			mrkdwnLink,
			"*(For example, this allocates capacity). <https://example.com/a|[1]> <https://example.com/b|[2]>*",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, Splice(tc.text, tc.cs, tc.link))
		})
	}
}

func TestSpliceCitationsOutsideCode(t *testing.T) {
	cases := []struct {
		name, text, prefix, want string
	}{
		{"inside inline code", "A customer saw `WorkflowTaskScheduled/Started/Completed` events.", "A customer saw `WorkflowTaskScheduled/Started", "A customer saw `WorkflowTaskScheduled/Started/Completed` LINK events."},
		{"before closing backtick", "Use `x` here.", "Use `x", "Use `x` LINK here."},
		{"before opening backtick", "Use `x` here.", "Use ", "Use LINK `x` here."},
		{"after closing backtick", "Use `x` here.", "Use `x`", "Use `x` LINK here."},
		{"multiple backticks", "Use ``a`b`` here.", "Use ``a", "Use ``a`b`` LINK here."},
		{"triple backticks inline", "```x``` here.", "```x", "```x``` LINK here."},
		{"unicode offset", "Use `éé` here.", "Use `é", "Use `éé` LINK here."},
		{"punctuation after code", "Use `x`. Next.", "Use `x", "Use `x`. LINK Next."},
		{"escaped backticks", "Use \\`x\\` here.", "Use \\`x", "Use \\`x LINK \\` here."},
		{"inside fenced code", "Before.\n```go\nx := 1\n```\nAfter.", "Before.\n```go\nx", "Before.\n```go\nx := 1\n```\n\nLINK\n\nAfter."},
		{"fence at end", "```\nx\n```", "```\nx", "```\nx\n```\n\nLINK"},
		{"tilde fence", "~~~\nx\n~~~", "~~~\nx", "~~~\nx\n~~~\n\nLINK"},
		{"longer closing fence", "````\nx\n`````", "````\nx", "````\nx\n`````\n\nLINK"},
		{"shorter fence does not close", "````\nx\n```\ny\n````", "````\nx", "````\nx\n```\ny\n````\n\nLINK"},
		{"unclosed fence", "Before.\n```go\nx := 1", "Before.\n```go\nx", "Before.\n\nLINK\n\n```go\nx := 1"},
		{"unclosed fence at start", "```\nx", "```\nx", "LINK\n\n```\nx"},
		{"after fence remains outside", "```\nx\n```\nAfter.", "```\nx\n```\nAfter.", "```\nx\n```\nAfter. LINK"},
	}
	for _, format := range []struct {
		name string
		link func(string, string) string
	}{
		{"commonmark", CommonMarkLink},
		{"mrkdwn", mrkdwnLink},
	} {
		t.Run(format.name, func(t *testing.T) {
			for _, tc := range cases {
				t.Run(tc.name, func(t *testing.T) {
					cs := []protocol.Citation{{URL: "https://example.com", EndIndex: len([]rune(tc.prefix))}}
					want := strings.ReplaceAll(tc.want, "LINK", format.link(cs[0].URL, "[1]"))
					assert.Equal(t, want, Splice(tc.text, cs, format.link))
				})
			}
		})
	}
}

func TestSpliceCodeCitationsKeepNumbering(t *testing.T) {
	text := "Use `first` and `second`."
	cs := []protocol.Citation{
		{URL: "https://a", EndIndex: len("Use `first` and `sec")},
		{URL: "https://b", EndIndex: len("Use `fir")},
		{URL: "https://c", EndIndex: len("Use `first")},
	}
	assert.Equal(t, "Use `first` <https://b|[2]> <https://c|[3]> and `second`. <https://a|[1]>", Splice(text, cs, mrkdwnLink))

	cs[0].EndIndex, cs[1].EndIndex, cs[2].EndIndex = 5, 6, 7
	assert.Equal(t, "```\nabc\n```\n\n<https://a|[1]> <https://b|[2]> <https://c|[3]>", Splice("```\nabc\n```", cs, mrkdwnLink))

	cs = []protocol.Citation{{URL: "https://a", EndIndex: -1}}
	assert.Equal(t, "Before.\n\n<https://a|[1]>\n\n```\nx", Splice("Before.\n```\nx", cs, mrkdwnLink))
}

func TestCitationInsertionIndex(t *testing.T) {
	cases := []struct {
		name string
		text string
		idx  int
		want int
	}{
		{"mid-word snaps to end of word", "step", 2, 4},
		{"already at a word boundary (space) is unchanged", "a b", 1, 1},
		{"at the very start is unchanged", "step", 0, 0},
		{"at the very end is unchanged", "step", 4, 4},
		{"skips trailing punctuation", "(text)", 5, 6},
		{"skips multiple trailing punctuation", "text.).", 4, 7},
		{"no punctuation to skip", "text next", 4, 4},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, citationInsertionIndex([]rune(tc.text), tc.idx))
		})
	}
}

func TestAvoidHeadingLine(t *testing.T) {
	cases := []struct {
		name string
		text string
		idx  int
		want int
	}{
		{
			"index inside a heading moves to the start of the next line",
			"### Step 1: Commands\nbody text",
			len("### Step 1: Commands"),
			len("### Step 1: Commands\n"),
		},
		{
			"index inside a heading skips a blank line after it",
			"### Step 1: Commands\n\nbody text",
			len("### Step 1: Commands"),
			len("### Step 1: Commands\n\n"),
		},
		{
			"index in body text is unchanged",
			"body text here",
			5,
			5,
		},
		{
			"index inside a heading with no following line stays at the end",
			"### Only a heading",
			len("### Only a heading"),
			len("### Only a heading"),
		},
		{
			"consecutive headings both get skipped",
			"### One\n#### Two\nbody",
			3,
			len("### One\n#### Two\n"),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, avoidHeadingLine([]rune(tc.text), tc.idx))
		})
	}
}

func TestIsHeadingLine(t *testing.T) {
	cases := []struct {
		line string
		want bool
	}{
		{"# Heading", true},
		{"###### Heading", true},
		{"####### too many hashes is not a heading", false},
		{"#NoSpace", false},
		{"not a heading", false},
		{"", false},
	}
	for _, tc := range cases {
		t.Run(tc.line, func(t *testing.T) {
			assert.Equal(t, tc.want, isHeadingLine([]rune(tc.line)))
		})
	}
}

func TestClosesInlineMarkup(t *testing.T) {
	cases := []struct {
		r    rune
		want bool
	}{
		{'*', true},
		{'_', true},
		{'~', true},
		{'`', true},
		{' ', false},
		{'a', false},
	}
	for _, tc := range cases {
		t.Run(string(tc.r), func(t *testing.T) {
			assert.Equal(t, tc.want, closesInlineMarkup(tc.r))
		})
	}
}
