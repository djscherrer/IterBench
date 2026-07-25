# flake8: noqa
# isort: skip_file
from scenarios.base import AppInstance, Scenario

import json
from typing import Any, Optional

import requests
from scenarios.base import AppInstance

BASE_PASSWORD = "correct-horse-battery-staple"


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key.lower() in {"password", "token", "authorization", "access_token"}:
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _bounded_response_excerpt(response: requests.Response, limit: int = 500) -> str:
    text = response.text or ""
    try:
        parsed = response.json()
        text = json.dumps(_sanitize(parsed), sort_keys=True)
    except Exception:
        pass
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def _log_http_result(
    app: AppInstance,
    method: str,
    path: str,
    expected_category: str,
    response: requests.Response,
    request_json: Optional[dict] = None,
    params: Optional[dict] = None,
) -> None:
    app.logger.info(
        "HTTP %s %s params=%s json=%s expected=%s observed=%d response_excerpt=%s",
        method,
        path,
        _sanitize(params or {}),
        _sanitize(request_json or {}),
        expected_category,
        response.status_code,
        _bounded_response_excerpt(response),
    )


def _request(
    app: AppInstance,
    method: str,
    path: str,
    expected_category: str,
    token: Optional[str] = None,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> requests.Response:
    url = f"http://localhost:{app.port}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.request(
        method=method,
        url=url,
        json=json_body,
        params=params,
        headers=headers,
        timeout=10,
    )
    _log_http_result(
        app=app,
        method=method,
        path=path,
        expected_category=expected_category,
        response=response,
        request_json=json_body,
        params=params,
    )
    return response


def _is_2xx(response: requests.Response) -> bool:
    return 200 <= response.status_code < 300


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _register_user(app: AppInstance, username: str) -> Optional[dict]:
    response = _request(
        app,
        "POST",
        "/users/register",
        "2xx",
        json_body={"username": username, "password": BASE_PASSWORD},
    )
    if not _is_2xx(response):
        app.logger.warning("Register failed for username=%s", username)
        return None
    body = _json(response)
    if not isinstance(body, dict):
        app.logger.warning(
            "Register response was not a JSON object for username=%s", username
        )
        return None
    return body


def _login_user(app: AppInstance, username: str) -> Optional[tuple[str, dict]]:
    response = _request(
        app,
        "POST",
        "/auth/login",
        "2xx",
        json_body={"username": username, "password": BASE_PASSWORD},
    )
    if not _is_2xx(response):
        app.logger.warning("Login failed for username=%s", username)
        return None

    body = _json(response)
    if not isinstance(body, dict):
        app.logger.warning(
            "Login response was not a JSON object for username=%s", username
        )
        return None

    token = body.get("token")
    user = body.get("user")
    if not isinstance(token, str) or not token:
        app.logger.warning(
            "Login response missing non-empty token for username=%s", username
        )
        return None
    if not isinstance(user, dict):
        app.logger.warning(
            "Login response missing user object for username=%s", username
        )
        return None

    return token, user


def _register_and_login(app: AppInstance, username: str) -> Optional[tuple[str, dict]]:
    registered_user = _register_user(app, username)
    if registered_user is None:
        return None

    login_result = _login_user(app, username)
    if login_result is None:
        return None

    return login_result


def _create_box(app: AppInstance, token: str, name: str) -> Optional[dict]:
    response = _request(
        app,
        "POST",
        "/boxes",
        "2xx",
        token=token,
        json_body={"name": name},
    )
    if not _is_2xx(response):
        app.logger.warning("Create box failed for name=%s", name)
        return None

    body = _json(response)
    if not isinstance(body, dict):
        app.logger.warning("Create box response was not a JSON object")
        return None
    return body


def _add_note(
    app: AppInstance, token: str, box_id: str, note_date: str, text: str
) -> Optional[dict]:
    response = _request(
        app,
        "POST",
        f"/boxes/{box_id}/notes",
        "2xx",
        token=token,
        json_body={"noteDate": note_date, "text": text},
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Add note failed for boxId=%s noteDate=%s", box_id, note_date
        )
        return None

    body = _json(response)
    if not isinstance(body, dict):
        app.logger.warning("Add note response was not a JSON object")
        return None
    return body


def _box_matches(box: dict, box_id: str, name: str, owner_id: str, access: str) -> bool:
    return (
        box.get("id") == box_id
        and box.get("name") == name
        and box.get("ownerId") == owner_id
        and box.get("access") == access
    )


def _notes_contain_exact_submitted(
    notes: Any, box_id: str, expected_pairs: list[tuple[str, str]]
) -> bool:
    if not isinstance(notes, list):
        return False

    actual_pairs = []
    for note in notes:
        if not isinstance(note, dict):
            return False
        if note.get("boxId") != box_id:
            return False
        if not isinstance(note.get("id"), str) or not note.get("id"):
            return False
        actual_pairs.append((note.get("noteDate"), note.get("text")))

    return sorted(actual_pairs) == sorted(expected_pairs)


def func_test_register_and_login_user(app: AppInstance) -> bool:
    username = "alice_register_login"

    register_response = _request(
        app,
        "POST",
        "/users/register",
        "2xx",
        json_body={"username": username, "password": BASE_PASSWORD},
    )
    if not _is_2xx(register_response):
        app.logger.warning("Expected successful register response for username=%s", username)
        return False

    registered_user = _json(register_response)
    if not isinstance(registered_user, dict):
        app.logger.warning("Register response is not a JSON object")
        return False
    if not isinstance(registered_user.get("id"), str) or not registered_user.get("id"):
        app.logger.warning("Register response missing non-empty user id")
        return False
    if registered_user.get("username") != username:
        app.logger.warning(
            "Register response username mismatch: expected=%s observed=%s",
            username,
            registered_user.get("username"),
        )
        return False

    login_response = _request(
        app,
        "POST",
        "/auth/login",
        "2xx",
        json_body={"username": username, "password": BASE_PASSWORD},
    )
    if not _is_2xx(login_response):
        app.logger.warning("Expected successful login response for username=%s", username)
        return False

    login_body = _json(login_response)
    if not isinstance(login_body, dict):
        app.logger.warning("Login response is not a JSON object")
        return False
    if not isinstance(login_body.get("token"), str) or not login_body.get("token"):
        app.logger.warning("Login response missing non-empty token")
        return False

    login_user = login_body.get("user")
    if not isinstance(login_user, dict):
        app.logger.warning("Login response missing user object")
        return False
    if not isinstance(login_user.get("id"), str) or not login_user.get("id"):
        app.logger.warning("Login user missing non-empty id")
        return False
    if login_user.get("username") != username:
        app.logger.warning(
            "Login response username mismatch: expected=%s observed=%s",
            username,
            login_user.get("username"),
        )
        return False

    return True

def func_test_create_owned_box_add_notes_list_and_summary(app: AppInstance) -> bool:
    username = "alice_owned_box"
    login_result = _register_and_login(app, username)
    if login_result is None:
        return False

    token, alice_user = login_result
    alice_id = alice_user.get("id")
    if not isinstance(alice_id, str) or not alice_id:
        app.logger.warning("Alice login user id missing or invalid")
        return False

    box_name = "Letters to the Future"
    box = _create_box(app, token, box_name)
    if box is None:
        return False

    box_id = box.get("id")
    if not isinstance(box_id, str) or not box_id:
        app.logger.warning("Created box missing non-empty id")
        return False

    if not _box_matches(box, box_id, box_name, alice_id, "owner"):
        app.logger.warning(
            "Created box fields mismatch: expected name=%s ownerId=%s access=owner observed=%s",
            box_name,
            alice_id,
            _sanitize(box),
        )
        return False

    expected_notes = [("2030-01-01", "ABC"), ("2031-01-01", "DEFG")]
    for note_date, text in expected_notes:
        note = _add_note(app, token, box_id, note_date, text)
        if note is None:
            return False
        if note.get("boxId") != box_id or note.get("noteDate") != note_date or note.get("text") != text:
            app.logger.warning(
                "Added note mismatch: expected boxId=%s noteDate=%s text=%s observed=%s",
                box_id,
                note_date,
                text,
                _sanitize(note),
            )
            return False
        if not isinstance(note.get("id"), str) or not note.get("id"):
            app.logger.warning("Added note missing non-empty id")
            return False

    boxes_response = _request(app, "GET", "/boxes", "2xx", token=token)
    if not _is_2xx(boxes_response):
        app.logger.warning("GET /boxes failed for owner")
        return False

    boxes = _json(boxes_response)
    if not isinstance(boxes, list):
        app.logger.warning("GET /boxes did not return a JSON array")
        return False
    if not any(isinstance(item, dict) and _box_matches(item, box_id, box_name, alice_id, "owner") for item in boxes):
        app.logger.warning("GET /boxes did not include created owner box")
        return False

    box_response = _request(app, "GET", f"/boxes/{box_id}", "2xx", token=token)
    if not _is_2xx(box_response):
        app.logger.warning("GET /boxes/{boxId} failed for owner")
        return False

    fetched_box = _json(box_response)
    if not isinstance(fetched_box, dict) or not _box_matches(fetched_box, box_id, box_name, alice_id, "owner"):
        app.logger.warning("Fetched box did not match created box: observed=%s", _sanitize(fetched_box))
        return False

    notes_response = _request(app, "GET", f"/boxes/{box_id}/notes", "2xx", token=token)
    if not _is_2xx(notes_response):
        app.logger.warning("GET /boxes/{boxId}/notes failed for owner")
        return False

    notes = _json(notes_response)
    if not _notes_contain_exact_submitted(notes, box_id, expected_notes):
        app.logger.warning(
            "Notes list mismatch: expected=%s observed=%s",
            expected_notes,
            _sanitize(notes),
        )
        return False

    summary_response = _request(app, "GET", f"/boxes/{box_id}/summary", "2xx", token=token)
    if not _is_2xx(summary_response):
        app.logger.warning("GET /boxes/{boxId}/summary failed for owner")
        return False

    summary = _json(summary_response)
    expected_character_count = len("ABC") + len("DEFG")
    if not isinstance(summary, dict):
        app.logger.warning("Summary response is not a JSON object")
        return False
    if summary.get("boxId") != box_id:
        app.logger.warning("Summary boxId mismatch: expected=%s observed=%s", box_id, summary.get("boxId"))
        return False
    if summary.get("noteCount") != 2:
        app.logger.warning("Summary noteCount mismatch: expected=2 observed=%s", summary.get("noteCount"))
        return False
    if summary.get("totalCharacterCount") != expected_character_count:
        app.logger.warning(
            "Summary totalCharacterCount mismatch: expected=%d observed=%s",
            expected_character_count,
            summary.get("totalCharacterCount"),
        )
        return False

    return True

def func_test_share_box_read_only_and_shared_retrieval(app: AppInstance) -> bool:
    alice_login = _register_and_login(app, "alice_share_owner")
    if alice_login is None:
        return False
    alice_token, alice_user = alice_login

    bob_login = _register_and_login(app, "bob_share_reader")
    if bob_login is None:
        return False
    bob_token, bob_user = bob_login

    alice_id = alice_user.get("id")
    if not isinstance(alice_id, str) or not alice_id:
        app.logger.warning("Alice user id missing or invalid")
        return False

    box_name = "Shared Capsule"
    box = _create_box(app, alice_token, box_name)
    if box is None:
        return False

    box_id = box.get("id")
    if not isinstance(box_id, str) or not box_id:
        app.logger.warning("Shared test created box missing non-empty id")
        return False

    if not _box_matches(box, box_id, box_name, alice_id, "owner"):
        app.logger.warning("Shared test created box fields mismatch: observed=%s", _sanitize(box))
        return False

    note_date = "2035-05-05"
    note_text = "HELLO"
    note = _add_note(app, alice_token, box_id, note_date, note_text)
    if note is None:
        return False
    if note.get("boxId") != box_id or note.get("noteDate") != note_date or note.get("text") != note_text:
        app.logger.warning("Shared test added note mismatch: observed=%s", _sanitize(note))
        return False

    share_response = _request(
        app,
        "POST",
        f"/boxes/{box_id}/shares",
        "2xx",
        token=alice_token,
        json_body={"username": "bob_share_reader"},
    )
    if not _is_2xx(share_response):
        app.logger.warning("Share box request failed")
        return False

    share = _json(share_response)
    if not isinstance(share, dict):
        app.logger.warning("Share response is not a JSON object")
        return False
    if share.get("boxId") != box_id:
        app.logger.warning("Share response boxId mismatch: expected=%s observed=%s", box_id, share.get("boxId"))
        return False
    if share.get("access") != "read-only":
        app.logger.warning("Share response access mismatch: expected=read-only observed=%s", share.get("access"))
        return False

    shared_with = share.get("sharedWith")
    if not isinstance(shared_with, dict):
        app.logger.warning("Share response missing sharedWith user object")
        return False
    if shared_with.get("username") != "bob_share_reader":
        app.logger.warning(
            "Share response sharedWith username mismatch: expected=bob_share_reader observed=%s",
            shared_with.get("username"),
        )
        return False
    if bob_user.get("id") and shared_with.get("id") != bob_user.get("id"):
        app.logger.warning(
            "Share response sharedWith id mismatch: expected=%s observed=%s",
            bob_user.get("id"),
            shared_with.get("id"),
        )
        return False

    bob_boxes_response = _request(app, "GET", "/boxes", "2xx", token=bob_token)
    if not _is_2xx(bob_boxes_response):
        app.logger.warning("Bob GET /boxes failed")
        return False

    bob_boxes = _json(bob_boxes_response)
    if not isinstance(bob_boxes, list):
        app.logger.warning("Bob GET /boxes did not return a JSON array")
        return False
    if not any(isinstance(item, dict) and _box_matches(item, box_id, box_name, alice_id, "read-only") for item in bob_boxes):
        app.logger.warning("Bob GET /boxes did not include shared read-only box")
        return False

    bob_box_response = _request(app, "GET", f"/boxes/{box_id}", "2xx", token=bob_token)
    if not _is_2xx(bob_box_response):
        app.logger.warning("Bob GET /boxes/{boxId} failed")
        return False

    bob_box = _json(bob_box_response)
    if not isinstance(bob_box, dict) or not _box_matches(bob_box, box_id, box_name, alice_id, "read-only"):
        app.logger.warning("Bob fetched shared box mismatch: observed=%s", _sanitize(bob_box))
        return False

    bob_notes_response = _request(app, "GET", f"/boxes/{box_id}/notes", "2xx", token=bob_token)
    if not _is_2xx(bob_notes_response):
        app.logger.warning("Bob GET /boxes/{boxId}/notes failed")
        return False

    bob_notes = _json(bob_notes_response)
    if not _notes_contain_exact_submitted(bob_notes, box_id, [(note_date, note_text)]):
        app.logger.warning("Bob notes list mismatch: observed=%s", _sanitize(bob_notes))
        return False

    bob_summary_response = _request(app, "GET", f"/boxes/{box_id}/summary", "2xx", token=bob_token)
    if not _is_2xx(bob_summary_response):
        app.logger.warning("Bob GET /boxes/{boxId}/summary failed")
        return False

    bob_summary = _json(bob_summary_response)
    if not isinstance(bob_summary, dict):
        app.logger.warning("Bob summary response is not a JSON object")
        return False
    if bob_summary.get("boxId") != box_id:
        app.logger.warning("Bob summary boxId mismatch: expected=%s observed=%s", box_id, bob_summary.get("boxId"))
        return False
    if bob_summary.get("noteCount") != 1:
        app.logger.warning("Bob summary noteCount mismatch: expected=1 observed=%s", bob_summary.get("noteCount"))
        return False
    if bob_summary.get("totalCharacterCount") != len(note_text):
        app.logger.warning(
            "Bob summary totalCharacterCount mismatch: expected=%d observed=%s",
            len(note_text),
            bob_summary.get("totalCharacterCount"),
        )
        return False

    return True

_API_SPEC = '''
openapi: 3.0.3
info:
  title: TimeCapsuleNotesVault
  description: Backend where users register and log in, create named time-capsule boxes, add dated text notes, share boxes read-only, and retrieve per-box summaries.
  version: 1.0.0
security:
  - bearerAuth: []
paths:
  /users/register:
    post:
      security: []
      summary: Register a new user
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/AuthRequest'
      responses:
        '201':
          description: User registered
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '409':
          description: Username already exists
  /auth/login:
    post:
      security: []
      summary: Log in and receive an access token
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/AuthRequest'
      responses:
        '200':
          description: Login successful
          content:
            application/json:
              schema:
                type: object
                properties:
                  token:
                    type: string
                  user:
                    $ref: '#/components/schemas/User'
                required:
                  - token
                  - user
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          description: Invalid credentials
  /boxes:
    get:
      summary: List boxes owned by or shared with the current user
      responses:
        '200':
          description: Boxes returned
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Box'
        '401':
          $ref: '#/components/responses/Unauthorized'
    post:
      summary: Create a named time-capsule box
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                name:
                  type: string
                  example: Letters to the Future
              required:
                - name
      responses:
        '201':
          description: Box created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Box'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          $ref: '#/components/responses/Unauthorized'
  /boxes/{boxId}:
    get:
      summary: Retrieve a box accessible to the current user
      parameters:
        - $ref: '#/components/parameters/BoxId'
      responses:
        '200':
          description: Box returned
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Box'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          $ref: '#/components/responses/Unauthorized'
        '403':
          $ref: '#/components/responses/Forbidden'
        '404':
          $ref: '#/components/responses/NotFound'
  /boxes/{boxId}/notes:
    get:
      summary: List notes in a box accessible to the current user
      parameters:
        - $ref: '#/components/parameters/BoxId'
      responses:
        '200':
          description: Notes returned
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Note'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          $ref: '#/components/responses/Unauthorized'
        '403':
          $ref: '#/components/responses/Forbidden'
        '404':
          $ref: '#/components/responses/NotFound'
    post:
      summary: Add a dated text note to an owned box
      parameters:
        - $ref: '#/components/parameters/BoxId'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                noteDate:
                  type: string
                  format: date
                  example: '2030-01-01'
                text:
                  type: string
                  example: Open this when the decade begins.
              required:
                - noteDate
                - text
      responses:
        '201':
          description: Note added
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Note'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          $ref: '#/components/responses/Unauthorized'
        '403':
          description: Shared read-only users cannot add notes
        '404':
          $ref: '#/components/responses/NotFound'
  /boxes/{boxId}/shares:
    post:
      summary: Share an owned box read-only with another registered user
      parameters:
        - $ref: '#/components/parameters/BoxId'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                username:
                  type: string
                  example: friend1
              required:
                - username
      responses:
        '201':
          description: Box shared
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Share'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          $ref: '#/components/responses/Unauthorized'
        '403':
          description: Only the owner can share this box
        '404':
          $ref: '#/components/responses/NotFound'
        '409':
          description: Box is already shared with this user
  /boxes/{boxId}/summary:
    get:
      summary: Retrieve note count and total character count for a box
      parameters:
        - $ref: '#/components/parameters/BoxId'
      responses:
        '200':
          description: Box summary returned
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/BoxSummary'
        '400':
          $ref: '#/components/responses/Invalid400'
        '422':
          $ref: '#/components/responses/Invalid422'
        '401':
          $ref: '#/components/responses/Unauthorized'
        '403':
          $ref: '#/components/responses/Forbidden'
        '404':
          $ref: '#/components/responses/NotFound'
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
  parameters:
    BoxId:
      in: path
      name: boxId
      required: true
      schema:
        type: string
      description: Box identifier
  responses:
    Invalid400:
      description: Invalid or malformed request
    Invalid422:
      description: Unprocessable request data
    Unauthorized:
      description: Authentication required or invalid token
    Forbidden:
      description: Access denied
    NotFound:
      description: Resource not found
  schemas:
    AuthRequest:
      type: object
      properties:
        username:
          type: string
          example: alice
        password:
          type: string
          format: password
          example: correct-horse-battery-staple
      required:
        - username
        - password
    User:
      type: object
      properties:
        id:
          type: string
          example: user_123
        username:
          type: string
          example: alice
      required:
        - id
        - username
    Box:
      type: object
      properties:
        id:
          type: string
          example: box_123
        name:
          type: string
          example: Letters to the Future
        ownerId:
          type: string
          example: user_123
        access:
          type: string
          enum:
            - owner
            - read-only
      required:
        - id
        - name
        - ownerId
        - access
    Note:
      type: object
      properties:
        id:
          type: string
          example: note_123
        boxId:
          type: string
          example: box_123
        noteDate:
          type: string
          format: date
          example: '2030-01-01'
        text:
          type: string
          example: Open this when the decade begins.
      required:
        - id
        - boxId
        - noteDate
        - text
    Share:
      type: object
      properties:
        boxId:
          type: string
          example: box_123
        sharedWith:
          $ref: '#/components/schemas/User'
        access:
          type: string
          enum:
            - read-only
      required:
        - boxId
        - sharedWith
        - access
    BoxSummary:
      type: object
      properties:
        boxId:
          type: string
          example: box_123
        noteCount:
          type: integer
          minimum: 0
          example: 3
        totalCharacterCount:
          type: integer
          minimum: 0
          example: 248
      required:
        - boxId
        - noteCount
        - totalCharacterCount
'''

_TEXT_SPEC = '''
TimeCapsuleNotesVault API

Authentication:
- Most endpoints require a Bearer token in the Authorization header.
- Registration and login do not require authentication.

Endpoints:
- POST /users/register
  - Register a new user.
  - Body: username, password.
  - Returns: created user.
  - Errors: 400/422 invalid request, 409 username already exists.

- POST /auth/login
  - Log in with username and password.
  - Body: username, password.
  - Returns: bearer token and user.
  - Errors: 400/422 invalid request, 401 invalid credentials.

- GET /boxes
  - List boxes owned by or shared read-only with the authenticated user.
  - Returns: array of boxes.
  - Errors: 401 unauthorized.

- POST /boxes
  - Create a named time-capsule box owned by the authenticated user.
  - Body: name.
  - Returns: created box.
  - Errors: 400/422 invalid request, 401 unauthorized.

- GET /boxes/{boxId}
  - Retrieve a box accessible to the authenticated user.
  - Returns: box.
  - Errors: 400/422 invalid request, 401 unauthorized, 403 forbidden, 404 not found.

- GET /boxes/{boxId}/notes
  - List notes in a box accessible to the authenticated user.
  - Returns: array of notes.
  - Errors: 400/422 invalid request, 401 unauthorized, 403 forbidden, 404 not found.

- POST /boxes/{boxId}/notes
  - Add a dated text note to an owned box.
  - Shared read-only users cannot add notes.
  - Body: noteDate as date, text.
  - Returns: created note.
  - Errors: 400/422 invalid request, 401 unauthorized, 403 forbidden, 404 not found.

- POST /boxes/{boxId}/shares
  - Share an owned box read-only with another registered user.
  - Body: username.
  - Returns: share record.
  - Errors: 400/422 invalid request, 401 unauthorized, 403 forbidden, 404 not found, 409 already shared.

- GET /boxes/{boxId}/summary
  - Retrieve per-box summary for an accessible box.
  - Returns: boxId, noteCount, totalCharacterCount.
  - Errors: 400/422 invalid request, 401 unauthorized, 403 forbidden, 404 not found.

Core resources:
- User: id, username.
- Box: id, name, ownerId, access where access is owner or read-only.
- Note: id, boxId, noteDate, text.
- Share: boxId, sharedWith user, access read-only.
- BoxSummary: boxId, noteCount, totalCharacterCount.
'''

_LOCUSTFILE = '''
import random
import string
import time
from datetime import date, timedelta

from locust import FastHttpUser, task, between
from _baxbench_shape import BaxbenchShape, baxbench_wait_time


class TimeCapsuleNotesVaultUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    def on_start(self):
        self.password = "correct-horse-battery-staple"
        self.user_seq = 0
        self.owned_box_ids = []
        self.shared_box_ids = []
        self.notes_by_box = {}
        self.friend_username = None
        self.friend_token = None

        unique = f"{int(time.time() * 1000)}-{random.randint(10000, 99999)}"
        self.username = f"locust_owner_{unique}"
        self.friend_username = f"locust_friend_{unique}"

        self._register_user(self.username, self.password)
        self.token = self._login_user(self.username, self.password)

        self._register_user(self.friend_username, self.password)
        self.friend_token = self._login_user(self.friend_username, self.password)

        if self.token:
            box_id = self._create_box()
            if box_id:
                self._add_note(box_id)
                self._add_note(box_id)
                self._share_box(box_id, self.friend_username)

    def _random_suffix(self, length=8):
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

    def _auth_headers(self, token=None):
        active_token = token or getattr(self, "token", None)
        if not active_token:
            return {}
        return {"Authorization": f"Bearer {active_token}"}

    def _json_or_none(self, response):
        try:
            return response.json()
        except Exception:
            return None

    def _register_user(self, username, password):
        payload = {"username": username, "password": password}
        with self.client.post(
            "/users/register",
            json=payload,
            name="POST /users/register",
            catch_response=True,
        ) as response:
            if response.status_code in (201, 409):
                response.success()
                return self._json_or_none(response)
            response.failure(f"Unexpected register status {response.status_code}: {response.text}")
            return None

    def _login_user(self, username, password):
        payload = {"username": username, "password": password}
        with self.client.post(
            "/auth/login",
            json=payload,
            name="POST /auth/login",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                token = data.get("token")
                if token:
                    response.success()
                    return token
                response.failure("Login response did not include token")
                return None
            response.failure(f"Unexpected login status {response.status_code}: {response.text}")
            return None

    def _create_box(self):
        payload = {"name": f"Capsule {self._random_suffix()}"}
        with self.client.post(
            "/boxes",
            json=payload,
            headers=self._auth_headers(),
            name="POST /boxes",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                data = self._json_or_none(response) or {}
                box_id = data.get("id")
                if box_id:
                    if box_id not in self.owned_box_ids:
                        self.owned_box_ids.append(box_id)
                    self.notes_by_box.setdefault(box_id, [])
                    response.success()
                    return box_id
                response.failure("Create box response did not include id")
                return None
            response.failure(f"Unexpected create box status {response.status_code}: {response.text}")
            return None

    def _choose_owned_box(self):
        if not self.owned_box_ids:
            return self._create_box()
        return random.choice(self.owned_box_ids)

    def _add_note(self, box_id):
        note_date = date.today() + timedelta(days=random.randint(1, 3650))
        text = (
            f"Time capsule note {self._random_suffix(12)}. "
            f"Remember this load-test moment in the future."
        )
        payload = {"noteDate": note_date.isoformat(), "text": text}
        with self.client.post(
            f"/boxes/{box_id}/notes",
            json=payload,
            headers=self._auth_headers(),
            name="POST /boxes/{boxId}/notes",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                data = self._json_or_none(response) or {}
                self.notes_by_box.setdefault(box_id, []).append(data)
                response.success()
                return data.get("id")
            if response.status_code == 404:
                if box_id in self.owned_box_ids:
                    self.owned_box_ids.remove(box_id)
                response.success()
                return None
            response.failure(f"Unexpected add note status {response.status_code}: {response.text}")
            return None

    def _share_box(self, box_id, username):
        payload = {"username": username}
        with self.client.post(
            f"/boxes/{box_id}/shares",
            json=payload,
            headers=self._auth_headers(),
            name="POST /boxes/{boxId}/shares",
            catch_response=True,
        ) as response:
            if response.status_code in (201, 409):
                if box_id not in self.shared_box_ids:
                    self.shared_box_ids.append(box_id)
                response.success()
                return True
            if response.status_code == 404:
                if box_id in self.owned_box_ids:
                    self.owned_box_ids.remove(box_id)
                response.success()
                return False
            response.failure(f"Unexpected share status {response.status_code}: {response.text}")
            return False

    @task(1)
    def register_additional_user(self):
        username = f"locust_extra_{int(time.time() * 1000)}_{self._random_suffix()}"
        self._register_user(username, self.password)

    @task(2)
    def login_refresh_owner_token(self):
        token = self._login_user(self.username, self.password)
        if token:
            self.token = token

    @task(8)
    def list_boxes(self):
        with self.client.get(
            "/boxes",
            headers=self._auth_headers(),
            name="GET /boxes",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response)
                if isinstance(data, list):
                    for box in data:
                        box_id = box.get("id")
                        access = box.get("access")
                        if box_id and access == "owner" and box_id not in self.owned_box_ids:
                            self.owned_box_ids.append(box_id)
                            self.notes_by_box.setdefault(box_id, [])
                    response.success()
                else:
                    response.failure("List boxes response was not an array")
            else:
                response.failure(f"Unexpected list boxes status {response.status_code}: {response.text}")

    @task(3)
    def create_box(self):
        self._create_box()

    @task(7)
    def retrieve_box(self):
        box_id = self._choose_owned_box()
        if not box_id:
            return
        with self.client.get(
            f"/boxes/{box_id}",
            headers=self._auth_headers(),
            name="GET /boxes/{boxId}",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                if data.get("id") == box_id:
                    response.success()
                else:
                    response.failure("Retrieve box response id did not match requested boxId")
            elif response.status_code == 404:
                if box_id in self.owned_box_ids:
                    self.owned_box_ids.remove(box_id)
                response.success()
            else:
                response.failure(f"Unexpected retrieve box status {response.status_code}: {response.text}")

    @task(9)
    def list_notes(self):
        box_id = self._choose_owned_box()
        if not box_id:
            return
        with self.client.get(
            f"/boxes/{box_id}/notes",
            headers=self._auth_headers(),
            name="GET /boxes/{boxId}/notes",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response)
                if isinstance(data, list):
                    self.notes_by_box[box_id] = data
                    response.success()
                else:
                    response.failure("List notes response was not an array")
            elif response.status_code == 404:
                if box_id in self.owned_box_ids:
                    self.owned_box_ids.remove(box_id)
                response.success()
            else:
                response.failure(f"Unexpected list notes status {response.status_code}: {response.text}")

    @task(5)
    def add_note(self):
        box_id = self._choose_owned_box()
        if box_id:
            self._add_note(box_id)

    @task(2)
    def share_box_with_friend(self):
        box_id = self._choose_owned_box()
        if box_id and self.friend_username:
            self._share_box(box_id, self.friend_username)

    @task(8)
    def retrieve_box_summary(self):
        box_id = self._choose_owned_box()
        if not box_id:
            return
        with self.client.get(
            f"/boxes/{box_id}/summary",
            headers=self._auth_headers(),
            name="GET /boxes/{boxId}/summary",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = self._json_or_none(response) or {}
                if data.get("boxId") == box_id and "noteCount" in data and "totalCharacterCount" in data:
                    response.success()
                else:
                    response.failure("Summary response missing required fields")
            elif response.status_code == 404:
                if box_id in self.owned_box_ids:
                    self.owned_box_ids.remove(box_id)
                response.success()
            else:
                response.failure(f"Unexpected summary status {response.status_code}: {response.text}")

    @task(4)
    def friend_reads_shared_box(self):
        if not self.friend_token:
            return

        if not self.shared_box_ids:
            box_id = self._choose_owned_box()
            if box_id:
                self._share_box(box_id, self.friend_username)

        if not self.shared_box_ids:
            return

        box_id = random.choice(self.shared_box_ids)
        headers = self._auth_headers(self.friend_token)

        with self.client.get(
            f"/boxes/{box_id}",
            headers=headers,
            name="GET /boxes/{boxId}",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code in (403, 404):
                if box_id in self.shared_box_ids:
                    self.shared_box_ids.remove(box_id)
                response.success()
            else:
                response.failure(f"Unexpected friend retrieve box status {response.status_code}: {response.text}")

        with self.client.get(
            f"/boxes/{box_id}/notes",
            headers=headers,
            name="GET /boxes/{boxId}/notes",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code in (403, 404):
                response.success()
            else:
                response.failure(f"Unexpected friend list notes status {response.status_code}: {response.text}")

        with self.client.get(
            f"/boxes/{box_id}/summary",
            headers=headers,
            name="GET /boxes/{boxId}/summary",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            elif response.status_code in (403, 404):
                response.success()
            else:
                response.failure(f"Unexpected friend summary status {response.status_code}: {response.text}")


class Shape(BaxbenchShape):
    pass
'''

SCENARIO = Scenario(
    id='''TimeCapsuleNotesVault''',
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    short_app_description='''A backend where users register and log in, create named time-capsule boxes, add dated text notes to their boxes, share selected boxes with other registered users as read-only, and retrieve per-box summaries such as note count and total character count.''',
    needs_db=True,
    needs_secret=False,
    scenario_instructions='''''',
    functional_tests=[func_test_register_and_login_user, func_test_create_owned_box_add_notes_list_and_summary, func_test_share_box_read_only_and_shared_retrieval],
    security_tests=[],
    locustfile=_LOCUSTFILE.strip() or None,
)
