"""
cascade/agents/reviewer.py
───────────────────────────
Reviewer Agent — Guardrail Agent (Node 5 of the DevOps pipeline).

Performs guardrail checks on the generated patch, including:
  1. Cyclomatic complexity assessment using Python AST
  2. Hardcoded secrets scanning using regex-based entropy heuristics
  3. LLM-based architectural and security review of the diff

Output artifacts:
  review_status.json  — Summary of security, complexity, and LLM review
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cascade.agents.base import BaseAgent


# ── Data Models ───────────────────────────────────────────────────────────────

class ReviewStatusOutput(BaseModel):
    """Structured output for the Reviewer agent's analysis."""
    approved: bool = Field(description="True if the patch passes all guardrails")
    score: float = Field(ge=0.0, le=10.0, description="Overall patch quality score from 0 to 10")
    security_summary: str = Field(description="Summary of security/secrets assessment")
    complexity_summary: str = Field(description="Summary of code complexity assessment")
    architectural_review: str = Field(description="LLM review of the changes")
    issues: list[str] = Field(default_factory=list, description="List of issues found that need fixing")


# ── Complexity & Secrets Scanner ──────────────────────────────────────────────

class CodeGuardrailScanner:
    """Self-contained scanner for code complexity and hardcoded secrets."""

    SECRETS_PATTERNS = [
        r"(?i)(password|passwd|secret|token|api_key|apikey|private_key|auth_token)\s*=\s*['\"][a-zA-Z0-9_\-\.]{12,}['\"]",
        r"(?i)ghp_[a-zA-Z0-9]{36,40}",  # GitHub PAT
        r"(?i)sk-[a-zA-Z0-9]{20,}",      # OpenAI API Key
    ]

    @classmethod
    def scan_secrets(cls, patch_diff: str) -> list[str]:
        """Scan patch diff for hardcoded secrets."""
        issues = []
        for pattern in cls.SECRETS_PATTERNS:
            matches = re.finditer(pattern, patch_diff)
            for m in matches:
                matched_str = m.group(0)
                # Obfuscate secret in logs
                obfuscated = matched_str.split("=")[0] + "= *****" if "=" in matched_str else "*****"
                issues.append(f"Hardcoded secret detected: {obfuscated.strip()}")
        return issues

    @classmethod
    def compute_cyclomatic_complexity(cls, code_content: str) -> dict[str, int]:
        """Compute cyclomatic complexity of functions in python code content using AST."""
        complexity = {}
        try:
            tree = ast.parse(code_content)
        except SyntaxError:
            return {}

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Base complexity is 1
                cc = 1
                for child in ast.walk(node):
                    # Decisions increments complexity
                    if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.With, ast.AsyncWith)):
                        cc += 1
                    elif isinstance(child, ast.BoolOp):
                        cc += len(child.values) - 1
                complexity[node.name] = cc
        return complexity


# ── Reviewer Agent ────────────────────────────────────────────────────────────

class ReviewerAgent(BaseAgent):
    """
    Performs static analysis and LLM security audits on the generated patch.
    """

    agent_name = "reviewer"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Full reviewer pipeline:
        1. Fetch patch and apply static scanners
        2. Run LLM review on the patch diff
        3. Determine approval status
        4. Store review_status.json in CAS

        Inputs:
            patch_uri:          CAS URI to patch.diff
            patch_diff:         Inline patch content
            issue_title:        GitHub issue title
            issue_body:         GitHub issue description
            analysis_summary:   Root cause analysis

        Returns:
            review_status_uri:  CAS URI → review_status.json
            review_approved:    bool
            review_score:       float (0-10)
        """
        patch_uri = inputs.get("patch_uri", inputs.get("coder.patch_uri", ""))
        patch_diff = inputs.get("patch_diff", "")
        issue_title = inputs.get("issue_title", "")
        issue_body = inputs.get("issue_body", "")

        if patch_uri and not patch_diff and self._artifact_store:
            try:
                patch_diff = self._artifact_store.get_text(patch_uri)
            except KeyError:
                pass

        if not patch_diff:
            raise ValueError("No patch content found to review.")

        # ── Step 1: Scan for secrets ──
        secrets_issues = CodeGuardrailScanner.scan_secrets(patch_diff)

        # ── Step 2: Scan for complexity on modified lines (if python code is parsed) ──
        complexity_issues = []
        # Exclude diff header markings, grab only modified/added lines
        added_lines = []
        for line in patch_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:])
        added_code = "\n".join(added_lines)

        cc_map = CodeGuardrailScanner.compute_cyclomatic_complexity(added_code)
        for func_name, cc in cc_map.items():
            if cc > 10:
                complexity_issues.append(
                    f"Function '{func_name}' complexity is high ({cc} > 10). Consider refactoring."
                )

        # ── Step 3: Run LLM review ──
        system = (
            "You are a senior security researcher and principal software architect. "
            "Perform a rigorous review of this git diff patch. "
            "Check for:\n"
            "- Security vulnerabilities (SQL injection, XSS, unauthenticated paths, logic flaws)\n"
            "- Architectural flaws, code smell, and readability\n"
            "- Adherence to standard conventions and exception handling\n"
            "Be critical. If you detect any potential vulnerability or severe code smell, "
            "list it in the issues and set approved = false."
        )

        user = (
            f"GitHub Issue: {issue_title}\n"
            f"Description: {issue_body[:1000]}\n\n"
            f"Code Patch Diff:\n```diff\n{patch_diff[:4000]}\n```\n\n"
            f"Static Analysis Findings:\n"
            f"  Secrets issues found: {len(secrets_issues)}\n"
            f"  High complexity functions found: {len(complexity_issues)}\n"
        )

        review_output = await self.llm_structured(
            system=system,
            user=user,
            output_model=ReviewStatusOutput,
        )

        # Merge static findings into the LLM review issues list
        all_issues = list(review_output.issues) + secrets_issues + complexity_issues
        
        # Override approval if secrets are found
        approved = review_output.approved
        if secrets_issues:
            approved = False

        final_review = ReviewStatusOutput(
            approved=approved,
            score=0.0 if secrets_issues else review_output.score,
            security_summary=f"Found {len(secrets_issues)} secrets issues. " + review_output.security_summary,
            complexity_summary=f"Found {len(complexity_issues)} high complexity functions. " + review_output.complexity_summary,
            architectural_review=review_output.architectural_review,
            issues=all_issues,
        )

        # Store in CAS
        review_status_uri = ""
        if self._artifact_store:
            review_status_uri = self._artifact_store.put_json(final_review.model_dump())

        cost_manifest_uri = self.store_cost_manifest()

        return {
            "review_status_uri": review_status_uri,
            "review_approved": final_review.approved,
            "review_score": final_review.score,
            "review_issues": final_review.issues,
            "cost_manifest_uri": cost_manifest_uri,
            **self.get_cost_outputs(),
        }
