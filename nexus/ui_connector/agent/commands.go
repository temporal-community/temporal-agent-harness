package agent

import (
	"strings"
	"unicode"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func singleTextField(fields []protocol.Field) bool {
	return len(fields) == 1 && fields[0].Kind == protocol.KindString
}

// parseText accepts the full text for one string field, or key=value pairs.
// Values in pairs can use single or double quotes.
func parseText(fields []protocol.Field, text string) map[string]string {
	text = strings.TrimSpace(text)
	if text == "" {
		return map[string]string{}
	}
	if singleTextField(fields) && !strings.HasPrefix(text, fields[0].Name+"=") {
		return map[string]string{fields[0].Name: text}
	}
	values := map[string]string{}
	for _, tok := range tokenize(text) {
		eq := strings.Index(tok, "=")
		if eq <= 0 {
			continue
		}
		values[tok[:eq]] = tok[eq+1:]
	}
	return values
}

func tokenize(text string) []string {
	var tokens []string
	var cur strings.Builder
	var quote rune
	flush := func() {
		if cur.Len() > 0 {
			tokens = append(tokens, cur.String())
			cur.Reset()
		}
	}
	for _, r := range text {
		switch {
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				cur.WriteRune(r)
			}
		case r == '"' || r == '\'':
			quote = r
		case unicode.IsSpace(r):
			flush()
		default:
			cur.WriteRune(r)
		}
	}
	flush()
	return tokens
}

// intent is what a chat message asks for.
type intent struct {
	Kind    intentKind
	Text    string // intentAsk: the message text
	Handler string // intentHandler: handler name
	Args    string // intentHandler: argument text
}

type intentKind int

const (
	intentAsk intentKind = iota
	intentGuide
	intentHandler
)

// parseIntent selects chat, the handler guide, or a named handler request.
func parseIntent(raw string) intent {
	text := strings.TrimSpace(raw)
	if !strings.HasPrefix(text, "/") {
		return intent{Kind: intentAsk, Text: text}
	}
	body := strings.TrimSpace(text[1:])
	name, args := body, ""
	if i := strings.IndexFunc(body, unicode.IsSpace); i >= 0 {
		name, args = body[:i], strings.TrimSpace(body[i+1:])
	}
	switch strings.ToLower(name) {
	case "help", "handlers", "commands", "?", "":
		return intent{Kind: intentGuide}
	}
	return intent{Kind: intentHandler, Handler: name, Args: args}
}
