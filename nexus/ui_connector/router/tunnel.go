package router

import (
	"encoding/json"
	"fmt"
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

const (
	TunnelWorkflowName         = "UIAgentTunnelWorkflow"
	RegisterSubscriberUpdate   = "registerSubscriber"
	ReadEventsUpdate           = "readEvents"
	UnregisterSubscriberSignal = "unregisterSubscriber"
	StopTunnelSignal           = "stopTunnel"
)

const maxBufferedEvents = 4096

// SubscriberMode controls which mutations a mounted UI may make. Rendering is never
// performed here: every subscriber receives the same lossless A2A stream.
type SubscriberMode string

const (
	Observer    SubscriberMode = "observer"
	TurnOwner   SubscriberMode = "turn-owner"
	Participant SubscriberMode = "participant"
)

// StreamItem is the transport-neutral unit retained by the tunnel. Data is the base64
// encoded, serialized A2A StreamResponse returned by SubscribeToTask. The router never
// projects it into a UI-specific event vocabulary.
type StreamItem struct {
	Offset int64  `json:"offset"`
	Data   string `json:"data"`
}

type StreamPage struct {
	Items        []StreamItem `json:"items"`
	NextCursor   int64        `json:"nextCursor"`
	MoreReady    bool         `json:"moreReady"`
	Closed       bool         `json:"closed"`
	TurnComplete bool         `json:"turnComplete"`
}

type TunnelInput struct {
	SessionID     string          `json:"sessionId"`
	NexusEndpoint string          `json:"nexusEndpoint"`
	TurnNumber    int64           `json:"turnNumber"`
	FromOffset    int64           `json:"fromOffset"`
	KnownComplete bool            `json:"knownComplete"`
	Resume        *TunnelSnapshot `json:"resume,omitempty"`
}

// TunnelSnapshot contains only routing state. Stream records are intentionally not
// copied into a new run: A2A remains the durable source and a subscriber that is
// behind replays from its own cursor.
type TunnelSnapshot struct {
	Cursor       int64                `json:"cursor"`
	TurnComplete bool                 `json:"turnComplete"`
	Subscribers  []SubscriberSnapshot `json:"subscribers,omitempty"`
}

type SubscriberSnapshot struct {
	Subscriber Subscriber      `json:"subscriber"`
	State      json.RawMessage `json:"state,omitempty"`
}

type DeliveryTarget struct {
	Activity  string          `json:"activity"`
	TaskQueue string          `json:"taskQueue"`
	Context   json.RawMessage `json:"context,omitempty"`
}

type Subscriber struct {
	ID       string          `json:"id"`
	Mode     SubscriberMode  `json:"mode"`
	Cursor   int64           `json:"cursor"`
	Delivery *DeliveryTarget `json:"delivery,omitempty"`
}

type RegisterSubscriberInput struct {
	Subscriber Subscriber `json:"subscriber"`
}

type RegisterSubscriberOutput struct {
	Cursor int64 `json:"cursor"`
}

type ReadEventsInput struct {
	SubscriberID string  `json:"subscriberId"`
	Cursor       int64   `json:"cursor"`
	MaximumItems int     `json:"maximumItems"`
	WaitSeconds  float64 `json:"waitSeconds"`
}

type ReadEventsOutput struct {
	Items      []StreamItem `json:"items"`
	NextCursor int64        `json:"nextCursor"`
	Closed     bool         `json:"closed"`
	Replayed   bool         `json:"replayed"`
}

type SendMessageInput struct {
	SubscriberID string         `json:"subscriberId"`
	MessageType  string         `json:"messageType"`
	Payload      map[string]any `json:"payload"`
	ExpectedTurn int64          `json:"expectedTurn"`
	Metadata     map[string]any `json:"metadata,omitempty"`
}

type SendAndMountInput struct {
	Subscriber Subscriber       `json:"subscriber"`
	Message    SendMessageInput `json:"message"`
}

type TurnAccepted struct {
	TurnNumber       int64  `json:"turnNumber"`
	TurnID           string `json:"turnId"`
	StreamHeadOffset int64  `json:"streamHeadOffset"`
	Pending          bool   `json:"pending"`
	// Reply is populated for a synchronous harness command. Ordinary A2A turns
	// leave it empty and deliver their rich stream through the subscriber.
	Reply string `json:"reply,omitempty"`
}

type ControlInput struct {
	Kind     string          `json:"kind"`
	Payload  json.RawMessage `json:"payload"`
	Delivery *DeliveryTarget `json:"delivery,omitempty"`
}

type ControlOutput struct {
	Accepted bool            `json:"accepted"`
	Payload  json.RawMessage `json:"payload,omitempty"`
}

type ControlDeliveryInput struct {
	Context json.RawMessage `json:"context,omitempty"`
	Result  ControlOutput   `json:"result"`
}

type DeliveryInput struct {
	SubscriberID string          `json:"subscriberId"`
	SessionID    string          `json:"sessionId"`
	Context      json.RawMessage `json:"context,omitempty"`
	State        json.RawMessage `json:"state,omitempty"`
	Items        []StreamItem    `json:"items"`
	NextCursor   int64           `json:"nextCursor"`
	Closed       bool            `json:"closed"`
}

type DeliveryOutput struct {
	State        json.RawMessage `json:"state,omitempty"`
	TurnComplete bool            `json:"turnComplete"`
	// TaskQueue lets a driver pin later delivery to the process that created a
	// platform-native stream (Teams uses this for its in-memory stream handle).
	TaskQueue string `json:"taskQueue,omitempty"`
}

// AgentBackend is the only backend port in the connector. A2ABackend implements it via
// Nexus; tests can supply an in-memory implementation without weakening the wire model.
type AgentBackend interface {
	Poll(workflow.Context, TunnelInput, int64, float64) (StreamPage, error)
}

type subscriberState struct {
	Subscriber
	State   json.RawMessage
	running bool
}

// TunnelWorkflow is one bounded workflow per agent turn. It performs one common A2A
// poll and fans the untouched records out to independently paced subscribers, then
// closes after that turn's terminal record has been delivered.
type TunnelWorkflow struct {
	backend      AgentBackend
	input        TunnelInput
	items        []StreamItem
	cursor       int64
	closed       bool
	turnComplete bool
	stopped      bool
	rotating     bool
	subs         map[string]*subscriberState
	subOrder     []string
}

func NewTunnelWorkflow(backend AgentBackend) *TunnelWorkflow {
	return &TunnelWorkflow{backend: backend}
}

func TunnelWorkflowID(sessionID string, turnNumber int64) string {
	return fmt.Sprintf("ui-tunnel-%s-turn-%d", sessionID, turnNumber)
}

func (w *TunnelWorkflow) Run(ctx workflow.Context, input TunnelInput) error {
	execution := &TunnelWorkflow{backend: w.backend}
	return execution.run(ctx, input)
}

// run owns all mutable state for one workflow execution. The Worker registers a
// bound Run method so the backend can be injected, but that receiver is shared by
// every execution handled by the process and must remain immutable.
func (w *TunnelWorkflow) run(ctx workflow.Context, input TunnelInput) error {
	w.input = input
	w.subs = map[string]*subscriberState{}
	w.cursor = input.FromOffset
	w.turnComplete = input.KnownComplete
	if input.Resume != nil {
		w.cursor = input.Resume.Cursor
		w.turnComplete = input.Resume.TurnComplete
		for _, saved := range input.Resume.Subscribers {
			sub := saved.Subscriber
			w.subs[sub.ID] = &subscriberState{Subscriber: sub, State: saved.State}
			w.subOrder = append(w.subOrder, sub.ID)
		}
		w.input.Resume = nil
	}
	if err := workflow.SetUpdateHandler(ctx, RegisterSubscriberUpdate, w.registerSubscriber(ctx)); err != nil {
		return err
	}
	if err := workflow.SetUpdateHandler(ctx, ReadEventsUpdate, w.readEvents()); err != nil {
		return err
	}
	unregister := workflow.GetSignalChannel(ctx, UnregisterSubscriberSignal)
	stop := workflow.GetSignalChannel(ctx, StopTunnelSignal)
	workflow.Go(ctx, func(ctx workflow.Context) {
		for {
			selector := workflow.NewSelector(ctx)
			selector.AddReceive(unregister, func(channel workflow.ReceiveChannel, _ bool) {
				var id string
				channel.Receive(ctx, &id)
				delete(w.subs, id)
				w.removeSubscriberID(id)
			})
			selector.AddReceive(stop, func(channel workflow.ReceiveChannel, _ bool) {
				var ignored struct{}
				channel.Receive(ctx, &ignored)
				w.stopped = true
			})
			selector.Select(ctx)
			if w.stopped {
				return
			}
		}
	})

	for _, id := range w.subOrder {
		w.startDelivery(ctx, w.subs[id])
	}

	if err := workflow.Await(ctx, func() bool { return len(w.subs) > 0 || w.stopped }); err != nil || w.stopped {
		return nil
	}
	for !w.closed && !w.stopped && !w.rotating {
		if len(w.subs) == 0 {
			return nil
		}
		pollTimeout := 30.0
		if w.turnComplete {
			pollTimeout = .1
		}
		page, err := w.backend.Poll(ctx, w.input, w.cursor, pollTimeout)
		if err != nil {
			workflow.GetLogger(ctx).Warn("A2A SubscribeToTask failed", "error", err)
			if err := workflow.Sleep(ctx, time.Second); err != nil {
				return nil
			}
			continue
		}
		w.append(page.Items)
		w.cursor = page.NextCursor
		w.turnComplete = w.turnComplete || page.TurnComplete
		w.closed = page.Closed || (w.turnComplete && !page.MoreReady)
		if workflow.GetInfo(ctx).GetContinueAsNewSuggested() {
			w.rotating = true
		}
	}
	if w.rotating {
		if err := workflow.Await(ctx, func() bool {
			return workflow.AllHandlersFinished(ctx) && !w.deliveryRunning()
		}); err != nil {
			return err
		}
		next := w.input
		next.Resume = w.snapshot()
		return workflow.NewContinueAsNewError(ctx, w.Run, next)
	}

	// Pull subscribers receive the terminal page through an update and unregister;
	// push subscribers remove themselves after their final delivery. Do not retain a
	// second copy of agent history indefinitely if a client disappears mid-drain.
	_, _ = workflow.AwaitWithTimeout(ctx, 30*time.Second, func() bool {
		return len(w.subs) == 0 && workflow.AllHandlersFinished(ctx) && !w.deliveryRunning()
	})
	return nil
}

func (w *TunnelWorkflow) registerSubscriber(ctx workflow.Context) func(workflow.Context, RegisterSubscriberInput) (RegisterSubscriberOutput, error) {
	return func(_ workflow.Context, input RegisterSubscriberInput) (RegisterSubscriberOutput, error) {
		sub := input.Subscriber
		if sub.ID == "" {
			return RegisterSubscriberOutput{}, temporal.NewNonRetryableApplicationError("subscriber id is required", "InvalidSubscriber", nil)
		}
		if sub.Mode != Observer && sub.Mode != TurnOwner && sub.Mode != Participant {
			return RegisterSubscriberOutput{}, temporal.NewNonRetryableApplicationError("invalid subscriber mode", "InvalidSubscriber", nil)
		}
		if existing := w.subs[sub.ID]; existing != nil {
			existing.Mode = sub.Mode
			existing.Delivery = sub.Delivery
			if sub.Cursor > existing.Cursor {
				existing.Cursor = sub.Cursor
			}
			w.startDelivery(ctx, existing)
			return RegisterSubscriberOutput{Cursor: existing.Cursor}, nil
		}
		state := &subscriberState{Subscriber: sub}
		w.subs[sub.ID] = state
		w.subOrder = append(w.subOrder, sub.ID)
		w.startDelivery(ctx, state)
		return RegisterSubscriberOutput{Cursor: sub.Cursor}, nil
	}
}

func (w *TunnelWorkflow) readEvents() func(workflow.Context, ReadEventsInput) (ReadEventsOutput, error) {
	return func(handlerCtx workflow.Context, input ReadEventsInput) (ReadEventsOutput, error) {
		sub := w.subs[input.SubscriberID]
		if sub == nil {
			return ReadEventsOutput{}, temporal.NewNonRetryableApplicationError("unknown subscriber", "UnknownSubscriber", nil)
		}
		maximum := input.MaximumItems
		if maximum <= 0 || maximum > 256 {
			maximum = 256
		}
		wait := input.WaitSeconds
		if wait <= 0 || wait > 30 {
			wait = 20
		}

		if input.Cursor < w.cursor && (len(w.items) == 0 || input.Cursor < w.items[0].Offset) {
			page, err := w.backend.Poll(handlerCtx, w.input, input.Cursor, .1)
			if err != nil {
				return ReadEventsOutput{}, err
			}
			items := limitItems(page.Items, maximum)
			next := input.Cursor
			if len(items) > 0 {
				next = items[len(items)-1].Offset + 1
			} else {
				next = page.NextCursor
			}
			sub.Cursor = next
			closed := (page.Closed || page.TurnComplete) && !page.MoreReady && next >= page.NextCursor
			if closed {
				delete(w.subs, input.SubscriberID)
				w.removeSubscriberID(input.SubscriberID)
			}
			return ReadEventsOutput{Items: items, NextCursor: next, Closed: closed, Replayed: true}, nil
		}

		ready := func() bool { return w.closed || hasItemAt(w.items, input.Cursor) }
		if !ready() {
			_, _ = workflow.AwaitWithTimeout(handlerCtx, time.Duration(wait*float64(time.Second)), ready)
		}
		items := limitItems(itemsAt(w.items, input.Cursor), maximum)
		next := input.Cursor
		if len(items) > 0 {
			next = items[len(items)-1].Offset + 1
		}
		sub.Cursor = next
		closed := w.closed && next >= w.cursor
		if closed {
			delete(w.subs, input.SubscriberID)
			w.removeSubscriberID(input.SubscriberID)
		}
		return ReadEventsOutput{Items: items, NextCursor: next, Closed: closed}, nil
	}
}

func (w *TunnelWorkflow) startDelivery(ctx workflow.Context, sub *subscriberState) {
	if sub == nil || w.rotating || sub.Delivery == nil || sub.running {
		return
	}
	sub.running = true
	workflow.Go(ctx, func(ctx workflow.Context) {
		defer func() { sub.running = false }()
		for !w.rotating && w.subs[sub.ID] == sub {
			// A delivery activity may be slow while the shared poller continues. If
			// its cursor fell out of the in-workflow window, replay just this
			// subscriber from the agent's durable A2A stream.
			if sub.Cursor < w.cursor && (len(w.items) == 0 || sub.Cursor < w.items[0].Offset) {
				page, err := w.backend.Poll(ctx, w.input, sub.Cursor, .1)
				if err != nil {
					workflow.GetLogger(ctx).Warn("subscriber replay failed", "subscriber", sub.ID, "error", err)
					return
				}
				if len(page.Items) > 0 {
					items := limitItems(page.Items, 256)
					next := items[len(items)-1].Offset + 1
					terminal := (page.Closed || page.TurnComplete) && !page.MoreReady && next >= page.NextCursor
					if !w.deliver(ctx, sub, items, terminal) {
						return
					}
					if terminal {
						delete(w.subs, sub.ID)
						w.removeSubscriberID(sub.ID)
						return
					}
					continue
				}
				sub.Cursor = page.NextCursor
			}
			if len(w.items) == 0 || sub.Cursor >= w.cursor {
				if w.closed {
					return
				}
				if err := workflow.Await(ctx, func() bool {
					return w.rotating || w.subs[sub.ID] != sub || w.closed || hasItemAt(w.items, sub.Cursor)
				}); err != nil || w.subs[sub.ID] != sub {
					return
				}
				if w.rotating {
					return
				}
			}
			items := limitItems(itemsAt(w.items, sub.Cursor), 256)
			if len(items) == 0 {
				continue
			}
			next := items[len(items)-1].Offset + 1
			terminal := w.closed && next >= w.cursor
			if !w.deliver(ctx, sub, items, terminal) {
				return
			}
			if terminal {
				delete(w.subs, sub.ID)
				w.removeSubscriberID(sub.ID)
				return
			}
		}
	})
}

func (w *TunnelWorkflow) deliveryRunning() bool {
	for _, id := range w.subOrder {
		sub := w.subs[id]
		if sub.running {
			return true
		}
	}
	return false
}

func (w *TunnelWorkflow) snapshot() *TunnelSnapshot {
	snapshot := &TunnelSnapshot{Cursor: w.cursor, TurnComplete: w.turnComplete}
	for _, id := range w.subOrder {
		sub := w.subs[id]
		snapshot.Subscribers = append(snapshot.Subscribers, SubscriberSnapshot{
			Subscriber: sub.Subscriber,
			State:      sub.State,
		})
	}
	return snapshot
}

func (w *TunnelWorkflow) removeSubscriberID(id string) {
	for index, current := range w.subOrder {
		if current == id {
			w.subOrder = append(w.subOrder[:index], w.subOrder[index+1:]...)
			return
		}
	}
}

func (w *TunnelWorkflow) deliver(ctx workflow.Context, sub *subscriberState, items []StreamItem, closed bool) bool {
	if len(items) == 0 || sub.Delivery == nil {
		return true
	}
	next := items[len(items)-1].Offset + 1
	target := sub.Delivery
	activityCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		TaskQueue:           target.TaskQueue,
		StartToCloseTimeout: 5 * time.Minute,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 3},
	})
	var output DeliveryOutput
	err := workflow.ExecuteActivity(activityCtx, target.Activity, DeliveryInput{
		SubscriberID: sub.ID,
		SessionID:    w.input.SessionID,
		Context:      target.Context,
		State:        sub.State,
		Items:        items,
		NextCursor:   next,
		Closed:       closed,
	}).Get(ctx, &output)
	if err != nil {
		workflow.GetLogger(ctx).Warn("subscriber delivery failed", "subscriber", sub.ID, "error", err)
		return false
	}
	sub.State = output.State
	sub.Cursor = next
	if output.TaskQueue != "" {
		target.TaskQueue = output.TaskQueue
	}
	return true
}

func (w *TunnelWorkflow) append(items []StreamItem) {
	if len(items) == 0 {
		return
	}
	w.items = append(w.items, items...)
	if len(w.items) > maxBufferedEvents {
		w.items = w.items[len(w.items)-maxBufferedEvents:]
	}
}

func hasItemAt(items []StreamItem, cursor int64) bool {
	for _, item := range items {
		if item.Offset >= cursor {
			return true
		}
	}
	return false
}

func itemsAt(items []StreamItem, cursor int64) []StreamItem {
	for i, item := range items {
		if item.Offset >= cursor {
			return items[i:]
		}
	}
	return nil
}

func limitItems(items []StreamItem, maximum int) []StreamItem {
	if len(items) <= maximum {
		return items
	}
	return items[:maximum]
}

func (m SubscriberMode) Validate() error {
	if m != Observer && m != TurnOwner && m != Participant {
		return fmt.Errorf("unknown subscriber mode %q", m)
	}
	return nil
}
