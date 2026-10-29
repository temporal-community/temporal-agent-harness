package markdown

type codeRange struct {
	start, end int
	block      bool
	closed     bool
}

// codeRanges finds inline code and fenced blocks in the original reply text.
// Offsets count runes and include the delimiters.
func codeRanges(text []rune) []codeRange {
	var spans []codeRange
	for i := 0; i < len(text); {
		if i == 0 || text[i-1] == '\n' {
			start := i
			for start < len(text) && (text[start] == ' ' || text[start] == '\t') {
				start++
			}
			if start < len(text) && (text[start] == '`' || text[start] == '~') {
				end := delimiterEnd(text, start)
				validFence := true
				if text[start] == '`' {
					for _, r := range text[end:lineEnd(text, end)] {
						if r == '`' {
							validFence = false
							break
						}
					}
				}
				if end-start >= 3 && validFence {
					span := codeRange{start: i, end: len(text), block: true}
					for line := lineEnd(text, end); line < len(text); {
						line++
						next := lineEnd(text, line)
						p := line
						for p < next && (text[p] == ' ' || text[p] == '\t') {
							p++
						}
						if p < next && text[p] == text[start] {
							q := delimiterEnd(text, p)
							if q-p >= end-start && onlySpace(text[q:next]) {
								span.end, span.closed = next, true
								break
							}
						}
						line = next
					}
					spans = append(spans, span)
					i = span.end
					continue
				}
			}
		}
		if text[i] == '\\' && i+1 < len(text) {
			i += 2
			continue
		}
		if text[i] == '`' {
			end := delimiterEnd(text, i)
			close := end
			for close < len(text) {
				if text[close] != '`' {
					close++
					continue
				}
				next := delimiterEnd(text, close)
				if next-close == end-i {
					spans = append(spans, codeRange{start: i, end: next, closed: true})
					end = next
					break
				}
				close = next
			}
			i = end
			continue
		}
		i++
	}
	return spans
}

func delimiterEnd(text []rune, start int) int {
	end := start + 1
	for end < len(text) && text[end] == text[start] {
		end++
	}
	return end
}

func lineEnd(text []rune, start int) int {
	for start < len(text) && text[start] != '\n' {
		start++
	}
	return start
}

func onlySpace(text []rune) bool {
	for _, r := range text {
		if r != ' ' && r != '\t' && r != '\r' {
			return false
		}
	}
	return true
}
