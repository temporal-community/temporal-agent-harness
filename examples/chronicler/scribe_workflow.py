"""SessionScribe — the per-session CHILD agent driven as a subagent.

One Scribe instance owns ONE D&D session. The map-reduce parent
(``conversational_subagent_workflow.ChroniclerSubagentAgent``) starts one Scribe per session and
drives it via the harness subagent toolset; each accepts handler below becomes a parent tool
(``scribe_process`` / ``scribe_answer``) whose typed input/output models cross the subagent
boundary intact.

The Scribe has no model in its own loop — like the Monty dynamic agent, it's a deterministic
orchestrator of durable tools. ``process`` runs transcribe → summarize → extract for its session
(each a durable, retryable activity); ``answer`` does grounded Q&A over that session's transcript.
Its runner skips approvals: the *parent* already gates each subagent turn, so gating the child's
internal calls too would just create nested prompts.

Why a subagent rather than the parent calling these tools directly: each session's processing
becomes its own durable, independently-observable child workflow (visible + resumable in the
Temporal UI), and the parent fans many out concurrently — the map step of a map-reduce over the
campaign. See the inline ``conversational_workflow`` for the same work done with Code Mode instead.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import chronicler_activities as tools
    from .chronicler_models import (
        ScribeAnswer,
        ScribeQuestion,
        ScribeTask,
        SessionDigest,
        TranscriptMeta,
    )


@workflow.defn(name="ChroniclerScribeAgent")
@agent.defn
class ChroniclerScribeAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The parent gates every subagent turn, so the child's own internal tool calls run
            # unattended — otherwise every scribe step would raise a second approval prompt.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def process(self, message: ScribeTask) -> SessionDigest:
        """Process one session end to end: transcribe its audio (from the Gemini upload the parent
        threaded in via ``file_ref`` — the child can't reach the user's machine itself), summarize
        it, and extract the notable entities. Returns a lightweight digest for the parent to reduce
        over (the full transcript stays worker-side; only metadata crosses the boundary)."""
        sid = message.session_id
        transcript = await self._runner.run_tool(
            str(workflow.uuid4()),
            tools.transcribe_recording_activity,
            file_ref=message.file_ref,
            session_id=sid,
        )
        summary = await self._runner.run_tool(
            str(workflow.uuid4()), tools.summarize_transcript_activity, session_id=sid
        )
        entities = await self._runner.run_tool(
            str(workflow.uuid4()), tools.extract_entities_activity, session_id=sid
        )
        meta = TranscriptMeta(
            session_id=sid,
            model=transcript.model,
            duration_s=transcript.duration_s,
            segment_count=len(transcript.segments),
            word_count=sum(len(s.text.split()) for s in transcript.segments),
            speakers=sorted({s.speaker for s in transcript.segments}),
            preview=transcript.full_text[:600],
        )
        return SessionDigest(
            session_id=sid, transcript=meta, summary=summary, entities=entities
        )

    @agent.accepts
    async def answer(self, message: ScribeQuestion) -> ScribeAnswer:
        """Answer a question about this session, grounded in its transcript (must be processed
        first this run so the transcript is cached on the worker)."""
        answer = await self._runner.run_tool(
            str(workflow.uuid4()),
            tools.answer_question_activity,
            session_id=message.session_id,
            question=message.question,
        )
        return ScribeAnswer(
            session_id=message.session_id, question=message.question, answer=answer
        )
