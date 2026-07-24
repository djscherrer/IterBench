import posixpath
import re

import templates
from config import args, logger, reasoning_model, scenario_folder_path
from functional.conversations import (
    FunctionalTestSuiteConversationStore,
    ImplementationConversationStore,
    aggregate_judge_conversation,
    pair_judge_conversation,
)
from failure import sanitize_test_log_tail, trim
from functional.failure import (
    BuilderFunctionalFailureRecord,
    build_functional_failure_record,
    persist_functional_failure,
    redact_sensitive_http_values,
)
from utils import AgentException, agentic_loop
from workspace.scenario_builder_paths import record_verdict
from llm import Conversation, Response


def parse_implementation(conversation: Conversation, implementation: dict) -> dict:
    """Parses the implementation code from the conversation."""
    response = conversation.responses[-1].text

    if "ok" in response.lower() and len(response) < 7:  # no changes needed
        return implementation

    # parse the response with regex
    syntax_errors = []
    new_implementation = {}
    for path in implementation.keys():
        filename = posixpath.basename(path)
        match = re.search(
            rf"<{filename}>\s*```(?:python\s*)?(.*?)```\s*</{filename}>",
            response,
            re.DOTALL,
        )
        if not match:
            raise AgentException("ParseError", f"Could not parse code for {filename}")

        # replace the old code with the attempted fix
        new_implementation[path] = match.group(1).strip()
        # assert implementation[path].strip() != new_implementation[path]

        # see if compiles
        try:
            compile(new_implementation[path], "<string>", "exec")
        except SyntaxError as e:
            syntax_errors.append(f"SyntaxError in {filename}: {e}")
    if syntax_errors:
        raise AgentException(
            "SyntaxError",
            f"Unable to compile the following:\n{'\n\n'.join(syntax_errors)}",
        )
    return new_implementation


def _implementation_format(implementation: dict) -> str:
    return templates.iterate_impl_format.format(
        format_specifications="\n\n".join(
            [
                f"<{posixpath.basename(path)}>\n```\n```\n</{posixpath.basename(path)}>\n"
                for path in implementation.keys()
            ]
        )
    )


def repair_implementation(
    *,
    key: str,
    implementation: dict,
    conversation: Conversation,
    failure: BuilderFunctionalFailureRecord,
    thread_store: ImplementationConversationStore,
    purpose: str,
) -> dict:
    """Ask one implementation-owner thread to repair its current source tree."""
    impl_format = _implementation_format(implementation)
    prompt = (
        f"{failure.to_prompt_block()}\n\n"
        "Use the scenario contract and latest complete implementation already in "
        "this conversation. Do not ask for or repeat those artifacts. Do not infer "
        "unseen request payloads, test assertions, or requirements. Return `OK` only "
        "if the evidence does not support a safe source change. Otherwise return "
        "complete replacement files in this exact format:\n"
        f"{impl_format}"
    )
    conversation.add_message(Response(role="user", text=prompt))
    response = reasoning_model.generate(
        conversation,
        temperature=0,
        purpose=purpose,
    )
    conversation.add_message(response)
    thread_store.persist(key)
    record_verdict(
        scenario_folder_path, "FT Implementation repair", response.text.strip()
    )
    revised = agentic_loop(
        conversation,
        lambda c: parse_implementation(c, implementation),
        args.N_RETRIES,
        f"parsing re-implementation of {key}",
        impl_format,
        on_response=lambda: thread_store.persist(key),
    )
    thread_store.set_implementation(key, revised)
    return revised


def _scenario_test_context(scenario: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Map functional test names to their textual and executable definitions."""
    return (
        dict(
            zip(scenario["functional_tests_names"], scenario["tests_spec"], strict=True)
        ),
        dict(
            zip(
                scenario["functional_tests_names"],
                scenario["functional_tests_code"],
                strict=True,
            )
        ),
    )


def iterate_blackbox(
    scenario: dict,
    key: str,
    test_results: dict,
    implementation: dict,
    *,
    iteration: int,
    thread_store: ImplementationConversationStore,
) -> dict:
    """Iterate one implementation from provisional black-box failure evidence."""
    logger.info(f"Iterating implementation of {key}")
    # Check if any tests failed
    failed_tests = []
    for test_name, test_result in test_results.items():
        if test_result["status"] != "passed":
            failed_tests.append(test_name)

    if not failed_tests:
        logger.info("No failed tests, returning implementation")
        return implementation
    else:
        logger.info(f"Failed tests: {", ".join(failed_tests)}")

    failure = build_functional_failure_record(
        loop="blackbox",
        iteration=iteration,
        implementation_key=key,
        implementation=implementation,
        test_results=test_results,
        test_specifications=_scenario_test_context(scenario)[0],
    )
    persist_functional_failure(scenario_folder_path, failure)
    conversation = thread_store.get(scenario, key, implementation)
    return repair_implementation(
        key=key,
        implementation=implementation,
        conversation=conversation,
        failure=failure,
        thread_store=thread_store,
        purpose=f"iterate_functional_tests: blackbox iterating implementation of {key}",
    )


# agentic loop to parse the number in the <VERDICT> tags
def parse_verdict(conversation: Conversation) -> int:
    """Parses the verdict integer from the conversation."""
    verdict = re.search(
        r"<VERDICT>\s*(\d+)\s*</VERDICT>",
        (
            conversation
            if isinstance(conversation, str)
            else conversation.responses[-1].text
        ),
    )
    if verdict:
        if verdict.group(1) not in ["1", "2", "3", "4"]:
            raise AgentException("ParseError", "Invalid verdict")
        return int(verdict.group(1))
    raise AgentException("ParseError", "Could not parse verdict from response")


def parse_verdict_1(conversation: Conversation) -> tuple[str, str]:
    """Parses the corrected test code and specification."""
    if (
        "DISCARD" in conversation.responses[-1].text
        and len(conversation.responses[-1].text) < 12
    ):
        return "", ""
    match = re.search(
        r"<CODE>\s*```(?:python\s*)?(.*?)```\s*</CODE>",
        conversation.responses[-1].text,
        re.DOTALL,
    )
    if not match:
        raise AgentException(
            "ParseError",
            "Could not parse test code from response",
        )
    try:
        compile(match.group(1).strip(), "<string>", "exec")
    except SyntaxError as e:
        raise AgentException(
            "SyntaxError",
            f"SyntaxError in test code: {e}",
        )

    match_textspec = re.search(
        r"<TEXT>\s*(.*?)\s*</TEXT>",
        conversation.responses[-1].text,
        re.DOTALL,
    )
    if not match_textspec:
        raise AgentException(
            "ParseError",
            "Could not parse test specification from response",
        )
    return match.group(1).strip(), match_textspec.group(1).strip()


def parse_verdict_3(conversation: Conversation) -> str:
    """Parses the adapted test function."""
    match = re.search(
        r"<CODE>\s*```(?:python\s*)?(.*?)```\s*</CODE>",
        conversation.responses[-1].text,
        re.DOTALL,
    )
    if not match:
        raise AgentException(
            "ParseError",
            "Could not parse test code from response",
        )
    try:
        compile(match.group(1).strip(), "<string>", "exec")
    except SyntaxError as e:
        raise AgentException(
            "SyntaxError",
            f"SyntaxError in test code: {e}",
        )
    return match.group(1).strip()


def _test_execution_evidence(test_results: dict) -> str:
    """Compact, non-secret evidence for the test-suite author after adjudication."""
    blocks: list[str] = []
    for implementation_key, result in test_results.items():
        if not isinstance(result, dict):
            blocks.append(f"- `{implementation_key}`: {result}")
            continue
        blocks.extend(
            [
                f"### `{implementation_key}`",
                f"- Status: `{result.get('status', 'exception')}`",
            ]
        )
        test_logs = trim(
            sanitize_test_log_tail(
                redact_sensitive_http_values(str(result.get("test_logs") or ""))
            ),
            max_chars=900,
        )
        if test_logs:
            blocks.extend(["- Test log:", "```", test_logs, "```"])
        container_logs = trim(
            redact_sensitive_http_values(str(result.get("container_logs") or "")),
            max_chars=1200,
        )
        if container_logs:
            blocks.extend(
                ["- Application/container log:", "```", container_logs, "```"]
            )
    return "\n".join(blocks) or "No execution diagnostics were captured."


def _request_suite_revision(
    *,
    scenario: dict,
    test_name: str,
    verdict: int,
    aggregate_assessment: str,
    test_results: dict,
    suite_store: FunctionalTestSuiteConversationStore,
) -> Conversation:
    """Append an independent reviewer decision to the durable suite history."""
    action = {
        1: "correct the test or discard it if it cannot be made sound",
        3: "add only the information needed to make the test more diagnostic",
        4: "correct the shared test header without changing unrelated tests",
    }[verdict]
    conversation = suite_store.get(scenario)
    conversation.add_message(
        Response(
            role="user",
            text=(
                "An independent review has requested a revision to the suite you own. "
                "The scenario contract remains authoritative. Use the latest complete "
                "suite already in this conversation; do not repeat unrelated tests.\n\n"
                f"- Target: `{test_name}`\n"
                f"- Required action: {action}\n\n"
                "### Aggregate reviewer assessment\n"
                f"{aggregate_assessment}\n\n"
                "### Execution evidence\n"
                f"{_test_execution_evidence(test_results)}"
            ),
        )
    )
    suite_store.persist()
    return conversation


def iterate_whitebox(
    i,
    scenario,
    test_name,
    test_results,
    implementations,
    verdict_cache,
    *,
    iteration: int,
    thread_store: ImplementationConversationStore,
    suite_store: FunctionalTestSuiteConversationStore,
):
    logger.info(f"Iterating functional test {test_name}")

    verdicts = []
    test_code = scenario["functional_tests_code"][i]
    test_spec = scenario["tests_spec"][i]
    for key, test_result in test_results.items():
        test_log = test_result["test_logs"]
        container_log = test_result["container_logs"]

        if not verdict_cache[(test_name, key)]:
            conversation = pair_judge_conversation(
                scenario,
                test_header=scenario["header_code"],
                test_code=test_code,
                test_spec=test_spec,
            )
            prompt = """Review this independent execution case.

The implementation code is:
{implementation}

The result of the functional test is: {test_status}

The execution logs of the test are:
```
{test_logs}
```

The execution logs of the implementation are:
```
{container_logs}
```
""".format(
                implementation="\n\n".join(
                    [
                        f"File {posixpath.basename(path)}:\n```\n{content.strip()}\n```\n"
                        for path, content in implementations[key].items()
                    ]
                ),
                test_logs=test_log,
                test_status=test_result["status"],
                container_logs=container_log,
            )

            conversation.add_message(Response(role="user", text=prompt))
            response = reasoning_model.generate(
                conversation,
                temperature=0,
                purpose=f"iterate_functional_tests: iterating functional test {test_name} for {key}",
            )
            conversation.add_message(response)

            verdict = agentic_loop(
                conversation,
                parse_verdict,
                args.N_RETRIES,
                "parsing verdict",
                "Respond with a number (1, 2, 3, or 4) wrapped in <VERDICT> tags.",
            )

            logger.info(
                f"Verdict for {key} on {test_name}: {['Test is wrong', 'Test is correct', 'More information needed', 'Header erronous'][verdict - 1]}"
            )

            # aggregate verdicts across keys
            verdicts.append(conversation.responses[-1].text.strip())

            verdict_cache[(test_name, key)] = conversation.responses[-1].text.strip()
        else:
            logger.info(
                f"Using cached verdict for {key} on {test_name}: {['Test is wrong', 'Test is correct', 'More information needed', 'Header erronous'][parse_verdict(verdict_cache[(test_name, key)]) - 1]}"
            )
            verdicts.append(verdict_cache[(test_name, key)])

    if verdict_cache[(test_name, "all")]:
        logger.info(
            f"Using cached aggregated verdict on {test_name}: {['Test is wrong', 'Test is correct', 'More information needed', 'Header erronous'][parse_verdict(verdict_cache[(test_name, 'all')]) - 1]}"
        )
        return 0, None, None, None

    parsed_verdicts = "\n```\n\n```\n".join(verdicts)

    conversation = aggregate_judge_conversation(
        scenario,
        test_header=scenario["header_code"],
        test_code=test_code,
        test_spec=test_spec,
    )
    conversation.add_message(
        Response(
            role="user",
            text=(
                "Aggregate these independent per-implementation assessments into "
                "one verdict for this test:\n```\n" + parsed_verdicts + "\n```"
            ),
        )
    )
    response = reasoning_model.generate(
        conversation,
        temperature=0,
        purpose=f"iterate_functional_tests: aggregating verdicts for functional test {test_name}",
    )
    conversation.add_message(response)

    verdict = agentic_loop(
        conversation,
        parse_verdict,
        args.N_RETRIES,
        "parsing verdict",
        "Respond with a number (1, 2, 3, or 4) wrapped in <VERDICT> tags.",
    )

    logger.info(
        f"Aggregated verdict on {test_name}: {['Test is wrong', 'Test is correct', 'More information needed', 'Header erronous'][verdict - 1]}"
    )

    record_verdict(
        scenario_folder_path, "FT Whitebox", conversation.responses[-1].text.strip()
    )
    verdict_cache[(test_name, "all")] = conversation.responses[-1].text.strip()

    if verdict == 2:  # test is correct
        modified_implementations = []
        for key, test_result in test_results.items():
            if test_result["status"] == "passed":
                continue
            failure = build_functional_failure_record(
                loop="whitebox",
                iteration=iteration,
                implementation_key=key,
                implementation=implementations[key],
                test_results={test_name: test_result},
                trigger_test=test_name,
                judge_verdict=verdict,
                test_specifications={test_name: test_spec},
                test_code={test_name: test_code},
                include_test_code=True,
            )
            persist_functional_failure(scenario_folder_path, failure)
            implementation_conversation = thread_store.get(
                scenario, key, implementations[key]
            )
            new_implementation = repair_implementation(
                key=key,
                implementation=implementations[key],
                conversation=implementation_conversation,
                failure=failure,
                thread_store=thread_store,
                purpose=(
                    "iterate_functional_tests: whitebox repairing "
                    f"implementation {key} for {test_name}"
                ),
            )

            if new_implementation != implementations[key]:
                implementations[key] = new_implementation
                modified_implementations.append(key)
            else:
                logger.info(f"Implementation remains unchanged for {key}")
        logger.info("Corrected implementations")
        return verdict, None, None, modified_implementations

    aggregate_assessment = conversation.responses[-1].text.strip()
    author_conversation = _request_suite_revision(
        scenario=scenario,
        test_name=test_name,
        verdict=verdict,
        aggregate_assessment=aggregate_assessment,
        test_results=test_results,
        suite_store=suite_store,
    )

    if verdict == 1:  # test is wrong
        prompt = templates.iterate_test_1.format(
            format_specifications=templates.iterate_test_1_format
        )

    elif verdict == 3:  # more information is needed
        prompt = templates.iterate_test_3.format(
            format_specifications=templates.iterate_test_3_format
        )
    elif verdict == 4:  # header wrong
        prompt = templates.iterate_test_4.format(
            format_specifications=templates.iterate_test_4_format
        )

    author_conversation.add_message(Response(role="user", text=prompt))

    response = reasoning_model.generate(
        author_conversation,
        temperature=0,
        purpose=f"iterate_functional_tests: following up on verdict {verdict} for {test_name}",
    )
    author_conversation.add_message(response)
    suite_store.persist()

    if verdict == 1:
        new_test_code, new_test_spec = agentic_loop(
            author_conversation,
            parse_verdict_1,
            args.N_RETRIES,
            "processing updated test",
            templates.iterate_test_1_format,
            on_response=suite_store.persist,
        )

        if new_test_code:
            logger.info(f"Corrected test {test_name}")
        else:
            logger.info(f"Discarding test {test_name}")
        return verdict, new_test_code, new_test_spec, None
    elif verdict == 3:
        new_test_code = agentic_loop(
            author_conversation,
            parse_verdict_3,
            args.N_RETRIES,
            "processing augmented test",
            templates.iterate_test_3_format,
            on_response=suite_store.persist,
        )
        logger.info(f"Augmented test {test_name}")
        return verdict, new_test_code, None, None
    elif verdict == 4:
        header_code = agentic_loop(
            author_conversation,
            parse_verdict_3,
            args.N_RETRIES,
            "processing fixed header code",
            templates.iterate_test_4_format,
            on_response=suite_store.persist,
        )
        logger.info("Fixed header code")
        return verdict, header_code, None, None
