package markdown

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestBreakTextAvoidsInlineFormattingAndLinks(t *testing.T) {
	for _, body := range []string{
		"**bold across\n\nseveral lines**",
		"`code across\n\nseveral lines`",
		"[a label across\n\nseveral lines](https://example.com)",
		"an escaped \\* and a snake_case_name",
	} {
		value := body + "\n\nNext paragraph."
		plan := BreakText(value, 0, 5, 200)
		assert.Equal(t, len([]rune(body+"\n\n")), plan.Index, "%q", body)
		assert.Empty(t, plan.Prefix)
		assert.Empty(t, plan.Suffix)
	}
}

func TestForcedSplitClosesEmphasisAfterWhitespace(t *testing.T) {
	value := "**words "
	plan := BreakText(value, len(value), len(value), len(value))
	assert.Equal(t, "\u200b**", plan.Suffix)
	assert.Equal(t, "**", plan.Prefix)
}

func FuzzBreakTextMakesBoundedProgress(f *testing.F) {
	for _, seed := range []string{
		strings.Repeat("word ", 50),
		"**" + strings.Repeat("bold ", 50) + "**",
		"```go\n" + strings.Repeat("call()\n", 30) + "```",
		"[label](" + strings.Repeat("x", 100) + ")",
		strings.Repeat("`", 200),
		"```" + strings.Repeat("lang", 50) + "\ncontent",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, value string) {
		r := []rune(value)
		if len(r) > 4096 {
			r = r[:4096]
		}
		plan := BreakText(string(r), 0, 24, 48)
		if plan.Index > 0 {
			if plan.Index > len(r) {
				t.Fatalf("split %d exceeds input %d", plan.Index, len(r))
			}
			if plan.Index+len([]rune(plan.Suffix)) > 48 {
				t.Fatalf("split exceeds hard limit: %+v", plan)
			}
			if len([]rune(plan.Prefix)) >= 48 {
				t.Fatalf("continuation cannot fit: %+v", plan)
			}
		} else if len(r) >= 48 {
			t.Fatalf("no progress for %q", string(r))
		}
	})
}
