package linearoutbound

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/linear"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/markdown"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
)

type LinearPlatform struct {
	client *Client
}

func NewLinearPlatform(client *Client) *LinearPlatform {
	return &LinearPlatform{client: client}
}

type deliveryState struct {
	Text      string              `json:",omitempty"`
	Citations []protocol.Citation `json:",omitempty"`
	Output    string              `json:",omitempty"`
	Messages  []string            `json:",omitempty"`
	Error     string              `json:",omitempty"`
	Started   bool                `json:",omitempty"`
}

func (p *LinearPlatform) Deliver(ctx context.Context, in DeliverInput) (json.RawMessage, error) {
	var addr linear.Address
	if err := json.Unmarshal(in.Address, &addr); err != nil {
		return nil, fmt.Errorf("decode address: %w", err)
	}
	if addr.AgentSessionID == "" || addr.OrganizationID == "" {
		return nil, errors.New("Linear agent session and organization IDs are required")
	}
	var st deliveryState
	if !router.Empty(in.State) {
		if err := json.Unmarshal(in.State, &st); err != nil {
			return nil, fmt.Errorf("decode state: %w", err)
		}
	}
	started := false
	for _, ev := range in.Events {
		switch {
		case ev.TurnStarted:
			started = true
		case ev.Chunk != nil:
			st.Text += ev.Chunk.Text
			st.Citations = append(st.Citations, ev.Chunk.Citations...)
		case ev.HandlerOutput != nil:
			st.Output = markdown.HandlerOutput(ev.HandlerOutput.Value)
		case ev.Error != "":
			st.Error = ev.Error
		case ev.Notice != "":
			st.Messages = append(st.Messages, "ℹ️ **System message:** "+ev.Notice)
		case ev.Guide != nil:
			st.Messages = append(st.Messages, markdown.Guide(ev.Guide.Handlers))
		case ev.Form != nil:
			st.Messages = append(st.Messages, markdown.GuideEntry(*ev.Form))
		case ev.HandlerHelp != nil:
			st.Messages = append(st.Messages, markdown.HandlerHelp(*ev.HandlerHelp))
		}
	}
	if in.End {
		body, kind := st.reply(), activityTypeResponse
		if st.Error != "" {
			body, kind = st.Error, activityTypeError
		} else if strings.TrimSpace(body) == "" && (st.Started || started) {
			body = "_Done._"
		}
		if strings.TrimSpace(body) != "" {
			if err := p.client.createActivity(ctx, addr.AgentSessionID, activityContent{Type: kind, Body: body}, false); err != nil {
				return nil, err
			}
		}
		return nil, nil
	}
	if started && !st.Started {
		st.Started = true
		if err := p.client.createActivity(ctx, addr.AgentSessionID,
			activityContent{Type: activityTypeThought, Body: "Working on your request."}, true); err != nil {
			log.Printf("Linear acknowledgement failed: %v", err)
		}
	}
	return json.Marshal(st)
}

func (s deliveryState) reply() string {
	text := s.Text
	if text == "" {
		text = s.Output
	}
	text = markdown.Splice(text, s.Citations, markdown.CommonMarkLink)
	parts := append([]string(nil), s.Messages...)
	if text != "" {
		parts = append(parts, text)
	}
	return strings.Join(parts, "\n\n")
}
