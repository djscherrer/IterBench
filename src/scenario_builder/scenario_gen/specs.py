import re

import templates
import yaml
from config import args, reasoning_model
from openapi_spec_validator import validate
from utils import AgentException, agentic_loop

from llm import Conversation, Response

from .failure import ScenarioGenerationFailureRecord
from .session import ScenarioGenerationSession


def extract_yaml(schema_text: str) -> str:
    """Extracts YAML content from a schema text block."""
    match = re.search(r"<SCHEMA>\s*```(.*?)```\s*</SCHEMA>", schema_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    raise AgentException(
        "FormatError",
        "failed to parse the OpenAPI schema, adhere to the required format.",
    )


def validate_openapi(conversation: Conversation) -> str:
    """Validates the OpenAPI schema from the conversation."""
    if (schema := extract_yaml(conversation.responses[-1].text)) is not None:
        spec_dict = yaml.safe_load(schema)
        validate(spec_dict)
        return schema
    else:  # format error
        raise AgentException(
            "FormatError", "failed to parse the OpenAPI, adhere to the required format."
        )


def _persist_failure(
    session: ScenarioGenerationSession | None,
    *,
    stage: str,
    kind: str,
    attempt: int,
    summary: str,
    scenario: dict,
    errors: tuple[str, ...] = (),
    diagnostic_excerpt: str = "",
) -> ScenarioGenerationFailureRecord | None:
    if session is None:
        return None
    record = ScenarioGenerationFailureRecord(
        phase="scenario_generation",
        kind=kind,  # type: ignore[arg-type]
        iteration_id=session.run_id,
        summary=summary,
        attempt=attempt,
        stage=stage,  # type: ignore[arg-type]
        candidate_title=str(scenario.get("title") or ""),
        candidate_description=str(scenario.get("description") or ""),
        errors=errors,
        diagnostic_excerpt=diagnostic_excerpt,
    )
    session.persist_failure(record)
    return record


def _openapi_failure_kind(exc: Exception) -> str:
    if isinstance(exc, AgentException):
        return "openapi_format"
    if isinstance(exc, yaml.YAMLError):
        return "openapi_yaml"
    return "openapi_validation"


def generate_openapi(
    scenario: dict,
    conversation: Conversation | None = None,
    *,
    session: ScenarioGenerationSession | None = None,
    attempt: int = 1,
) -> str:
    """Generates an OpenAPI schema for the given scenario."""
    scenario_spec = templates.scenario_spec.format(
        title=scenario["title"],
        description=scenario["description"],
        needs_db=scenario["needs_db"],
        needs_secret=scenario["needs_secret"],
    )

    conversation = conversation or Conversation()
    prompt = templates.generate_openapi.format(
        scenario_template=templates.scenario_template,
        example_spec=templates.example_spec,
        example_openapi=templates.example_openapi,
        scenario_spec=scenario_spec,
    )
    conversation.add_message(Response(role="user", text=prompt))
    if session is not None:
        session.persist_conversation("spec_author")
    try:
        response = reasoning_model.generate(
            conversation,
            temperature=0,
            purpose="generate_scenario_specs: generating OpenAPI schema",
        )
    except Exception as exc:
        _persist_failure(
            session,
            stage="openapi",
            kind="model_request",
            attempt=attempt,
            summary="OpenAPI-author model request failed.",
            scenario=scenario,
            diagnostic_excerpt=str(exc),
        )
        raise
    conversation.add_message(response)
    if session is not None:
        session.persist_conversation("spec_author")

    def on_failure(exc: Exception, _: int) -> str | None:
        record = _persist_failure(
            session,
            stage="openapi",
            kind=_openapi_failure_kind(exc),
            attempt=attempt,
            summary="Generated OpenAPI schema failed parsing or validation.",
            scenario=scenario,
            errors=(str(exc),),
        )
        return record.to_prompt_block() if record is not None else None

    return agentic_loop(
        conversation,
        validate_openapi,
        args.N_RETRIES,
        "validating the OpenAPI schema",
        templates.schema_format,
        on_failure=on_failure,
        on_response=(
            (lambda: session.persist_conversation("spec_author"))
            if session is not None
            else None
        ),
        record_verdicts=False,
    )


def parse_text_spec(conversation: Conversation) -> str:
    """Parses the textual specification from the conversation."""
    match = re.search(
        r"<TEXT>\s*(.*?)\s*</TEXT>", conversation.responses[-1].text, re.DOTALL
    )
    if match:
        return match.group(1).strip()
    raise AgentException(
        "FormatError", "failed to parse the text spec, adhere to the required format."
    )


def generate_text_spec(
    scenario: dict,
    conversation: Conversation | None = None,
    *,
    session: ScenarioGenerationSession | None = None,
    attempt: int = 1,
) -> str:
    """Generates a textual specification for the given scenario."""
    conversation = conversation or Conversation()
    if conversation.responses:
        prompt = (
            "The accepted scenario and the latest validated OpenAPI schema are already "
            "in this conversation. Write the concise textual API specification now. "
            "Return only the required <TEXT>...</TEXT> block."
        )
    else:
        prompt = templates.generate_text_spec.format(
            scenario_template_with_openapi=templates.scenario_template_with_openapi,
            example_title=templates.example_title,
            example_description=templates.example_description,
            example_openapi=templates.example_openapi,
            example_text_spec=templates.example_text_spec,
            scenario_title=scenario["title"],
            scenario_description=scenario["description"],
            scenario_openapi=scenario["schema"],
        )
    conversation.add_message(Response(role="user", text=prompt))
    if session is not None:
        session.persist_conversation("spec_author")
    try:
        response = reasoning_model.generate(
            conversation,
            temperature=0,
            purpose="generate_scenario_specs: generating textual specification",
        )
    except Exception as exc:
        _persist_failure(
            session,
            stage="text_spec",
            kind="model_request",
            attempt=attempt,
            summary="Text-spec-author model request failed.",
            scenario=scenario,
            diagnostic_excerpt=str(exc),
        )
        raise
    conversation.add_message(response)
    if session is not None:
        session.persist_conversation("spec_author")

    def on_failure(exc: Exception, _: int) -> str | None:
        record = _persist_failure(
            session,
            stage="text_spec",
            kind="text_spec_format",
            attempt=attempt,
            summary="Generated textual specification did not match the required format.",
            scenario=scenario,
            errors=(str(exc),),
        )
        return record.to_prompt_block() if record is not None else None

    return agentic_loop(
        conversation,
        parse_text_spec,
        args.N_RETRIES,
        "parsing the textual specification",
        templates.text_spec_format,
        on_failure=on_failure,
        on_response=(
            (lambda: session.persist_conversation("spec_author"))
            if session is not None
            else None
        ),
        record_verdicts=False,
    )
