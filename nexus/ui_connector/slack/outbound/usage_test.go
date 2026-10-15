package slackoutbound

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/timeline"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func count(n int64) *int64 { return &n }

func usageEvent(model string, input, cached, output int64) protocol.Event {
	return protocol.Event{Usage: &protocol.ModelUsage{
		Model: model, InputTokens: count(input), CachedTokens: count(cached), OutputTokens: count(output),
	}}
}

func TestUsageIsSavedAcrossDeliveriesAndShownOnlyAtTheEnd(t *testing.T) {
	f := newFakeSlack(t)
	p := f.platform()
	address, err := json.Marshal(addr)
	require.NoError(t, err)
	in := DeliverInput{Address: address, Events: []protocol.Event{
		{Chunk: &protocol.Chunk{Text: "Answer."}},
		usageEvent("gpt-6.1-sol", 1_000_000, 900_000, 1_000),
	}}
	in.State, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	in.Events = []protocol.Event{usageEvent("gpt-6.1-sol", 1_190_890, 1_091_589, 1_564)}
	in.State, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	assert.Equal(t, []string{"/chat.startStream"}, f.paths())
	assert.NotContains(t, chunksOf(t, f.requests[0])[0]["text"], "Input:")

	in.Events, in.End = nil, true
	_, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	assert.Equal(t, []string{"/chat.startStream", "/chat.stopStream"}, f.paths())
	assert.Equal(t, "\n\ngpt-6.1-sol · Input: 2.19M · Cached: 1.99M · Output: 2.56K", chunksOf(t, f.requests[1])[0]["text"])
}

func TestUsageDoesNotFlushBufferedMarkdown(t *testing.T) {
	f := newFakeSlack(t)
	p := f.platform()
	p.Limits = timeline.Limits{MaxChars: 10, MaxOverflow: 100}
	address, err := json.Marshal(addr)
	require.NoError(t, err)
	in := DeliverInput{Address: address, Events: []protocol.Event{{Chunk: &protocol.Chunk{Text: "Paragraph one.\n"}}}}
	in.State, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	in.Events = []protocol.Event{usageEvent("model", 10, 0, 5)}
	in.State, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	in.Events = []protocol.Event{{Chunk: &protocol.Chunk{Text: "\nParagraph two."}}}
	_, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	assert.Equal(t, []string{"/chat.startStream", "/chat.appendStream", "/chat.stopStream", "/chat.startStream"}, f.paths())
	assert.Equal(t, "Paragraph two.", chunksOf(t, f.requests[3])[0]["text"])
}

func TestUsageAppearsOnlyOnTheLastSegment(t *testing.T) {
	f := newFakeSlack(t)
	p := f.platform()
	p.Limits = timeline.Limits{MaxChars: 12, MaxOverflow: 24}
	address, err := json.Marshal(addr)
	require.NoError(t, err)
	in := DeliverInput{Address: address, Events: []protocol.Event{
		usageEvent("model", 100, 0, 10),
		{Chunk: &protocol.Chunk{Text: "First paragraph.\n\nSecond paragraph."}},
	}}
	in.State, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	in.Events, in.End = nil, true
	_, err = p.Deliver(context.Background(), in)
	require.NoError(t, err)
	var endings []string
	for _, req := range f.requests {
		if req.path == "/chat.stopStream" && req.form.Get("chunks") != "" {
			for _, chunk := range chunksOf(t, req) {
				if text, ok := chunk["text"].(string); ok {
					endings = append(endings, text)
				}
			}
		}
	}
	assert.Equal(t, []string{"\n\nmodel · Input: 100 · Cached: 0 · Output: 10"}, endings)
	assert.Equal(t, 2, strings.Count(strings.Join(f.paths(), " "), "/chat.stopStream"))
}

func TestUsageFooterWithCitationsAndFallback(t *testing.T) {
	for _, mode := range []string{"inline citations", "cards", "posted reply", "closed message"} {
		t.Run(mode, func(t *testing.T) {
			f := newFakeSlack(t)
			p := f.platform()
			address, err := json.Marshal(addr)
			require.NoError(t, err)
			in := DeliverInput{Address: address, Events: []protocol.Event{
				{Chunk: &protocol.Chunk{Text: "hello world"}},
				{Chunk: &protocol.Chunk{Citations: []protocol.Citation{{URL: "https://a", Title: "A", EndIndex: 5}}}},
				usageEvent("model", 20, 0, 2),
			}}
			switch mode {
			case "cards":
				in.Events = append(in.Events, protocol.Event{Chunk: &protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t", Name: "search", Phase: protocol.ToolComplete}}})
			case "posted reply":
				in.State = json.RawMessage(`{"NoStream":true}`)
			case "closed message":
				in.State = json.RawMessage(`{"NoStream":true}`)
				in.Events = append(in.Events, protocol.Event{Guide: &protocol.Guide{}})
			}
			in.State, err = p.Deliver(context.Background(), in)
			require.NoError(t, err)
			beforeEnd := len(f.requests)
			in.Events, in.End = nil, true
			_, err = p.Deliver(context.Background(), in)
			require.NoError(t, err)
			footer := "model · Input: 20 · Cached: 0 · Output: 2"
			for _, req := range f.requests[:beforeEnd] {
				assert.NotContains(t, req.form.Get("chunks")+req.form.Get("text")+req.form.Get("markdown_text"), "Input:")
			}
			lastContent := ""
			for _, req := range f.requests[beforeEnd:] {
				switch req.path {
				case "/chat.stopStream":
					for _, chunk := range chunksOf(t, req) {
						lastContent += chunk["text"].(string)
					}
				case "/chat.update":
					lastContent = req.form.Get("markdown_text")
				case "/chat.postMessage":
					lastContent = req.form.Get("text")
				}
			}
			assert.True(t, strings.HasSuffix(lastContent, footer), lastContent)
			if mode != "closed message" {
				assert.Contains(t, lastContent, "https://a")
			}
			if mode == "cards" {
				assert.NotContains(t, f.paths(), "/chat.update")
			}
		})
	}
}

func TestUsageTotalsKeepModelsSeparateAndUnknownCountsUnknown(t *testing.T) {
	a := usageEvent("a", 10, 0, 3)
	b := usageEvent("b", 20, 0, 4)
	var totals []protocol.ModelUsage
	totals = addUsage(totals, *a.Usage)
	totals = addUsage(totals, *b.Usage)
	totals = addUsage(totals, protocol.ModelUsage{Model: "a", InputTokens: count(5), OutputTokens: count(0)})
	totals = addUsage(totals, *a.Usage)
	assert.Equal(t, "\n\na · Input: 25 · Cached: — · Output: 6\nb · Input: 20 · Cached: 0 · Output: 4", usageFooter(totals))
	assert.Equal(t, int64(10), *a.Usage.InputTokens, "aggregation must not change the event")
	assert.Equal(t, "", usageFooter(nil))
	assert.Equal(t, "\n\nUnknown model · Input: — · Cached: — · Output: —", usageFooter([]protocol.ModelUsage{{}}))
}

func TestFormatTokens(t *testing.T) {
	for _, tc := range []struct {
		n    int64
		want string
	}{
		{0, "0"}, {999, "999"}, {1000, "1K"}, {1234, "1.23K"}, {12000, "12K"}, {999999, "1000K"}, {1_000_000, "1M"}, {2_190_890, "2.19M"},
	} {
		assert.Equal(t, tc.want, formatTokens(count(tc.n)))
	}
	assert.Equal(t, "—", formatTokens(nil))
}
