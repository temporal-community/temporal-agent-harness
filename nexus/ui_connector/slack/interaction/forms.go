package interaction

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	slackapi "github.com/slack-go/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/presentation"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

const (
	modalTitleMax      = 24
	placeholderMax     = 150
	optionTextMax      = 75
	buttonValueMax     = 2000
	privateMetadataMax = 3000
	radioMaxOptions    = 4
	// Strings with no maximum length or a large maximum use a text area.
	multilineAbove = 200
)

// FormButtonValue identifies a handler. Inline fields let the webhook open its form.
// The fields are a snapshot taken when the guide is posted.
type FormButtonValue struct {
	SessionID string      `json:"s"`
	Handler   string      `json:"h"`
	Inline    bool        `json:"i,omitempty"`
	Fields    []formField `json:"f,omitempty"`
}

// FormMetadata identifies the handler and thread. Fields validate submitted values.
type FormMetadata struct {
	SessionID string      `json:"s"`
	ThreadID  string      `json:"th"`
	Handler   string      `json:"h"`
	Fields    []formField `json:"f,omitempty"`
}

// formField is a protocol.Field in compact JSON, sized for Slack's value limits.
type formField struct {
	Name        string          `json:"n"`
	Kind        protocol.Kind   `json:"k"`
	Label       string          `json:"l,omitempty"`
	Description string          `json:"ds,omitempty"`
	Required    bool            `json:"r,omitempty"`
	Options     []string        `json:"o,omitempty"`
	MaxLength   *int            `json:"ml,omitempty"`
	JSONReason  string          `json:"jr,omitempty"`
	Min         *float64        `json:"mn,omitempty"`
	Max         *float64        `json:"mx,omitempty"`
	Default     json.RawMessage `json:"d,omitempty"`
}

func compactFields(fields []protocol.Field, withDescriptions bool) []formField {
	out := make([]formField, len(fields))
	for i, f := range fields {
		c := formField{
			Name: f.Name, Kind: f.Kind, Label: f.Label, Required: f.Required,
			Options: f.Options, MaxLength: f.MaxLength, JSONReason: f.JSONReason,
			Min: f.Min, Max: f.Max, Default: f.Default,
		}
		if withDescriptions {
			c.Description = presentation.Clip(f.Description, placeholderMax)
		}
		out[i] = c
	}
	return out
}

func expandFields(compact []formField) []protocol.Field {
	out := make([]protocol.Field, len(compact))
	for i, c := range compact {
		if c.Kind == "text" {
			c.Kind = protocol.KindString
		}
		out[i] = protocol.Field{
			Name: c.Name, Kind: c.Kind, Label: c.Label, Description: c.Description,
			Required: c.Required, Options: c.Options, MaxLength: c.MaxLength, JSONReason: c.JSONReason,
			Min: c.Min, Max: c.Max, Default: c.Default,
		}
	}
	return out
}

// EncodeFormButton encodes a guide button. It carries the handler's fields when they
// fit, first with descriptions, then without; otherwise only the handler name.
func EncodeFormButton(sessionID string, h protocol.Handler) string {
	base := FormButtonValue{SessionID: sessionID, Handler: h.Name}
	for _, withDescriptions := range []bool{true, false} {
		v := base
		v.Inline = true
		v.Fields = compactFields(h.Fields, withDescriptions)
		if b, err := json.Marshal(v); err == nil && len(b) <= buttonValueMax {
			return string(b)
		}
	}
	b, _ := json.Marshal(base)
	return string(b)
}

// FormViewFromButton builds the modal from a guide button that carries its fields.
// It returns false when the button carries only the handler name.
func FormViewFromButton(val FormButtonValue, threadID string) (slackapi.ModalViewRequest, bool) {
	if !val.Inline {
		return slackapi.ModalViewRequest{}, false
	}
	return BuildFormView(FormMetadata{SessionID: val.SessionID, ThreadID: threadID, Handler: val.Handler}, "", expandFields(val.Fields)), true
}

// BuildFormView builds the handler's modal. The fields go into private_metadata when
// they fit, so the submission is decoded against them.
func BuildFormView(meta FormMetadata, description string, fields []protocol.Field) slackapi.ModalViewRequest {
	meta.Fields = compactFields(fields, false)
	encoded, _ := json.Marshal(meta)
	if len(encoded) > privateMetadataMax {
		meta.Fields = nil
		encoded, _ = json.Marshal(meta)
	}

	var blocks []slackapi.Block
	if description != "" {
		blocks = append(blocks, Section(description))
	}
	for _, f := range fields {
		if len(blocks) >= MaxBlocks {
			break
		}
		blocks = append(blocks, inputBlock(f))
	}
	if len(fields) == 0 {
		blocks = append(blocks, Section(fmt.Sprintf("`%s` takes no input. Send runs it.", meta.Handler)))
	}
	return slackapi.ModalViewRequest{
		Type:            slackapi.VTModal,
		Title:           PlainText(meta.Handler, modalTitleMax),
		Submit:          PlainText("Send", 24),
		Close:           PlainText("Cancel", 24),
		CallbackID:      FormCallbackID,
		PrivateMetadata: string(encoded),
		Blocks:          slackapi.Blocks{BlockSet: blocks},
	}
}

func inputBlock(f protocol.Field) *slackapi.InputBlock {
	var hint *slackapi.TextBlockObject
	if f.Description != "" {
		hint = PlainText(f.Description, 2000)
	}
	placeholder := func() *slackapi.TextBlockObject {
		if f.Description == "" {
			return nil
		}
		return PlainText(f.Description, placeholderMax)
	}
	// Include the kind so submissions can be decoded without a schema query.
	actionID := "field:" + string(f.Kind)
	var element slackapi.BlockElement

	switch f.Kind {
	case protocol.KindEnum, protocol.KindBoolean:
		values, labels := f.Options, make([]string, len(f.Options))
		for i, v := range values {
			labels[i] = presentation.Humanize(v)
		}
		if f.Kind == protocol.KindBoolean {
			values, labels = []string{"true", "false"}, []string{"Yes", "No"}
		}
		var initial *slackapi.OptionBlockObject
		options := make([]*slackapi.OptionBlockObject, len(values))
		defaultValue := defaultString(f.Default)
		for i, v := range values {
			options[i] = slackapi.NewOptionBlockObject(v, PlainText(labels[i], optionTextMax), nil)
			if v == defaultValue {
				initial = options[i]
			}
		}
		if len(options) <= radioMaxOptions {
			radio := slackapi.NewRadioButtonsBlockElement(actionID, options...)
			radio.InitialOption = initial
			element = radio
		} else {
			sel := slackapi.NewOptionsSelectBlockElement(slackapi.OptTypeStatic, PlainText("Choose…", placeholderMax), actionID, options...)
			if initial != nil {
				sel = sel.WithInitialOption(initial)
			}
			element = sel
		}
	case protocol.KindInteger, protocol.KindNumber:
		n := slackapi.NewNumberInputBlockElement(placeholder(), actionID, f.Kind == protocol.KindNumber)
		if f.Min != nil {
			n.MinValue = strconv.FormatFloat(*f.Min, 'f', -1, 64)
		}
		if f.Max != nil {
			n.MaxValue = strconv.FormatFloat(*f.Max, 'f', -1, 64)
		}
		n.InitialValue = defaultString(f.Default)
		element = n
	case protocol.KindDate:
		d := slackapi.NewDatePickerBlockElement(actionID)
		d.InitialDate = defaultString(f.Default)
		element = d
	default:
		t := slackapi.NewPlainTextInputBlockElement(placeholder(), actionID)
		t.Multiline = f.Kind == protocol.KindJSON || f.MaxLength == nil || *f.MaxLength > multilineAbove
		if f.Kind == protocol.KindJSON {
			t.Placeholder = PlainText("JSON value — "+f.JSONReason, placeholderMax)
			if len(f.Default) > 0 {
				t.InitialValue = string(f.Default)
			}
		} else {
			t.InitialValue = defaultString(f.Default)
		}
		element = t
	}

	block := slackapi.NewInputBlock(f.Name, PlainText(presentation.FieldLabel(f), 2000), hint, element)
	block.Optional = !f.Required
	return block
}

// defaultString converts a scalar JSON default to input text.
func defaultString(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil || v == nil {
		return ""
	}
	switch t := v.(type) {
	case string:
		return t
	case bool:
		return strconv.FormatBool(t)
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	}
	return ""
}

// DecodeFormSubmission returns modal metadata, a JSON payload, and errors by field ID.
// Fields in private_metadata validate the values. If absent, action_id supplies
// the value type and Slack checks required fields, options, and number bounds.
func DecodeFormSubmission(view slackapi.View) (FormMetadata, string, map[string]string, error) {
	var meta FormMetadata
	if err := json.Unmarshal([]byte(view.PrivateMetadata), &meta); err != nil {
		return meta, "", nil, fmt.Errorf("decode private_metadata: %w", err)
	}
	if meta.SessionID == "" || meta.Handler == "" {
		return meta, "", nil, fmt.Errorf("private_metadata is missing the session or handler")
	}

	var fields []protocol.Field
	values := map[string]string{}
	if view.State != nil {
		for blockID, actions := range view.State.Values {
			for actionID, action := range actions {
				kind, ok := strings.CutPrefix(actionID, "field:")
				if !ok {
					continue
				}
				if meta.Fields == nil {
					k := protocol.Kind(kind)
					if k == "text" {
						k = protocol.KindString
					}
					if k == protocol.KindEnum {
						// No option list to check against; Slack only returns offered options.
						k = protocol.KindString
					}
					fields = append(fields, protocol.Field{Name: blockID, Kind: k})
				}
				switch {
				case action.SelectedOption.Value != "":
					values[blockID] = action.SelectedOption.Value
				case action.SelectedDate != "":
					values[blockID] = action.SelectedDate
				default:
					values[blockID] = action.Value
				}
			}
		}
	}
	if meta.Fields != nil {
		fields = expandFields(meta.Fields)
	}
	payload, errs := protocol.Decode(fields, values)
	if len(errs) > 0 {
		return meta, "", errs, nil
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return meta, "", nil, err
	}
	return meta, string(body), nil, nil
}
