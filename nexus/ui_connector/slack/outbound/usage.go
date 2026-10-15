package slackoutbound

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func addUsage(totals []protocol.ModelUsage, call protocol.ModelUsage) []protocol.ModelUsage {
	for i := range totals {
		if totals[i].Model == call.Model {
			totals[i].InputTokens = sumTokens(totals[i].InputTokens, call.InputTokens)
			totals[i].CachedTokens = sumTokens(totals[i].CachedTokens, call.CachedTokens)
			totals[i].OutputTokens = sumTokens(totals[i].OutputTokens, call.OutputTokens)
			return totals
		}
	}
	return append(totals, call)
}

// A total is unavailable if any call did not report that count.
func sumTokens(a, b *int64) *int64 {
	if a == nil || b == nil {
		return nil
	}
	total := *a + *b
	return &total
}

func usageFooter(totals []protocol.ModelUsage) string {
	if len(totals) == 0 {
		return ""
	}
	lines := make([]string, 0, len(totals))
	for _, usage := range totals {
		model := usage.Model
		if model == "" {
			model = "Unknown model"
		}
		lines = append(lines, fmt.Sprintf("%s · Input: %s · Cached: %s · Output: %s",
			model, formatTokens(usage.InputTokens), formatTokens(usage.CachedTokens), formatTokens(usage.OutputTokens)))
	}
	return "\n\n" + strings.Join(lines, "\n")
}

func formatTokens(count *int64) string {
	if count == nil {
		return "—"
	}
	var value string
	var suffix string
	switch {
	case *count >= 1_000_000:
		value, suffix = fmt.Sprintf("%.2f", float64(*count)/1_000_000), "M"
	case *count >= 1_000:
		value, suffix = fmt.Sprintf("%.2f", float64(*count)/1_000), "K"
	default:
		return strconv.FormatInt(*count, 10)
	}
	return strings.TrimRight(strings.TrimRight(value, "0"), ".") + suffix
}
