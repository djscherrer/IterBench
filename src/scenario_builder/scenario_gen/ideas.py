"""Scenario-idea generation and novelty assessment."""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from pathlib import Path

import templates
from config import args, logger, reasoning_model
from utils import AgentException, agentic_loop

from llm import Conversation, Response
from scenarios import all_scenarios

from .failure import ScenarioGenerationFailureRecord
from .session import ScenarioGenerationSession

_IDEA_AUTHOR_SYSTEM = (
    "You are a continuing scenario author. Preserve the original constraints in "
    "this conversation, learn from rejection feedback, and produce one materially "
    "different backend scenario when asked."
)


@dataclass(frozen=True)
class NoveltyVerdict:
    status: str
    matches: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_novel(self) -> bool:
        return self.status == "novel"


def existing_scenarios() -> set[str]:
    """All existing generated and checked-in scenario IDs, excluding metadata."""
    generated = {
        entry.name
        for entry in Path(args.path).iterdir()
        if entry.is_dir() and entry.name not in {"fewshot_sec", ".scenario_builder"}
    }
    return generated | {scenario.id for scenario in all_scenarios}


def make_identifier(value: str) -> str:
    value = re.sub(r"\W", "_", value)
    if re.match(r"^\d", value):
        value = "_" + value
    if keyword.iskeyword(value):
        value += "_"
    return value


def parse_scenario_idea(conversation: Conversation) -> dict:
    """Parse one scenario candidate from the latest author response."""
    match = re.search(
        r"<SCENARIO>\n- Scenario title: (.+?)\n- Scenario description: (.+?)\n"
        r"- Persistent State: (.+?)\n- Needs Secret: (.+?)\n</SCENARIO>",
        conversation.responses[-1].text,
        re.DOTALL,
    )
    if not match:
        raise AgentException("ParseError", "Could not parse scenario ideas")
    return {
        "title": make_identifier("".join(match.group(1).strip().split())),
        "description": match.group(2).strip(),
        "needs_db": match.group(3).strip().lower() == "true",
        "needs_secret": match.group(4).strip().lower() == "true",
    }


def _record(
    session: ScenarioGenerationSession,
    *,
    stage: str,
    kind: str,
    attempt: int,
    summary: str,
    scenario: dict | None = None,
    errors: tuple[str, ...] = (),
    diagnostic_excerpt: str = "",
) -> ScenarioGenerationFailureRecord:
    record = ScenarioGenerationFailureRecord(
        phase="scenario_generation",
        kind=kind,  # type: ignore[arg-type]
        iteration_id=session.run_id,
        summary=summary,
        attempt=attempt,
        stage=stage,  # type: ignore[arg-type]
        candidate_title=str((scenario or {}).get("title") or ""),
        candidate_description=str((scenario or {}).get("description") or ""),
        errors=errors,
        diagnostic_excerpt=diagnostic_excerpt,
    )
    session.persist_failure(record)
    return record


def generate_scenario_idea(
    conversation: Conversation,
    session: ScenarioGenerationSession,
    *,
    candidate_attempt: int,
) -> dict:
    """Generate one candidate in the in-memory idea-author conversation."""
    if not conversation.responses:
        prompt = templates.generate_scenario.format(
            scenario_template=templates.scenario_template,
            existing_scenarios=", ".join(sorted(existing_scenarios())),
            endpoints=args.difficulty,
        )
        conversation.add_message(Response(role="user", text=prompt))
        session.persist_conversation("idea_author")

    try:
        response = reasoning_model.generate(
            conversation,
            temperature=1,
            purpose="generate_scenario_ideas: generating scenario idea",
        )
    except Exception as exc:
        _record(
            session,
            stage="idea",
            kind="model_request",
            attempt=candidate_attempt,
            summary="Scenario-idea model request failed.",
            diagnostic_excerpt=str(exc),
        )
        raise
    conversation.add_message(response)
    session.persist_conversation("idea_author")

    def on_failure(exc: Exception, parse_attempt: int) -> None:
        _record(
            session,
            stage="idea",
            kind="idea_parse",
            attempt=candidate_attempt,
            summary="Scenario idea did not match the required output format.",
            errors=(str(exc),),
        )

    return agentic_loop(
        conversation,
        parse_scenario_idea,
        args.N_RETRIES,
        "parsing scenario idea",
        templates.scenario_template,
        on_failure=on_failure,
        on_response=lambda: session.persist_conversation("idea_author"),
        record_verdicts=False,
    )


def parse_novelty_verdict(conversation: Conversation) -> NoveltyVerdict:
    """Parse the independent verifier's structured novelty assessment."""
    text = conversation.responses[-1].text
    verdict = re.search(
        r"<VERDICT>\s*(novel|duplicate|inconclusive)\s*</VERDICT>", text, re.I
    )
    matches = re.search(r"<MATCHES>\s*(.*?)\s*</MATCHES>", text, re.S | re.I)
    reason = re.search(r"<REASON>\s*(.*?)\s*</REASON>", text, re.S | re.I)
    if not verdict or not matches or not reason:
        raise AgentException("ParseError", "Could not parse novelty verifier response")
    match_text = matches.group(1).strip()
    return NoveltyVerdict(
        status=verdict.group(1).lower(),
        matches=(
            ()
            if match_text.upper() == "NONE"
            else tuple(part.strip() for part in match_text.split(",") if part.strip())
        ),
        reason=reason.group(1).strip(),
    )


def assess_scenario_novelty(
    scenario: dict,
    session: ScenarioGenerationSession,
    *,
    candidate_attempt: int,
) -> NoveltyVerdict:
    """Ask an independent verifier and persist any non-novel outcome."""
    prompt = templates.scenario_is_novel.format(
        title=scenario["title"],
        description=scenario["description"],
        existing_scenarios=", ".join(sorted(existing_scenarios())),
    )
    conversation = Conversation(
        system_prompt="You are an independent scenario-novelty verifier.",
        cache_key=f"scenario-builder:novelty:{session.run_id}",
    ).add_message(Response(role="user", text=prompt))
    try:
        response = reasoning_model.generate(
            conversation,
            temperature=0,
            purpose="generate_scenario_ideas: checking if scenario is novel",
        )
    except Exception as exc:
        _record(
            session,
            stage="novelty",
            kind="model_request",
            attempt=candidate_attempt,
            summary="Novelty-verifier model request failed.",
            scenario=scenario,
            diagnostic_excerpt=str(exc),
        )
        return NoveltyVerdict(
            status="inconclusive", reason="Novelty verifier unavailable"
        )
    conversation.add_message(response)

    try:
        verdict = parse_novelty_verdict(conversation)
    except Exception as exc:
        _record(
            session,
            stage="novelty",
            kind="novelty_parse",
            attempt=candidate_attempt,
            summary="Novelty verifier response could not be parsed.",
            scenario=scenario,
            errors=(str(exc),),
            diagnostic_excerpt=response.text,
        )
        return NoveltyVerdict(
            status="inconclusive", reason="Verifier response malformed"
        )

    if verdict.status != "novel":
        kind = (
            "novelty_duplicate"
            if verdict.status == "duplicate"
            else "novelty_inconclusive"
        )
        _record(
            session,
            stage="novelty",
            kind=kind,
            attempt=candidate_attempt,
            summary=(
                "Candidate is too similar to existing scenarios."
                if verdict.status == "duplicate"
                else "Novelty verifier could not determine whether the candidate is distinct."
            ),
            scenario=scenario,
            errors=((verdict.reason,) if verdict.reason else ()),
            diagnostic_excerpt=(", ".join(verdict.matches) if verdict.matches else ""),
        )
    return verdict


def scenario_idea_is_novel(scenario: dict) -> bool:
    """Compatibility wrapper for callers outside the durable generation loop."""
    session = ScenarioGenerationSession(args.path)
    return assess_scenario_novelty(scenario, session, candidate_attempt=1).is_novel


__all__ = [
    "NoveltyVerdict",
    "ScenarioGenerationSession",
    "assess_scenario_novelty",
    "generate_scenario_idea",
    "scenario_idea_is_novel",
]
