// Package debuguiinbound is the HTTP boundary for the debug UI's Nexus-fronted
// deployment mode: it serves the built Svelte UI, translates its JSON/SSE API into
// router.Input (for turn-shaped requests) or one of the debuguiadmin one-shot workflows
// (for everything else), and never touches the agent's own Temporal namespace directly -
// every session/agent operation crosses the AgentService Nexus boundary, same as Slack/
// Teams.
//
// Unlike Slack/Teams, there is no separate webhook/worker split: a debug UI is normally
// run as a single instance, so cmd/debugui runs this HTTP server and the Temporal worker
// hosting RouterWorkflow/debuguiadmin's workflows in one process, sharing one in-memory
// Broker directly rather than needing a network hop between them.
package debuguiinbound

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent/generated"
	debuguiadmin "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/debugui/admin"
	debuguioutbound "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/debugui/outbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
)

// AgentConfig is one launchable/attachable agent type this server's UI offers. There is
// deliberately no Nexus operation for this: which agent types exist, and which Nexus
// endpoint fronts each, is this server's own deployment-time configuration, not
// something the agent side needs to know about (see the package doc's "why" - Nexus
// stays scoped to one agent type per endpoint, exactly like Slack/Teams).
type AgentConfig struct {
	Key           string
	WorkflowType  string
	TaskQueue     string
	Label         string
	Description   string
	NexusEndpoint string
}

// Registry is the set of agent types this server's UI can launch/attach to.
type Registry []AgentConfig

func (r Registry) byKey(key string) (AgentConfig, bool) {
	for _, a := range r {
		if a.Key == key {
			return a, true
		}
	}
	return AgentConfig{}, false
}

// sessionID encodes which registered agent a session belongs to directly into the ID
// itself ("<agentKey>/<opaque>"), so this server never needs its own persistent session
// index - every session's Nexus endpoint is derivable from its own ID alone, the same
// way Slack/Teams derive a session's identity from platform-supplied IDs rather than a
// lookup table.
func newSessionID(agentKey string) string {
	return agentKey + "/" + randomID()
}

func randomID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x", b)
}

// RouterWorkflowName returns the registered RouterWorkflow type name for agentKey. A
// single debugui instance's registry can span several agent types on different Nexus
// endpoints (see AgentConfig's doc comment), but one RouterWorkflow registration binds
// one fixed BackendDriver/endpoint - exactly like a Slack/Teams deployment, which only
// ever needs one. cmd/debugui resolves this by registering one RouterWorkflow type per
// registry entry rather than growing router.BackendDriver with routing logic it has no
// business knowing.
func RouterWorkflowName(agentKey string) string {
	return router.WorkflowName + "-" + agentKey
}

func (s *Server) agentForSession(sessionID string) (AgentConfig, error) {
	agentKey, _, ok := strings.Cut(sessionID, "/")
	if !ok {
		return AgentConfig{}, fmt.Errorf("malformed session id %q", sessionID)
	}
	cfg, ok := s.registry.byKey(agentKey)
	if !ok {
		return AgentConfig{}, fmt.Errorf("session %q names an unregistered agent %q", sessionID, agentKey)
	}
	return cfg, nil
}

// Server is the debug UI's HTTP boundary. Construct with NewServer.
type Server struct {
	tc        client.Client
	taskQueue string
	identity  string
	registry  Registry
	broker    *debuguioutbound.Broker
	staticDir string
	mux       *http.ServeMux
}

// NewServer wires up the debug UI's HTTP handler. staticDir, if non-empty, is served for
// every path not matched by /api/*, for the built Svelte UI - see cmd/debugui.
func NewServer(tc client.Client, taskQueue, identity string, registry Registry, broker *debuguioutbound.Broker, staticDir string) *Server {
	if identity == "" {
		identity = "debugui"
	}
	s := &Server{
		tc:        tc,
		taskQueue: taskQueue,
		identity:  identity,
		registry:  registry,
		broker:    broker,
		staticDir: staticDir,
		mux:       http.NewServeMux(),
	}
	s.routes()
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.mux.ServeHTTP(w, r) }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /api/agents", s.handleListAgents)
	s.mux.HandleFunc("GET /api/sessions", s.handleListSessions)
	s.mux.HandleFunc("POST /api/sessions", s.handleCreateSession)
	s.mux.HandleFunc("GET /api/workflow-status/{session_id}", s.handleWorkflowStatus)
	s.mux.HandleFunc("GET /api/status/{session_id}", s.handleStatus)
	s.mux.HandleFunc("POST /api/sessions/{session_id}/close", s.handleCloseSession)
	s.mux.HandleFunc("GET /api/agent-interface/{session_id}", s.handleAgentInterface)
	s.mux.HandleFunc("GET /api/operator-interface/{session_id}", s.handleOperatorInterface)
	s.mux.HandleFunc("POST /api/operator-commands", s.handleExecuteOperatorCommand)
	s.mux.HandleFunc("POST /api/approve", s.handleApprove)
	s.mux.HandleFunc("POST /api/callback-result", s.handleCallbackResult)
	s.mux.HandleFunc("POST /api/messages", s.handleSubmitMessage)
	s.mux.HandleFunc("POST /api/chat", s.handleChat)
	s.mux.HandleFunc("GET /api/attach", s.handleAttach)

	if s.staticDir != "" {
		s.mux.Handle("/", http.FileServer(http.Dir(s.staticDir)))
	}
}

// -- shared helpers -----------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, errCode, message string) {
	writeJSON(w, status, map[string]string{"error": errCode, "message": message})
}

func (s *Server) executeWorkflow(r *http.Request, workflowType string, input any, out any) error {
	handle, err := s.tc.ExecuteWorkflow(r.Context(),
		client.StartWorkflowOptions{ID: workflowType + "-" + randomID(), TaskQueue: s.taskQueue},
		workflowType, input,
	)
	if err != nil {
		return err
	}
	return handle.Get(r.Context(), out)
}

func adminConfig(a AgentConfig) debuguiadmin.Config {
	return debuguiadmin.Config{NexusEndpoint: a.NexusEndpoint}
}

// -- GET /api/agents ------------------------------------------------------------------

type agentDescriptorResponse struct {
	Key          string `json:"key"`
	WorkflowType string `json:"workflow_type"`
	TaskQueue    string `json:"task_queue"`
	Label        string `json:"label"`
	Description  string `json:"description"`
}

func (s *Server) handleListAgents(w http.ResponseWriter, _ *http.Request) {
	agents := make([]agentDescriptorResponse, 0, len(s.registry))
	for _, a := range s.registry {
		agents = append(agents, agentDescriptorResponse{
			Key: a.Key, WorkflowType: a.WorkflowType, TaskQueue: a.TaskQueue,
			Label: a.Label, Description: a.Description,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"agents": agents})
}

// -- sessions ---------------------------------------------------------------------

type sessionResponse struct {
	WorkflowID              string `json:"workflow_id"`
	CreatedAt               int64  `json:"created_at"`
	Label                   string `json:"label"`
	AgentWorkflowType       string `json:"agent_workflow_type"`
	IsMessageQueuingEnabled bool   `json:"is_message_queuing_enabled"`
	IsDiscovered            bool   `json:"is_discovered"`
	ExecutionStatus         string `json:"execution_status,omitempty"`
	Closed                  *bool  `json:"closed,omitempty"`
}

// handleListSessions fans DiscoverSessionsWorkflow out across every registered agent and
// merges the results - discovery is inherently scoped to one agent type per Nexus
// endpoint (see AgentConfig's doc comment), so a UI showing several agent types has no
// single Nexus call that lists them all.
func (s *Server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	var sessions []sessionResponse
	for _, a := range s.registry {
		var out harnessgen.DiscoverSessionsOutput
		if err := s.executeWorkflow(r, debuguiadmin.DiscoverSessionsWorkflowName,
			debuguiadmin.DiscoverSessionsInput{Config: adminConfig(a)}, &out); err != nil {
			writeError(w, http.StatusBadGateway, "discover_failed", err.Error())
			return
		}
		for _, sess := range out.Sessions {
			sessions = append(sessions, sessionResponse{
				WorkflowID:        a.Key + "/" + sess.SessionId,
				CreatedAt:         int64(sess.CreatedAt),
				Label:             a.Label,
				AgentWorkflowType: a.WorkflowType,
				IsDiscovered:      true,
				ExecutionStatus:   sess.ExecutionStatus,
				Closed:            &sess.Closed,
			})
		}
	}
	if sessions == nil {
		sessions = []sessionResponse{}
	}
	writeJSON(w, http.StatusOK, sessions)
}

type createSessionRequest struct {
	AgentWorkflowType       string `json:"agent_workflow_type"`
	IsMessageQueuingEnabled bool   `json:"is_message_queuing_enabled"`
}

// handleCreateSession mints a session ID locally and returns it without starting any
// Temporal workflow - like a Slack thread, the underlying agent workflow starts lazily
// on the session's first message (see SubmitMessageWorkflow/handleChat), not here.
func (s *Server) handleCreateSession(w http.ResponseWriter, r *http.Request) {
	var req createSessionRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error())
		return
	}
	var agentCfg AgentConfig
	var found bool
	for _, a := range s.registry {
		if a.WorkflowType == req.AgentWorkflowType {
			agentCfg, found = a, true
			break
		}
	}
	if !found {
		writeError(w, http.StatusUnprocessableEntity, "unknown_agent", "no registered agent has workflow_type "+req.AgentWorkflowType)
		return
	}

	sessionID := newSessionID(agentCfg.Key)
	writeJSON(w, http.StatusOK, sessionResponse{
		WorkflowID:              sessionID,
		CreatedAt:               time.Now().Unix(),
		Label:                   agentCfg.Label,
		AgentWorkflowType:       agentCfg.WorkflowType,
		IsMessageQueuingEnabled: req.IsMessageQueuingEnabled,
	})
}

// -- GET /api/workflow-status/{session_id}, GET /api/status/{session_id} ------------

func (s *Server) handleWorkflowStatus(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	agentCfg, err := s.agentForSession(sessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(sessionID, "/")

	var out harnessgen.DescribeSessionOutput
	if err := s.executeWorkflow(r, debuguiadmin.DescribeSessionWorkflowName,
		debuguiadmin.QuerySessionActionInput{Config: adminConfig(agentCfg), Session: harnessgen.QuerySessionInput{SessionId: innerID}},
		&out); err != nil {
		writeError(w, http.StatusBadGateway, "describe_failed", err.Error())
		return
	}
	closed := out.ExecutionStatus != "RUNNING"
	writeJSON(w, http.StatusOK, map[string]any{
		"workflow_id":      sessionID,
		"execution_status": out.ExecutionStatus,
		"closed":           closed,
	})
}

type agentStatusResponse struct {
	CurrentTurn               int64                     `json:"current_turn"`
	TurnActive                bool                      `json:"turn_active"`
	PendingTurns              []pendingTurnResponse     `json:"pending_turns"`
	IsMessageQueuingEnabled   bool                      `json:"is_message_queuing_enabled"`
	PendingApprovals          []pendingApprovalResponse `json:"pending_approvals"`
	PendingCallbacks          []pendingCallbackResponse `json:"pending_callbacks"`
	Subagents                 []subagentInfoResponse    `json:"subagents"`
	ApprovalPolicy            approvalPolicyResponse    `json:"approval_policy"`
	HasCustomApprovalFallback bool                      `json:"has_custom_approval_fallback"`
}

type pendingTurnResponse struct {
	TurnNumber int64  `json:"turn_number"`
	TurnID     string `json:"turn_id"`
	Message    string `json:"message"`
}

type pendingApprovalResponse struct {
	ToolID     string         `json:"tool_id"`
	ToolName   string         `json:"tool_name"`
	ToolInput  map[string]any `json:"tool_input"`
	TurnNumber int64          `json:"turn_number"`
}

type pendingCallbackResponse struct {
	ToolID       string         `json:"tool_id"`
	ToolName     string         `json:"tool_name"`
	ToolInput    map[string]any `json:"tool_input"`
	OutputSchema map[string]any `json:"output_schema"`
	TurnNumber   int64          `json:"turn_number"`
}

type subagentInfoResponse struct {
	SubagentID       string `json:"subagent_id"`
	AgentKey         string `json:"agent_key"`
	WorkflowID       string `json:"workflow_id"`
	NextExpectedTurn int64  `json:"next_expected_turn"`
}

type approvalPolicyResponse struct {
	DangerouslySkipAllApprovals bool     `json:"dangerously_skip_all_approvals"`
	AutoApproveInherentlySafe   bool     `json:"auto_approve_inherently_safe"`
	AutoApproveTools            []string `json:"auto_approve_tools"`
}

func jsonObject(raw string) map[string]any {
	if raw == "" {
		return map[string]any{}
	}
	var v map[string]any
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		return map[string]any{}
	}
	return v
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	agentCfg, err := s.agentForSession(sessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(sessionID, "/")

	var out harnessgen.AgentStatusOutput
	if err := s.executeWorkflow(r, debuguiadmin.QueryStatusWorkflowName,
		debuguiadmin.QuerySessionActionInput{Config: adminConfig(agentCfg), Session: harnessgen.QuerySessionInput{SessionId: innerID}},
		&out,
	); err != nil {
		writeError(w, http.StatusBadGateway, "status_failed", err.Error())
		return
	}

	resp := agentStatusResponse{
		CurrentTurn:               out.CurrentTurn,
		TurnActive:                out.TurnActive,
		IsMessageQueuingEnabled:   out.IsMessageQueuingEnabled,
		HasCustomApprovalFallback: out.HasCustomApprovalFallback,
		ApprovalPolicy: approvalPolicyResponse{
			DangerouslySkipAllApprovals: out.ApprovalPolicy.DangerouslySkipAllApprovals,
			AutoApproveInherentlySafe:   out.ApprovalPolicy.AutoApproveInherentlySafe,
			AutoApproveTools:            out.ApprovalPolicy.AutoApproveTools,
		},
	}
	for _, pt := range out.PendingTurns {
		resp.PendingTurns = append(resp.PendingTurns, pendingTurnResponse{TurnNumber: pt.TurnNumber, TurnID: pt.TurnId, Message: pt.Message})
	}
	for _, pa := range out.PendingApprovals {
		resp.PendingApprovals = append(resp.PendingApprovals, pendingApprovalResponse{
			ToolID: pa.ToolId, ToolName: pa.ToolName, ToolInput: jsonObject(pa.ToolInput), TurnNumber: pa.TurnNumber,
		})
	}
	for _, pc := range out.PendingCallbacks {
		resp.PendingCallbacks = append(resp.PendingCallbacks, pendingCallbackResponse{
			ToolID: pc.ToolId, ToolName: pc.ToolName, ToolInput: jsonObject(pc.ToolInput),
			OutputSchema: jsonObject(pc.OutputSchema), TurnNumber: pc.TurnNumber,
		})
	}
	for _, sa := range out.Subagents {
		resp.Subagents = append(resp.Subagents, subagentInfoResponse{
			SubagentID: sa.SubagentId, AgentKey: sa.AgentKey, WorkflowID: sa.WorkflowId, NextExpectedTurn: sa.NextExpectedTurn,
		})
	}
	writeJSON(w, http.StatusOK, resp)
}

// -- POST /api/sessions/{session_id}/close ------------------------------------------

func (s *Server) handleCloseSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	agentCfg, err := s.agentForSession(sessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(sessionID, "/")

	var out harnessgen.CloseSessionOutput
	if err := s.executeWorkflow(r, debuguiadmin.CloseSessionWorkflowName,
		debuguiadmin.QuerySessionActionInput{Config: adminConfig(agentCfg), Session: harnessgen.QuerySessionInput{SessionId: innerID}},
		&out); err != nil {
		writeError(w, http.StatusBadGateway, "close_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// -- GET /api/agent-interface/{session_id} ------------------------------------------

type acceptedFunctionResponse struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Parameters  map[string]any `json:"parameters"`
	Output      map[string]any `json:"output"`
}

func (s *Server) handleAgentInterface(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	agentCfg, err := s.agentForSession(sessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(sessionID, "/")

	var out harnessgen.AgentInterfaceOutput
	if err := s.executeWorkflow(r, debuguiadmin.QueryAgentInterfaceWorkflowName,
		debuguiadmin.QuerySessionActionInput{Config: adminConfig(agentCfg), Session: harnessgen.QuerySessionInput{SessionId: innerID}},
		&out); err != nil {
		writeError(w, http.StatusBadGateway, "query_failed", err.Error())
		return
	}
	functions := make([]acceptedFunctionResponse, 0, len(out.Handlers))
	for _, h := range out.Handlers {
		functions = append(functions, acceptedFunctionResponse{
			Name: h.Name, Description: h.Description, Parameters: jsonObject(h.Parameters), Output: jsonObject(h.Output),
		})
	}
	writeJSON(w, http.StatusOK, functions)
}

// -- GET /api/operator-interface/{session_id} ---------------------------------------

type operatorCommandArgumentResponse struct {
	Kind          string   `json:"kind"`
	Required      bool     `json:"required"`
	Choices       []string `json:"choices"`
	Placeholder   *string  `json:"placeholder,omitempty"`
	AllowMultiple bool     `json:"allow_multiple"`
}

type operatorCommandResponse struct {
	Name        string                           `json:"name"`
	PayloadName string                           `json:"payload_name"`
	Label       string                           `json:"label"`
	Description string                           `json:"description"`
	Aliases     []string                         `json:"aliases"`
	Argument    *operatorCommandArgumentResponse `json:"argument,omitempty"`
	Source      string                           `json:"source"`
}

func (s *Server) handleOperatorInterface(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	agentCfg, err := s.agentForSession(sessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(sessionID, "/")

	var out harnessgen.QueryOperatorInterfaceOutput
	if err := s.executeWorkflow(r, debuguiadmin.QueryOperatorInterfaceWorkflowName,
		debuguiadmin.QuerySessionActionInput{Config: adminConfig(agentCfg), Session: harnessgen.QuerySessionInput{SessionId: innerID}},
		&out); err != nil {
		writeError(w, http.StatusBadGateway, "query_failed", err.Error())
		return
	}
	commands := make([]operatorCommandResponse, 0, len(out.Commands))
	for _, c := range out.Commands {
		resp := operatorCommandResponse{
			// payload_name/aliases have no Nexus-contract equivalent (Slack/Teams never
			// needed them - they route by raw slash text, not a command palette); name
			// doubles as payload_name and aliases is empty until the contract grows them.
			Name: c.Name, PayloadName: c.Name, Label: c.Label, Description: c.Description,
			Aliases: []string{}, Source: c.Source,
		}
		if c.Argument != nil {
			resp.Argument = &operatorCommandArgumentResponse{
				Kind: c.Argument.Kind, Required: c.Argument.Required, Choices: c.Argument.Choices,
				Placeholder: c.Argument.Placeholder, AllowMultiple: derefBool(c.Argument.AllowMultiple),
			}
		}
		commands = append(commands, resp)
	}
	writeJSON(w, http.StatusOK, commands)
}

func derefBool(p *bool) bool {
	if p == nil {
		return false
	}
	return *p
}

// -- POST /api/operator-commands ------------------------------------------------------

type executeOperatorCommandRequest struct {
	SessionID string  `json:"session_id"`
	Name      string  `json:"name"`
	Arg       *string `json:"arg"`
}

func (s *Server) handleExecuteOperatorCommand(w http.ResponseWriter, r *http.Request) {
	var req executeOperatorCommandRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error())
		return
	}
	agentCfg, err := s.agentForSession(req.SessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(req.SessionID, "/")
	arg := ""
	if req.Arg != nil {
		arg = *req.Arg
	}
	var out harnessgen.ExecuteOperatorCommandOutput
	if err := s.executeWorkflow(r, debuguiadmin.ExecuteOperatorCommandWorkflowName,
		debuguiadmin.ExecuteOperatorCommandActionInput{
			Config:  adminConfig(agentCfg),
			Command: harnessgen.ExecuteOperatorCommandInput{SessionId: innerID, Name: req.Name, Arg: &arg},
		}, &out); err != nil {
		writeError(w, http.StatusBadGateway, "command_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"text": out.Reply})
}

// -- POST /api/approve ----------------------------------------------------------------

type toolApprovalRequest struct {
	SessionID string  `json:"session_id"`
	ToolID    string  `json:"tool_id"`
	Approved  bool    `json:"approved"`
	Reason    *string `json:"reason"`
	Remember  bool    `json:"remember"`
}

// handleApprove routes through RouterWorkflow, same as Slack/Teams's interaction
// webhooks - unlike message sends, there is no double-polling concern here (see
// SubmitMessageWorkflow's doc comment): resolving an approval never polls PollTurn.
func (s *Server) handleApprove(w http.ResponseWriter, r *http.Request) {
	var req toolApprovalRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error())
		return
	}
	agentCfg, err := s.agentForSession(req.SessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(req.SessionID, "/")

	input := router.Input{
		SessionID: req.SessionID,
		Identity:  s.identity,
		Approval: &router.ApprovalDecision{
			ToolID: req.ToolID, Approved: req.Approved, ActivityID: req.ToolID,
		},
	}
	if _, err := s.tc.ExecuteWorkflow(r.Context(),
		client.StartWorkflowOptions{ID: router.RouterWorkflowID(s.identity, req.SessionID, "approve-"+innerID+"-"+randomID()), TaskQueue: s.taskQueue},
		RouterWorkflowName(agentCfg.Key), input,
	); err != nil {
		writeError(w, http.StatusBadGateway, "approve_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"tool_id": req.ToolID, "accepted": true})
}

// -- POST /api/callback-result ---------------------------------------------------------

type callbackResultRequest struct {
	SessionID string `json:"session_id"`
	ToolID    string `json:"tool_id"`
	Result    any    `json:"result"`
	Error     string `json:"error"`
}

func (s *Server) handleCallbackResult(w http.ResponseWriter, r *http.Request) {
	var req callbackResultRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error())
		return
	}
	agentCfg, err := s.agentForSession(req.SessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(req.SessionID, "/")
	var result *map[string]json.RawMessage
	if req.Result != nil {
		b, _ := json.Marshal(req.Result)
		var m map[string]json.RawMessage
		if err := json.Unmarshal(b, &m); err == nil {
			result = &m
		}
	}
	var out harnessgen.ProvideCallbackResultOutput
	if err := s.executeWorkflow(r, debuguiadmin.ProvideCallbackResultWorkflowName,
		debuguiadmin.ProvideCallbackResultInput{
			Config: adminConfig(agentCfg),
			Result: harnessgen.ProvideCallbackResultInput{SessionId: innerID, ToolId: req.ToolID, Result: result, Error: &req.Error},
		},
		&out,
	); err != nil {
		writeError(w, http.StatusBadGateway, "callback_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"tool_id": out.ToolId, "accepted": out.Accepted})
}

// -- message submission ----------------------------------------------------------------

type chatRequest struct {
	SessionID string `json:"session_id"`
	Message   any    `json:"message"`
}

func splitMessage(raw any) (msgType string, payload map[string]any) {
	switch m := raw.(type) {
	case string:
		return "ask", map[string]any{"text": m}
	case map[string]any:
		msgType, _ = m["type"].(string)
		payload, _ = m["payload"].(map[string]any)
		if payload == nil {
			payload = map[string]any{}
		}
		return msgType, payload
	default:
		return "ask", map[string]any{}
	}
}

func messageInput(sessionID, identity string, msgType string, payload map[string]any) router.Input {
	if msgType == "slash" {
		name, _ := payload["name"].(string)
		arg, _ := payload["arg"].(string)
		return router.Input{SessionID: sessionID, Identity: identity, Slash: &router.SlashCommand{Name: name, Arg: arg}}
	}
	text, _ := payload["text"].(string)
	return router.Input{SessionID: sessionID, Identity: identity, Message: &router.IncomingMessage{
		Text: text, Timestamp: fmt.Sprint(time.Now().UnixNano()),
	}}
}

// handleSubmitMessage dispatches via SubmitMessageWorkflow (StartTurn only, no polling) -
// see that workflow's doc comment for why this deliberately does not use RouterWorkflow.
// The caller is expected to watch the result via GET /api/attach.
func (s *Server) handleSubmitMessage(w http.ResponseWriter, r *http.Request) {
	var req chatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error())
		return
	}
	agentCfg, err := s.agentForSession(req.SessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	msgType, payload := splitMessage(req.Message)
	input := messageInput(req.SessionID, s.identity, msgType, payload)

	var result router.StartResult
	if err := s.executeWorkflow(r, debuguiadmin.SubmitMessageWorkflowName,
		debuguiadmin.SubmitMessageInput{Config: adminConfig(agentCfg), Input: input}, &result); err != nil {
		writeError(w, http.StatusBadGateway, "submit_failed", err.Error())
		return
	}
	if result.Handle == nil {
		writeError(w, http.StatusBadGateway, "submit_failed", "no turn was created")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"turn_number":     result.Handle.TurnNumber,
		"turn_id":         result.Handle.TurnID,
		"accepted_offset": result.Handle.StreamHeadOffset,
		"pending":         result.Handle.Pending,
	})
}

// handleChat dispatches through RouterWorkflow so its outbound driver (see
// ../outbound.Driver) streams this turn's events live. Subscribes to the broker BEFORE
// starting the workflow so no early event can be published before the SSE connection is
// listening. Known simplification: the SSE response begins as soon as the workflow is
// successfully *started*, not once StartTurn itself has succeeded - a StartTurn-level
// rejection (e.g. a stale turn) is logged server-side (same as it already is for Slack/
// Teams) but does not currently produce a terminal SSE error frame here.
func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	var req chatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_request", err.Error())
		return
	}
	agentCfg, err := s.agentForSession(req.SessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	msgType, payload := splitMessage(req.Message)
	input := messageInput(req.SessionID, s.identity, msgType, payload)

	frames, unsubscribe := s.broker.Subscribe(req.SessionID)
	defer unsubscribe()

	if _, err := s.tc.ExecuteWorkflow(r.Context(),
		client.StartWorkflowOptions{ID: router.RouterWorkflowID(s.identity, req.SessionID, randomID()), TaskQueue: s.taskQueue},
		RouterWorkflowName(agentCfg.Key), input,
	); err != nil {
		writeError(w, http.StatusBadGateway, "chat_failed", err.Error())
		return
	}

	streamSSE(w, r, frames)
}

// -- GET /api/attach --------------------------------------------------------------------

// handleAttach ensures a persistent AttachWorkflow is running for the session (idempotent
// - a page reload or a second browser tab joins the same running workflow) and streams
// every event the broker publishes for it from then on. Unlike handleChat, this is the
// long-lived, session-scoped (not turn-scoped) connection the UI keeps open between
// messages sent via POST /api/messages.
func (s *Server) handleAttach(w http.ResponseWriter, r *http.Request) {
	sessionID := r.URL.Query().Get("session_id")
	agentCfg, err := s.agentForSession(sessionID)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_session", err.Error())
		return
	}
	_, innerID, _ := strings.Cut(sessionID, "/")
	fromOffset := int64(0)
	if v := r.URL.Query().Get("from_offset"); v != "" {
		fmt.Sscanf(v, "%d", &fromOffset)
	}

	frames, unsubscribe := s.broker.Subscribe(sessionID)
	defer unsubscribe()

	_, err = s.tc.ExecuteWorkflow(r.Context(),
		client.StartWorkflowOptions{
			ID: "attach-" + sessionID, TaskQueue: s.taskQueue,
			WorkflowIDConflictPolicy: enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING,
		},
		debuguiadmin.AttachWorkflowName,
		debuguiadmin.AttachInput{Config: adminConfig(agentCfg), SessionID: innerID, FromOffset: fromOffset},
	)
	if err != nil {
		writeError(w, http.StatusBadGateway, "attach_failed", err.Error())
		return
	}

	streamSSE(w, r, frames)
}

// -- SSE plumbing --------------------------------------------------------------------

func streamSSE(w http.ResponseWriter, r *http.Request, frames <-chan debuguioutbound.Frame) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "streaming_unsupported", "response writer cannot flush")
		return
	}
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	for {
		select {
		case <-r.Context().Done():
			return
		case frame, ok := <-frames:
			if !ok {
				return
			}
			if frame.Event == "stream_end" {
				fmt.Fprintf(w, "event: %s\ndata: {}\n\n", frame.Event)
				flusher.Flush()
				return
			}
			data := frame.Data
			if len(data) == 0 {
				data = []byte("{}")
			}
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", frame.Event, data)
			flusher.Flush()
		}
	}
}

// -- static asset helper used by cmd/debugui -------------------------------------------

// ResolvePackagedUI returns dir if it contains an index.html, else "". Lets cmd/debugui
// fall back to API-only mode (no static_dir configured) instead of failing outright.
func ResolvePackagedUI(dir string) string {
	if dir == "" {
		return ""
	}
	if _, err := os.Stat(filepath.Join(dir, "index.html")); err != nil {
		return ""
	}
	return dir
}
