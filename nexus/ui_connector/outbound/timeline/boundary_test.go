package timeline

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func boundaryConfig(target, overflow int) Config {
	return Config{Streaming: true, Limits: Limits{MaxChars: target, MaxOverflow: overflow}, SplitText: markdown.BreakText}
}

func TestTextWaitsForCompleteBlocks(t *testing.T) {
	cases := []struct {
		name  string
		parts []string
	}{
		{"bold paragraph", []string{"Public docs describe **Current and R", "amping Versions**", ", with Pinned Workflows on their assigned version.", "\n\nNext paragraph."}},
		{"paragraph", []string{"When an Activity completes, the Service schedules a Workflow Task", " so the Workflow can decide what happens next.", "\n\nNext paragraph."}},
		{"list continuation", []string{"- Use endpoint evolution, not Worker Deployment Versioning", " alone.", "\n  Keep continuation lines with the same item.", "\n- ", "Next item."}},
		{"nested list", []string{"- A long parent item with some explanation.", "\n  - A nested item.", "\n- Next item."}},
		{"list paragraph", []string{"- A long parent item with some explanation.", "\n\n  An indented paragraph belongs to that item.", "\n- Next item."}},
		{"fenced block", []string{"```go\n", "some_long_function_name()\n", "another_function()\n", "```\nNext paragraph."}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg, sink, st := boundaryConfig(20, 300), &fakeSink{}, State{}
			for i, part := range tc.parts {
				Apply(cfg, sink, &st, []protocol.Event{text(part)})
				if i < len(tc.parts)-2 {
					assert.Empty(t, sink.finishes)
				}
				// The next batch can be handled by another activity process.
				encoded, err := json.Marshal(st)
				require.NoError(t, err)
				require.NoError(t, json.Unmarshal(encoded, &st))
			}
			End(cfg, sink, &st)
			require.Len(t, sink.finishes, 2)
			assert.NotEmpty(t, sink.opens, "the current block is streamed while it grows")
			assert.Equal(t, strings.Join(tc.parts, ""), sink.finishes[0].Text+sink.finishes[1].Text)
			assert.Greater(t, len([]rune(sink.finishes[0].Text)), cfg.Limits.MaxChars)
			for _, message := range sink.finishes {
				assert.Empty(t, message.Prefix)
				assert.Empty(t, message.Suffix)
			}
		})
	}
}

func TestOneBatchSplitsBeforeSendingTheNextBlock(t *testing.T) {
	cfg, sink, st := boundaryConfig(10, 100), &fakeSink{}, State{}
	Apply(cfg, sink, &st, []protocol.Event{text("The first paragraph is complete.\n\nThe second paragraph.")})
	require.Len(t, sink.finishes, 1)
	assert.Equal(t, "The first paragraph is complete.\n\n", sink.finishes[0].Text)
	require.Len(t, sink.opens, 2)
	assert.Equal(t, "The second paragraph.", markdown.RenderChunks(sink.opens[1]))
	assert.NotContains(t, markdown.RenderChunks(sink.opens[0]), "second")
	End(cfg, sink, &st)
}

func TestSplitListMarkerAcrossBatches(t *testing.T) {
	cfg, sink, st := boundaryConfig(10, 100), &fakeSink{}, State{}
	Apply(cfg, sink, &st, []protocol.Event{text("- This is the entire first item.\n")})
	Apply(cfg, sink, &st, []protocol.Event{text("1")})
	assert.Equal(t, "1", st.TextTail)
	Apply(cfg, sink, &st, []protocol.Event{text(".")})
	assert.Equal(t, "1.", st.TextTail)
	Apply(cfg, sink, &st, []protocol.Event{text(" Next item.")})
	End(cfg, sink, &st)
	require.Len(t, sink.finishes, 2)
	assert.Equal(t, "- This is the entire first item.\n", sink.finishes[0].Text)
	assert.Equal(t, "1. Next item.", sink.finishes[1].Text)
}

func TestBoundaryBehaviorDoesNotDependOnDeltaSize(t *testing.T) {
	input := "Public docs describe **Current and Ramping Versions**, with Pinned Workflows staying on their assigned version.\n\n- First list item that stays on one line.\n- Second list item.\n\nFinal paragraph."
	for _, step := range []int{1, 2, 7, 20, 1000} {
		t.Run(fmt.Sprint(step), func(t *testing.T) {
			cfg, sink, st := boundaryConfig(35, 150), &fakeSink{}, State{}
			r := []rune(input)
			for i := 0; i < len(r); i += step {
				Apply(cfg, sink, &st, []protocol.Event{text(string(r[i:min(i+step, len(r))]))})
			}
			End(cfg, sink, &st)
			var combined string
			for _, message := range sink.finishes {
				combined += message.Text
				assert.Empty(t, message.Prefix)
				assert.Empty(t, message.Suffix)
			}
			assert.Equal(t, input, combined)
			require.GreaterOrEqual(t, len(sink.finishes), 2)
			assert.Equal(t, "Public docs describe **Current and Ramping Versions**, with Pinned Workflows staying on their assigned version.\n\n", sink.finishes[0].Text)
		})
	}
}

func TestOversizedBlocksStayBoundedAndKeepSourceText(t *testing.T) {
	for _, input := range []string{
		"**" + strings.TrimSpace(strings.Repeat("bold words ", 30)) + "**",
		"_*" + strings.TrimSpace(strings.Repeat("nested words ", 30)) + "*_",
		"```go\n" + strings.Repeat("call()\n", 30) + "```",
		"- " + strings.Repeat("long list item ", 30),
		strings.Repeat("é", 240),
	} {
		for _, step := range []int{1, 2, 7, 1000} {
			t.Run(fmt.Sprintf("%s/%d", input[:min(6, len(input))], step), func(t *testing.T) {
				cfg, sink, st := boundaryConfig(40, 40), &fakeSink{}, State{}
				r := []rune(input)
				for i := 0; i < len(r); i += step {
					Apply(cfg, sink, &st, []protocol.Event{text(string(r[i:min(i+step, len(r))]))})
				}
				End(cfg, sink, &st)
				var original string
				require.Greater(t, len(sink.finishes), 1)
				for _, message := range sink.finishes {
					original += message.Text
					assert.NotEmpty(t, message.Text)
					display := markdown.PrefixText(message.Text, message.Prefix) + message.Suffix
					assert.LessOrEqual(t, len([]rune(display)), 80, "%q", display)
					if strings.HasPrefix(input, "**") {
						assert.True(t, strings.HasPrefix(strings.TrimSpace(display), "**"), "%q", display)
						assert.True(t, strings.HasSuffix(display, "**"), "%q", display)
					}
					if strings.HasPrefix(input, "_*") {
						assert.True(t, strings.HasPrefix(strings.TrimSpace(display), "_*"), "%q", display)
						assert.True(t, strings.HasSuffix(display, "*_"), "%q", display)
					}
					if strings.HasPrefix(input, "```go") {
						assert.True(t, strings.HasPrefix(display, "```go\n"), "%q", display)
						assert.Equal(t, 2, strings.Count(display, "```"), "%q", display)
					}
					if strings.HasPrefix(input, "- ") {
						assert.True(t, strings.HasPrefix(strings.TrimLeft(display, "\n\r\t "), "- "), "%q", display)
					}
				}
				assert.Equal(t, input, original)
			})
		}
	}
}

func TestEndFlushesHeldTextAndCitationsUseSourceOffsets(t *testing.T) {
	cfg, sink, st := boundaryConfig(10, 100), &fakeSink{}, State{}
	first := "A complete paragraph.\n\n"
	second := "The next paragraph."
	Apply(cfg, sink, &st, []protocol.Event{text(first + second)})
	Apply(cfg, sink, &st, []protocol.Event{{Chunk: &protocol.Chunk{Citations: []protocol.Citation{{URL: "u", EndIndex: len([]rune(first + second))}}}}})
	End(cfg, sink, &st)
	require.Len(t, sink.finishes, 2)
	assert.Equal(t, len([]rune(second)), sink.finishes[1].Citations[0].EndIndex)

	sink, st = &fakeSink{}, State{}
	Apply(cfg, sink, &st, []protocol.Event{text("- A complete item.\n-")})
	assert.Equal(t, "-", st.TextTail)
	End(cfg, sink, &st)
	assert.Empty(t, st.TextTail)
	require.Len(t, sink.finishes, 1)
	assert.Equal(t, "- A complete item.\n-", sink.finishes[0].Text)
}

type boundaryClock struct {
	fakeSink
	now time.Time
}

func (s *boundaryClock) Now() time.Time { return s.now }

func TestAgeAndCardLimitsCarryOpenFormatting(t *testing.T) {
	for _, age := range []bool{false, true} {
		t.Run(fmt.Sprint(age), func(t *testing.T) {
			cfg := boundaryConfig(1000, 100)
			sink := &boundaryClock{now: time.Unix(0, 0)}
			st := State{}
			Apply(cfg, sink, &st, []protocol.Event{text("**An unfinished paragraph")})
			if age {
				cfg.Limits.MaxAge = time.Second
				sink.now = sink.now.Add(2 * time.Second)
				Apply(cfg, sink, &st, nil)
			} else {
				cfg.Limits.MaxCards = 1
				Apply(cfg, sink, &st, []protocol.Event{tool("t1", "search", protocol.ToolComplete, "")})
			}
			require.Len(t, sink.finishes, 1)
			assert.Equal(t, "**", sink.finishes[0].Suffix)
			Apply(cfg, sink, &st, []protocol.Event{text(" that finishes here.**")})
			End(cfg, sink, &st)
			require.Len(t, sink.finishes, 2)
			assert.Equal(t, "**", sink.finishes[1].Prefix)
			assert.Equal(t, " **that finishes here.**", markdown.RenderChunks(sink.opens[1]))
		})
	}
}

func TestCardSizeIsIncludedInTheTextBudget(t *testing.T) {
	cfg, sink, st := boundaryConfig(40, 40), &fakeSink{}, State{}
	cfg.Size = func(c protocol.Chunk) int {
		if c.Tool != nil {
			return 25
		}
		return len([]rune(c.Text))
	}
	input := strings.Repeat("word ", 40)
	Apply(cfg, sink, &st, []protocol.Event{tool("t1", "search", protocol.ToolComplete, ""), text(input)})
	End(cfg, sink, &st)
	var source string
	for _, message := range sink.finishes {
		source += message.Text
		size := len([]rune(message.Prefix + message.Text + message.Suffix))
		for _, c := range message.Chunks {
			if c.Tool != nil {
				size += 25
			}
		}
		assert.LessOrEqual(t, size, 80)
	}
	assert.Equal(t, input, source)
}

func TestFailedAppendPreservesFormattingAndSource(t *testing.T) {
	cfg := boundaryConfig(100, 100)
	sink := &fakeSink{appendErrs: []error{errors.New("expired")}}
	var st State
	Apply(cfg, sink, &st, []protocol.Event{text("**First part")})
	Apply(cfg, sink, &st, []protocol.Event{text(" second part**")})
	End(cfg, sink, &st)
	require.Len(t, sink.finishes, 2)
	assert.Equal(t, "**First part", sink.finishes[0].Text)
	assert.Equal(t, "**", sink.finishes[0].Suffix)
	assert.Equal(t, " second part**", sink.finishes[1].Text)
	assert.Equal(t, " **second part**", markdown.RenderChunks(sink.opens[1]))
}

func TestPostingFallbackUsesTheSameBoundaries(t *testing.T) {
	cfg := boundaryConfig(10, 100)
	sink := &fakeSink{openErr: errors.New("unavailable")}
	var st State
	input := "The first paragraph is complete.\n\nThe second paragraph."
	Apply(cfg, sink, &st, []protocol.Event{text(input)})
	End(cfg, sink, &st)
	require.Len(t, sink.posts, 2)
	assert.Equal(t, input, sink.posts[0].Text+sink.posts[1].Text)
}

func TestEndMarksOnlyTheLastMessageFinalAfterAppendFailure(t *testing.T) {
	cfg := boundaryConfig(10, 100)
	sink, st := &fakeSink{}, State{}
	Apply(cfg, sink, &st, []protocol.Event{text("- A complete item.\n-")})
	require.Equal(t, "-", st.TextTail)
	sink.appendErrs = []error{errors.New("expired")}
	End(cfg, sink, &st)
	require.Len(t, sink.finishes, 2)
	assert.False(t, sink.finishes[0].Final)
	assert.True(t, sink.finishes[1].Final)
	assert.Equal(t, "- A complete item.\n-", sink.finishes[0].Text+sink.finishes[1].Text)
}
