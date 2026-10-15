package protocol

import (
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
)

// Handler describes one request type that the agent accepts.
type Handler struct {
	Name        string
	Description string
	// MidTurn is "accept" when the handler works while a turn is open, "enqueue" when
	// it waits, and "reject" when it is refused. Empty if unknown.
	MidTurn string  `json:",omitempty"`
	Fields  []Field `json:",omitempty"`
}

// HandlerHelp contains the handler and validation errors by field name.
// An empty Errors map asks for help with the handler's arguments.
type HandlerHelp struct {
	Handler Handler
	Errors  map[string]string `json:",omitempty"`
}

// Kind identifies a field's value type.
type Kind string

const (
	KindString  Kind = "string"
	KindNumber  Kind = "number"
	KindInteger Kind = "integer"
	KindBoolean Kind = "boolean"
	KindEnum    Kind = "enum"
	KindDate    Kind = "date"
	KindJSON    Kind = "json"
)

// Field is one input of a handler.
type Field struct {
	Name        string
	Label       string
	Description string   `json:",omitempty"`
	Kind        Kind     `json:",omitempty"`
	Required    bool     `json:",omitempty"`
	Options     []string `json:",omitempty"` // allowed values, for KindEnum
	MaxLength   *int     `json:",omitempty"` // maximum string length in characters
	Min         *float64 `json:",omitempty"`
	Max         *float64 `json:",omitempty"`
	// Default is the default value as JSON. Empty if there is no default.
	Default json.RawMessage `json:",omitempty"`
	// JSONReason is set when the field uses KindJSON because no simpler kind fits.
	JSONReason string `json:",omitempty"`
}

// Decode converts entered values to typed payload values. It returns errors by field
// name. An empty optional field is left out, so the handler's default applies.
func Decode(fields []Field, values map[string]string) (map[string]any, map[string]string) {
	payload := map[string]any{}
	errs := map[string]string{}
	for _, f := range fields {
		raw, ok := values[f.Name]
		if !ok || strings.TrimSpace(raw) == "" {
			if f.Required {
				errs[f.Name] = "Required."
			}
			continue
		}
		switch f.Kind {
		case KindInteger, KindNumber:
			n, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
			switch {
			case err != nil || math.IsInf(n, 0) || math.IsNaN(n):
				errs[f.Name] = "Must be a number."
			case f.Kind == KindInteger && n != math.Trunc(n):
				errs[f.Name] = "Must be a whole number."
			case f.Min != nil && n < *f.Min:
				errs[f.Name] = fmt.Sprintf("Must be at least %v.", *f.Min)
			case f.Max != nil && n > *f.Max:
				errs[f.Name] = fmt.Sprintf("Must be at most %v.", *f.Max)
			case f.Kind == KindInteger:
				payload[f.Name] = int64(n)
			default:
				payload[f.Name] = n
			}
		case KindBoolean:
			switch strings.ToLower(strings.TrimSpace(raw)) {
			case "true", "yes":
				payload[f.Name] = true
			case "false", "no":
				payload[f.Name] = false
			default:
				errs[f.Name] = "Must be true or false."
			}
		case KindEnum:
			if !slices.Contains(f.Options, raw) {
				errs[f.Name] = "Must be one of: " + strings.Join(f.Options, ", ") + "."
			} else {
				payload[f.Name] = raw
			}
		case KindJSON:
			var v any
			if err := json.Unmarshal([]byte(raw), &v); err != nil {
				errs[f.Name] = "Invalid JSON: " + err.Error()
			} else {
				payload[f.Name] = v
			}
		default:
			if f.MaxLength != nil && len([]rune(raw)) > *f.MaxLength {
				errs[f.Name] = fmt.Sprintf("Must be at most %d characters.", *f.MaxLength)
			} else {
				payload[f.Name] = raw
			}
		}
	}
	return payload, errs
}
