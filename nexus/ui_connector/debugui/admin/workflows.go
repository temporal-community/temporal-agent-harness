// Package debuguiadmin holds the debug UI's non-turn workflows: session
// lifecycle/discovery/status/interface one-shot mappers, and the whole-session replay
// AttachWorkflow. None of this is RouterWorkflow's job - RouterWorkflow only ever maps
// one Message/Slash/Approval turn between the non-Temporal and Temporal sides. These
// workflows follow the exact same "dumb mapper" shape (one HTTP request in, one Nexus
// call out, nothing else) for requests that aren't turn-shaped at all: listing/describing/
// closing sessions, querying status/interfaces, and tailing a session's full event
// history rather than one turn's stream.
//
// Every one of these reaches the agent exclusively through the AgentService Nexus
// contract, same as RouterWorkflow's agent.Driver - none of it ever touches the agent's
// own Temporal namespace directly.
package debuguiadmin

import (
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent"
	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent/generated"
	debuguioutbound "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/debugui/outbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

// Registered workflow names. The inbound HTTP server starts workflows by these names
// (client.ExecuteWorkflow only ever takes a name, even in-process); Register wires the
// matching Go types to them on the worker side.
const (
	DescribeSessionWorkflowName        = "DebugUIDescribeSession"
	CloseSessionWorkflowName           = "DebugUICloseSession"
	DiscoverSessionsWorkflowName       = "DebugUIDiscoverSessions"
	QueryStatusWorkflowName            = "DebugUIQueryStatus"
	QueryAgentInterfaceWorkflowName    = "DebugUIQueryAgentInterface"
	QueryOperatorInterfaceWorkflowName = "DebugUIQueryOperatorInterface"
	ProvideCallbackResultWorkflowName  = "DebugUIProvideCallbackResult"
	ExecuteOperatorCommandWorkflowName = "DebugUIExecuteOperatorCommand"
	SubmitMessageWorkflowName          = "DebugUISubmitMessage"
	AttachWorkflowName                 = "DebugUIAttach"
)

// Register registers every workflow in this package, plus the broker-publishing Activity
// they (and the outbound driver) depend on, on w. Unlike RouterWorkflow (one Config per
// Slack/Teams deployment, each targeting exactly one agent type), a single debugui
// instance's registry can span several agent types with different Nexus endpoints - so
// Config travels in each workflow's input instead of being fixed at registration time.
func Register(w worker.Worker, broker *debuguioutbound.Broker) {
	describe := &DescribeSessionWorkflow{}
	closeSession := &CloseSessionWorkflow{}
	discover := &DiscoverSessionsWorkflow{}
	status := &QueryStatusWorkflow{}
	agentIface := &QueryAgentInterfaceWorkflow{}
	operatorIface := &QueryOperatorInterfaceWorkflow{}
	callback := &ProvideCallbackResultWorkflow{}
	executeCommand := &ExecuteOperatorCommandWorkflow{}
	submit := &SubmitMessageWorkflow{}
	attach := &AttachWorkflow{}

	w.RegisterWorkflowWithOptions(describe.Run, workflow.RegisterOptions{Name: DescribeSessionWorkflowName})
	w.RegisterWorkflowWithOptions(closeSession.Run, workflow.RegisterOptions{Name: CloseSessionWorkflowName})
	w.RegisterWorkflowWithOptions(discover.Run, workflow.RegisterOptions{Name: DiscoverSessionsWorkflowName})
	w.RegisterWorkflowWithOptions(status.Run, workflow.RegisterOptions{Name: QueryStatusWorkflowName})
	w.RegisterWorkflowWithOptions(agentIface.Run, workflow.RegisterOptions{Name: QueryAgentInterfaceWorkflowName})
	w.RegisterWorkflowWithOptions(operatorIface.Run, workflow.RegisterOptions{Name: QueryOperatorInterfaceWorkflowName})
	w.RegisterWorkflowWithOptions(callback.Run, workflow.RegisterOptions{Name: ProvideCallbackResultWorkflowName})
	w.RegisterWorkflowWithOptions(executeCommand.Run, workflow.RegisterOptions{Name: ExecuteOperatorCommandWorkflowName})
	w.RegisterWorkflowWithOptions(submit.Run, workflow.RegisterOptions{Name: SubmitMessageWorkflowName})
	w.RegisterWorkflowWithOptions(attach.Run, workflow.RegisterOptions{Name: AttachWorkflowName})

	brokerActivities := &debuguioutbound.BrokerActivities{Broker: broker}
	w.RegisterActivityWithOptions(brokerActivities.Publish, activity.RegisterOptions{Name: debuguioutbound.PublishActivityName})
}

// DefaultNexusEndpoint matches agent.AgentNexusEndpoint - kept as its own constant (not
// an import of agent's) so this package's default doesn't silently change if agent's
// ever does; NexusEndpoint should always be configured explicitly per deployment anyway
// (see agent.Driver.NexusEndpoint's doc comment on why one Nexus endpoint per
// environment is required).
const DefaultNexusEndpoint = agent.AgentNexusEndpoint

const defaultActionTimeout = 30 * time.Second

// Config is shared by every workflow in this package.
type Config struct {
	// NexusEndpoint is the Nexus endpoint name to target. Empty uses DefaultNexusEndpoint.
	NexusEndpoint string
}

func (c Config) endpoint() string {
	if c.NexusEndpoint == "" {
		return DefaultNexusEndpoint
	}
	return c.NexusEndpoint
}

func (c Config) client() workflow.NexusClient {
	return workflow.NewNexusClient(c.endpoint(), harnessgen.AgentService.ServiceName)
}

// proxyOperation runs the one Nexus call this workflow exists to make, and nothing else.
func proxyOperation[TOut any](ctx workflow.Context, c Config, operation any, input any) (TOut, error) {
	var out TOut
	err := c.client().ExecuteOperation(ctx, operation, input,
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: defaultActionTimeout},
	).Get(ctx, &out)
	return out, err
}

// QuerySessionActionInput wraps a QuerySessionInput with which Nexus endpoint to target -
// every proxy workflow below that keys off a session ID uses this, since a single debugui
// instance's registry can span several agent types on different endpoints (see Register's
// doc comment); the caller (the inbound HTTP server) always knows the right endpoint for
// the session at hand from its own registry.
type QuerySessionActionInput struct {
	Config
	Session harnessgen.QuerySessionInput
}

// DescribeSessionWorkflow maps one HTTP request onto the describeSession Nexus operation.
type DescribeSessionWorkflow struct{}

func (w *DescribeSessionWorkflow) Run(ctx workflow.Context, in QuerySessionActionInput) (harnessgen.DescribeSessionOutput, error) {
	return proxyOperation[harnessgen.DescribeSessionOutput](ctx, in.Config, harnessgen.AgentService.DescribeSession, in.Session)
}

// CloseSessionWorkflow maps one HTTP request onto the closeSession Nexus operation.
type CloseSessionWorkflow struct{}

func (w *CloseSessionWorkflow) Run(ctx workflow.Context, in QuerySessionActionInput) (harnessgen.CloseSessionOutput, error) {
	return proxyOperation[harnessgen.CloseSessionOutput](ctx, in.Config, harnessgen.AgentService.CloseSession, in.Session)
}

// DiscoverSessionsInput wraps the discoverSessions operation's (empty) input with which
// Nexus endpoint - i.e. which registered agent type - to discover sessions of.
type DiscoverSessionsInput struct {
	Config
}

// DiscoverSessionsWorkflow maps one HTTP request onto the discoverSessions Nexus
// operation - the ONLY sanctioned way to learn about sessions this instance didn't
// itself start, since debugui never gets direct visibility-API access to the agent's
// namespace.
type DiscoverSessionsWorkflow struct{}

func (w *DiscoverSessionsWorkflow) Run(ctx workflow.Context, in DiscoverSessionsInput) (harnessgen.DiscoverSessionsOutput, error) {
	return proxyOperation[harnessgen.DiscoverSessionsOutput](ctx, in.Config, harnessgen.AgentService.DiscoverSessions, harnessgen.EmptyInput{})
}

// QueryStatusWorkflow maps one HTTP request onto the queryAgentStatus Nexus operation.
type QueryStatusWorkflow struct{}

func (w *QueryStatusWorkflow) Run(ctx workflow.Context, in QuerySessionActionInput) (harnessgen.AgentStatusOutput, error) {
	return proxyOperation[harnessgen.AgentStatusOutput](ctx, in.Config, harnessgen.AgentService.QueryAgentStatus, in.Session)
}

// QueryAgentInterfaceWorkflow maps one HTTP request onto the queryAgentInterface Nexus
// operation.
type QueryAgentInterfaceWorkflow struct{}

func (w *QueryAgentInterfaceWorkflow) Run(ctx workflow.Context, in QuerySessionActionInput) (harnessgen.AgentInterfaceOutput, error) {
	return proxyOperation[harnessgen.AgentInterfaceOutput](ctx, in.Config, harnessgen.AgentService.QueryAgentInterface, in.Session)
}

// QueryOperatorInterfaceWorkflow maps one HTTP request onto the queryOperatorInterface
// Nexus operation.
type QueryOperatorInterfaceWorkflow struct{}

func (w *QueryOperatorInterfaceWorkflow) Run(ctx workflow.Context, in QuerySessionActionInput) (harnessgen.QueryOperatorInterfaceOutput, error) {
	return proxyOperation[harnessgen.QueryOperatorInterfaceOutput](ctx, in.Config, harnessgen.AgentService.QueryOperatorInterface, in.Session)
}

// ExecuteOperatorCommandActionInput wraps the executeOperatorCommand operation's input
// with which Nexus endpoint to target. Distinct from SubmitMessageWorkflow's Slash path
// through RouterWorkflow: the debug UI's own command palette (POST /api/operator-commands)
// wants the reply back directly, not routed through a turn/outbound delivery, mirroring
// how agent.Driver.startSlashTurn already treats a harness-owned command as synchronous
// and turn-free.
type ExecuteOperatorCommandActionInput struct {
	Config
	Command harnessgen.ExecuteOperatorCommandInput
}

// ExecuteOperatorCommandWorkflow maps one HTTP request onto the executeOperatorCommand
// Nexus operation.
type ExecuteOperatorCommandWorkflow struct{}

func (w *ExecuteOperatorCommandWorkflow) Run(ctx workflow.Context, in ExecuteOperatorCommandActionInput) (harnessgen.ExecuteOperatorCommandOutput, error) {
	return proxyOperation[harnessgen.ExecuteOperatorCommandOutput](ctx, in.Config, harnessgen.AgentService.ExecuteOperatorCommand, in.Command)
}

// ProvideCallbackResultInput wraps the provideCallbackResult operation's input with which
// Nexus endpoint to target.
type ProvideCallbackResultInput struct {
	Config
	Result harnessgen.ProvideCallbackResultInput
}

// ProvideCallbackResultWorkflow maps one HTTP request onto the provideCallbackResult
// Nexus operation. Not turn-shaped (no Message/Slash/Approval fits a callback tool's
// result), so - like the rest of this file - it stays out of RouterWorkflow/router.Input
// entirely rather than growing that shared envelope for a debug-UI-only concern.
type ProvideCallbackResultWorkflow struct{}

func (w *ProvideCallbackResultWorkflow) Run(ctx workflow.Context, in ProvideCallbackResultInput) (harnessgen.ProvideCallbackResultOutput, error) {
	return proxyOperation[harnessgen.ProvideCallbackResultOutput](ctx, in.Config, harnessgen.AgentService.ProvideCallbackResult, in.Result)
}

// SubmitMessageInput wraps a turn's router.Input with which Nexus endpoint to target.
type SubmitMessageInput struct {
	Config
	Input router.Input
}

// SubmitMessageWorkflow dispatches one turn via the same router.BackendDriver Slack/Teams
// use, but returns as soon as StartTurn does - it never polls or delivers the turn's
// response itself. This is deliberately NOT RouterWorkflow: a debug UI submit-without-
// streaming request (POST /api/messages) expects the caller to separately watch the
// result via AttachWorkflow's independent, session-scoped tail, so driving RouterWorkflow's
// own per-turn PollTurn loop on top of that would just poll (and needlessly bill) the same
// events twice for no second delivery path that needed it.
type SubmitMessageWorkflow struct{}

func (w *SubmitMessageWorkflow) Run(ctx workflow.Context, in SubmitMessageInput) (router.StartResult, error) {
	d := &agent.Driver{NexusEndpoint: in.Config.endpoint()}
	return d.StartTurn(ctx, in.Input)
}

// MaxAttachIterationsBeforeContinueAsNew bounds one AttachWorkflow run's history size for
// a session that stays open a long time.
const MaxAttachIterationsBeforeContinueAsNew = 500

// AttachInput is AttachWorkflow's argument, including on each continue-as-new.
type AttachInput struct {
	Config
	SessionID  string
	FromOffset int64
}

// AttachWorkflow replays a session's full event history from an offset and then tails it
// live, publishing every event to Broker via BrokerActivities.Publish. This is session-
// scoped, not turn-scoped like RouterWorkflow: a debug UI's attach connection wants every
// turn's events from cursor onward, not just the one turn RouterWorkflow happened to be
// routing. It has no natural end (until the underlying agent session closes) and so
// periodically continues-as-new to bound its own history.
type AttachWorkflow struct{}

func (w *AttachWorkflow) Run(ctx workflow.Context, input AttachInput) error {
	cursor := input.FromOffset

	if err := publish(ctx, input.SessionID, "stream_start", nil); err != nil {
		workflow.GetLogger(ctx).Warn("AttachWorkflow: publish stream_start failed", "error", err)
	}

	for range MaxAttachIterationsBeforeContinueAsNew {
		events, next, closed, err := agent.PollSession(ctx, input.endpoint(), input.SessionID, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("AttachWorkflow: PollSession failed", "error", err)
			return endStream(ctx, input.SessionID)
		}
		cursor = next

		if closed {
			return endStream(ctx, input.SessionID)
		}

		for _, event := range events {
			if len(event.Payload) == 0 {
				continue
			}
			if err := publish(ctx, input.SessionID, event.EventType, event.Payload); err != nil {
				workflow.GetLogger(ctx).Warn("AttachWorkflow: publish failed", "error", err)
			}
		}
	}

	if err := endStream(ctx, input.SessionID); err != nil {
		return err
	}
	return workflow.NewContinueAsNewError(ctx, w.Run, AttachInput{
		Config: input.Config, SessionID: input.SessionID, FromOffset: cursor,
	})
}

func endStream(ctx workflow.Context, sessionID string) error {
	return publish(ctx, sessionID, "stream_end", nil)
}

func publish(ctx workflow.Context, sessionID, eventType string, payload []byte) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, workflow.ActivityOptions{StartToCloseTimeout: 10 * time.Second}),
		debuguioutbound.PublishActivityName,
		debuguioutbound.PublishInput{
			SessionID: sessionID,
			Frame:     debuguioutbound.Frame{Event: eventType, Data: payload},
		},
	).Get(ctx, nil)
}
