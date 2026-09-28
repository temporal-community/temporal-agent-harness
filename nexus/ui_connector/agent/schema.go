package agent

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// planFields converts handler JSON Schema to protocol fields.
// Objects, lists, and unions use JSON inputs.

type schemaNode struct {
	Type        json.RawMessage   `json:"type"`
	Title       string            `json:"title"`
	Description string            `json:"description"`
	Enum        []any             `json:"enum"`
	Format      string            `json:"format"`
	Properties  json.RawMessage   `json:"properties"`
	Required    []string          `json:"required"`
	MaxLength   *int              `json:"maxLength"`
	Minimum     *float64          `json:"minimum"`
	Maximum     *float64          `json:"maximum"`
	Default     json.RawMessage   `json:"default"`
	Ref         string            `json:"$ref"`
	AnyOf       []json.RawMessage `json:"anyOf"`
	OneOf       []json.RawMessage `json:"oneOf"`
}

// planFields returns the ordered fields of a handler's parameters schema (JSON text).
func planFields(parametersSchema string) ([]protocol.Field, error) {
	if strings.TrimSpace(parametersSchema) == "" {
		return nil, nil
	}
	var root map[string]any
	if err := json.Unmarshal([]byte(parametersSchema), &root); err != nil {
		return nil, fmt.Errorf("parse schema: %w", err)
	}
	var top schemaNode
	if err := json.Unmarshal([]byte(parametersSchema), &top); err != nil {
		return nil, fmt.Errorf("parse schema: %w", err)
	}
	names, props, err := orderedObject(top.Properties)
	if err != nil {
		return nil, fmt.Errorf("parse properties: %w", err)
	}
	required := map[string]bool{}
	for _, r := range top.Required {
		required[r] = true
	}

	fields := make([]protocol.Field, 0, len(names))
	for _, name := range names {
		var raw schemaNode
		if err := json.Unmarshal(props[name], &raw); err != nil {
			return nil, fmt.Errorf("parse property %q: %w", name, err)
		}
		unwrapped, nullable := unwrapNullable(resolveRef(raw, root), root)
		resolved := resolveRef(unwrapped, root)
		kind, options, reason := classify(resolved)

		f := protocol.Field{
			Name:        name,
			Kind:        kind,
			Label:       firstNonEmpty(resolved.Title, raw.Title),
			Description: firstNonEmpty(resolved.Description, raw.Description),
			Required:    required[name] && !nullable,
			Options:     options,
			MaxLength:   resolved.MaxLength,
			Min:         resolved.Minimum,
			Max:         resolved.Maximum,
			Default:     resolved.Default,
			JSONReason:  reason,
		}
		if len(f.Default) == 0 {
			f.Default = raw.Default
		}
		fields = append(fields, f)
	}
	return fields, nil
}

// Keep schema property order in the form.
func orderedObject(raw json.RawMessage) ([]string, map[string]json.RawMessage, error) {
	values := map[string]json.RawMessage{}
	if len(bytes.TrimSpace(raw)) == 0 || string(bytes.TrimSpace(raw)) == "null" {
		return nil, values, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	tok, err := dec.Token()
	if err != nil {
		return nil, nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, nil, fmt.Errorf("expected object")
	}
	var keys []string
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, nil, err
		}
		key, _ := tok.(string)
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, nil, err
		}
		keys = append(keys, key)
		values[key] = v
	}
	return keys, values, nil
}

// resolveRef reads a local "#/..." schema reference.
func resolveRef(n schemaNode, root map[string]any) schemaNode {
	if !strings.HasPrefix(n.Ref, "#/") {
		return n
	}
	var node any = root
	for seg := range strings.SplitSeq(n.Ref[2:], "/") {
		m, ok := node.(map[string]any)
		if !ok {
			return n
		}
		node = m[seg]
	}
	b, err := json.Marshal(node)
	if err != nil {
		return n
	}
	var out schemaNode
	if err := json.Unmarshal(b, &out); err != nil {
		return n
	}
	// Values on the reference override values in the definition.
	out.Title = firstNonEmpty(n.Title, out.Title)
	out.Description = firstNonEmpty(n.Description, out.Description)
	if len(n.Default) > 0 {
		out.Default = n.Default
	}
	if n.MaxLength != nil {
		out.MaxLength = n.MaxLength
	}
	return out
}

// unwrapNullable extracts T from anyOf or oneOf with branches [T, null].
func unwrapNullable(n schemaNode, root map[string]any) (schemaNode, bool) {
	branches := n.AnyOf
	if branches == nil {
		branches = n.OneOf
	}
	if branches == nil {
		return n, false
	}
	var nonNull []schemaNode
	for _, raw := range branches {
		var b schemaNode
		if err := json.Unmarshal(raw, &b); err != nil {
			return n, false
		}
		if typeName(b) == "null" {
			continue
		}
		nonNull = append(nonNull, b)
	}
	if len(nonNull) != 1 {
		return n, false
	}
	only := resolveRef(nonNull[0], root)
	only.Title = firstNonEmpty(n.Title, only.Title)
	only.Description = firstNonEmpty(n.Description, only.Description)
	if len(n.Default) > 0 {
		only.Default = n.Default
	}
	if n.MaxLength != nil {
		only.MaxLength = n.MaxLength
	}
	return only, true
}

func typeName(n schemaNode) string {
	if len(n.Type) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(n.Type, &s); err == nil {
		return s
	}
	var list []string
	if err := json.Unmarshal(n.Type, &list); err == nil && len(list) > 0 {
		return list[0]
	}
	return ""
}

func classify(n schemaNode) (protocol.Kind, []string, string) {
	if len(n.Enum) > 0 {
		options := make([]string, 0, len(n.Enum))
		for _, v := range n.Enum {
			s, ok := v.(string)
			if !ok {
				return protocol.KindJSON, nil, "enum has non-string members"
			}
			options = append(options, s)
		}
		return protocol.KindEnum, options, ""
	}
	switch typeName(n) {
	case "string":
		if n.Format == "date" {
			return protocol.KindDate, nil, ""
		}
		return protocol.KindString, nil, ""
	case "integer":
		return protocol.KindInteger, nil, ""
	case "number":
		return protocol.KindNumber, nil, ""
	case "boolean":
		return protocol.KindBoolean, nil, ""
	case "object":
		return protocol.KindJSON, nil, "nested object"
	case "array":
		return protocol.KindJSON, nil, "list"
	default:
		return protocol.KindJSON, nil, "no single concrete type"
	}
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
