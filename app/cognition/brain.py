"""CognitionBrain: implements BrainInterface over OpenAICompatibleLLMAdapter.

Privilege separation (task book section 6):
This class holds ONLY an LLM adapter. It has no access to databases,
permissions, governance configs, or external agent runners.
"""

from __future__ import annotations

from typing import Any

from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
)
from app.cognition.llm_adapter import OpenAICompatibleLLMAdapter
from app.drives.interfaces import DriveState
from app.perception.interfaces import Observation


class CognitionBrain(BrainInterface):
    def __init__(self, adapter: OpenAICompatibleLLMAdapter) -> None:
        self._adapter = adapter

    async def assess(
        self,
        observation: Observation,
        memories: list[dict[str, Any]],
        drives: list[DriveState],
    ) -> AssessmentResult:
        system_prompt = (
            "You are the cognitive assessment module of Artificial Organism Core (AOC). "
            "Analyze the observation, recent memories, and current drive states. "
            "Identify anomalies, open knowledge gaps (especially from environment_state.unknown_topics), "
            "and missing capabilities. "
            "Do NOT claim unavailable metrics are healthy. Do NOT propose direct system modifications."
        )
        payload = {
            "observation": observation.model_dump(mode="json"),
            "drives": [d.model_dump(mode="json") for d in drives],
            "recent_memories": memories[:10],
        }
        return await self._adapter.complete_structured(
            operation="assess",
            system_prompt=system_prompt,
            user_payload=payload,
            schema_cls=AssessmentResult,
        )

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        system_prompt = (
            "You are the goal generator of AOC. Given the selected drive, assessment, "
            "available agents, and remaining budget, propose ONE concrete, bounded, achievable goal "
            "that addresses the selected drive. "
            "If nothing worthwhile or safe can be done, set should_act=false. "
            "For reproduction drive in V1.0, ONLY propose virtual capability-building or skill research "
            "goals — NEVER creating servers, cloning instances, or elevating permissions. "
            "Set related_key to the relevant topic/capability key if applicable."
        )
        payload = {
            "selected_drive": context.decision.selected_drive,
            "decision_reason": context.decision.reason,
            "constraints": context.decision.constraints,
            "drives": [d.model_dump(mode="json") for d in context.drives],
            "assessment": context.assessment.model_dump(mode="json") if context.assessment else None,
            "environment_state": context.observation.environment_state,
            "system_state": context.observation.system_state,
            "available_agents": context.available_agents,
            "budget_remaining": context.budget_remaining,
            "active_goals": context.active_goals[:5],
        }
        proposal = await self._adapter.complete_structured(
            operation="generate_goal",
            system_prompt=system_prompt,
            user_payload=payload,
            schema_cls=GoalProposal,
        )
        if not proposal.drive_type:
            proposal = proposal.model_copy(update={"drive_type": context.decision.selected_drive})
        return proposal

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        system_prompt = (
            "You are the action planner of AOC. Turn the proposed goal into ONE bounded, "
            "verifiable task for an available external AI agent. "
            "Rules:\n"
            "- Pick assigned_agent strictly from available_agents (prefer enabled & healthy agents).\n"
            "- Use the minimum permission_level needed: L0 (read-only) or L1 (write inside isolated workspace). "
            "NEVER request L3.\n"
            "- Include concrete success_criteria and at least one deterministic verification check "
            "(e.g. kind='non_empty_output' or kind='file_exists' with target='result.md' or kind='min_output_chars').\n"
            "- Keep timeout_seconds realistic (60-300s) and within budget.\n"
            "- Never delegate open-ended infinite exploration."
        )
        payload = {
            "selected_drive": context.decision.selected_drive,
            "constraints": context.decision.constraints,
            "goal": context.goal.model_dump(mode="json") if context.goal else None,
            "assessment": context.assessment.model_dump(mode="json") if context.assessment else None,
            "available_agents": context.available_agents,
            "budget_remaining": context.budget_remaining,
            "memories": context.memories[:8],
        }
        return await self._adapter.complete_structured(
            operation="plan",
            system_prompt=system_prompt,
            user_payload=payload,
            schema_cls=ActionPlan,
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        system_prompt = (
            "You are the outcome evaluator of AOC. Review the goal, plan, actual agent execution output, "
            "deterministic verification_passed flag, and pre/post observations. "
            "CRITICAL RULES:\n"
            "- If execution failed/timed out OR verification_passed is false, you MUST set goal_achieved=false.\n"
            "- Estimate information_gain (0..1) honestly based on actual new knowledge produced.\n"
            "- Extract concise learned_facts (semantic knowledge) and acquired_skill (if a reusable skill/procedure was created).\n"
            "- Set resolved_topic ONLY if the topic was genuinely researched and verified.\n"
            "- Choose memory_type from: identity, episodic, semantic, procedural, self_model."
        )
        payload = {
            "goal": context.goal,
            "plan": context.plan,
            "execution": {
                "status": context.execution.get("status"),
                "error": context.execution.get("error"),
                "result": context.execution.get("result"),
                "stdout_preview": (context.execution.get("stdout") or "")[:4000],
                "stderr_preview": (context.execution.get("stderr") or "")[:1000],
            },
            "verification_passed": context.verification_passed,
            "verification_details": context.verification_details,
            "pre_observation_summary": {
                "system_state": context.pre_observation.system_state,
                "environment_state": context.pre_observation.environment_state,
            },
            "post_observation_summary": {
                "system_state": context.post_observation.system_state,
                "environment_state": context.post_observation.environment_state,
            },
        }
        result = await self._adapter.complete_structured(
            operation="evaluate",
            system_prompt=system_prompt,
            user_payload=payload,
            schema_cls=EvaluationResult,
        )
        # Hard guard (task book 17.7): LLM cannot declare success if execution/verification failed
        exec_status = str(context.execution.get("status", ""))
        if not context.verification_passed or exec_status != "SUCCEEDED":
            if result.goal_achieved:
                result = result.model_copy(
                    update={
                        "goal_achieved": False,
                        "resolved_topic": None,
                        "acquired_skill": None,
                        "notes": (result.notes + " [Overridden: execution or deterministic verification failed]").strip(),
                    }
                )
        return result
