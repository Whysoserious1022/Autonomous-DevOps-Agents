"""
cascade/agents/planner.py
──────────────────────────
Planner Agent — Tree of Thoughts (Node 2 of the DevOps pipeline).

Generates 3-5 distinct solution branches for a GitHub issue using the
Tree of Thoughts (ToT) prompting technique. Each branch represents a
different architectural approach to solving the problem.

The Cascade Cache Superpower:
  If the user adjusts the issue description or wants to try a different
  approach, ONLY this step re-runs. The Explorer step (repo cloning + AST)
  is still SKIPPED via cache, saving the majority of the token cost.

Output artifacts:
  tot_branches.json — All solution branches with confidence scores
  Selected branch is passed directly to the Coder.
"""

from __future__ import annotations

from typing import Any
import asyncio

from pydantic import BaseModel, Field

from cascade.agents.base import BaseAgent


class CriticEvaluation(BaseModel):
    """Evaluation output from the ToT critic."""
    viability_score: float = Field(ge=0.0, le=1.0, description="Score of how viable this plan is (0.0 to 1.0)")
    criticism: str = Field(description="Constructive feedback, edge cases, or potential problems with this plan")
    mitigation_steps: list[str] = Field(default_factory=list, description="Steps to mitigate the identified risks")


# ── Data Models ───────────────────────────────────────────────────────────────

class ImplementationStep(BaseModel):
    """A single implementation step within a solution branch."""
    step_number: int
    description: str
    files_involved: list[str] = Field(default_factory=list)
    code_hint: str = Field(default="", description="Pseudocode or key logic hint")


class SolutionBranch(BaseModel):
    """
    A single Tree-of-Thoughts solution branch.
    Represents one distinct approach to solving the issue.
    """
    branch_id: int
    hypothesis: str = Field(
        description="One-sentence description of this approach"
    )
    approach_name: str = Field(
        description="Short name for this approach (e.g., 'Middleware Patch')"
    )
    files_to_modify: list[str] = Field(
        description="Relative paths of files that need to be changed"
    )
    files_to_create: list[str] = Field(
        default_factory=list,
        description="New files to create (if any)"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence score: 0.0 (low) to 1.0 (high)"
    )
    reasoning: str = Field(
        description="Detailed explanation of why this approach would work"
    )
    implementation_steps: list[ImplementationStep] = Field(
        default_factory=list,
        description="Step-by-step implementation plan"
    )
    estimated_complexity: str = Field(
        default="medium",
        description="low / medium / high"
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Potential downsides or risks of this approach"
    )
    requires_tests: list[str] = Field(
        default_factory=list,
        description="Test files that should be updated or created"
    )


class ToTBranchesOutput(BaseModel):
    """Full Tree-of-Thoughts output with all solution branches."""
    branches: list[SolutionBranch] = Field(
        description="List of 3-5 solution branches, sorted by confidence descending"
    )
    selected_branch_index: int = Field(
        default=0,
        description="Index of the highest-confidence branch (0-indexed)"
    )
    analysis_summary: str = Field(
        description="Brief summary of the root cause analysis"
    )


class RootCauseAnalysis(BaseModel):
    """Initial root cause analysis before branch generation."""
    root_cause: str
    affected_components: list[str]
    issue_type: str  # bug / feature / refactor / performance / security
    complexity_assessment: str


# ── Planner Agent ─────────────────────────────────────────────────────────────

class PlannerAgent(BaseAgent):
    """
    Generates Tree-of-Thoughts solution branches for a GitHub issue.

    The Planner receives the RepoGraph from the Explorer and produces
    multiple distinct solution approaches, each with implementation steps,
    confidence scores, and risk assessment.
    """

    agent_name = "planner"
    DEFAULT_N_BRANCHES = 3

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Full planner pipeline:
        1. Load RepoGraph from CAS
        2. Root cause analysis
        3. Generate N solution branches
        4. Score and rank branches
        5. Store tot_branches.json artifact

        Inputs (from @step decorator / Explorer outputs):
            explorer.repo_graph_uri:     CAS URI to repo_graph.json
            explorer.relevant_files_uri: CAS URI to relevant_files.json
            issue_title:                 GitHub issue title
            issue_body:                  GitHub issue description
            n_branches:                  Number of branches to generate (default 3)
            custom_prompt:               Optional user directive to influence planning

        Returns:
            tot_branches_uri:       CAS URI → tot_branches.json
            selected_branch_index:  Index of highest-confidence branch
            selected_branch:        Dict of the selected branch (for Coder input)
            analysis_summary:       1-paragraph root cause analysis
        """
        # ── Load upstream artifacts ───────────────────────────────────────────
        repo_graph_uri = inputs.get("explorer.repo_graph_uri", "")
        relevant_files_uri = inputs.get("explorer.relevant_files_uri", "")
        issue_title = inputs.get("issue_title", "")
        issue_body = inputs.get("issue_body", "")
        n_branches = inputs.get("n_branches", self.DEFAULT_N_BRANCHES)
        custom_prompt = inputs.get("custom_prompt", "")

        # Load RepoGraph from CAS artifact store
        repo_graph_data = {}
        relevant_files_data = {}
        if repo_graph_uri and self._artifact_store:
            try:
                repo_graph_data = self._artifact_store.get_json(repo_graph_uri)
            except KeyError:
                pass  # Graceful: use empty graph if artifact missing
        if relevant_files_uri and self._artifact_store:
            try:
                relevant_files_data = self._artifact_store.get_json(relevant_files_uri)
            except KeyError:
                pass

        # ── Step 1: Root cause analysis ───────────────────────────────────────
        rca = await self._analyze_root_cause(
            repo_graph_data, relevant_files_data, issue_title, issue_body
        )

        # ── Step 2: Generate solution branches ────────────────────────────────
        tot_output = await self._generate_branches(
            repo_graph_data=repo_graph_data,
            relevant_files=relevant_files_data,
            issue_title=issue_title,
            issue_body=issue_body,
            rca=rca,
            n_branches=n_branches,
            custom_prompt=custom_prompt,
        )

        # ── Step 3: Store artifacts ───────────────────────────────────────────
        selected_branch = (
            tot_output.branches[tot_output.selected_branch_index].model_dump()
            if tot_output.branches
            else {}
        )

        tot_uri = self._artifact_store.put_json(tot_output.model_dump()) if self._artifact_store else ""
        cost_manifest_uri = self.store_cost_manifest()

        return {
            "tot_branches_uri": tot_uri,
            "selected_branch_index": tot_output.selected_branch_index,
            "selected_branch": selected_branch,
            "analysis_summary": tot_output.analysis_summary,
            "root_cause": rca.root_cause,
            "issue_type": rca.issue_type,
            "n_branches": len(tot_output.branches),
            "cost_manifest_uri": cost_manifest_uri,
            **self.get_cost_outputs(),
        }

    # ── Analysis Steps ────────────────────────────────────────────────────────

    async def _analyze_root_cause(
        self,
        repo_graph: dict[str, Any],
        relevant_files: dict[str, Any],
        issue_title: str,
        issue_body: str,
    ) -> RootCauseAnalysis:
        """
        Phase 1 of Tree-of-Thoughts: Identify the root cause before branching.
        This grounds all subsequent branches in a shared understanding.
        """
        relevant_file_list = self._format_relevant_files(relevant_files)
        repo_summary = repo_graph.get("summary", "")
        commit_sha = repo_graph.get("commit_sha", "unknown")

        system = (
            "You are a senior software engineer performing root cause analysis on a GitHub issue. "
            "Your task is to identify the exact root cause of the issue before proposing solutions. "
            "Be precise, technical, and grounded in the actual codebase structure."
        )
        user = (
            f"Repository: {repo_graph.get('repo_url', 'unknown')} @ {commit_sha[:8]}\n"
            f"Summary: {repo_summary}\n\n"
            f"GitHub Issue: {issue_title}\n\n"
            f"Issue Description:\n{issue_body[:3000]}\n\n"
            f"Relevant Files:\n{relevant_file_list}"
        )

        return await self.llm_structured(
            system=system,
            user=user,
            output_model=RootCauseAnalysis,
        )

    async def _generate_branches(
        self,
        repo_graph_data: dict[str, Any],
        relevant_files: dict[str, Any],
        issue_title: str,
        issue_body: str,
        rca: RootCauseAnalysis,
        n_branches: int,
        custom_prompt: str,
    ) -> ToTBranchesOutput:
        """
        Phase 2 of Tree-of-Thoughts: Generate solution branches, run parallel critic reviews, and backtrack if needed.
        """
        # 1. Generate candidate branches using standard LLM call
        tot_output = await self._raw_generate_branches(
            repo_graph_data, relevant_files, issue_title, issue_body, rca, n_branches, custom_prompt
        )

        if self._is_mock_mode() or not tot_output.branches:
            return tot_output

        # 2. Evaluate candidate branches in parallel using Critic
        eval_tasks = [
            self.evaluate_candidate(b, issue_title, issue_body, rca)
            for b in tot_output.branches
        ]
        evaluations = await asyncio.gather(*eval_tasks)

        # 3. Update branches with Critic scores
        for b, eval_res in zip(tot_output.branches, evaluations):
            b.confidence = eval_res.viability_score
            b.reasoning += f"\n\n[Architect Critic Feedback]: {eval_res.criticism}\n[Mitigations]: {', '.join(eval_res.mitigation_steps)}"

        # Re-sort by updated confidence descending
        tot_output.branches.sort(key=lambda x: x.confidence, reverse=True)
        tot_output.selected_branch_index = 0

        # 4. Backtracking: If all branches score below threshold (e.g. 0.5), run corrective generation
        if tot_output.branches[0].confidence < 0.5:
            corrective_feedback = "\n\nCritique of previous approaches (please avoid these issues and suggest alternative pathways):\n"
            for idx, b in enumerate(tot_output.branches):
                feedback_str = b.reasoning.split("[Architect Critic Feedback]:")[-1].strip()
                corrective_feedback += f"- Branch {idx} ({b.approach_name}) Critique: {feedback_str}\n"

            # Regenerate with the corrective feedback injected
            tot_output = await self._raw_generate_branches(
                repo_graph_data, relevant_files, issue_title, issue_body, rca, n_branches,
                custom_prompt=f"{custom_prompt}\n{corrective_feedback}"
            )
            # Re-evaluate new candidates
            eval_tasks = [
                self.evaluate_candidate(b, issue_title, issue_body, rca)
                for b in tot_output.branches
            ]
            evaluations = await asyncio.gather(*eval_tasks)
            for b, eval_res in zip(tot_output.branches, evaluations):
                b.confidence = eval_res.viability_score
                b.reasoning += f"\n\n[Architect Critic Feedback]: {eval_res.criticism}"

            tot_output.branches.sort(key=lambda x: x.confidence, reverse=True)
            tot_output.selected_branch_index = 0

        return tot_output

    async def _raw_generate_branches(
        self,
        repo_graph_data: dict[str, Any],
        relevant_files: dict[str, Any],
        issue_title: str,
        issue_body: str,
        rca: RootCauseAnalysis,
        n_branches: int,
        custom_prompt: str,
    ) -> ToTBranchesOutput:
        """Call LLM directly to generate raw branches."""
        relevant_file_list = self._format_relevant_files(relevant_files)
        repo_url = repo_graph_data.get("repo_url", "unknown")
        total_files = repo_graph_data.get("total_files_analyzed", 0)

        custom_directive = (
            f"\n\nUser directive: {custom_prompt}"
            if custom_prompt
            else ""
        )

        system = (
            "You are a senior software engineer using Tree-of-Thoughts reasoning to solve a GitHub issue. "
            "Generate exactly {n} distinct solution approaches. Each approach must:\n"
            "1. Target a DIFFERENT set of files or use a DIFFERENT architectural approach\n"
            "2. Include concrete implementation steps (not vague)\n"
            "3. Have an honest confidence score (0.0-1.0)\n"
            "4. Identify specific risks and edge cases\n"
            "Sort branches by confidence descending. Set selected_branch_index to 0 (highest confidence).\n"
            "IMPORTANT: Keep reasoning, descriptions, and code_hints extremely brief (1-2 sentences max). "
            "Do not output long paragraphs, otherwise the JSON output will be truncated. Be concise and technical."
        ).format(n=n_branches)

        user = (
            f"Repository: {repo_url} ({total_files} files analyzed)\n\n"
            f"Root Cause Analysis:\n"
            f"  Type: {rca.issue_type}\n"
            f"  Root cause: {rca.root_cause}\n"
            f"  Affected components: {', '.join(rca.affected_components)}\n"
            f"  Complexity: {rca.complexity_assessment}\n\n"
            f"GitHub Issue: {issue_title}\n\n"
            f"Issue Description:\n{issue_body[:2000]}\n\n"
            f"Relevant Files to Modify:\n{relevant_file_list}"
            f"{custom_directive}"
        )

        return await self.llm_structured(
            system=system,
            user=user,
            output_model=ToTBranchesOutput,
        )

    async def evaluate_candidate(
        self,
        branch: SolutionBranch,
        issue_title: str,
        issue_body: str,
        rca: RootCauseAnalysis
    ) -> CriticEvaluation:
        """Critic LLM evaluation of a single branch plan."""
        system = (
            "You are an independent principal software architect acting as a critic. "
            "Evaluate the proposed solution branch and find any flaws, missing imports, edge cases, or architectural issues."
        )
        user = (
            f"Issue: {issue_title}\n"
            f"Issue Description: {issue_body[:1000]}\n\n"
            f"RCA: {rca.root_cause}\n\n"
            f"Proposed Plan: {branch.approach_name}\n"
            f"Hypothesis: {branch.hypothesis}\n"
            f"Files to modify: {branch.files_to_modify}\n"
            f"Reasoning: {branch.reasoning}\n"
        )
        return await self.llm_structured(
            system=system,
            user=user,
            output_model=CriticEvaluation
        )

    def _format_relevant_files(self, relevant_files: dict[str, Any]) -> str:
        """Format the relevant files list for LLM context."""
        if not relevant_files:
            return "  (no relevant files identified)"
        files = relevant_files.get("files", [])
        if not files:
            return "  (no relevant files identified)"
        lines = []
        for f in files[:20]:
            path = f.get("path", "?")
            reason = f.get("reason", "")
            score = f.get("relevance_score", "?")
            lines.append(f"  [{score:.1f}] {path} — {reason}" if isinstance(score, float) else f"  {path} — {reason}")
        return "\n".join(lines)
