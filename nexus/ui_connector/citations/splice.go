// Package citations splices router.Citation markers inline into reply text, shared by
// the Slack and Teams outbound drivers.
package citations

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
)

// CommonMarkLink renders a link in standard CommonMark syntax "[title](url)", used by
// Slack's markdown_text fields and Teams' Bot Framework "markdown" text format.
func CommonMarkLink(url, title string) string {
	return fmt.Sprintf("[%s](%s)", title, url)
}

// markerOpen/markerClose wrap a citation's index as an internal-only sentinel, so the
// final substitution can't collide with citation-shaped text ("[1]") already in the reply.
const (
	markerOpen  = '￹'
	markerClose = '￻'
)

func marker(n int) string {
	return fmt.Sprintf("%c%d%c", markerOpen, n, markerClose)
}

var markerPattern = regexp.QuoteMeta(string(markerOpen)) + `\d+` + regexp.QuoteMeta(string(markerClose))

// Splice inserts a citation marker into text at each citation's EndIndex, then replaces
// it with a real link built from the citation's URL and Title via link. No dedup:
// repeated sources get a new number each time. Citations at the same index render
// adjacent. EndIndex < 0 appends at the very end.
//
// Two-phase: place internal sentinel markers, reflow lines (relocateLeadingCitations),
// then substitute real links last - so line-reflow can match markers by their sentinel
// regardless of link syntax or citation title/URL content.
func Splice(text string, cs []router.Citation, link func(url, title string) string) string {
	if len(cs) == 0 {
		return text
	}
	runes := []rune(text)
	markersByIndex := make(map[int][]string, len(cs))
	var order []int
	for i, c := range cs {
		idx := c.EndIndex
		if idx < 0 || idx > len(runes) {
			idx = len(runes)
		}
		idx = citationInsertionIndex(runes, idx)
		idx = avoidHeadingLine(runes, idx)
		if _, ok := markersByIndex[idx]; !ok {
			order = append(order, idx)
		}
		markersByIndex[idx] = append(markersByIndex[idx], marker(i+1))
	}
	sort.Sort(sort.Reverse(sort.IntSlice(order)))
	for _, idx := range order {
		// Exactly one space each side, none if nothing on that side (start/end of text).
		left := trimTrailingSpaces(runes[:idx])
		right := trimLeadingSpaces(runes[idx:])
		markerText := strings.Join(markersByIndex[idx], " ")
		if len(left) > 0 {
			markerText = " " + markerText
		}
		if len(right) > 0 && !closesInlineMarkup(right[0]) {
			markerText += " "
		}
		m := []rune(markerText)
		merged := make([]rune, 0, len(left)+len(m)+len(right))
		merged = append(merged, left...)
		merged = append(merged, m...)
		merged = append(merged, right...)
		runes = merged
	}

	result := relocateLeadingCitations(string(runes))
	for i, c := range cs {
		href := c.URL
		if href == "" {
			href = "#"
		}
		result = strings.Replace(result, marker(i+1), link(href, fmt.Sprintf("[%d]", i+1)), 1)
	}
	return result
}

func isWordChar(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_'
}

func isSentencePunctuation(r rune) bool {
	switch r {
	case '.', ',', ';', ':', '!', '?', ')':
		return true
	}
	return false
}

// closesInlineMarkup reports whether r is a markdown delimiter that closes emphasis,
// strikethrough, or a code span. A space before it (e.g. "text [1] *") would stop it from
// closing the span at all, so the marker must be glued directly against it instead.
func closesInlineMarkup(r rune) bool {
	switch r {
	case '*', '_', '~', '`':
		return true
	}
	return false
}

// citationInsertionIndex: if idx splits a word, moves to the end of that word (e.g.
// "st|ep" -> "step|"). Otherwise skips forward past trailing punctuation (.,;:!?))
// so the marker doesn't land before it, e.g. avoids "text[n])".
func citationInsertionIndex(runes []rune, idx int) int {
	var before, after rune
	if idx-1 >= 0 && idx-1 < len(runes) {
		before = runes[idx-1]
	}
	if idx >= 0 && idx < len(runes) {
		after = runes[idx]
	}
	if isWordChar(before) && isWordChar(after) {
		for idx < len(runes) && isWordChar(runes[idx]) {
			idx++
		}
		return idx
	}
	for idx < len(runes) && isSentencePunctuation(runes[idx]) {
		idx++
	}
	return idx
}

// avoidHeadingLine: Slack and Teams don't render links inside ATX headings ("#"-"######").
// If idx lands on a heading line, moves to the start of the next non-heading line
// (skips blank lines too). Loops for consecutive headings.
func avoidHeadingLine(runes []rune, idx int) int {
	for {
		lineStart := idx
		for lineStart > 0 && runes[lineStart-1] != '\n' {
			lineStart--
		}
		if !isHeadingLine(runes[lineStart:]) {
			return idx
		}
		next := lineStart
		for next < len(runes) && runes[next] != '\n' {
			next++
		}
		for next < len(runes) && runes[next] == '\n' {
			next++
		}
		if next == idx {
			return idx
		}
		idx = next
	}
}

// isHeadingLine reports whether line (the start of a line through the end of the text)
// begins with 1-6 '#' characters followed by a space, i.e. an ATX heading.
func isHeadingLine(line []rune) bool {
	i := 0
	for i < len(line) && i < 6 && line[i] == '#' {
		i++
	}
	return i > 0 && i < len(line) && line[i] == ' '
}

var (
	// A run of markers leading a line, e.g. "<markers>2. Durable Sleep..." (a citation
	// landed right before a list item's own start).
	leadingCitationRun = regexp.MustCompile(`^(\s*)((?:` + markerPattern + `\s*)+)(.+)$`)
	// A run of markers right after a heading prefix, e.g. "### <markers> Step 1".
	headingCitationRun = regexp.MustCompile(`^(#{1,4}\s+)((?:` + markerPattern + `\s*)+)(.+)$`)
)

// relocateLeadingCitations: a marker glued to the front of a line (before a list item's
// number, a heading's text, etc.) breaks Slack/Teams' block parsing for that line. Per
// line, moves any such leading run to the end of the line instead.
func relocateLeadingCitations(text string) string {
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		line = moveMatchedCitationsToEnd(leadingCitationRun, line)
		line = moveMatchedCitationsToEnd(headingCitationRun, line)
		lines[i] = line
	}
	return strings.Join(lines, "\n")
}

func moveMatchedCitationsToEnd(pattern *regexp.Regexp, line string) string {
	m := pattern.FindStringSubmatch(line)
	if m == nil || strings.TrimSpace(m[3]) == "" {
		return line
	}
	return m[1] + strings.TrimSpace(m[3]) + " " + strings.Join(strings.Fields(m[2]), " ")
}

func trimTrailingSpaces(runes []rune) []rune {
	i := len(runes)
	for i > 0 && runes[i-1] == ' ' {
		i--
	}
	return runes[:i]
}

func trimLeadingSpaces(runes []rune) []rune {
	i := 0
	for i < len(runes) && runes[i] == ' ' {
		i++
	}
	return runes[i:]
}
