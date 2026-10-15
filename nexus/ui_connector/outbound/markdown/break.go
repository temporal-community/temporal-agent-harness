package markdown

import (
	"strings"
	"unicode"
)

// TextBreak uses rune offsets. Hold marks text to save until the next delivery.
// Suffix closes formatting in this message. Prefix reopens it in the next message.
type TextBreak struct {
	Index, Hold    int
	Prefix, Suffix string
}

// BreakText prefers the first complete block after target. It never moves text
// before minimum, which has already been sent, and forces a split at maximum.
func BreakText(text string, minimum, target, maximum int) TextBreak {
	r := []rune(text)
	code := codeRanges(r)
	if minimum >= maximum {
		close, open, _ := formattingAt(r, code, minimum)
		return TextBreak{Index: minimum, Suffix: close, Prefix: open}
	}
	boundary := func(i int) bool {
		if i < minimum || i < target || i > maximum || i == 0 {
			return false
		}
		close, _, link := formattingAt(r, code, i)
		return close == "" && !link
	}
	for i := 0; i < len(r); i++ {
		if r[i] != '\n' {
			continue
		}
		line := i + 1
		next := lineEnd(r, line)
		level := listLevelBefore(r, line)
		if onlySpace(r[line:next]) && next < len(r) && boundary(next+1) {
			following := next + 1
			indent := 0
			for following+indent < len(r) && (r[following+indent] == ' ' || r[following+indent] == '\t') {
				indent++
			}
			if level < 0 || following+indent < len(r) && indent <= level {
				return TextBreak{Index: following}
			}
		}
		if complete, _ := blockMarker(r[line:next]); complete && boundary(line) {
			indent := 0
			for line+indent < next && (r[line+indent] == ' ' || r[line+indent] == '\t') {
				indent++
			}
			if level < 0 || indent <= level {
				return TextBreak{Index: line}
			}
		}
		for _, span := range code {
			if span.block && span.closed && span.end == i && boundary(line) {
				return TextBreak{Index: line}
			}
		}
	}
	ending, _, _ := formattingAt(r, code, len(r))
	if len(r)+len([]rune(ending)) < maximum {
		line := len(r)
		for line > 0 && r[line-1] != '\n' {
			line--
		}
		if line >= minimum && line >= target {
			if _, partial := blockMarker(r[line:]); partial {
				return TextBreak{Hold: line}
			}
		}
		return TextBreak{}
	}

	// Prefer a line, then a sentence, then a word when a whole block cannot fit.
	best, rank := TextBreak{}, -1
	for i := max(minimum, 1); i <= min(maximum, len(r)); i++ {
		close, open, link := boundedFormattingAt(r, code, i, maximum)
		if i+len([]rune(close)) > maximum || link || insideDelimiter(r, code, i) {
			continue
		}
		priority := 0
		if r[i-1] == '\n' {
			priority = 3
		} else if i < len(r) && unicode.IsSpace(r[i]) && !unicode.IsSpace(r[i-1]) {
			priority = 1
			j := i - 1
			if strings.ContainsRune(".!?", r[j]) {
				priority = 2
			}
		}
		if priority >= rank {
			best, rank = TextBreak{Index: i, Suffix: close, Prefix: open}, priority
		}
	}
	if best.Index == 0 {
		// A single long token still has to make progress.
		i := min(maximum, len(r))
		close, open, _ := boundedFormattingAt(r, code, i, maximum)
		i = max(minimum+1, i-len([]rune(close)))
		close, open, _ = boundedFormattingAt(r, code, i, maximum)
		best = TextBreak{Index: i, Suffix: close, Prefix: open}
	}
	return best
}

// Limit display-only delimiters too. An oversized fence label can be omitted;
// delimiters that cannot fit are left as literal text in the continuation.
func boundedFormattingAt(r []rune, code []codeRange, index, maximum int) (close, open string, link bool) {
	close, open, link = formattingAt(r, code, index)
	if len([]rune(open)) >= maximum && strings.HasSuffix(open, "\n") {
		end := 0
		for end < len(open) && (open[end] == '`' || open[end] == '~') {
			end++
		}
		open = open[:end] + "\n"
	}
	if len([]rune(open)) >= maximum || len([]rune(close)) >= maximum {
		close, open = "", ""
	}
	return close, open, link
}

// blockMarker recognizes list items and headings, including an incomplete marker
// at the end of a delivery. Continuation lines are part of the current item.
func blockMarker(line []rune) (complete, partial bool) {
	i := 0
	for i < len(line) && (line[i] == ' ' || line[i] == '\t') {
		i++
	}
	if i == len(line) {
		return false, true
	}
	switch line[i] {
	case '-', '+', '*':
		i++
	case '#':
		for i < len(line) && line[i] == '#' {
			i++
		}
	default:
		start := i
		for i < len(line) && unicode.IsDigit(line[i]) {
			i++
		}
		if i == start {
			return false, false
		}
		if i == len(line) {
			return false, true
		}
		if line[i] != '.' && line[i] != ')' {
			return false, false
		}
		i++
	}
	if i == len(line) {
		return false, true
	}
	return line[i] == ' ' || line[i] == '\t', false
}

// listLevelBefore keeps nested items and indented paragraphs with their parent.
func listLevelBefore(r []rune, until int) int {
	level, blank := -1, false
	for start := 0; start < until; {
		end := min(lineEnd(r, start), until)
		indent := 0
		for start+indent < end && (r[start+indent] == ' ' || r[start+indent] == '\t') {
			indent++
		}
		if start+indent == end {
			blank = true
		} else {
			complete, _ := blockMarker(r[start:end])
			if complete && r[start+indent] != '#' {
				if level < 0 || indent < level {
					level = indent
				}
			} else if blank && indent <= level {
				level = -1
			}
			blank = false
		}
		start = end + 1
	}
	return level
}

func insideDelimiter(r []rune, code []codeRange, i int) bool {
	if i < len(r) && r[i] == '\\' || i > 0 && r[i-1] == '\\' {
		return true
	}
	if i > 0 && i < len(r) && strings.ContainsRune("*_~`", r[i]) && r[i] == r[i-1] {
		return true
	}
	for _, span := range code {
		if i <= span.start || i >= span.end {
			continue
		}
		start := span.start
		for start < len(r) && (r[start] == ' ' || r[start] == '\t') {
			start++
		}
		if span.block && i <= lineEnd(r, start)+1 || !span.block && i < delimiterEnd(r, start) {
			return true
		}
	}
	return false
}

// formattingAt returns closing and opening delimiters for the split position.
// These are display text; they are not part of the agent's reply or its offsets.
func formattingAt(r []rune, code []codeRange, index int) (close, open string, link bool) {
	var marks []string
	var inline string
	brackets, parens := 0, 0
	for i := 0; i < index && i < len(r); {
		var span *codeRange
		for j := range code {
			if i == code[j].start {
				span = &code[j]
				break
			}
		}
		if span != nil {
			if index < span.end || span.block && !span.closed && index == span.end {
				start := span.start
				for start < len(r) && (r[start] == ' ' || r[start] == '\t') {
					start++
				}
				ticks := string(r[start:delimiterEnd(r, start)])
				if span.block {
					end := lineEnd(r, start)
					open = string(r[start:min(end, start+128)]) + "\n"
					close = "\n" + ticks + "\n"
				} else {
					open, close = ticks, ticks
				}
				break
			}
			i = span.end
			continue
		}
		if r[i] == '\\' {
			i += 2
			continue
		}
		if r[i] == '`' {
			end := delimiterEnd(r, i)
			if inline == "" {
				inline = string(r[i:end])
			} else if inline == string(r[i:end]) {
				inline = ""
			}
			i = end
			continue
		}
		if inline != "" {
			i++
			continue
		}
		switch r[i] {
		case '[':
			brackets++
		case ']':
			if brackets > 0 {
				brackets--
			}
			if i+1 < len(r) && r[i+1] == '(' {
				parens++
				i += 2
				continue
			}
		case '(':
			if parens > 0 {
				parens++
			}
		case ')':
			if parens > 0 {
				parens--
			}
		case '*', '_', '~':
			end := delimiterEnd(r, i)
			if r[i] == '_' && i > 0 && end < len(r) && isWordChar(r[i-1]) && isWordChar(r[end]) {
				i = end
				continue
			}
			for n := end - i; n > 0; {
				if len(marks) > 0 && rune(marks[len(marks)-1][0]) == r[i] && len(marks[len(marks)-1]) <= n && i > 0 && !unicode.IsSpace(r[i-1]) {
					n -= len(marks[len(marks)-1])
					marks = marks[:len(marks)-1]
				} else if end < len(r) && !unicode.IsSpace(r[end]) {
					count := min(n, 2)
					marks = append(marks, strings.Repeat(string(r[i]), count))
					n -= count
				} else {
					break
				}
			}
			i = end
			continue
		}
		i++
	}
	if inline != "" {
		close, open = inline+close, open+inline
	}
	open = strings.Join(marks, "") + open
	for i := len(marks) - 1; i >= 0; i-- {
		close += marks[i]
	}
	if close != "" || index > 0 && r[index-1] != '\n' {
		line := index
		for line > 0 && r[line-1] != '\n' {
			line--
		}
		end := lineEnd(r, line)
		if complete, _ := blockMarker(r[line:end]); complete && r[line] != '#' {
			i := line
			for i < end && (r[i] == ' ' || r[i] == '\t') {
				i++
			}
			marker := i
			for i < end && !unicode.IsSpace(r[i]) {
				i++
			}
			for i < end && (r[i] == ' ' || r[i] == '\t') {
				i++
			}
			open = string(r[marker:i]) + open
		}
	}
	if close != "" && !strings.HasPrefix(close, "\n") && index > 0 && unicode.IsSpace(r[index-1]) {
		// A nonspacing character lets emphasis close after streamed whitespace.
		close = "\u200b" + close
	}
	return close, open, brackets > 0 || parens > 0
}
