// Package debuguioutbound implements router.OutboundDriver for the debug UI: rendering
// here means forwarding the backend's own event shape to the browser almost unchanged,
// since the debug UI (unlike Slack/Teams) can render full fidelity, not flattened text.
package debuguioutbound

import (
	"context"
	"sync"
)

// Frame is one server-sent event, ready to write to an open SSE connection: "event:
// Event\ndata: Data\n\n". Event/Data are pre-serialized here (not left as a Go struct)
// because they're produced inside a Temporal Activity and consumed by a plain HTTP
// handler in the same process - a single, already-encoded shape avoids re-deriving the
// SSE wire format in two places.
type Frame struct {
	Event string
	Data  []byte
}

// Broker is an in-memory, per-session, single-process pub/sub fanning Frames out to every
// browser SSE connection currently attached to a session. Deliberately in-memory: this
// mirrors how the debug UI's own direct-Temporal /api/attach already worked (per-process,
// no cross-replica delivery) and keeps a debugging tool's infra footprint minimal. A
// multi-replica deployment would need a real pub/sub (Redis, NATS, ...) here instead.
type Broker struct {
	mu          sync.Mutex
	subscribers map[string]map[chan Frame]struct{}
}

// NewBroker returns an empty Broker, ready to use.
func NewBroker() *Broker {
	return &Broker{subscribers: make(map[string]map[chan Frame]struct{})}
}

// Publish fans frame out to every current subscriber of sessionID. Non-blocking: a slow
// or gone subscriber never blocks the Activity call that's publishing (their channel is
// buffered; see Subscribe), so one stalled browser tab can't stall event delivery to
// others or make the underlying Temporal Activity time out.
func (b *Broker) Publish(sessionID string, frame Frame) {
	b.mu.Lock()
	subs := make([]chan Frame, 0, len(b.subscribers[sessionID]))
	for ch := range b.subscribers[sessionID] {
		subs = append(subs, ch)
	}
	b.mu.Unlock()

	for _, ch := range subs {
		select {
		case ch <- frame:
		default:
			// Subscriber's buffer is full - drop rather than block. A debugging UI that
			// falls behind should reconnect (via from_offset) rather than back-pressure
			// event delivery for every other subscriber of this session.
		}
	}
}

// Subscribe registers a new listener for sessionID's events. The caller must call the
// returned unsubscribe func when done (e.g. when the HTTP request ends).
func (b *Broker) Subscribe(sessionID string) (ch chan Frame, unsubscribe func()) {
	ch = make(chan Frame, 64)

	b.mu.Lock()
	if b.subscribers[sessionID] == nil {
		b.subscribers[sessionID] = make(map[chan Frame]struct{})
	}
	b.subscribers[sessionID][ch] = struct{}{}
	b.mu.Unlock()

	return ch, func() {
		b.mu.Lock()
		delete(b.subscribers[sessionID], ch)
		if len(b.subscribers[sessionID]) == 0 {
			delete(b.subscribers, sessionID)
		}
		b.mu.Unlock()
	}
}

// PublishActivityName is the Activity name BrokerActivities.Publish is registered under.
const PublishActivityName = "DebugUIPublishEvent"

// PublishInput is BrokerActivities.Publish's argument.
type PublishInput struct {
	SessionID string
	Frame     Frame
}

// BrokerActivities bridges workflow code to Broker. Publishing is a side effect (it
// mutates channel state shared with the HTTP server's SSE handlers), so it must run as
// an Activity even though - because everything here lives in one process - it's a plain
// in-memory call rather than network I/O.
type BrokerActivities struct {
	Broker *Broker
}

// Publish is registered on the worker under PublishActivityName.
func (a *BrokerActivities) Publish(_ context.Context, input PublishInput) error {
	a.Broker.Publish(input.SessionID, input.Frame)
	return nil
}
