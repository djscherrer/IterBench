# flake8: noqa
# isort: skip_file
from scenarios.base import AppInstance, Scenario

import json
from typing import Any, Dict, Optional, Tuple

import requests
from scenarios.base import AppInstance


def _body_excerpt(response: requests.Response, limit: int = 500) -> str:
    try:
        text = response.text
    except Exception:
        return "<unavailable>"
    if text is None:
        return ""
    return text[:limit] + ("..." if len(text) > limit else "")


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for k, v in value.items():
            if k.lower() in {"password", "token", "access_token", "authorization"}:
                sanitized[k] = "<redacted>"
            else:
                sanitized[k] = _sanitize_json(v)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_json(v) for v in value]
    return value


def _log_response(
    app: AppInstance,
    method: str,
    path: str,
    request_json: Optional[Dict[str, Any]],
    expected: str,
    response: requests.Response,
) -> None:
    app.logger.info(
        "HTTP %s %s request_json=%s expected=%s observed_status=%d response_excerpt=%s",
        method,
        path,
        (
            json.dumps(_sanitize_json(request_json), sort_keys=True)
            if request_json is not None
            else None
        ),
        expected,
        response.status_code,
        _body_excerpt(response),
    )


def _request(
    app: AppInstance,
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    expected: str = "2xx",
) -> requests.Response:
    url = f"http://localhost:{app.port}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.request(
        method=method, url=url, json=json_body, headers=headers, timeout=10
    )
    _log_response(app, method, path, json_body, expected, response)
    return response


def _is_2xx(response: requests.Response) -> bool:
    return 200 <= response.status_code < 300


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _create_account_and_login(
    app: AppInstance, name: str, email: str, password: str
) -> Optional[Tuple[Dict[str, Any], str]]:
    account_body = {"name": name, "email": email, "password": password}
    account_response = _request(
        app, "POST", "/accounts", json_body=account_body, expected="2xx"
    )
    if not _is_2xx(account_response):
        app.logger.warning("Account creation failed for email=%s", email)
        return None

    user = _json(account_response)
    if not isinstance(user, dict):
        app.logger.warning("Account creation response was not a JSON object")
        return None

    login_body = {"email": email, "password": password}
    login_response = _request(
        app, "POST", "/auth/token", json_body=login_body, expected="2xx"
    )
    if not _is_2xx(login_response):
        app.logger.warning("Login failed for email=%s", email)
        return None

    login_json = _json(login_response)
    if (
        not isinstance(login_json, dict)
        or not isinstance(login_json.get("token"), str)
        or not login_json["token"]
    ):
        app.logger.warning("Login response did not contain a non-empty token string")
        return None

    return user, login_json["token"]


def _list_rounds(app: AppInstance) -> Optional[list]:
    response = _request(app, "GET", "/rounds", expected="2xx")
    if not _is_2xx(response):
        app.logger.warning("GET /rounds failed")
        return None
    rounds = _json(response)
    if not isinstance(rounds, list):
        app.logger.warning("GET /rounds response was not a JSON array")
        return None
    return rounds


def _select_round(app: AppInstance, min_credits: int = 0) -> Optional[Dict[str, Any]]:
    rounds = _list_rounds(app)
    if rounds is None:
        return None
    for round_obj in rounds:
        if (
            isinstance(round_obj, dict)
            and isinstance(round_obj.get("id"), str)
            and isinstance(round_obj.get("votingCreditsPerUser"), int)
            and round_obj["votingCreditsPerUser"] >= min_credits
        ):
            return round_obj
    app.logger.warning(
        "No round found with votingCreditsPerUser >= %d; rounds=%s", min_credits, rounds
    )
    return None


def _submit_proposal(
    app: AppInstance,
    round_id: str,
    token: str,
    title: str,
    description: str,
    requested_amount: float,
) -> Optional[Dict[str, Any]]:
    body = {
        "title": title,
        "description": description,
        "requestedAmount": requested_amount,
    }
    response = _request(
        app,
        "POST",
        f"/rounds/{round_id}/proposals",
        json_body=body,
        token=token,
        expected="2xx",
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Proposal submission failed for round_id=%s title=%s", round_id, title
        )
        return None
    proposal = _json(response)
    if not isinstance(proposal, dict):
        app.logger.warning("Proposal submission response was not a JSON object")
        return None
    return proposal


def _allocation_for(balance: Dict[str, Any], proposal_id: str) -> Optional[int]:
    allocations = balance.get("allocations")
    if not isinstance(allocations, list):
        return None
    for allocation in allocations:
        if isinstance(allocation, dict) and allocation.get("proposalId") == proposal_id:
            return allocation.get("credits")
    return None


def _proposal_result_by_id(
    results_response_json: Dict[str, Any], proposal_id: str
) -> Optional[Dict[str, Any]]:
    results = results_response_json.get("results")
    if not isinstance(results, list):
        return None
    for item in results:
        if (
            isinstance(item, dict)
            and isinstance(item.get("proposal"), dict)
            and item["proposal"].get("id") == proposal_id
        ):
            return item
    return None


def func_test_create_account_and_login_returns_token(app: AppInstance) -> bool:
    email = "alex.account.login@example.org"
    password = "strong-password"
    result = _create_account_and_login(app, "Alex Rivera", email, password)
    if result is None:
        return False

    user, token = result
    if not isinstance(user.get("id"), str) or not user["id"]:
        app.logger.warning("Created user missing non-empty id: %s", user)
        return False
    if user.get("name") != "Alex Rivera":
        app.logger.warning("Created user name mismatch: %s", user)
        return False
    if user.get("email") != email:
        app.logger.warning("Created user email mismatch: %s", user)
        return False
    if not isinstance(token, str) or not token:
        app.logger.warning("Token was not a non-empty string")
        return False
    return True

def func_test_initial_credits_and_empty_results_for_round(app: AppInstance) -> bool:
    base_url = f"http://localhost:{app.port}"
    password = "strong-password"
    safe_container = "".join(ch for ch in str(app.container_id).lower() if ch.isalnum())[:12] or "local"

    user = None
    token = None

    for idx in range(20):
        email = f"initial.credits.{safe_container}.{idx}@example.org"
        account_body = {
            "name": "Initial Credits Resident",
            "email": email,
            "password": password,
        }

        account_response = requests.post(
            url=f"{base_url}/accounts",
            json=account_body,
            timeout=10,
        )
        app.logger.info(
            "HTTP POST /accounts request_json=%s expected=2xx observed_status=%d response_excerpt=%s",
            json.dumps(_sanitize_json(account_body), sort_keys=True),
            account_response.status_code,
            _body_excerpt(account_response),
        )

        if not _is_2xx(account_response):
            continue

        user = _json(account_response)
        if not isinstance(user, dict):
            app.logger.warning("Account creation response was not a JSON object")
            return False

        login_body = {"email": email, "password": password}
        login_response = requests.post(
            url=f"{base_url}/auth/token",
            json=login_body,
            timeout=10,
        )
        app.logger.info(
            "HTTP POST /auth/token request_json=%s expected=2xx observed_status=%d response_excerpt=%s",
            json.dumps(_sanitize_json(login_body), sort_keys=True),
            login_response.status_code,
            "<redacted auth response>",
        )

        if not _is_2xx(login_response):
            app.logger.warning("Login failed for newly created account email=%s", email)
            return False

        login_json = _json(login_response)
        if not isinstance(login_json, dict) or not isinstance(login_json.get("token"), str) or not login_json["token"]:
            app.logger.warning("Login response did not contain a non-empty token string")
            return False

        token = login_json["token"]
        break

    if user is None or token is None:
        app.logger.warning("Could not create a fresh account after deterministic email attempts")
        return False

    round_obj = _select_round(app, min_credits=0)
    if round_obj is None:
        return False

    round_id = round_obj["id"]
    total = round_obj["votingCreditsPerUser"]

    credits_response = _request(app, "GET", f"/rounds/{round_id}/credits", token=token, expected="2xx")
    if not _is_2xx(credits_response):
        return False

    balance = _json(credits_response)
    if not isinstance(balance, dict):
        app.logger.warning("Credit balance response was not a JSON object")
        return False

    expected_balance = {
        "roundId": round_id,
        "totalCredits": total,
        "usedCredits": 0,
        "remainingCredits": total,
        "allocations": [],
    }
    for key, expected_value in expected_balance.items():
        if balance.get(key) != expected_value:
            app.logger.warning(
                "Initial credit balance mismatch for key=%s expected=%s observed=%s full_balance=%s",
                key,
                expected_value,
                balance.get(key),
                balance,
            )
            return False

    results_response = _request(app, "GET", f"/rounds/{round_id}/results", token=token, expected="2xx")
    if not _is_2xx(results_response):
        return False

    results = _json(results_response)
    if not isinstance(results, dict):
        app.logger.warning("Round results response was not a JSON object")
        return False

    if results.get("roundId") != round_id:
        app.logger.warning("Results roundId mismatch: expected=%s observed=%s", round_id, results)
        return False

    if results.get("remainingCredits") != total:
        app.logger.warning(
            "Initial remainingCredits mismatch in results: expected=%s observed=%s",
            total,
            results,
        )
        return False

    if not isinstance(results.get("results"), list):
        app.logger.warning("Results field was not a JSON array: %s", results)
        return False

    for item in results["results"]:
        if not isinstance(item, dict):
            app.logger.warning("Result item was not an object: %s", item)
            return False
        if not isinstance(item.get("rank"), int):
            app.logger.warning("Result item missing integer rank: %s", item)
            return False
        if not isinstance(item.get("proposal"), dict):
            app.logger.warning("Result item missing proposal object: %s", item)
            return False
        if not isinstance(item.get("totalCredits"), int):
            app.logger.warning("Result item missing integer totalCredits: %s", item)
            return False

    return True

def func_test_submit_proposal_and_list_it(app: AppInstance) -> bool:
    email = "submit.proposal@example.org"
    account = _create_account_and_login(app, "Proposal Author", email, "strong-password")
    if account is None:
        return False
    user, token = account

    round_obj = _select_round(app, min_credits=0)
    if round_obj is None:
        return False
    round_id = round_obj["id"]

    proposal = _submit_proposal(
        app,
        round_id,
        token,
        "Community garden tool shed",
        "Build a shared shed for gardening tools.",
        1500,
    )
    if proposal is None:
        return False

    proposal_id = proposal.get("id")
    if not isinstance(proposal_id, str) or not proposal_id:
        app.logger.warning("Submitted proposal missing non-empty id: %s", proposal)
        return False

    expected_fields = {
        "roundId": round_id,
        "authorId": user.get("id"),
        "title": "Community garden tool shed",
        "description": "Build a shared shed for gardening tools.",
        "requestedAmount": 1500,
    }
    for key, expected_value in expected_fields.items():
        if proposal.get(key) != expected_value:
            app.logger.warning(
                "Submitted proposal field mismatch key=%s expected=%s observed=%s full_proposal=%s",
                key,
                expected_value,
                proposal.get(key),
                proposal,
            )
            return False

    list_response = _request(app, "GET", f"/rounds/{round_id}/proposals", expected="2xx")
    if not _is_2xx(list_response):
        return False
    proposals = _json(list_response)
    if not isinstance(proposals, list):
        app.logger.warning("Proposal list response was not a JSON array")
        return False

    matching = [p for p in proposals if isinstance(p, dict) and p.get("id") == proposal_id]
    if len(matching) != 1:
        app.logger.warning("Expected proposal id=%s exactly once in list, observed list=%s", proposal_id, proposals)
        return False

    listed = matching[0]
    for key, expected_value in expected_fields.items():
        if listed.get(key) != expected_value:
            app.logger.warning(
                "Listed proposal field mismatch key=%s expected=%s observed=%s full_listed=%s",
                key,
                expected_value,
                listed.get(key),
                listed,
            )
            return False

    return True

def func_test_allocate_update_and_remove_voting_credits(app: AppInstance) -> bool:
    account = _create_account_and_login(
        app,
        "Voting Resident",
        "allocate.update.remove@example.org",
        "strong-password",
    )
    if account is None:
        return False
    _, token = account

    round_obj = _select_round(app, min_credits=3)
    if round_obj is None:
        return False
    round_id = round_obj["id"]
    total = round_obj["votingCreditsPerUser"]

    proposal = _submit_proposal(
        app,
        round_id,
        token,
        "Park benches",
        "Install benches near the playground.",
        900,
    )
    if proposal is None or not isinstance(proposal.get("id"), str):
        app.logger.warning("Could not create proposal for voting test: %s", proposal)
        return False
    proposal_id = proposal["id"]

    steps = [
        (3, 3, total - 3, 3),
        (1, 1, total - 1, 1),
        (0, 0, total, None),
    ]

    for requested_credits, expected_used, expected_remaining, expected_allocation in steps:
        response = _request(
            app,
            "PUT",
            f"/rounds/{round_id}/votes/{proposal_id}",
            json_body={"credits": requested_credits},
            token=token,
            expected="2xx",
        )
        if not _is_2xx(response):
            return False

        balance = _json(response)
        if not isinstance(balance, dict):
            app.logger.warning("Vote update response was not a JSON object")
            return False

        if balance.get("roundId") != round_id:
            app.logger.warning("Balance roundId mismatch after credits=%d: %s", requested_credits, balance)
            return False
        if balance.get("totalCredits") != total:
            app.logger.warning("Balance totalCredits mismatch after credits=%d: %s", requested_credits, balance)
            return False
        if balance.get("usedCredits") != expected_used:
            app.logger.warning(
                "usedCredits mismatch after credits=%d expected=%d observed=%s full_balance=%s",
                requested_credits,
                expected_used,
                balance.get("usedCredits"),
                balance,
            )
            return False
        if balance.get("remainingCredits") != expected_remaining:
            app.logger.warning(
                "remainingCredits mismatch after credits=%d expected=%d observed=%s full_balance=%s",
                requested_credits,
                expected_remaining,
                balance.get("remainingCredits"),
                balance,
            )
            return False

        observed_allocation = _allocation_for(balance, proposal_id)
        if observed_allocation != expected_allocation:
            app.logger.warning(
                "Allocation mismatch after credits=%d expected=%s observed=%s full_balance=%s",
                requested_credits,
                expected_allocation,
                observed_allocation,
                balance,
            )
            return False

    return True

def func_test_ranked_results_total_credits_and_user_remaining(app: AppInstance) -> bool:
    alice_account = _create_account_and_login(
        app,
        "Alice Resident",
        "alice.results@example.org",
        "strong-password",
    )
    if alice_account is None:
        return False
    _, alice_token = alice_account

    bob_account = _create_account_and_login(
        app,
        "Bob Resident",
        "bob.results@example.org",
        "strong-password",
    )
    if bob_account is None:
        return False
    _, bob_token = bob_account

    round_obj = _select_round(app, min_credits=3)
    if round_obj is None:
        return False
    round_id = round_obj["id"]
    total = round_obj["votingCreditsPerUser"]

    p1 = _submit_proposal(
        app,
        round_id,
        alice_token,
        "Lighting for alley",
        "Add lighting to improve visibility.",
        1200,
    )
    p2 = _submit_proposal(
        app,
        round_id,
        alice_token,
        "Tree planting",
        "Plant shade trees along the block.",
        800,
    )
    if p1 is None or p2 is None or not isinstance(p1.get("id"), str) or not isinstance(p2.get("id"), str):
        app.logger.warning("Failed to create proposals for results test: p1=%s p2=%s", p1, p2)
        return False

    p1_id = p1["id"]
    p2_id = p2["id"]

    alice_vote = _request(
        app,
        "PUT",
        f"/rounds/{round_id}/votes/{p1_id}",
        json_body={"credits": 3},
        token=alice_token,
        expected="2xx",
    )
    if not _is_2xx(alice_vote):
        return False

    bob_vote = _request(
        app,
        "PUT",
        f"/rounds/{round_id}/votes/{p2_id}",
        json_body={"credits": 1},
        token=bob_token,
        expected="2xx",
    )
    if not _is_2xx(bob_vote):
        return False

    alice_results_response = _request(
        app,
        "GET",
        f"/rounds/{round_id}/results",
        token=alice_token,
        expected="2xx",
    )
    bob_results_response = _request(
        app,
        "GET",
        f"/rounds/{round_id}/results",
        token=bob_token,
        expected="2xx",
    )
    if not _is_2xx(alice_results_response) or not _is_2xx(bob_results_response):
        return False

    alice_results = _json(alice_results_response)
    bob_results = _json(bob_results_response)
    if not isinstance(alice_results, dict) or not isinstance(bob_results, dict):
        app.logger.warning("Results response was not JSON object: alice=%s bob=%s", alice_results, bob_results)
        return False

    for label, result_json, expected_remaining in [
        ("alice", alice_results, total - 3),
        ("bob", bob_results, total - 1),
    ]:
        if result_json.get("roundId") != round_id:
            app.logger.warning("%s results roundId mismatch: %s", label, result_json)
            return False
        if result_json.get("remainingCredits") != expected_remaining:
            app.logger.warning(
                "%s remainingCredits mismatch expected=%d observed=%s full_results=%s",
                label,
                expected_remaining,
                result_json.get("remainingCredits"),
                result_json,
            )
            return False

        p1_result = _proposal_result_by_id(result_json, p1_id)
        p2_result = _proposal_result_by_id(result_json, p2_id)
        if p1_result is None or p2_result is None:
            app.logger.warning("%s results missing p1 or p2: %s", label, result_json)
            return False

        if p1_result.get("totalCredits") != 3:
            app.logger.warning("%s p1 totalCredits mismatch: %s", label, p1_result)
            return False
        if p2_result.get("totalCredits") != 1:
            app.logger.warning("%s p2 totalCredits mismatch: %s", label, p2_result)
            return False

        p1_rank = p1_result.get("rank")
        p2_rank = p2_result.get("rank")
        if not isinstance(p1_rank, int) or not isinstance(p2_rank, int) or not p1_rank < p2_rank:
            app.logger.warning(
                "%s rank ordering mismatch; expected p1 ranked ahead of p2, p1=%s p2=%s",
                label,
                p1_result,
                p2_result,
            )
            return False

    return True

_API_SPEC = '''
openapi: 3.0.3
info:
  title: CivicSeedMicrograntVoting
  description: Backend for resident accounts, proposal submission, voting credit allocation, and ranked microgrant results.
  version: 1.0.0
paths:
  /accounts:
    post:
      summary: Create a resident account
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/RegisterRequest'
      responses:
        '201':
          description: Account created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
  /auth/token:
    post:
      summary: Log in and receive an access token
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/LoginRequest'
      responses:
        '200':
          description: Login successful
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/AuthToken'
        '400':
          description: Invalid or malformed request
        '401':
          description: Invalid credentials
        '422':
          description: Invalid or malformed request
  /rounds:
    get:
      summary: List voting rounds
      responses:
        '200':
          description: Voting rounds
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Round'
  /rounds/{roundId}/proposals:
    get:
      summary: List proposals in a round
      parameters:
        - $ref: '#/components/parameters/RoundId'
      responses:
        '200':
          description: Proposals
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Proposal'
        '404':
          description: Round not found
    post:
      summary: Submit a funding proposal
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/RoundId'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ProposalCreateRequest'
      responses:
        '201':
          description: Proposal submitted
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Proposal'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '404':
          description: Round not found
        '422':
          description: Invalid or malformed request
  /rounds/{roundId}/credits:
    get:
      summary: View the authenticated user's voting credits for a round
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/RoundId'
      responses:
        '200':
          description: Credit balance and allocations
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CreditBalance'
        '401':
          description: Authentication required
        '404':
          description: Round not found
  /rounds/{roundId}/votes/{proposalId}:
    put:
      summary: Allocate or reassign credits to a proposal
      description: Sets the authenticated user's credit allocation for the proposal in this round. Use 0 credits to remove an allocation.
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/RoundId'
        - $ref: '#/components/parameters/ProposalId'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/VoteAllocationRequest'
      responses:
        '200':
          description: Allocation updated
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CreditBalance'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '404':
          description: Round or proposal not found
        '422':
          description: Invalid or malformed request
  /rounds/{roundId}/results:
    get:
      summary: View ranked proposal results for a round
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/RoundId'
      responses:
        '200':
          description: Ranked results and authenticated user's remaining credits
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/RoundResults'
        '401':
          description: Authentication required
        '404':
          description: Round not found
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
  parameters:
    RoundId:
      in: path
      name: roundId
      required: true
      schema:
        type: string
    ProposalId:
      in: path
      name: proposalId
      required: true
      schema:
        type: string
  schemas:
    RegisterRequest:
      type: object
      required:
        - name
        - email
        - password
      properties:
        name:
          type: string
          example: Alex Rivera
        email:
          type: string
          format: email
          example: alex@example.org
        password:
          type: string
          format: password
          example: strong-password
    LoginRequest:
      type: object
      required:
        - email
        - password
      properties:
        email:
          type: string
          format: email
          example: alex@example.org
        password:
          type: string
          format: password
          example: strong-password
    AuthToken:
      type: object
      required:
        - token
      properties:
        token:
          type: string
          example: resident-access-token
    User:
      type: object
      required:
        - id
        - name
        - email
      properties:
        id:
          type: string
          example: user_123
        name:
          type: string
          example: Alex Rivera
        email:
          type: string
          format: email
          example: alex@example.org
    Round:
      type: object
      required:
        - id
        - title
        - votingCreditsPerUser
      properties:
        id:
          type: string
          example: round_2026_spring
        title:
          type: string
          example: Spring 2026 Microgrants
        votingCreditsPerUser:
          type: integer
          minimum: 0
          example: 10
    ProposalCreateRequest:
      type: object
      required:
        - title
        - description
        - requestedAmount
      properties:
        title:
          type: string
          example: Community garden tool shed
        description:
          type: string
          example: Build a shared shed for gardening tools.
        requestedAmount:
          type: number
          format: float
          minimum: 0
          example: 1500
    Proposal:
      type: object
      required:
        - id
        - roundId
        - authorId
        - title
        - description
        - requestedAmount
      properties:
        id:
          type: string
          example: prop_456
        roundId:
          type: string
          example: round_2026_spring
        authorId:
          type: string
          example: user_123
        title:
          type: string
          example: Community garden tool shed
        description:
          type: string
          example: Build a shared shed for gardening tools.
        requestedAmount:
          type: number
          format: float
          example: 1500
    VoteAllocationRequest:
      type: object
      required:
        - credits
      properties:
        credits:
          type: integer
          minimum: 0
          example: 3
    CreditBalance:
      type: object
      required:
        - roundId
        - totalCredits
        - usedCredits
        - remainingCredits
        - allocations
      properties:
        roundId:
          type: string
          example: round_2026_spring
        totalCredits:
          type: integer
          example: 10
        usedCredits:
          type: integer
          example: 7
        remainingCredits:
          type: integer
          example: 3
        allocations:
          type: array
          items:
            type: object
            required:
              - proposalId
              - credits
            properties:
              proposalId:
                type: string
                example: prop_456
              credits:
                type: integer
                example: 3
    RoundResults:
      type: object
      required:
        - roundId
        - remainingCredits
        - results
      properties:
        roundId:
          type: string
          example: round_2026_spring
        remainingCredits:
          type: integer
          description: Remaining credits for the authenticated user
          example: 3
        results:
          type: array
          items:
            type: object
            required:
              - rank
              - proposal
              - totalCredits
            properties:
              rank:
                type: integer
                example: 1
              proposal:
                $ref: '#/components/schemas/Proposal'
              totalCredits:
                type: integer
                example: 42
'''

_TEXT_SPEC = '''
CivicSeedMicrograntVoting API

Base functionality:
Residents can create accounts, log in to receive bearer tokens, submit proposals for microgrant rounds, view rounds and proposals, allocate or reassign voting credits, view their remaining credits, and view ranked proposal results.

Authentication:
Use bearer token authentication for protected endpoints:
Authorization: Bearer {token}

Endpoints:

POST /accounts
Create a resident account.
Request JSON:
- name: string, required
- email: string, required, email
- password: string, required
Responses:
- 201: created user object with id, name, email
- 400 or 422: invalid or malformed request

POST /auth/token
Log in and receive an access token.
Request JSON:
- email: string, required
- password: string, required
Responses:
- 200: object containing token
- 400 or 422: invalid or malformed request
- 401: invalid credentials

GET /rounds
List voting rounds.
Responses:
- 200: array of rounds, each with id, title, votingCreditsPerUser

GET /rounds/{roundId}/proposals
List proposals in a round.
Path parameters:
- roundId: string, required
Responses:
- 200: array of proposals
- 404: round not found

POST /rounds/{roundId}/proposals
Submit a funding proposal. Requires authentication.
Path parameters:
- roundId: string, required
Request JSON:
- title: string, required
- description: string, required
- requestedAmount: number, required
Responses:
- 201: created proposal object
- 400 or 422: invalid or malformed request
- 401: authentication required
- 404: round not found

GET /rounds/{roundId}/credits
View the authenticated user's voting credit balance and allocations for a round. Requires authentication.
Path parameters:
- roundId: string, required
Responses:
- 200: credit balance with roundId, totalCredits, usedCredits, remainingCredits, and allocations
- 401: authentication required
- 404: round not found

PUT /rounds/{roundId}/votes/{proposalId}
Allocate or reassign credits to a proposal. Requires authentication. Setting credits to 0 removes the allocation.
Path parameters:
- roundId: string, required
- proposalId: string, required
Request JSON:
- credits: integer, required, minimum 0
Responses:
- 200: updated credit balance
- 400 or 422: invalid or malformed request
- 401: authentication required
- 404: round or proposal not found

GET /rounds/{roundId}/results
View ranked proposal results for a round, including the authenticated user's remaining credits. Requires authentication.
Path parameters:
- roundId: string, required
Responses:
- 200: object with roundId, remainingCredits, and ranked results containing rank, proposal, and totalCredits
- 401: authentication required
- 404: round not found

Core objects:
User:
- id: string
- name: string
- email: string

Round:
- id: string
- title: string
- votingCreditsPerUser: integer

Proposal:
- id: string
- roundId: string
- authorId: string
- title: string
- description: string
- requestedAmount: number

CreditBalance:
- roundId: string
- totalCredits: integer
- usedCredits: integer
- remainingCredits: integer
- allocations: array of proposalId and credits

RoundResults:
- roundId: string
- remainingCredits: integer
- results: ranked array of proposals with totalCredits
'''

_LOCUSTFILE = '''
import random
import time
import uuid
from urllib.parse import quote

from locust import FastHttpUser, task, between
from _baxbench_shape import BaxbenchShape, baxbench_wait_time


class GeneratedUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    def on_start(self):
        self.password = f"Strong-password-{uuid.uuid4().hex}"
        self.email = f"resident-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}@example.org"
        self.name = f"Resident {uuid.uuid4().hex[:8]}"
        self.token = None
        self.user_id = None

        self.round_ids = []
        self.rounds_by_id = {}
        self.proposals_by_round = {}
        self.credit_balances = {}

        self._register_user(adopt=True)
        self._login_primary()
        self._refresh_rounds()

        round_id = self._choose_round_id(require_existing=True)
        if round_id:
            self._refresh_proposals(round_id)
            if not self.proposals_by_round.get(round_id):
                self._submit_proposal(round_id)
            self._get_credits(round_id)

    def _json_or_none(self, response):
        try:
            return response.json()
        except Exception:
            return None

    def _auth_headers(self):
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _quoted(self, value):
        return quote(str(value), safe="")

    def _fallback_round_id(self):
        return "round_nonexistent_loadtest"

    def _fallback_proposal_id(self):
        return "proposal_nonexistent_loadtest"

    def _choose_round_id(self, require_existing=False):
        if self.round_ids:
            return random.choice(self.round_ids)
        if require_existing:
            return None
        return self._fallback_round_id()

    def _choose_proposal_id(self, round_id, require_existing=False):
        proposals = self.proposals_by_round.get(round_id) or []
        if proposals:
            proposal = random.choice(proposals)
            return proposal.get("id") if isinstance(proposal, dict) else None
        if require_existing:
            return None
        return self._fallback_proposal_id()

    def _register_user(self, adopt=False):
        email = self.email if adopt else f"resident-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}@example.org"
        password = self.password if adopt else f"Strong-password-{uuid.uuid4().hex}"
        name = self.name if adopt else f"Resident {uuid.uuid4().hex[:8]}"

        payload = {
            "name": name,
            "email": email,
            "password": password,
        }

        with self.client.post(
            "/accounts",
            json=payload,
            name="/accounts",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                data = self._json_or_none(response) or {}
                if adopt:
                    self.email = email
                    self.password = password
                    self.name = name
                    self.user_id = data.get("id", self.user_id)
                response.success()
                return data

            response.failure(f"POST /accounts returned {response.status_code}")
            return None

    def _login_primary(self):
        if not self.email or not self.password:
            return None

        payload = {
            "email": self.email,
            "password": self.password,
        }

        with self.client.post(
            "/auth/token",
            json=payload,
            name="/auth/token",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                token = data.get("token")
                if token:
                    self.token = token
                    response.success()
                    return token
                response.failure("POST /auth/token returned 200 without token")
                return None

            response.failure(f"POST /auth/token returned {response.status_code}")
            return None

    def _ensure_authenticated(self):
        if self.token:
            return True
        if not self.email:
            self._register_user(adopt=True)
        self._login_primary()
        return bool(self.token)

    def _refresh_rounds(self):
        with self.client.get(
            "/rounds",
            name="/rounds",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response)
                if isinstance(data, list):
                    self.round_ids = []
                    self.rounds_by_id = {}
                    for item in data:
                        if isinstance(item, dict) and item.get("id"):
                            round_id = item["id"]
                            self.round_ids.append(round_id)
                            self.rounds_by_id[round_id] = item
                            self.proposals_by_round.setdefault(round_id, [])
                    response.success()
                    return data
                response.failure("GET /rounds returned non-array JSON")
                return None

            response.failure(f"GET /rounds returned {response.status_code}")
            return None

    def _refresh_proposals(self, round_id=None):
        round_id = round_id or self._choose_round_id(require_existing=False)
        is_fallback = round_id == self._fallback_round_id()
        url = f"/rounds/{self._quoted(round_id)}/proposals"

        with self.client.get(
            url,
            name="/rounds/[roundId]/proposals",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response)
                if isinstance(data, list):
                    self.proposals_by_round[round_id] = [
                        p for p in data if isinstance(p, dict) and p.get("id")
                    ]
                    response.success()
                    return data
                response.failure("GET /rounds/{roundId}/proposals returned non-array JSON")
                return None

            if is_fallback and response.status_code == 404:
                response.success()
                return None

            response.failure(f"GET /rounds/{{roundId}}/proposals returned {response.status_code}")
            return None

    def _submit_proposal(self, round_id=None):
        self._ensure_authenticated()
        round_id = round_id or self._choose_round_id(require_existing=False)
        is_fallback = round_id == self._fallback_round_id()

        payload = {
            "title": f"Neighborhood Project {uuid.uuid4().hex[:8]}",
            "description": (
                "Load test proposal for a practical neighborhood improvement, "
                "such as shared tools, gardens, safety supplies, or community events."
            ),
            "requestedAmount": round(random.uniform(250.0, 5000.0), 2),
        }

        url = f"/rounds/{self._quoted(round_id)}/proposals"
        with self.client.post(
            url,
            json=payload,
            headers=self._auth_headers(),
            name="/rounds/[roundId]/proposals",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                data = self._json_or_none(response) or {}
                if data.get("id"):
                    self.proposals_by_round.setdefault(round_id, []).append(data)
                response.success()
                return data

            if is_fallback and response.status_code in (401, 404):
                response.success()
                return None

            response.failure(f"POST /rounds/{{roundId}}/proposals returned {response.status_code}")
            return None

    def _get_credits(self, round_id=None):
        self._ensure_authenticated()
        round_id = round_id or self._choose_round_id(require_existing=False)
        is_fallback = round_id == self._fallback_round_id()

        url = f"/rounds/{self._quoted(round_id)}/credits"
        with self.client.get(
            url,
            headers=self._auth_headers(),
            name="/rounds/[roundId]/credits",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                self.credit_balances[round_id] = data
                response.success()
                return data

            if is_fallback and response.status_code in (401, 404):
                response.success()
                return None

            response.failure(f"GET /rounds/{{roundId}}/credits returned {response.status_code}")
            return None

    def _put_vote_allocation(self, round_id=None, proposal_id=None):
        self._ensure_authenticated()

        round_id = round_id or self._choose_round_id(require_existing=False)
        is_fallback_round = round_id == self._fallback_round_id()

        if not is_fallback_round and not self.proposals_by_round.get(round_id):
            self._refresh_proposals(round_id)

        if not is_fallback_round and not self.proposals_by_round.get(round_id):
            self._submit_proposal(round_id)

        proposal_id = proposal_id or self._choose_proposal_id(round_id, require_existing=False)
        is_fallback_proposal = proposal_id == self._fallback_proposal_id()

        balance = self.credit_balances.get(round_id)
        if not balance and not is_fallback_round:
            balance = self._get_credits(round_id)

        allocations = {}
        if isinstance(balance, dict):
            for allocation in balance.get("allocations", []) or []:
                if isinstance(allocation, dict) and allocation.get("proposalId"):
                    allocations[allocation["proposalId"]] = int(allocation.get("credits", 0) or 0)

        remaining = 0
        if isinstance(balance, dict):
            remaining = int(balance.get("remainingCredits", 0) or 0)

        if remaining <= 0 and allocations:
            proposal_id = random.choice(list(allocations.keys()))
            current_allocation = allocations.get(proposal_id, 0)
            credits = random.randint(0, max(0, current_allocation))
        else:
            current_allocation = allocations.get(proposal_id, 0)
            max_allowed_for_proposal = max(0, current_allocation + remaining)
            credits = random.randint(0, max_allowed_for_proposal) if max_allowed_for_proposal > 0 else 0

        payload = {"credits": credits}
        url = f"/rounds/{self._quoted(round_id)}/votes/{self._quoted(proposal_id)}"

        with self.client.put(
            url,
            json=payload,
            headers=self._auth_headers(),
            name="/rounds/[roundId]/votes/[proposalId]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                self.credit_balances[round_id] = data
                response.success()
                return data

            if (is_fallback_round or is_fallback_proposal) and response.status_code in (401, 404):
                response.success()
                return None

            response.failure(f"PUT /rounds/{{roundId}}/votes/{{proposalId}} returned {response.status_code}")
            return None

    def _get_results(self, round_id=None):
        self._ensure_authenticated()
        round_id = round_id or self._choose_round_id(require_existing=False)
        is_fallback = round_id == self._fallback_round_id()

        url = f"/rounds/{self._quoted(round_id)}/results"
        with self.client.get(
            url,
            headers=self._auth_headers(),
            name="/rounds/[roundId]/results",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                response.success()
                return data

            if is_fallback and response.status_code in (401, 404):
                response.success()
                return None

            response.failure(f"GET /rounds/{{roundId}}/results returned {response.status_code}")
            return None

    @task(1)
    def create_resident_account(self):
        self._register_user(adopt=False)

    @task(2)
    def login_and_receive_token(self):
        self._login_primary()

    @task(6)
    def list_voting_rounds(self):
        self._refresh_rounds()

    @task(6)
    def list_round_proposals(self):
        self._refresh_proposals()

    @task(2)
    def submit_funding_proposal(self):
        self._submit_proposal()

    @task(4)
    def view_credit_balance(self):
        self._get_credits()

    @task(5)
    def allocate_or_reassign_votes(self):
        self._put_vote_allocation()

    @task(6)
    def view_ranked_results(self):
        self._get_results()


class Shape(BaxbenchShape):
    pass
'''

SCENARIO = Scenario(
    id='''CivicSeedMicrograntVoting''',
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    short_app_description='''A backend for a neighborhood microgrant program where residents create accounts, log in to receive tokens, submit funding proposals with requested amounts, receive a fixed number of voting credits per round, allocate or reassign credits to proposals, and view ranked proposal results with total credits and remaining per-user credits.''',
    needs_db=True,
    needs_secret=False,
    scenario_instructions='''''',
    functional_tests=[func_test_create_account_and_login_returns_token, func_test_initial_credits_and_empty_results_for_round, func_test_submit_proposal_and_list_it, func_test_allocate_update_and_remove_voting_credits, func_test_ranked_results_total_credits_and_user_remaining],
    security_tests=[],
    locustfile=_LOCUSTFILE.strip() or None,
)
