"""Invoke the Bedrock Chef AI agent from Flask."""

from __future__ import annotations

import logging
import re

import boto3

from config import Config

logger = logging.getLogger(__name__)

_agent_runtime = None

# Bedrock test alias — always points at the working DRAFT (correct tools + KB).
DRAFT_ALIAS_ID = "TSTALIASID"

_AGENT_FAILURE_MARKERS = (
    "sorry, i am unable to assist you with this request",
    "sorry i cannot answer",
)

# Old prepared alias (v5) returns flattened catalog IDs instead of recipe names.
_STALE_PROD_RE = re.compile(r"\b\d{1,4}\s+\*\*Tags:\*\*")


def _client():
    global _agent_runtime
    if _agent_runtime is None:
        _agent_runtime = boto3.client(
            "bedrock-agent-runtime", region_name=Config.AWS_REGION
        )
    return _agent_runtime


def _is_failure_answer(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return True
    if any(marker in normalized for marker in _AGENT_FAILURE_MARKERS):
        return True
    # Short generic refusals from a stale prepared version — retry DRAFT.
    if normalized.startswith("sorry,") and len(normalized) < 160:
        return True
    if "cannot suggest" in normalized or "cannot answer" in normalized:
        return True
    if _STALE_PROD_RE.search(text):
        return True
    if "only provide the current date and time" in normalized:
        return True
    return False


def _alias_ids_to_try() -> list[str]:
    """Return alias IDs in invocation order.

    Prefer TSTALIASID (DRAFT) first when fallback is enabled. Custom prod aliases
    often still route to old prepared snapshots (GetTime/GetWeather only).
    """
    primary = (Config.BEDROCK_AGENT_ALIAS_ID or "").strip()
    if primary == DRAFT_ALIAS_ID:
        return [DRAFT_ALIAS_ID]
    if Config.BEDROCK_AGENT_FALLBACK_TO_DRAFT:
        return [DRAFT_ALIAS_ID, primary] if primary else [DRAFT_ALIAS_ID]
    return [primary] if primary else []


def _extract_answer_from_event(event: dict) -> tuple[str, str]:
    """Return (chunk_text, trace_final_text) from one completion event."""
    chunk_text = ""
    trace_text = ""

    chunk = event.get("chunk")
    if chunk and chunk.get("bytes"):
        chunk_text = chunk["bytes"].decode("utf-8")

    trace = event.get("trace", {}).get("trace", {})
    orch = trace.get("orchestrationTrace", {})
    observation = orch.get("observation", {})
    final = observation.get("finalResponse", {})
    if final.get("text"):
        trace_text = str(final["text"])

    return chunk_text, trace_text


def _invoke_once(
    alias_id: str,
    question: str,
    session_id: str,
    session_attributes: dict[str, str],
    prompt_attributes: dict[str, str],
) -> str:
    clean_session = {k: str(v) for k, v in session_attributes.items() if v is not None}
    clean_prompt = {k: str(v) for k, v in prompt_attributes.items() if v is not None}

    logger.info(
        "Invoking agent %s alias %s session %s",
        Config.BEDROCK_AGENT_ID,
        alias_id,
        session_id,
    )

    response = _client().invoke_agent(
        agentId=Config.BEDROCK_AGENT_ID,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=question,
        sessionState={
            "sessionAttributes": clean_session,
            "promptSessionAttributes": clean_prompt,
        },
        enableTrace=True,
    )

    parts: list[str] = []
    trace_parts: list[str] = []
    saw_failure_trace = False

    for event in response.get("completion", []):
        chunk_text, trace_text = _extract_answer_from_event(event)
        if chunk_text:
            parts.append(chunk_text)
        if trace_text:
            trace_parts.append(trace_text)

        trace = event.get("trace", {}).get("trace", {})
        if trace.get("failureTrace"):
            saw_failure_trace = True
            reason = trace["failureTrace"].get("failureReason", "")
            logger.warning("Agent failure trace (alias %s): %s", alias_id, reason[:300])

    answer = "".join(parts).strip()
    if not answer:
        answer = "".join(trace_parts).strip()

    if not answer and saw_failure_trace:
        raise RuntimeError(
            "Bedrock agent orchestration failed (stale alias or tool format error). "
            "Prepare the agent in the console and point your alias at the new version, "
            "or rely on the TSTALIASID draft fallback."
        )
    if not answer:
        raise RuntimeError("Bedrock agent returned an empty response")

    return answer


def invoke_chef_agent(
    question: str,
    session_id: str,
    session_attributes: dict[str, str],
    prompt_attributes: dict[str, str],
) -> str:
    """Call Bedrock Agent and return the final assistant text."""
    if not Config.BEDROCK_AGENT_ID or not Config.BEDROCK_AGENT_ALIAS_ID:
        raise RuntimeError(
            "BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID must be set in the environment"
        )

    aliases = _alias_ids_to_try()
    last_error: Exception | None = None

    for idx, alias_id in enumerate(aliases):
        is_last = idx == len(aliases) - 1
        # DRAFT alias gets its own session suffix so a poisoned prod session does not carry over.
        call_session_id = (
            f"{session_id}::draft" if alias_id == DRAFT_ALIAS_ID else session_id
        )
        try:
            answer = _invoke_once(
                alias_id,
                question,
                call_session_id,
                session_attributes,
                prompt_attributes,
            )
            if _is_failure_answer(answer) and not is_last:
                logger.warning(
                    "Agent alias %s returned unusable answer; trying next alias",
                    alias_id,
                )
                continue
            if alias_id == DRAFT_ALIAS_ID and alias_id != aliases[0]:
                logger.info("Chef AI answered via DRAFT alias %s", DRAFT_ALIAS_ID)
            return answer
        except Exception as exc:
            last_error = exc
            logger.warning("Agent alias %s failed: %s", alias_id, exc)
            if is_last:
                raise

    if last_error:
        raise last_error
    raise RuntimeError("Bedrock agent returned no usable response")


def is_agent_configured() -> bool:
    return bool(Config.BEDROCK_AGENT_ID and Config.BEDROCK_AGENT_ALIAS_ID)
