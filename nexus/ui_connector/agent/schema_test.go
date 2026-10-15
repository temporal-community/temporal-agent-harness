package agent

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

// pydanticSchema contains references, nullable fields, scalar types, objects, and lists.
const pydanticSchema = `{
  "$defs": {
    "Policy": {"enum": ["allow_safe", "strict"], "title": "Policy", "type": "string"},
    "Inner": {"properties": {"a": {"type": "integer"}}, "type": "object"}
  },
  "properties": {
    "new_policy": {"$ref": "#/$defs/Policy", "description": "The posture."},
    "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Note"},
    "code": {"type": "string", "maxLength": 8},
    "count": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
    "ratio": {"type": "number"},
    "verbose": {"type": "boolean"},
    "day": {"type": "string", "format": "date"},
    "inner": {"$ref": "#/$defs/Inner"},
    "tags": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["new_policy", "note", "count"],
  "type": "object"
}`

func TestPlan(t *testing.T) {
	fields, err := planFields(pydanticSchema)
	require.NoError(t, err)

	names := make([]string, len(fields))
	for i, f := range fields {
		names[i] = f.Name
	}
	assert.Equal(t, []string{"new_policy", "note", "code", "count", "ratio", "verbose", "day", "inner", "tags"}, names, "schema order is kept")

	byName := map[string]protocol.Field{}
	for _, f := range fields {
		byName[f.Name] = f
	}
	policy := byName["new_policy"]
	assert.Equal(t, protocol.KindEnum, policy.Kind)
	assert.Equal(t, []string{"allow_safe", "strict"}, policy.Options)
	assert.Equal(t, "Policy", policy.Label)
	assert.Equal(t, "The posture.", policy.Description)
	assert.True(t, policy.Required)

	note := byName["note"]
	assert.Equal(t, protocol.KindString, note.Kind, "Optional[str] unwraps to its string type")
	assert.False(t, note.Required, "a nullable field is never required")

	assert.Equal(t, protocol.KindString, byName["code"].Kind)
	assert.Equal(t, 8, *byName["code"].MaxLength)
	assert.Equal(t, protocol.KindInteger, byName["count"].Kind)
	assert.Equal(t, 1.0, *byName["count"].Min)
	assert.Equal(t, "2", string(byName["count"].Default))
	assert.Equal(t, protocol.KindNumber, byName["ratio"].Kind)
	assert.Equal(t, protocol.KindBoolean, byName["verbose"].Kind)
	assert.Empty(t, byName["verbose"].Label, "the adapter supplies a display label when the schema has no title")
	assert.Equal(t, protocol.KindDate, byName["day"].Kind)
	assert.Equal(t, protocol.KindJSON, byName["inner"].Kind)
	assert.Equal(t, "nested object", byName["inner"].JSONReason)
	assert.Equal(t, protocol.KindJSON, byName["tags"].Kind)
}

func TestPlan_Empty(t *testing.T) {
	fields, err := planFields("")
	require.NoError(t, err)
	assert.Empty(t, fields)

	fields, err = planFields(`{"type":"object"}`)
	require.NoError(t, err)
	assert.Empty(t, fields)

	_, err = planFields("not json")
	assert.Error(t, err)
}

func TestDecode(t *testing.T) {
	fields, err := planFields(pydanticSchema)
	require.NoError(t, err)

	payload, errs := protocol.Decode(fields, map[string]string{
		"new_policy": "strict",
		"note":       "hi",
		"count":      "3",
		"ratio":      "0.5",
		"verbose":    "false",
		"tags":       `["a"]`,
		"code":       "  ",
	})
	assert.Empty(t, errs)
	assert.Equal(t, map[string]any{
		"new_policy": "strict", "note": "hi", "count": int64(3), "ratio": 0.5,
		"verbose": false, "tags": []any{"a"},
	}, payload, "an empty optional field is left out")

	_, errs = protocol.Decode(fields, map[string]string{"new_policy": "lax", "count": "9", "verbose": "maybe", "tags": "[", "ratio": "x"})
	assert.Equal(t, "Must be one of: allow_safe, strict.", errs["new_policy"])
	assert.Equal(t, "Must be at most 5.", errs["count"])
	assert.Equal(t, "Must be true or false.", errs["verbose"])
	assert.Contains(t, errs["tags"], "Invalid JSON")
	assert.Equal(t, "Must be a number.", errs["ratio"])
	assert.NotContains(t, errs, "note", "a nullable field may be left empty")

	_, errs = protocol.Decode(fields, map[string]string{"new_policy": "strict"})
	assert.Equal(t, "Required.", errs["count"])

	_, errs = protocol.Decode(fields, map[string]string{"new_policy": "strict", "note": "x", "count": "1.5"})
	assert.Equal(t, "Must be a whole number.", errs["count"])
}

func TestParseText(t *testing.T) {
	prose := []protocol.Field{{Name: "text", Kind: protocol.KindString}}
	assert.Equal(t, map[string]string{"text": "how is the deploy = going"}, parseText(prose, " how is the deploy = going "))
	assert.Equal(t, map[string]string{"text": "hi"}, parseText(prose, "text=hi"), "the explicit form still works")

	fields := []protocol.Field{{Name: "a", Kind: protocol.KindString}, {Name: "b", Kind: protocol.KindString}}
	assert.Equal(t, map[string]string{"a": "1", "b": "two words"}, parseText(fields, `a=1 b="two words" junk`))
	assert.Equal(t, map[string]string{"a": "x=y"}, parseText(fields, `a='x=y'`))
	assert.Empty(t, parseText(fields, ""))
}

func TestParseIntent(t *testing.T) {
	assert.Equal(t, intent{Kind: intentAsk, Text: "<@U1> hello"}, parseIntent("<@U1> hello"))
	assert.Equal(t, intent{Kind: intentAsk, Text: "<@U1>"}, parseIntent("<@U1>"))
	assert.Equal(t, intent{Kind: intentGuide}, parseIntent("/help"))
	assert.Equal(t, intent{Kind: intentGuide}, parseIntent(" /"))
	assert.Equal(t, intent{Kind: intentHandler, Handler: "set_model", Args: "model=a"}, parseIntent(" /set_model  model=a"))
	assert.Equal(t, intent{Kind: intentHandler, Handler: "stop"}, parseIntent("/stop"))
}

func TestStringConstraintsDoNotChooseControls(t *testing.T) {
	fields, err := planFields(`{"properties":{"short":{"type":"string","maxLength":8},"long":{"type":"string","maxLength":300},"free":{"type":"string"}}}`)
	require.NoError(t, err)
	for _, f := range fields {
		assert.Equal(t, protocol.KindString, f.Kind)
		assert.Empty(t, f.Label)
	}
	assert.Equal(t, 8, *fields[0].MaxLength)
	assert.Equal(t, 300, *fields[1].MaxLength)
	assert.Nil(t, fields[2].MaxLength)
}
