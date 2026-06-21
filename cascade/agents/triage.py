"""
cascade/agents/triage.py
────────────────────────
Triage Agent — Scopes and validates incoming GitHub issues (Node 0 of DevOps pipeline).
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field
from cascade.agents.base import BaseAgent

class TriageOutput(BaseModel):
    """Structured representation of issue triage assessment."""
    is_actionable: bool = Field(description="True if the issue contains enough details to attempt a fix.")
    clarification_question: str = Field(default="", description="Question to ask the user if the issue is not actionable.")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Confidence that we can fix this issue (0.0 to 1.0).")
    suggested_labels: list[str] = Field(default_factory=list, description="Labels to apply to the GitHub issue.")
    suspected_files: list[str] = Field(default_factory=list, description="Relative paths of files that might be the source of the issue.")

class TriageAgent(BaseAgent):
    """
    Validates if a reported issue is actionable and scopes the code location
    to narrow context windows for downstream agents.
    """
    agent_name = "triage"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Inputs:
            issue_title:  GitHub issue title
            issue_body:   GitHub issue body text
            repo_url:     Target repository URL
        
        Returns:
            is_actionable:          bool
            clarification_question: str
            confidence_score:       float
            suggested_labels:       list[str]
            suspected_files:        list[str]
        """
        issue_title = inputs.get("issue_title", "")
        issue_body = inputs.get("issue_body", "")
        repo_url = inputs.get("repo_url", "")

        if self._is_mock_mode():
            return {
                "is_actionable": True,
                "clarification_question": "",
                "confidence_score": 0.9,
                "suggested_labels": ["bug", "automation-scoped"],
                "suspected_files": ["app.py"],
                "cost_manifest_uri": self.store_cost_manifest(),
                **self.get_cost_outputs(),
            }

        system = (
            "You are a senior DevOps triage engineer. Scrutinize the reported GitHub issue.\n"
            "Assess if it contains a clear error trace, expected behavior, or concrete feature request.\n"
            "If it is too vague, mark is_actionable = false and supply a clear clarification_question.\n"
            "Suggest appropriate issue labels and files that are likely the source of the bug."
        )

        user = (
            f"Repository: {repo_url}\n"
            f"Issue Title: {issue_title}\n"
            f"Issue Body:\n{issue_body[:2000]}\n"
        )

        triage_res = await self.llm_structured(
            system=system,
            user=user,
            output_model=TriageOutput,
        )

        cost_manifest_uri = self.store_cost_manifest()

        return {
            **triage_res.model_dump(),
            "cost_manifest_uri": cost_manifest_uri,
            **self.get_cost_outputs(),
        }
