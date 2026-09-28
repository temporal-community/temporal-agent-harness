// Package presentation provides text helpers for connector interfaces.
package presentation

import (
	"regexp"
	"strings"
	"unicode"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// Clip shortens s to at most max runes. It ends with "…" when it removes text.
func Clip(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return string(r[:max-1]) + "…"
}

// FieldLabel uses the supplied label or formats the field name for display.
func FieldLabel(f protocol.Field) string {
	if f.Label != "" {
		return f.Label
	}
	return Humanize(f.Name)
}

var (
	humanizeSeparators = regexp.MustCompile(`[_-]+`)
	humanizeCamel      = regexp.MustCompile(`([a-z\d])([A-Z])`)
)

// Humanize turns "new_policy" into "New Policy".
func Humanize(name string) string {
	s := humanizeSeparators.ReplaceAllString(name, " ")
	s = humanizeCamel.ReplaceAllString(s, "$1 $2")
	words := strings.Fields(s)
	for i, w := range words {
		r := []rune(w)
		r[0] = unicode.ToUpper(r[0])
		words[i] = string(r)
	}
	return strings.Join(words, " ")
}
