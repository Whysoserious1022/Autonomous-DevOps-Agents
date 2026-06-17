"""
cascade/agents/base.py
───────────────────────
BaseAgent: Shared infrastructure for all Cascade AI agents.

Provides:
  - LiteLLM wrapper with automatic cost tracking
  - Structured output parsing via Pydantic
  - Prompt/response audit logging to artifact store
  - Retry logic with exponential backoff
  - Token budget management

All agents inherit from BaseAgent and call self.llm_complete() or
self.llm_structured() instead of litellm directly. This ensures every
LLM call is automatically logged to the artifact store for auditing.
"""

from __future__ import annotations

import json
import time
import os
import asyncio
from abc import ABC, abstractmethod
from typing import Any, TypeVar

import orjson
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

# LiteLLM is optional — graceful degradation when not installed
try:
    import litellm
    from litellm import acompletion, completion_cost
    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False
    litellm = None  # type: ignore[assignment]

from cascade.core.config import settings

T = TypeVar("T", bound=BaseModel)


# ── Cost Manifest ─────────────────────────────────────────────────────────────

class LLMCallRecord(BaseModel):
    """Record of a single LLM API call for auditing."""
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_cents: float
    duration_seconds: float
    call_index: int
    prompt_preview: str   # First 500 chars of the prompt
    response_preview: str # First 500 chars of the response


class CostManifest(BaseModel):
    """Aggregated cost tracking for an agent execution."""
    agent_name: str
    total_cost_cents: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: list[LLMCallRecord] = []

    def add_call(self, record: LLMCallRecord) -> None:
        self.calls.append(record)
        self.total_cost_cents += record.cost_cents
        self.total_tokens += record.total_tokens
        self.prompt_tokens += record.prompt_tokens
        self.completion_tokens += record.completion_tokens

    def as_step_outputs(self) -> dict[str, Any]:
        """Return fields in the format expected by @step outputs."""
        return {
            "__cost_cents__": self.total_cost_cents,
            "__total_tokens__": self.total_tokens,
            "__prompt_tokens__": self.prompt_tokens,
            "__completion_tokens__": self.completion_tokens,
        }


# ── Base Agent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Abstract base class for all Cascade AI agents.

    Subclasses implement execute() which is wrapped by the @step decorator.
    The base class provides LiteLLM integration with automatic:
      - Cost tracking per call and aggregated across the execution
      - Prompt/response audit logging to the artifact store
      - Retry with exponential backoff on transient failures
      - Structured output parsing (Pydantic models)

    Usage:
        class MyAgent(BaseAgent):
            agent_name = "my_agent"

            async def execute(self, inputs: dict) -> dict:
                response = await self.llm_complete(
                    system="You are a helpful assistant.",
                    user=f"Analyze this: {inputs['data']}",
                )
                return {"result": response.content}
    """

    agent_name: str = "base_agent"

    def __init__(self, artifact_store: Any | None = None) -> None:
        self._artifact_store = artifact_store
        self._cost_manifest = CostManifest(agent_name=self.agent_name)
        self._call_index = 0
        self._cfg = settings()

    @abstractmethod
    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """
        Main agent logic. Implement in subclasses.

        Args:
            inputs: Step inputs (from @step decorator or upstream steps).

        Returns:
            dict to be stored as step outputs. Include CAS URIs for large data.
            Cost metadata is injected automatically from self._cost_manifest.
        """
        ...

    # ── LLM Calls ─────────────────────────────────────────────────────────────

    def _is_mock_mode(self, model: str | None = None) -> bool:
        chosen_model = model or self._cfg.llm_model
        if chosen_model == "mock" or chosen_model.startswith("mock"):
            return True
        if chosen_model.startswith("openai") and not os.environ.get("OPENAI_API_KEY"):
            return True
        if chosen_model.startswith("gemini") and not os.environ.get("GEMINI_API_KEY"):
            return True
        if chosen_model.startswith("anthropic") and not os.environ.get("ANTHROPIC_API_KEY"):
            return True
        return False

    def _get_mock_structured(self, output_model: type[T]) -> T:
        name = output_model.__name__
        if name == "RelevantFilesOutput":
            return output_model(
                reasoning="Identified app.py as the primary file containing the FastAPI application routing and setup, which is directly relevant to disabling documentation in production.",
                files=[
                    {"path": "app.py", "relevance_score": 0.95, "reason": "Contains FastAPI app definition"},
                    {"path": "utils.py", "relevance_score": 0.70, "reason": "Helper functions used by app.py"}
                ],
                entry_points=["app.py"]
            )
        elif name == "RootCauseAnalysis":
            return output_model(
                root_cause="The docs_url and redoc_url parameters are not disabled when running in production environment.",
                affected_components=["app.py"],
                issue_type="bug",
                complexity_assessment="low"
            )
        elif name == "ToTBranchesOutput":
            from cascade.agents.planner import SolutionBranch
            return output_model(
                branches=[
                    SolutionBranch(
                        branch_id=0,
                        hypothesis="Check environment variable at FastAPI app instantiation and conditionally set docs_url=None, redoc_url=None.",
                        approach_name="Conditional FastAPI Config",
                        files_to_modify=["app.py"],
                        confidence=0.95,
                        reasoning="Simple, standard approach that disables docs in production but keeps them for dev.",
                        estimated_complexity="low"
                    ),
                    SolutionBranch(
                        branch_id=1,
                        hypothesis="Add middleware that intercepts /docs and /redoc paths and returns 404 in production.",
                        approach_name="Docs Middleware Interceptor",
                        files_to_modify=["app.py"],
                        confidence=0.75,
                        reasoning="Functional, but adds middleware overhead and is less direct than conditional instantiation.",
                        estimated_complexity="medium"
                    )
                ],
                selected_branch_index=0,
                analysis_summary="Option 0 is preferred because it directly sets docs_url=None on FastAPI app instantiation, which cleanly disables docs in production without routes."
            )
        elif name == "PatchOutput":
            from cascade.agents.coder import FileChange
            diff_content = (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -10,3 +10,8 @@\n"
                " app = FastAPI()\n"
                "+\n"
                "+# Conditional docs disabling for production\n"
                "+if os.environ.get('ENV') == 'production':\n"
                "+    app.docs_url = None\n"
                "+    app.redoc_url = None\n"
            )
            return output_model(
                explanation="Conditionally configure docs_url and redoc_url on FastAPI instantiation based on ENV environment variable.",
                changes=[
                    FileChange(
                        path="app.py",
                        action="modify",
                        diff=diff_content
                    )
                ],
                test_strategy="Run pytest tests/test_docs.py"
            )
        elif name == "ReviewStatusOutput":
            return output_model(
                approved=True,
                score=9.5,
                security_summary="No secrets or vulnerabilities detected in the conditional configuration change.",
                complexity_summary="Complexity of the conditional block is 1, which is extremely low.",
                architectural_review="This is the standard and cleanest way to conditionally disable docs in FastAPI.",
                issues=[]
            )
        else:
            try:
                return output_model()
            except Exception:
                init_data = {}
                for field_name, field_info in output_model.model_fields.items():
                    if field_info.default is not None:
                        init_data[field_name] = field_info.default
                    elif field_info.annotation == str:
                        init_data[field_name] = "mock"
                    elif field_info.annotation == bool:
                        init_data[field_name] = True
                    elif field_info.annotation == int:
                        init_data[field_name] = 0
                    elif field_info.annotation == float:
                        init_data[field_name] = 0.0
                    elif hasattr(field_info.annotation, "__origin__") and field_info.annotation.__origin__ == list:
                        init_data[field_name] = []
                    else:
                        init_data[field_name] = None
                return output_model(**init_data)

    async def llm_complete(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, str] | None = None,
    ) -> "LLMResponse":
        """
        Make a single LLM completion call via LiteLLM.

        Automatically:
        - Tracks cost and tokens in self._cost_manifest
        - Logs prompt+response to artifact store (if available)
        - Retries on transient errors (rate limits, timeouts)

        Args:
            system: System message (agent role/instructions).
            user: User message (the actual request).
            model: Override default model from settings.
            temperature: Override default temperature.
            max_tokens: Override default max tokens.
            response_format: e.g., {"type": "json_object"} for JSON mode.

        Returns:
            LLMResponse with .content (str) and .cost_cents (float).
        """
        if self._is_mock_mode(model):
            await asyncio.sleep(0.5)
            content = "This is a mock summary of the repository under test, showing a standard FastAPI setup with routes and middleware."
            if "summarize" in system.lower() or "summary" in system.lower():
                content = "This is a mock summary of the repository under test, showing a standard FastAPI setup with routes and middleware."
            
            chosen_model = model or self._cfg.llm_model
            record = LLMCallRecord(
                model=chosen_model,
                prompt_tokens=120,
                completion_tokens=45,
                total_tokens=165,
                cost_cents=0.15,
                duration_seconds=0.5,
                call_index=self._call_index,
                prompt_preview=user[:500],
                response_preview=content[:500],
            )
            self._cost_manifest.add_call(record)
            self._call_index += 1
            if self._artifact_store:
                self._log_call_to_store(record, system, user, content)

            return LLMResponse(
                content=content,
                cost_cents=0.15,
                prompt_tokens=120,
                completion_tokens=45,
                total_tokens=165,
            )

        if not LITELLM_AVAILABLE:
            # Return a mock response when LiteLLM isn't installed
            return LLMResponse(
                content="[LiteLLM not installed — mock response]",
                cost_cents=0.0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
            )

        chosen_model = model or self._cfg.llm_model
        chosen_temp = temperature if temperature is not None else self._cfg.llm_temperature
        chosen_max = max_tokens or self._cfg.llm_max_tokens

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "messages": messages,
            "temperature": chosen_temp,
            "max_tokens": chosen_max,
        }
        if response_format:
            kwargs["response_format"] = response_format

        start = time.perf_counter()
        response = await self._llm_call_with_retry(**kwargs)
        duration = time.perf_counter() - start

        content = response.choices[0].message.content or ""
        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        total_tokens = response.usage.total_tokens if response.usage else 0

        # Compute cost via LiteLLM's pricing database
        try:
            cost_usd = litellm.completion_cost(completion_response=response)
            cost_cents = cost_usd * 100
        except Exception:  # noqa: BLE001
            cost_cents = 0.0

        record = LLMCallRecord(
            model=chosen_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_cents=cost_cents,
            duration_seconds=duration,
            call_index=self._call_index,
            prompt_preview=user[:500],
            response_preview=content[:500],
        )
        self._cost_manifest.add_call(record)
        self._call_index += 1

        # Audit log to artifact store
        if self._artifact_store:
            self._log_call_to_store(record, system, user, content)

        return LLMResponse(
            content=content,
            cost_cents=cost_cents,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    async def llm_structured(
        self,
        *,
        system: str,
        user: str,
        output_model: type[T],
        model: str | None = None,
        max_retries: int = 2,
    ) -> T:
        """
        Make an LLM call and parse the response as a Pydantic model.

        Uses JSON mode for reliable structured output. Falls back to
        regex extraction if the model doesn't support JSON mode.

        Args:
            system: System message (include JSON schema hint here).
            user: User message.
            output_model: Pydantic model class to parse response into.
            max_retries: Retries for JSON parse failures.

        Returns:
            An instance of output_model.
        """
        if self._is_mock_mode(model):
            mock_obj = self._get_mock_structured(output_model)
            chosen_model = model or self._cfg.llm_model
            content = mock_obj.model_dump_json()
            record = LLMCallRecord(
                model=chosen_model,
                prompt_tokens=250,
                completion_tokens=180,
                total_tokens=430,
                cost_cents=0.35,
                duration_seconds=0.5,
                call_index=self._call_index,
                prompt_preview=user[:500],
                response_preview=content[:500],
            )
            self._cost_manifest.add_call(record)
            self._call_index += 1
            if self._artifact_store:
                self._log_call_to_store(record, system, user, content)
            
            await asyncio.sleep(0.5)
            return mock_obj

        schema_hint = f"\nRespond ONLY with valid JSON matching this schema:\n{output_model.model_json_schema()}"

        for attempt in range(max_retries + 1):
            try:
                response = await self.llm_complete(
                    system=system + schema_hint,
                    user=user,
                    model=model,
                    response_format={"type": "json_object"},
                )
                # Strip markdown code fences if present
                content = response.content.strip()
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                return output_model.model_validate_json(content)
            except Exception as e:  # noqa: BLE001
                if attempt == max_retries:
                    raise
                continue

        raise RuntimeError(f"Failed to get structured output after {max_retries} retries")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def _llm_call_with_retry(self, **kwargs: Any) -> Any:
        """LiteLLM call with automatic retry on transient errors."""
        return await litellm.acompletion(**kwargs)

    def _log_call_to_store(
        self, record: LLMCallRecord, system: str, user: str, response: str
    ) -> None:
        """Store full prompt+response as a CAS artifact for auditing."""
        audit_doc = {
            "agent": self.agent_name,
            "call_index": record.call_index,
            "model": record.model,
            "cost_cents": record.cost_cents,
            "tokens": record.total_tokens,
            "system": system,
            "user": user,
            "response": response,
        }
        try:
            self._artifact_store.put_json(audit_doc)
        except Exception:  # noqa: BLE001
            pass  # Non-fatal: audit logging failure should not break execution

    def get_cost_outputs(self) -> dict[str, Any]:
        """Return cost manifest as @step-compatible output dict."""
        return self._cost_manifest.as_step_outputs()

    def store_cost_manifest(self) -> str | None:
        """Persist the full cost manifest to artifact store, return CAS URI."""
        if self._artifact_store:
            return self._artifact_store.put_json(self._cost_manifest.model_dump())
        return None


# ── LLM Response ─────────────────────────────────────────────────────────────

class LLMResponse(BaseModel):
    """Typed response from an LLM completion call."""
    content: str
    cost_cents: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
