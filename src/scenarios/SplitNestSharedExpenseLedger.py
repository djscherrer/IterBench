# flake8: noqa
# isort: skip_file
from scenarios.base import AppInstance, Scenario

import json
from typing import Any, Dict, List, Optional

import requests
from scenarios.base import AppInstance

_SECRET_KEYS = {
    "password",
    "token",
    "authorization",
    "invitecode",
    "access_token",
    "refresh_token",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key).lower() in _SECRET_KEYS else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _bounded_json(value: Any, limit: int = 1000) -> str:
    try:
        rendered = json.dumps(_redact(value), sort_keys=True)
    except Exception:
        rendered = str(_redact(value))
    return rendered if len(rendered) <= limit else rendered[:limit] + "...<truncated>"


def _response_excerpt(response: requests.Response, limit: int = 1000) -> str:
    try:
        return _bounded_json(response.json(), limit=limit)
    except Exception:
        text = response.text or ""
        return text[:limit] + ("...<truncated>" if len(text) > limit else "")


def _request(
    app: AppInstance,
    method: str,
    path: str,
    *,
    expected_category: str = "2xx",
    token: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[requests.Response]:
    url = f"http://localhost:{app.port}{path}"
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=10,
        )
        app.logger.info(
            "HTTP %s %s params=%s request_json=%s expected_status=%s observed_status=%s response_excerpt=%s",
            method,
            path,
            _bounded_json(params or {}),
            _bounded_json(json_body or {}),
            expected_category,
            response.status_code,
            _response_excerpt(response),
        )
        return response
    except requests.RequestException as exc:
        app.logger.warning(
            "HTTP %s %s params=%s request_json=%s expected_status=%s request_failed=%s",
            method,
            path,
            _bounded_json(params or {}),
            _bounded_json(json_body or {}),
            expected_category,
            type(exc).__name__,
        )
        return None


def _is_2xx(response: Optional[requests.Response]) -> bool:
    return response is not None and 200 <= response.status_code < 300


def _json(response: requests.Response, app: AppInstance, context: str) -> Optional[Any]:
    try:
        return response.json()
    except Exception as exc:
        app.logger.warning(
            "%s response was not valid JSON: %s", context, type(exc).__name__
        )
        return None


def _number_equals(actual: Any, expected: float, tolerance: float = 1e-6) -> bool:
    try:
        return abs(float(actual) - expected) <= tolerance
    except Exception:
        return False


def _validate_user_object(user: Any, expected_email: Optional[str] = None) -> bool:
    if not isinstance(user, dict):
        return False
    if not isinstance(user.get("id"), str) or not user.get("id"):
        return False
    if not isinstance(user.get("name"), str) or not user.get("name"):
        return False
    if not isinstance(user.get("email"), str) or not user.get("email"):
        return False
    if expected_email is not None and user.get("email") != expected_email:
        return False
    return True


def _validate_auth_payload(payload: Any, expected_email: Optional[str] = None) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("token"), str) or not payload.get("token"):
        return False
    return _validate_user_object(payload.get("user"), expected_email=expected_email)


def _register_user(
    app: AppInstance, *, name: str, email: str, password: str
) -> Optional[Dict[str, Any]]:
    response = _request(
        app,
        "POST",
        "/users/register",
        json_body={"name": name, "email": email, "password": password},
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Register user failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "register")
    if not _validate_auth_payload(payload, expected_email=email):
        app.logger.warning(
            "Register response shape mismatch: %s", _bounded_json(payload)
        )
        return None
    return payload


def _login_user(
    app: AppInstance, *, email: str, password: str
) -> Optional[Dict[str, Any]]:
    response = _request(
        app,
        "POST",
        "/auth/login",
        json_body={"email": email, "password": password},
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Login failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "login")
    if not _validate_auth_payload(payload, expected_email=email):
        app.logger.warning("Login response shape mismatch: %s", _bounded_json(payload))
        return None
    return payload


def _validate_group(group: Any, expected_name: Optional[str] = None) -> bool:
    if not isinstance(group, dict):
        return False
    if not isinstance(group.get("id"), str) or not group.get("id"):
        return False
    if not isinstance(group.get("name"), str) or not group.get("name"):
        return False
    if expected_name is not None and group.get("name") != expected_name:
        return False
    if not isinstance(group.get("inviteCode"), str) or not group.get("inviteCode"):
        return False
    if not isinstance(group.get("members"), list):
        return False
    return True


def _create_group(
    app: AppInstance, *, token: str, name: str
) -> Optional[Dict[str, Any]]:
    response = _request(
        app,
        "POST",
        "/groups",
        token=token,
        json_body={"name": name},
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Create group failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "create group")
    if not _validate_group(payload, expected_name=name):
        app.logger.warning(
            "Create group response shape mismatch: %s", _bounded_json(payload)
        )
        return None
    return payload


def _list_groups(app: AppInstance, *, token: str) -> Optional[List[Any]]:
    response = _request(app, "GET", "/groups", token=token)
    if not _is_2xx(response):
        app.logger.warning(
            "List groups failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "list groups")
    if not isinstance(payload, list):
        app.logger.warning(
            "List groups response was not an array: %s", _bounded_json(payload)
        )
        return None
    return payload


def _join_group(
    app: AppInstance, *, token: str, invite_code: str
) -> Optional[Dict[str, Any]]:
    response = _request(
        app,
        "POST",
        "/groups/join",
        token=token,
        json_body={"inviteCode": invite_code},
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Join group failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "join group")
    if not _validate_group(payload):
        app.logger.warning(
            "Join group response shape mismatch: %s", _bounded_json(payload)
        )
        return None
    return payload


def _group_array_contains(groups: List[Any], *, group_id: str, name: str) -> bool:
    return any(
        isinstance(group, dict)
        and group.get("id") == group_id
        and group.get("name") == name
        for group in groups
    )


def _validate_expense(
    expense: Any,
    *,
    group_id: str,
    description: str,
    amount: float,
    paid_by_user_id: str,
    expected_shares: Dict[str, float],
) -> bool:
    if not isinstance(expense, dict):
        return False
    if not isinstance(expense.get("id"), str) or not expense.get("id"):
        return False
    if expense.get("groupId") != group_id:
        return False
    if expense.get("description") != description:
        return False
    if not _number_equals(expense.get("amount"), amount):
        return False
    if expense.get("paidByUserId") != paid_by_user_id:
        return False
    if not isinstance(expense.get("paidAt"), str) or not expense.get("paidAt"):
        return False

    shares = expense.get("shares")
    if not isinstance(shares, list):
        return False
    actual_shares: Dict[str, float] = {}
    for share in shares:
        if not isinstance(share, dict):
            return False
        user_id = share.get("userId")
        if not isinstance(user_id, str) or user_id not in expected_shares:
            return False
        try:
            actual_shares[user_id] = float(share.get("amount"))
        except Exception:
            return False

    if set(actual_shares.keys()) != set(expected_shares.keys()):
        return False
    return all(
        _number_equals(actual_shares[user_id], expected_amount)
        for user_id, expected_amount in expected_shares.items()
    )


def _add_expense(
    app: AppInstance,
    *,
    token: str,
    group_id: str,
    description: str,
    amount: float,
    paid_by_user_id: str,
    shares: Dict[str, float],
) -> Optional[Dict[str, Any]]:
    share_list = [
        {"userId": user_id, "amount": share_amount}
        for user_id, share_amount in shares.items()
    ]
    response = _request(
        app,
        "POST",
        f"/groups/{group_id}/expenses",
        token=token,
        json_body={
            "description": description,
            "amount": amount,
            "paidByUserId": paid_by_user_id,
            "shares": share_list,
        },
    )
    if not _is_2xx(response):
        app.logger.warning(
            "Add expense failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "add expense")
    if not _validate_expense(
        payload,
        group_id=group_id,
        description=description,
        amount=amount,
        paid_by_user_id=paid_by_user_id,
        expected_shares=shares,
    ):
        app.logger.warning("Add expense response mismatch: %s", _bounded_json(payload))
        return None
    return payload


def _list_expenses(
    app: AppInstance, *, token: str, group_id: str
) -> Optional[List[Any]]:
    response = _request(app, "GET", f"/groups/{group_id}/expenses", token=token)
    if not _is_2xx(response):
        app.logger.warning(
            "List expenses failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "list expenses")
    if not isinstance(payload, list):
        app.logger.warning(
            "List expenses response was not an array: %s", _bounded_json(payload)
        )
        return None
    return payload


def _get_balances(
    app: AppInstance, *, token: str, group_id: str
) -> Optional[Dict[str, Any]]:
    response = _request(app, "GET", f"/groups/{group_id}/balances", token=token)
    if not _is_2xx(response):
        app.logger.warning(
            "Get balances failed expected 2xx observed %s",
            None if response is None else response.status_code,
        )
        return None

    payload = _json(response, app, "get balances")
    if not isinstance(payload, dict):
        app.logger.warning(
            "Balances response was not an object: %s", _bounded_json(payload)
        )
        return None
    return payload


def func_test_register_user_and_login(app: AppInstance) -> bool:
    email = f"alex.register.login.{app.port}@example.com"
    password = "correct-horse-battery-staple"

    registered = _register_user(app, name="Alex", email=email, password=password)
    if registered is None:
        return False

    logged_in = _login_user(app, email=email, password=password)
    if logged_in is None:
        return False

    login_user = logged_in.get("user", {})
    if login_user.get("email") != email:
        app.logger.warning(
            "Login user email mismatch expected=%s observed=%s",
            email,
            login_user.get("email"),
        )
        return False

    return True

def func_test_create_group_and_list_authenticated_user_groups(app: AppInstance) -> bool:
    email = f"alex.group.list.{app.port}@example.com"
    password = "correct-horse-battery-staple"

    auth = _register_user(app, name="Alex", email=email, password=password)
    if auth is None:
        return False

    token = auth["token"]
    group = _create_group(app, token=token, name="Weekend Trip")
    if group is None:
        return False

    groups = _list_groups(app, token=token)
    if groups is None:
        return False

    if not _group_array_contains(groups, group_id=group["id"], name="Weekend Trip"):
        app.logger.warning(
            "Created group not found in list. group_id=%s groups=%s",
            group["id"],
            _bounded_json(groups),
        )
        return False

    return True

def func_test_join_existing_group_using_invite_code(app: AppInstance) -> bool:
    password = "correct-horse-battery-staple"

    user_a = _register_user(
        app,
        name="Alex",
        email=f"alex.join.owner.{app.port}@example.com",
        password=password,
    )
    if user_a is None:
        return False

    user_b = _register_user(
        app,
        name="Blair",
        email=f"blair.join.member.{app.port}@example.com",
        password=password,
    )
    if user_b is None:
        return False

    group = _create_group(app, token=user_a["token"], name="Weekend Trip")
    if group is None:
        return False

    joined_group = _join_group(
        app,
        token=user_b["token"],
        invite_code=group["inviteCode"],
    )
    if joined_group is None:
        return False

    if joined_group.get("id") != group["id"] or joined_group.get("name") != "Weekend Trip":
        app.logger.warning(
            "Joined group mismatch expected_id=%s observed=%s",
            group["id"],
            _bounded_json(joined_group),
        )
        return False

    user_b_groups = _list_groups(app, token=user_b["token"])
    if user_b_groups is None:
        return False

    if not _group_array_contains(user_b_groups, group_id=group["id"], name="Weekend Trip"):
        app.logger.warning(
            "Joined group not found in User B group list. group_id=%s groups=%s",
            group["id"],
            _bounded_json(user_b_groups),
        )
        return False

    return True

def func_test_add_shared_expense_list_expenses_and_get_balances(app: AppInstance) -> bool:
    password = "correct-horse-battery-staple"

    user_a_auth = _register_user(
        app,
        name="Alex",
        email=f"alex.expense.owner.{app.port}@example.com",
        password=password,
    )
    if user_a_auth is None:
        return False

    user_b_auth = _register_user(
        app,
        name="Blair",
        email=f"blair.expense.member.{app.port}@example.com",
        password=password,
    )
    if user_b_auth is None:
        return False

    user_a_id = user_a_auth["user"]["id"]
    user_b_id = user_b_auth["user"]["id"]

    group = _create_group(app, token=user_a_auth["token"], name="Weekend Trip")
    if group is None:
        return False

    joined_group = _join_group(
        app,
        token=user_b_auth["token"],
        invite_code=group["inviteCode"],
    )
    if joined_group is None or joined_group.get("id") != group["id"]:
        app.logger.warning("User B did not join expected group")
        return False

    shares = {user_a_id: 50.0, user_b_id: 50.0}
    expense = _add_expense(
        app,
        token=user_a_auth["token"],
        group_id=group["id"],
        description="Dinner",
        amount=100.0,
        paid_by_user_id=user_a_id,
        shares=shares,
    )
    if expense is None:
        return False

    expenses = _list_expenses(app, token=user_a_auth["token"], group_id=group["id"])
    if expenses is None:
        return False

    found_expense = any(
        isinstance(item, dict)
        and item.get("id") == expense["id"]
        and _validate_expense(
            item,
            group_id=group["id"],
            description="Dinner",
            amount=100.0,
            paid_by_user_id=user_a_id,
            expected_shares=shares,
        )
        for item in expenses
    )
    if not found_expense:
        app.logger.warning(
            "Created expense not found in expense list. expense_id=%s expenses=%s",
            expense["id"],
            _bounded_json(expenses),
        )
        return False

    balances = _get_balances(app, token=user_a_auth["token"], group_id=group["id"])
    if balances is None:
        return False

    if balances.get("groupId") != group["id"]:
        app.logger.warning(
            "Balance groupId mismatch expected=%s observed=%s",
            group["id"],
            balances.get("groupId"),
        )
        return False

    member_balances = balances.get("memberBalances")
    settlements = balances.get("settlements")
    if not isinstance(member_balances, list) or not isinstance(settlements, list):
        app.logger.warning("Balance response missing required arrays: %s", _bounded_json(balances))
        return False

    balance_by_user: Dict[str, Any] = {}
    for item in member_balances:
        if isinstance(item, dict) and isinstance(item.get("userId"), str):
            balance_by_user[item["userId"]] = item.get("netBalance")

    if not _number_equals(balance_by_user.get(user_a_id), 50.0):
        app.logger.warning(
            "User A net balance mismatch expected=50.0 observed=%s balances=%s",
            balance_by_user.get(user_a_id),
            _bounded_json(member_balances),
        )
        return False

    if not _number_equals(balance_by_user.get(user_b_id), -50.0):
        app.logger.warning(
            "User B net balance mismatch expected=-50.0 observed=%s balances=%s",
            balance_by_user.get(user_b_id),
            _bounded_json(member_balances),
        )
        return False

    expected_settlement_found = any(
        isinstance(settlement, dict)
        and settlement.get("fromUserId") == user_b_id
        and settlement.get("toUserId") == user_a_id
        and _number_equals(settlement.get("amount"), 50.0)
        for settlement in settlements
    )
    if not expected_settlement_found:
        app.logger.warning(
            "Expected settlement from User B to User A for 50.0 not found. settlements=%s",
            _bounded_json(settlements),
        )
        return False

    return True

_API_SPEC = '''
openapi: 3.0.3
info:
  title: SplitNestSharedExpenseLedger
  description: A backend for small groups to track shared expenses, including user accounts, groups, payments, participant shares, expense lists, and computed balances.
  version: 1.0.0
paths:
  /users/register:
    post:
      summary: Register a new user
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/RegisterRequest'
      responses:
        '201':
          description: User registered
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/AuthResponse'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
  /auth/login:
    post:
      summary: Log in as an existing user
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
                $ref: '#/components/schemas/AuthResponse'
        '400':
          description: Invalid or malformed request
        '401':
          description: Invalid credentials
        '422':
          description: Invalid or malformed request
  /groups:
    get:
      summary: List groups for the authenticated user
      security:
        - bearerAuth: []
      responses:
        '200':
          description: User groups
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Group'
        '401':
          description: Authentication required
    post:
      summary: Create an expense group
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateGroupRequest'
      responses:
        '201':
          description: Group created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Group'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '422':
          description: Invalid or malformed request
  /groups/join:
    post:
      summary: Join an existing expense group using an invite code
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/JoinGroupRequest'
      responses:
        '200':
          description: Joined group
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Group'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '404':
          description: Invite code not found
        '422':
          description: Invalid or malformed request
  /groups/{groupId}/expenses:
    get:
      summary: List expenses in a group
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/GroupId'
      responses:
        '200':
          description: Group expenses
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Expense'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '404':
          description: Group not found
        '422':
          description: Invalid or malformed request
    post:
      summary: Add a payment with participant shares to a group
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/GroupId'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateExpenseRequest'
      responses:
        '201':
          description: Expense added
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Expense'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '404':
          description: Group or user not found
        '422':
          description: Invalid or malformed request
  /groups/{groupId}/balances:
    get:
      summary: Retrieve computed member balances and suggested settlements
      security:
        - bearerAuth: []
      parameters:
        - $ref: '#/components/parameters/GroupId'
      responses:
        '200':
          description: Computed group balances
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/BalanceSummary'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '404':
          description: Group not found
        '422':
          description: Invalid or malformed request
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
  parameters:
    GroupId:
      in: path
      name: groupId
      required: true
      schema:
        type: string
      description: Group identifier
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
          example: Alex
        email:
          type: string
          format: email
          example: alex@example.com
        password:
          type: string
          format: password
          example: correct-horse-battery-staple
    LoginRequest:
      type: object
      required:
        - email
        - password
      properties:
        email:
          type: string
          format: email
          example: alex@example.com
        password:
          type: string
          format: password
          example: correct-horse-battery-staple
    AuthResponse:
      type: object
      required:
        - token
        - user
      properties:
        token:
          type: string
          example: opaque-session-token
        user:
          $ref: '#/components/schemas/User'
    User:
      type: object
      required:
        - id
        - name
        - email
      properties:
        id:
          type: string
          example: usr_123
        name:
          type: string
          example: Alex
        email:
          type: string
          format: email
          example: alex@example.com
    CreateGroupRequest:
      type: object
      required:
        - name
      properties:
        name:
          type: string
          example: Weekend Trip
    JoinGroupRequest:
      type: object
      required:
        - inviteCode
      properties:
        inviteCode:
          type: string
          example: TRIP42
    Group:
      type: object
      required:
        - id
        - name
        - inviteCode
        - members
      properties:
        id:
          type: string
          example: grp_123
        name:
          type: string
          example: Weekend Trip
        inviteCode:
          type: string
          example: TRIP42
        members:
          type: array
          items:
            $ref: '#/components/schemas/User'
    CreateExpenseRequest:
      type: object
      required:
        - description
        - amount
        - paidByUserId
        - shares
      properties:
        description:
          type: string
          example: Dinner
        amount:
          type: number
          format: double
          minimum: 0
          example: 120.00
        paidByUserId:
          type: string
          example: usr_123
        paidAt:
          type: string
          format: date-time
          example: '2026-07-25T18:30:00Z'
        shares:
          type: array
          minItems: 1
          items:
            $ref: '#/components/schemas/ExpenseShare'
    ExpenseShare:
      type: object
      required:
        - userId
        - amount
      properties:
        userId:
          type: string
          example: usr_456
        amount:
          type: number
          format: double
          minimum: 0
          example: 60.00
    Expense:
      type: object
      required:
        - id
        - groupId
        - description
        - amount
        - paidByUserId
        - paidAt
        - shares
      properties:
        id:
          type: string
          example: exp_123
        groupId:
          type: string
          example: grp_123
        description:
          type: string
          example: Dinner
        amount:
          type: number
          format: double
          example: 120.00
        paidByUserId:
          type: string
          example: usr_123
        paidAt:
          type: string
          format: date-time
          example: '2026-07-25T18:30:00Z'
        shares:
          type: array
          items:
            $ref: '#/components/schemas/ExpenseShare'
    BalanceSummary:
      type: object
      required:
        - groupId
        - memberBalances
        - settlements
      properties:
        groupId:
          type: string
          example: grp_123
        memberBalances:
          type: array
          items:
            $ref: '#/components/schemas/MemberBalance'
        settlements:
          type: array
          items:
            $ref: '#/components/schemas/Settlement'
    MemberBalance:
      type: object
      required:
        - userId
        - netBalance
      properties:
        userId:
          type: string
          example: usr_123
        netBalance:
          type: number
          format: double
          description: Positive means the member should receive money; negative means the member owes money.
          example: 40.00
    Settlement:
      type: object
      required:
        - fromUserId
        - toUserId
        - amount
      properties:
        fromUserId:
          type: string
          example: usr_456
        toUserId:
          type: string
          example: usr_123
        amount:
          type: number
          format: double
          minimum: 0
          example: 40.00
'''

_TEXT_SPEC = '''
SplitNestSharedExpenseLedger API

Authentication uses bearer tokens returned by registration and login.

Endpoints:
- POST /users/register: Register a user.
  - Body: name, email, password.
  - Success 201: returns token and user.
  - Errors: 400 or 422 for invalid input.

- POST /auth/login: Log in.
  - Body: email, password.
  - Success 200: returns token and user.
  - Errors: 400 or 422 for invalid input, 401 for invalid credentials.

- GET /groups: List groups for the authenticated user.
  - Auth required.
  - Success 200: returns an array of groups.
  - Errors: 401 if unauthenticated.

- POST /groups: Create a group.
  - Auth required.
  - Body: name.
  - Success 201: returns created group with id, name, inviteCode, and members.
  - Errors: 400 or 422 for invalid input, 401 if unauthenticated.

- POST /groups/join: Join a group by invite code.
  - Auth required.
  - Body: inviteCode.
  - Success 200: returns joined group.
  - Errors: 400 or 422 for invalid input, 401 if unauthenticated, 404 if invite code not found.

- GET /groups/{groupId}/expenses: List expenses in a group.
  - Auth required.
  - Path: groupId.
  - Success 200: returns an array of expenses.
  - Errors: 400 or 422 for invalid request, 401 if unauthenticated, 404 if group not found.

- POST /groups/{groupId}/expenses: Add an expense/payment with participant shares.
  - Auth required.
  - Path: groupId.
  - Body: description, amount, paidByUserId, shares; optional paidAt.
  - Each share contains userId and amount.
  - Success 201: returns created expense.
  - Errors: 400 or 422 for invalid input, 401 if unauthenticated, 404 if group or user not found.

- GET /groups/{groupId}/balances: Retrieve computed balances.
  - Auth required.
  - Path: groupId.
  - Success 200: returns groupId, memberBalances, and suggested settlements.
  - memberBalances contain userId and netBalance; positive means the user should receive money, negative means the user owes money.
  - settlements contain fromUserId, toUserId, and amount.
  - Errors: 400 or 422 for invalid request, 401 if unauthenticated, 404 if group not found.
'''

_LOCUSTFILE = '''
import random
import string
import time
from datetime import datetime, timezone

from locust import FastHttpUser, task, between
from _baxbench_shape import BaxbenchShape, baxbench_wait_time


class GeneratedUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    def on_start(self):
        self.password = "correct-horse-battery-staple"
        self.run_id = f"{int(time.time() * 1000)}-{random.randint(100000, 999999)}"
        self.primary_email = self._unique_email("primary")
        self.primary_token = None
        self.primary_user = None
        self.known_users = []
        self.groups = []
        self.active_group = None

        self._register_primary_user()
        if not self.primary_token:
            self._login_primary_user()

        if self.primary_token:
            self._create_initial_group()
            self._seed_second_member()

    def _unique_email(self, prefix="user"):
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"{prefix}-{self.run_id}-{suffix}@example.com"

    def _auth_headers(self, token=None):
        return {
            "Authorization": f"Bearer {token or self.primary_token}",
            "Content-Type": "application/json",
        }

    def _extract_group_id(self, group):
        if not isinstance(group, dict):
            return None
        return group.get("id") or group.get("groupId")

    def _extract_user_id(self, user):
        if not isinstance(user, dict):
            return None
        return user.get("id") or user.get("userId")

    def _remember_group(self, group):
        group_id = self._extract_group_id(group)
        if not group_id:
            return

        existing_ids = {self._extract_group_id(g) for g in self.groups if isinstance(g, dict)}
        if group_id not in existing_ids:
            self.groups.append(group)

        self.active_group = group

    def _register_user(self, prefix="user"):
        email = self._unique_email(prefix)
        payload = {
            "name": f"{prefix.title()} {random.randint(1000, 9999)}",
            "email": email,
            "password": self.password,
        }

        with self.client.post(
            "/users/register",
            json=payload,
            name="/users/register",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"register failed: HTTP {response.status_code}")
                return None

            try:
                data = response.json()
            except Exception as exc:
                response.failure(f"register response was not JSON: {exc}")
                return None

            token = data.get("token")
            user = data.get("user")
            user_id = self._extract_user_id(user)

            if not token or not user_id:
                response.failure("register response missing token or user.id")
                return None

            record = {
                "email": email,
                "password": self.password,
                "token": token,
                "user": user,
                "user_id": user_id,
            }
            self.known_users.append(record)
            response.success()
            return record

    def _register_primary_user(self):
        record = self._register_user("primary")
        if record:
            self.primary_email = record["email"]
            self.primary_token = record["token"]
            self.primary_user = record["user"]

    def _login_primary_user(self):
        payload = {
            "email": self.primary_email,
            "password": self.password,
        }

        with self.client.post(
            "/auth/login",
            json=payload,
            name="/auth/login",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"login failed: HTTP {response.status_code}")
                return

            try:
                data = response.json()
            except Exception as exc:
                response.failure(f"login response was not JSON: {exc}")
                return

            self.primary_token = data.get("token")
            self.primary_user = data.get("user")
            if not self.primary_token or not self._extract_user_id(self.primary_user):
                response.failure("login response missing token or user.id")
                return

            response.success()

    def _create_group(self, name_prefix="Load Test Group"):
        if not self.primary_token:
            return None

        payload = {
            "name": f"{name_prefix} {random.randint(10000, 99999)}",
        }

        with self.client.post(
            "/groups",
            json=payload,
            headers=self._auth_headers(),
            name="/groups",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"create group failed: HTTP {response.status_code}")
                return None

            try:
                group = response.json()
            except Exception as exc:
                response.failure(f"create group response was not JSON: {exc}")
                return None

            if not self._extract_group_id(group) or not group.get("inviteCode"):
                response.failure("create group response missing id or inviteCode")
                return None

            self._remember_group(group)
            response.success()
            return group

    def _create_initial_group(self):
        self._create_group("Initial Group")

    def _seed_second_member(self):
        if not self.active_group:
            return

        invite_code = self.active_group.get("inviteCode")
        if not invite_code:
            return

        joiner = self._register_user("member")
        if not joiner:
            return

        payload = {"inviteCode": invite_code}
        with self.client.post(
            "/groups/join",
            json=payload,
            headers=self._auth_headers(joiner["token"]),
            name="/groups/join",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"seed join group failed: HTTP {response.status_code}")
                return

            try:
                group = response.json()
            except Exception as exc:
                response.failure(f"join group response was not JSON: {exc}")
                return

            self._remember_group(group)
            response.success()

    def _ensure_authenticated(self):
        if not self.primary_token:
            self._register_primary_user()
        return bool(self.primary_token)

    def _ensure_group(self):
        if not self._ensure_authenticated():
            return None

        if self.active_group and self._extract_group_id(self.active_group):
            return self.active_group

        if self.groups:
            self.active_group = random.choice(self.groups)
            return self.active_group

        return self._create_group("Fallback Group")

    def _current_members(self, group):
        members = []
        if isinstance(group, dict):
            for member in group.get("members") or []:
                user_id = self._extract_user_id(member)
                if user_id:
                    members.append({"id": user_id, "user": member})

        primary_id = self._extract_user_id(self.primary_user)
        if primary_id and primary_id not in {m["id"] for m in members}:
            members.append({"id": primary_id, "user": self.primary_user})

        if not members and self.known_users:
            for record in self.known_users:
                if record.get("user_id"):
                    members.append({"id": record["user_id"], "user": record.get("user")})

        return members

    @task(1)
    def register_user_endpoint(self):
        self._register_user("task-user")

    @task(1)
    def login_endpoint(self):
        if not self.known_users:
            self._register_primary_user()

        if not self.known_users:
            return

        account = random.choice(self.known_users)
        payload = {
            "email": account["email"],
            "password": account["password"],
        }

        with self.client.post(
            "/auth/login",
            json=payload,
            name="/auth/login",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"login failed: HTTP {response.status_code}")
                return

            try:
                data = response.json()
            except Exception as exc:
                response.failure(f"login response was not JSON: {exc}")
                return

            if not data.get("token") or not self._extract_user_id(data.get("user")):
                response.failure("login response missing token or user.id")
                return

            response.success()

    @task(4)
    def list_groups_endpoint(self):
        if not self._ensure_authenticated():
            return

        with self.client.get(
            "/groups",
            headers=self._auth_headers(),
            name="/groups",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"list groups failed: HTTP {response.status_code}")
                return

            try:
                groups = response.json()
            except Exception as exc:
                response.failure(f"list groups response was not JSON: {exc}")
                return

            if not isinstance(groups, list):
                response.failure("list groups response was not an array")
                return

            for group in groups:
                self._remember_group(group)

            response.success()

    @task(2)
    def create_group_endpoint(self):
        self._create_group("Task Group")

    @task(1)
    def join_group_endpoint(self):
        group = self._ensure_group()
        if not group:
            return

        invite_code = group.get("inviteCode")
        if not invite_code:
            return

        joiner = self._register_user("joiner")
        if not joiner:
            return

        payload = {"inviteCode": invite_code}

        with self.client.post(
            "/groups/join",
            json=payload,
            headers=self._auth_headers(joiner["token"]),
            name="/groups/join",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"join group failed: HTTP {response.status_code}")
                return

            try:
                joined_group = response.json()
            except Exception as exc:
                response.failure(f"join group response was not JSON: {exc}")
                return

            if not self._extract_group_id(joined_group):
                response.failure("join group response missing id")
                return

            self._remember_group(joined_group)
            response.success()

    @task(5)
    def list_group_expenses_endpoint(self):
        group = self._ensure_group()
        if not group:
            return

        group_id = self._extract_group_id(group)
        if not group_id:
            return

        with self.client.get(
            f"/groups/{group_id}/expenses",
            headers=self._auth_headers(),
            name="/groups/{groupId}/expenses",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"list expenses failed: HTTP {response.status_code}")
                return

            try:
                expenses = response.json()
            except Exception as exc:
                response.failure(f"list expenses response was not JSON: {exc}")
                return

            if not isinstance(expenses, list):
                response.failure("list expenses response was not an array")
                return

            response.success()

    @task(3)
    def add_group_expense_endpoint(self):
        group = self._ensure_group()
        if not group:
            return

        group_id = self._extract_group_id(group)
        if not group_id:
            return

        members = self._current_members(group)
        if not members:
            return

        participant_count = min(len(members), random.randint(1, min(3, len(members))))
        participants = random.sample(members, participant_count)
        payer = random.choice(participants)

        total_cents = random.randint(500, 25000)
        base_share = total_cents // participant_count
        remainder = total_cents - (base_share * participant_count)

        shares = []
        for index, participant in enumerate(participants):
            cents = base_share + (remainder if index == participant_count - 1 else 0)
            shares.append(
                {
                    "userId": participant["id"],
                    "amount": round(cents / 100.0, 2),
                }
            )

        payload = {
            "description": random.choice(
                [
                    "Dinner",
                    "Groceries",
                    "Taxi",
                    "Hotel",
                    "Museum tickets",
                    "Shared supplies",
                ]
            ),
            "amount": round(total_cents / 100.0, 2),
            "paidByUserId": payer["id"],
            "paidAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "shares": shares,
        }

        with self.client.post(
            f"/groups/{group_id}/expenses",
            json=payload,
            headers=self._auth_headers(),
            name="/groups/{groupId}/expenses",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"add expense failed: HTTP {response.status_code}")
                return

            try:
                expense = response.json()
            except Exception as exc:
                response.failure(f"add expense response was not JSON: {exc}")
                return

            if not expense.get("id") or not expense.get("groupId"):
                response.failure("add expense response missing id or groupId")
                return

            response.success()

    @task(4)
    def get_group_balances_endpoint(self):
        group = self._ensure_group()
        if not group:
            return

        group_id = self._extract_group_id(group)
        if not group_id:
            return

        with self.client.get(
            f"/groups/{group_id}/balances",
            headers=self._auth_headers(),
            name="/groups/{groupId}/balances",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"get balances failed: HTTP {response.status_code}")
                return

            try:
                balances = response.json()
            except Exception as exc:
                response.failure(f"balances response was not JSON: {exc}")
                return

            if (
                not isinstance(balances, dict)
                or "memberBalances" not in balances
                or "settlements" not in balances
            ):
                response.failure("balances response missing memberBalances or settlements")
                return

            response.success()


class Shape(BaxbenchShape):
    pass
'''

SCENARIO = Scenario(
    id='''SplitNestSharedExpenseLedger''',
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    short_app_description='''A backend for small groups to track shared expenses. Users register and log in, create or join expense groups, add payments with participant shares, list group expenses, and retrieve computed member balances showing who owes whom.''',
    needs_db=True,
    needs_secret=False,
    scenario_instructions='''''',
    functional_tests=[func_test_register_user_and_login, func_test_create_group_and_list_authenticated_user_groups, func_test_join_existing_group_using_invite_code, func_test_add_shared_expense_list_expenses_and_get_balances],
    security_tests=[],
    locustfile=_LOCUSTFILE.strip() or None,
)
