package interaction

import (
	"encoding/json"
	"fmt"
	slackapi "github.com/slack-go/slack"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"strings"
	"testing"
)

func TestFormButtonValue(t *testing.T) {
	var options []string
	for i := range 300 {
		options = append(options, fmt.Sprintf("option_%d", i))
	}
	big := protocol.Handler{Name: "big", Fields: []protocol.Field{{Name: "pick", Kind: protocol.KindEnum, Options: options}}}
	value := EncodeFormButton("s", big)
	assert.JSONEq(t, `{"s":"s","h":"big"}`, value, "too big: only the handler name")
	var val FormButtonValue
	require.NoError(t, json.Unmarshal([]byte(value), &val))
	_, ok := FormViewFromButton(val, "1.0")
	assert.False(t, ok)

	long := strings.Repeat("d", 140)
	var fields []protocol.Field
	for i := range 14 {
		fields = append(fields, protocol.Field{Name: fmt.Sprintf("f%d", i), Label: "F", Kind: protocol.KindString, Description: long})
	}
	value = EncodeFormButton("s", protocol.Handler{Name: "h", Fields: fields})
	assert.LessOrEqual(t, len(value), buttonValueMax)
	assert.NotContains(t, value, long, "descriptions go first")
	assert.Contains(t, value, `"i":true`)
}

func TestFormView(t *testing.T) {
	one := 1.0
	fields := []protocol.Field{
		{Name: "policy", Label: "Policy", Kind: protocol.KindEnum, Required: true,
			Options: []string{"allow_safe", "strict", "dangerously_skip_all"},
			Default: json.RawMessage(`"strict"`)},
		{Name: "model", Label: "Model", Kind: protocol.KindEnum, Options: []string{"a", "b", "c", "d", "e"}},
		{Name: "count", Label: "Count", Kind: protocol.KindInteger, Required: true, Min: &one},
		{Name: "verbose", Label: "Verbose", Kind: protocol.KindBoolean},
		{Name: "note", Label: "Note", Kind: protocol.KindString, Description: "Optional note"},
		{Name: "tags", Label: "Tags", Kind: protocol.KindJSON, JSONReason: "list"},
	}
	view := BuildFormView(FormMetadata{SessionID: "slack:C1:1.0", ThreadID: "1.0", Handler: "set_approval_policy_with_a_long_name"}, "", fields)
	assert.Equal(t, FormCallbackID, view.CallbackID)
	assert.Len(t, []rune(view.Title.Text), modalTitleMax)

	var meta FormMetadata
	require.NoError(t, json.Unmarshal([]byte(view.PrivateMetadata), &meta))
	require.Len(t, meta.Fields, 6, "the field list rides along for decoding")

	input := func(i int) *slackapi.InputBlock { return view.Blocks.BlockSet[i].(*slackapi.InputBlock) }
	policy := input(0).Element.(*slackapi.RadioButtonsBlockElement)
	assert.Equal(t, "Dangerously Skip All", policy.Options[2].Text.Text)
	assert.Equal(t, "strict", policy.InitialOption.Value)
	_, isSelect := input(1).Element.(*slackapi.SelectBlockElement)
	assert.True(t, isSelect, "more than four options use a dropdown")
	assert.Equal(t, "1", input(2).Element.(*slackapi.NumberInputBlockElement).MinValue)
	assert.Equal(t, "Yes", input(3).Element.(*slackapi.RadioButtonsBlockElement).Options[0].Text.Text)
	assert.True(t, input(4).Optional)
	assert.True(t, input(5).Element.(*slackapi.PlainTextInputBlockElement).Multiline)
}

func TestDecodeFormSubmission(t *testing.T) {
	view := slackapi.View{
		PrivateMetadata: `{"s":"slack:C1:1.0","th":"1.0","h":"configure"}`,
		State: &slackapi.ViewState{Values: map[string]map[string]slackapi.BlockAction{
			"policy":  {"field:enum": {SelectedOption: slackapi.OptionBlockObject{Value: "strict"}}},
			"count":   {"field:integer": {Value: "3"}},
			"verbose": {"field:boolean": {SelectedOption: slackapi.OptionBlockObject{Value: "false"}}},
			"day":     {"field:date": {SelectedDate: "2026-09-28"}},
			"note":    {"field:text": {Value: ""}},
		}},
	}
	meta, payload, errs, err := DecodeFormSubmission(view)
	require.NoError(t, err)
	assert.Empty(t, errs)
	assert.Equal(t, FormMetadata{SessionID: "slack:C1:1.0", ThreadID: "1.0", Handler: "configure"}, meta)
	assert.JSONEq(t, `{"policy":"strict","count":3,"verbose":false,"day":"2026-09-28"}`, payload)

	view.State.Values["tags"] = map[string]slackapi.BlockAction{"field:json": {Value: "[1,"}}
	_, _, errs, err = DecodeFormSubmission(view)
	require.NoError(t, err)
	assert.Contains(t, errs["tags"], "Invalid JSON")

	_, _, _, err = DecodeFormSubmission(slackapi.View{PrivateMetadata: "{}"})
	assert.Error(t, err)
}

func TestDecodeFormSubmission_UsesTheFieldListFromMetadata(t *testing.T) {
	view := slackapi.View{
		PrivateMetadata: `{"s":"slack:C1:1.0","th":"1.0","h":"set_model","f":[{"n":"model","k":"enum","r":true,"o":["a","b"]},{"n":"n","k":"integer","mx":5}]}`,
		State: &slackapi.ViewState{Values: map[string]map[string]slackapi.BlockAction{
			"model": {"field:enum": {SelectedOption: slackapi.OptionBlockObject{Value: "c"}}},
			"n":     {"field:integer": {Value: "9"}},
		}},
	}
	_, _, errs, err := DecodeFormSubmission(view)
	require.NoError(t, err)
	assert.Equal(t, "Must be one of: a, b.", errs["model"])
	assert.Equal(t, "Must be at most 5.", errs["n"])
}

func TestStringControlsComeFromConstraints(t *testing.T) {
	short, long := 8, 201
	fields := []protocol.Field{
		{Name: "short_name", Kind: protocol.KindString, MaxLength: &short},
		{Name: "long_name", Kind: protocol.KindString, MaxLength: &long},
		{Name: "free_text", Kind: protocol.KindString},
		{Name: "tag_list", Kind: protocol.KindJSON, JSONReason: "list"},
	}
	h := protocol.Handler{Name: "configure", Fields: fields}
	var button FormButtonValue
	require.NoError(t, json.Unmarshal([]byte(EncodeFormButton("s", h)), &button))
	inline, ok := FormViewFromButton(button, "thread")
	require.True(t, ok)
	direct := BuildFormView(FormMetadata{SessionID: "s", ThreadID: "thread", Handler: "configure"}, "", fields)
	assert.Equal(t, direct, inline, "inline and workflow forms use the same constraints and labels")
	input := func(i int) *slackapi.InputBlock { return inline.Blocks.BlockSet[i].(*slackapi.InputBlock) }
	assert.Equal(t, "Short Name", input(0).Label.Text)
	assert.False(t, input(0).Element.(*slackapi.PlainTextInputBlockElement).Multiline)
	assert.True(t, input(1).Element.(*slackapi.PlainTextInputBlockElement).Multiline)
	assert.True(t, input(2).Element.(*slackapi.PlainTextInputBlockElement).Multiline)
	assert.Equal(t, "JSON value — list", input(3).Element.(*slackapi.PlainTextInputBlockElement).Placeholder.Text)
	view := slackapi.View{PrivateMetadata: inline.PrivateMetadata,
		State: &slackapi.ViewState{Values: map[string]map[string]slackapi.BlockAction{
			"short_name": {"field:string": {Value: "too-long-name"}},
		}},
	}
	_, _, errs, err := DecodeFormSubmission(view)
	require.NoError(t, err)
	assert.Equal(t, "Must be at most 8 characters.", errs["short_name"])
}
