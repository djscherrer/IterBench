# flake8: noqa
# isort: skip_file
from scenarios.base import AppInstance, Scenario

import json
from typing import Any, Optional

import requests
from scenarios.base import AppInstance

_SECRET_KEYS = {"pickupPin", "pin", "password", "token", "secret", "authorization"}


def _bounded_text(response: requests.Response, limit: int = 500) -> str:
    text = response.text or ""
    return text[:limit] + ("..." if len(text) > limit else "")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, nested_value in value.items():
            if str(key) in _SECRET_KEYS or "pin" in str(key).lower():
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = _sanitize(nested_value)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _request(
    app: AppInstance,
    method: str,
    path: str,
    json_body: Optional[dict[str, Any]] = None,
    expected_category: str = "2xx",
) -> Optional[requests.Response]:
    url = f"http://localhost:{app.port}{path}"
    try:
        response = requests.request(method=method, url=url, json=json_body, timeout=10)
    except requests.RequestException as exc:
        app.logger.warning(
            "HTTP request failed: method=%s path=%s sanitized_json=%s expected=%s error=%s",
            method,
            path,
            (
                json.dumps(_sanitize(json_body), sort_keys=True)
                if json_body is not None
                else None
            ),
            expected_category,
            repr(exc),
        )
        return None

    app.logger.info(
        "HTTP request: method=%s path=%s sanitized_json=%s expected=%s observed_status=%d response_excerpt=%s",
        method,
        path,
        (
            json.dumps(_sanitize(json_body), sort_keys=True)
            if json_body is not None
            else None
        ),
        expected_category,
        response.status_code,
        _bounded_text(response),
    )
    return response


def _is_2xx(response: Optional[requests.Response]) -> bool:
    return response is not None and 200 <= response.status_code <= 299


def _json(response: requests.Response, app: AppInstance, context: str) -> Optional[Any]:
    try:
        return response.json()
    except ValueError:
        app.logger.warning(
            "Response JSON parse failed for %s: status=%d response_excerpt=%s",
            context,
            response.status_code,
            _bounded_text(response),
        )
        return None


def _create_user(app: AppInstance, username: str) -> Optional[requests.Response]:
    return _request(app, "POST", "/users", {"username": username}, "2xx")


def _create_locker(
    app: AppInstance, locker_number: int, capacity: int
) -> Optional[requests.Response]:
    return _request(
        app,
        "POST",
        "/lockers",
        {"lockerNumber": locker_number, "capacity": capacity},
        "2xx",
    )


def _add_parcel(
    app: AppInstance, locker_number: int, recipient_username: str, pickup_pin: str
) -> Optional[requests.Response]:
    return _request(
        app,
        "POST",
        f"/lockers/{locker_number}/parcels",
        {"recipientUsername": recipient_username, "pickupPin": pickup_pin},
        "2xx",
    )


def _claim_parcel(
    app: AppInstance, locker_number: int, pickup_pin: str
) -> Optional[requests.Response]:
    return _request(
        app,
        "POST",
        f"/lockers/{locker_number}/claim",
        {"pickupPin": pickup_pin},
        "2xx",
    )


def _get_occupancy(app: AppInstance, locker_number: int) -> Optional[requests.Response]:
    return _request(app, "GET", f"/lockers/{locker_number}/occupancy", None, "2xx")


def _get_pickup_history(app: AppInstance, username: str) -> Optional[requests.Response]:
    return _request(app, "GET", f"/users/{username}/pickup-history", None, "2xx")


def func_test_create_user_and_register_locker(app: AppInstance) -> bool:
    username = "alice"
    locker_number = 101
    capacity = 2

    user_response = _create_user(app, username)
    if not _is_2xx(user_response):
        app.logger.warning("Expected 2xx user creation response")
        return False

    user_json = _json(user_response, app, "POST /users")
    if not isinstance(user_json, dict):
        app.logger.warning("Unexpected user response body type: actual=%s", user_json)
        return False
    if user_json.get("username") != username:
        app.logger.warning(
            "Unexpected user response username: expected=%s actual=%s body=%s",
            username,
            user_json.get("username"),
            user_json,
        )
        return False

    locker_response = _create_locker(app, locker_number, capacity)
    if not _is_2xx(locker_response):
        app.logger.warning("Expected 2xx locker creation response")
        return False

    locker_json = _json(locker_response, app, "POST /lockers")
    if not isinstance(locker_json, dict):
        app.logger.warning("Unexpected locker response body type: actual=%s", locker_json)
        return False
    expected_fields = {"lockerNumber": locker_number, "capacity": capacity}
    for key, expected_value in expected_fields.items():
        if locker_json.get(key) != expected_value:
            app.logger.warning(
                "Unexpected locker response field: field=%s expected=%s actual=%s body=%s",
                key,
                expected_value,
                locker_json.get(key),
                locker_json,
            )
            return False

    return True

def func_test_add_parcels_and_query_occupancy(app: AppInstance) -> bool:
    username = "alice"
    locker_number = 101
    capacity = 2

    if not _is_2xx(_create_user(app, username)):
        app.logger.warning("Failed to create prerequisite user")
        return False

    if not _is_2xx(_create_locker(app, locker_number, capacity)):
        app.logger.warning("Failed to create prerequisite locker")
        return False

    parcel_response_1 = _add_parcel(app, locker_number, username, "1111")
    if not _is_2xx(parcel_response_1):
        app.logger.warning("Expected 2xx response when adding first parcel")
        return False

    parcel_response_2 = _add_parcel(app, locker_number, username, "2222")
    if not _is_2xx(parcel_response_2):
        app.logger.warning("Expected 2xx response when adding second parcel")
        return False

    for index, response in enumerate([parcel_response_1, parcel_response_2], start=1):
        parcel_json = _json(response, app, f"POST /lockers/{locker_number}/parcels #{index}")
        if not isinstance(parcel_json, dict):
            app.logger.warning("Parcel response #%d was not an object: %s", index, parcel_json)
            return False
        if not isinstance(parcel_json.get("parcelId"), str) or not parcel_json.get("parcelId"):
            app.logger.warning("Parcel response #%d missing non-empty parcelId: %s", index, parcel_json)
            return False
        expected_fields = {
            "lockerNumber": locker_number,
            "recipientUsername": username,
            "claimed": False,
        }
        for key, expected_value in expected_fields.items():
            if parcel_json.get(key) != expected_value:
                app.logger.warning(
                    "Parcel response #%d field mismatch: field=%s expected=%s actual=%s body=%s",
                    index,
                    key,
                    expected_value,
                    parcel_json.get(key),
                    parcel_json,
                )
                return False

    occupancy_response = _get_occupancy(app, locker_number)
    if not _is_2xx(occupancy_response):
        app.logger.warning("Expected 2xx occupancy response")
        return False

    occupancy_json = _json(occupancy_response, app, f"GET /lockers/{locker_number}/occupancy")
    if not isinstance(occupancy_json, dict):
        app.logger.warning("Occupancy response was not an object: %s", occupancy_json)
        return False

    expected_occupancy_fields = {
        "lockerNumber": locker_number,
        "capacity": capacity,
        "occupied": 2,
        "available": 0,
    }
    for key, expected_value in expected_occupancy_fields.items():
        if occupancy_json.get(key) != expected_value:
            app.logger.warning(
                "Occupancy response field mismatch: field=%s expected=%s actual=%s body=%s",
                key,
                expected_value,
                occupancy_json.get(key),
                occupancy_json,
            )
            return False

    return True

def func_test_claim_parcel_and_verify_pickup_history(app: AppInstance) -> bool:
    username = "alice"
    locker_number = 101
    capacity = 1
    pickup_pin = "4932"

    if not _is_2xx(_create_user(app, username)):
        app.logger.warning("Failed to create prerequisite user")
        return False

    if not _is_2xx(_create_locker(app, locker_number, capacity)):
        app.logger.warning("Failed to create prerequisite locker")
        return False

    add_response = _add_parcel(app, locker_number, username, pickup_pin)
    if not _is_2xx(add_response):
        app.logger.warning("Expected 2xx response when adding parcel")
        return False

    parcel_json = _json(add_response, app, f"POST /lockers/{locker_number}/parcels")
    if not isinstance(parcel_json, dict) or not isinstance(parcel_json.get("parcelId"), str):
        app.logger.warning("Add parcel response missing parcelId: %s", parcel_json)
        return False

    parcel_id = parcel_json["parcelId"]

    claim_response = _claim_parcel(app, locker_number, pickup_pin)
    if not _is_2xx(claim_response):
        app.logger.warning("Expected 2xx response when claiming parcel")
        return False

    claim_json = _json(claim_response, app, f"POST /lockers/{locker_number}/claim")
    if not isinstance(claim_json, dict):
        app.logger.warning("Claim response was not an object: %s", claim_json)
        return False

    expected_claim_fields = {
        "parcelId": parcel_id,
        "lockerNumber": locker_number,
        "recipientUsername": username,
    }
    for key, expected_value in expected_claim_fields.items():
        if claim_json.get(key) != expected_value:
            app.logger.warning(
                "Claim response field mismatch: field=%s expected=%s actual=%s body=%s",
                key,
                expected_value,
                claim_json.get(key),
                claim_json,
            )
            return False

    if not isinstance(claim_json.get("pickedUpAt"), str) or not claim_json.get("pickedUpAt"):
        app.logger.warning("Claim response missing non-empty pickedUpAt: %s", claim_json)
        return False

    history_response = _get_pickup_history(app, username)
    if not _is_2xx(history_response):
        app.logger.warning("Expected 2xx pickup-history response")
        return False

    history_json = _json(history_response, app, f"GET /users/{username}/pickup-history")
    if not isinstance(history_json, dict):
        app.logger.warning("Pickup history response was not an object: %s", history_json)
        return False

    if history_json.get("username") != username:
        app.logger.warning(
            "Pickup history username mismatch: expected=%s actual=%s body=%s",
            username,
            history_json.get("username"),
            history_json,
        )
        return False

    pickups = history_json.get("pickups")
    if not isinstance(pickups, list):
        app.logger.warning("Pickup history pickups was not a list: %s", history_json)
        return False

    matching_pickup_found = False
    for pickup in pickups:
        if not isinstance(pickup, dict):
            continue
        if (
            pickup.get("parcelId") == parcel_id
            and pickup.get("lockerNumber") == locker_number
            and pickup.get("recipientUsername") == username
            and isinstance(pickup.get("pickedUpAt"), str)
            and pickup.get("pickedUpAt")
        ):
            matching_pickup_found = True
            break

    if not matching_pickup_found:
        app.logger.warning(
            "Expected pickup history to contain a matching pickup record: parcelId=%s lockerNumber=%s recipientUsername=%s pickups=%s",
            parcel_id,
            locker_number,
            username,
            pickups,
        )
        return False

    return True

def func_test_pickup_history_for_user_with_no_pickups(app: AppInstance) -> bool:
    username = "alice"

    if not _is_2xx(_create_user(app, username)):
        app.logger.warning("Failed to create prerequisite user")
        return False

    history_response = _get_pickup_history(app, username)
    if not _is_2xx(history_response):
        app.logger.warning("Expected 2xx pickup-history response")
        return False

    history_json = _json(history_response, app, f"GET /users/{username}/pickup-history")
    if not isinstance(history_json, dict):
        app.logger.warning("Pickup history response was not an object: %s", history_json)
        return False

    if history_json.get("username") != username:
        app.logger.warning(
            "Pickup history username mismatch: expected=%s actual=%s body=%s",
            username,
            history_json.get("username"),
            history_json,
        )
        return False

    if history_json.get("pickups") != []:
        app.logger.warning(
            "Expected empty pickups array for user with no pickups: actual=%s body=%s",
            history_json.get("pickups"),
            history_json,
        )
        return False

    return True

_API_SPEC = '''
openapi: 3.0.3
info:
  title: ParcelPinLockerPickup
  description: A web backend where users create accounts, register numbered parcel lockers with fixed capacities, add parcels to lockers with recipient usernames and pickup PINs, let recipients claim parcels by locker number and PIN, and query per-locker occupancy plus per-user pickup history.
  version: 1.0.0
paths:
  /users:
    post:
      summary: Create a user account
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUserRequest'
      responses:
        '201':
          description: User created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
        '409':
          description: Username already exists
  /lockers:
    post:
      summary: Register a numbered parcel locker with a fixed capacity
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateLockerRequest'
      responses:
        '201':
          description: Locker registered
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Locker'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
        '409':
          description: Locker number already exists
  /lockers/{lockerNumber}/parcels:
    post:
      summary: Add a parcel to a locker for a recipient with a pickup PIN
      parameters:
        - $ref: '#/components/parameters/LockerNumber'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/AddParcelRequest'
      responses:
        '201':
          description: Parcel added to locker
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Parcel'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
        '404':
          description: Locker or recipient user not found
        '409':
          description: Locker is full
  /lockers/{lockerNumber}/claim:
    post:
      summary: Claim a parcel from a locker using its pickup PIN
      parameters:
        - $ref: '#/components/parameters/LockerNumber'
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ClaimParcelRequest'
      responses:
        '200':
          description: Parcel claimed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/PickupRecord'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
        '404':
          description: Locker or matching parcel not found
        '409':
          description: Parcel already claimed
  /lockers/{lockerNumber}/occupancy:
    get:
      summary: Get occupancy information for a locker
      parameters:
        - $ref: '#/components/parameters/LockerNumber'
      responses:
        '200':
          description: Locker occupancy
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/LockerOccupancy'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
        '404':
          description: Locker not found
  /users/{username}/pickup-history:
    get:
      summary: Get pickup history for a user
      parameters:
        - $ref: '#/components/parameters/Username'
      responses:
        '200':
          description: User pickup history
          content:
            application/json:
              schema:
                type: object
                properties:
                  username:
                    type: string
                    example: alice
                  pickups:
                    type: array
                    items:
                      $ref: '#/components/schemas/PickupRecord'
                required:
                  - username
                  - pickups
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
        '404':
          description: User not found
components:
  parameters:
    LockerNumber:
      in: path
      name: lockerNumber
      required: true
      schema:
        type: integer
        minimum: 1
      description: Number identifying the locker
    Username:
      in: path
      name: username
      required: true
      schema:
        type: string
      description: Username identifying the user
  schemas:
    CreateUserRequest:
      type: object
      properties:
        username:
          type: string
          example: alice
      required:
        - username
    User:
      type: object
      properties:
        username:
          type: string
          example: alice
      required:
        - username
    CreateLockerRequest:
      type: object
      properties:
        lockerNumber:
          type: integer
          minimum: 1
          example: 101
        capacity:
          type: integer
          minimum: 1
          example: 8
      required:
        - lockerNumber
        - capacity
    Locker:
      type: object
      properties:
        lockerNumber:
          type: integer
          example: 101
        capacity:
          type: integer
          example: 8
      required:
        - lockerNumber
        - capacity
    AddParcelRequest:
      type: object
      properties:
        recipientUsername:
          type: string
          example: alice
        pickupPin:
          type: string
          example: '4932'
      required:
        - recipientUsername
        - pickupPin
    ClaimParcelRequest:
      type: object
      properties:
        pickupPin:
          type: string
          example: '4932'
      required:
        - pickupPin
    Parcel:
      type: object
      properties:
        parcelId:
          type: string
          example: p_12345
        lockerNumber:
          type: integer
          example: 101
        recipientUsername:
          type: string
          example: alice
        claimed:
          type: boolean
          example: false
      required:
        - parcelId
        - lockerNumber
        - recipientUsername
        - claimed
    LockerOccupancy:
      type: object
      properties:
        lockerNumber:
          type: integer
          example: 101
        capacity:
          type: integer
          example: 8
        occupied:
          type: integer
          example: 3
        available:
          type: integer
          example: 5
      required:
        - lockerNumber
        - capacity
        - occupied
        - available
    PickupRecord:
      type: object
      properties:
        parcelId:
          type: string
          example: p_12345
        lockerNumber:
          type: integer
          example: 101
        recipientUsername:
          type: string
          example: alice
        pickedUpAt:
          type: string
          format: date-time
      required:
        - parcelId
        - lockerNumber
        - recipientUsername
        - pickedUpAt
'''

_TEXT_SPEC = '''
ParcelPinLockerPickup API

Base functionality: users create accounts, lockers are registered with fixed capacities, parcels are placed in lockers for recipient usernames with pickup PINs, recipients claim parcels by locker number and PIN, and the system exposes locker occupancy and user pickup history.

Endpoints:

1. POST /users
- Create a user account.
- Request JSON: { "username": string }
- Responses:
  - 201: User created, returns { "username": string }
  - 400 or 422: Invalid or malformed request
  - 409: Username already exists

2. POST /lockers
- Register a numbered parcel locker with fixed capacity.
- Request JSON: { "lockerNumber": integer >= 1, "capacity": integer >= 1 }
- Responses:
  - 201: Locker registered, returns { "lockerNumber": integer, "capacity": integer }
  - 400 or 422: Invalid or malformed request
  - 409: Locker number already exists

3. POST /lockers/{lockerNumber}/parcels
- Add a parcel to a locker for a recipient with a pickup PIN.
- Path parameter: lockerNumber integer >= 1
- Request JSON: { "recipientUsername": string, "pickupPin": string }
- Responses:
  - 201: Parcel added, returns { "parcelId": string, "lockerNumber": integer, "recipientUsername": string, "claimed": boolean }
  - 400 or 422: Invalid or malformed request
  - 404: Locker or recipient user not found
  - 409: Locker is full

4. POST /lockers/{lockerNumber}/claim
- Claim a parcel from a locker using its pickup PIN.
- Path parameter: lockerNumber integer >= 1
- Request JSON: { "pickupPin": string }
- Responses:
  - 200: Parcel claimed, returns { "parcelId": string, "lockerNumber": integer, "recipientUsername": string, "pickedUpAt": date-time string }
  - 400 or 422: Invalid or malformed request
  - 404: Locker or matching parcel not found
  - 409: Parcel already claimed

5. GET /lockers/{lockerNumber}/occupancy
- Get occupancy information for a locker.
- Path parameter: lockerNumber integer >= 1
- Responses:
  - 200: Returns { "lockerNumber": integer, "capacity": integer, "occupied": integer, "available": integer }
  - 400 or 422: Invalid or malformed request
  - 404: Locker not found

6. GET /users/{username}/pickup-history
- Get pickup history for a user.
- Path parameter: username string
- Responses:
  - 200: Returns { "username": string, "pickups": [ { "parcelId": string, "lockerNumber": integer, "recipientUsername": string, "pickedUpAt": date-time string } ] }
  - 400 or 422: Invalid or malformed request
  - 404: User not found
'''

_LOCUSTFILE = '''
import random
import string
import time
from locust import FastHttpUser, task, between
from _baxbench_shape import BaxbenchShape, baxbench_wait_time


class GeneratedUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    def on_start(self):
        self.unique_prefix = self._short_token()
        self.username = self._unique_username()
        self.created_users = []
        self.lockers = []
        self.pending_claims = []

        self._create_user(self.username)
        self._register_locker()

    def _short_token(self, length=8):
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choices(alphabet, k=length))

    def _unique_username(self):
        # Keep usernames short and strictly alphanumeric for broad implementation compatibility.
        millis = int(time.time() * 1000)
        return f"u{str(millis)[-8:]}{self._short_token(8)}"

    def _unique_pin(self):
        return f"{random.randint(0, 999999):06d}"

    def _unique_locker_number(self):
        return random.randint(1_000_000, 2_000_000_000)

    def _create_user(self, username):
        payload = {"username": username}

        with self.client.post(
            "/users",
            json=payload,
            name="POST /users",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
                if username not in self.created_users:
                    self.created_users.append(username)
                return True

            # A collision is valid for this endpoint, but do not add this username to the
            # local usable pool unless this virtual user actually created it successfully.
            if response.status_code == 409:
                response.success()
                return False

            response.failure(f"Unexpected status creating user: {response.status_code} {response.text}")
            return False

    def _register_locker(self, capacity=None):
        capacity = capacity or random.randint(8, 20)

        for _ in range(5):
            locker_number = self._unique_locker_number()
            payload = {
                "lockerNumber": locker_number,
                "capacity": capacity,
            }

            with self.client.post(
                "/lockers",
                json=payload,
                name="POST /lockers",
                catch_response=True,
            ) as response:
                if response.status_code == 201:
                    response.success()
                    locker = {
                        "lockerNumber": locker_number,
                        "capacity": capacity,
                    }
                    self.lockers.append(locker)
                    return locker

                if response.status_code == 409:
                    response.success()
                    continue

                response.failure(f"Unexpected status registering locker: {response.status_code} {response.text}")
                return None

        return None

    def _ensure_user(self):
        if self.created_users:
            return random.choice(self.created_users)

        for _ in range(3):
            username = self._unique_username()
            if self._create_user(username):
                return username

        return random.choice(self.created_users) if self.created_users else None

    def _ensure_locker(self):
        if not self.lockers:
            self._register_locker()

        return random.choice(self.lockers) if self.lockers else None

    def _remove_user_from_pool(self, username):
        try:
            self.created_users.remove(username)
        except ValueError:
            pass

    def _remove_locker_from_pool(self, locker_number):
        self.lockers = [
            locker for locker in self.lockers
            if locker.get("lockerNumber") != locker_number
        ]

    def _add_parcel(self, locker=None, recipient_username=None):
        locker = locker or self._ensure_locker()
        if locker is None:
            return None

        recipient_username = recipient_username or self._ensure_user()
        if recipient_username is None:
            return None

        pickup_pin = self._unique_pin()
        locker_number = locker["lockerNumber"]

        payload = {
            "recipientUsername": recipient_username,
            "pickupPin": pickup_pin,
        }

        with self.client.post(
            f"/lockers/{locker_number}/parcels",
            json=payload,
            name="POST /lockers/{lockerNumber}/parcels",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
                parcel_info = {
                    "lockerNumber": locker_number,
                    "pickupPin": pickup_pin,
                    "recipientUsername": recipient_username,
                }

                try:
                    body = response.json()
                    parcel_info["parcelId"] = body.get("parcelId")
                except Exception:
                    pass

                self.pending_claims.append(parcel_info)
                return parcel_info

            if response.status_code == 409:
                # Locker is full; this is an expected domain response while load is running.
                response.success()
                return None

            if response.status_code == 404:
                # Under concurrent load some backends can report stale local setup state
                # for a recipient/locker. Treat the documented not-found response as
                # recoverable and remove that resource from this user's local pool.
                response_text = response.text.lower()
                if "recipient" in response_text or "user" in response_text:
                    self._remove_user_from_pool(recipient_username)
                    response.success()
                    return None
                if "locker" in response_text:
                    self._remove_locker_from_pool(locker_number)
                    response.success()
                    return None

            response.failure(f"Unexpected status adding parcel: {response.status_code} {response.text}")
            return None

    @task(1)
    def create_user_account(self):
        username = self._unique_username()
        self._create_user(username)

    @task(1)
    def register_locker(self):
        self._register_locker()

    @task(5)
    def add_parcel_to_locker(self):
        locker = self._ensure_locker()
        recipient_username = self._ensure_user()
        self._add_parcel(locker=locker, recipient_username=recipient_username)

    @task(4)
    def claim_parcel_from_locker(self):
        if not self.pending_claims:
            self._add_parcel()

        if not self.pending_claims:
            return

        claim = random.choice(self.pending_claims)
        locker_number = claim["lockerNumber"]
        pickup_pin = claim["pickupPin"]

        payload = {"pickupPin": pickup_pin}

        with self.client.post(
            f"/lockers/{locker_number}/claim",
            json=payload,
            name="POST /lockers/{lockerNumber}/claim",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                try:
                    self.pending_claims.remove(claim)
                except ValueError:
                    pass
                return

            if response.status_code in (404, 409):
                response.success()
                try:
                    self.pending_claims.remove(claim)
                except ValueError:
                    pass
                return

            response.failure(f"Unexpected status claiming parcel: {response.status_code} {response.text}")

    @task(6)
    def get_locker_occupancy(self):
        locker = self._ensure_locker()
        if locker is None:
            return

        locker_number = locker["lockerNumber"]

        with self.client.get(
            f"/lockers/{locker_number}/occupancy",
            name="GET /lockers/{lockerNumber}/occupancy",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return

            response.failure(f"Unexpected status getting occupancy: {response.status_code} {response.text}")

    @task(3)
    def get_user_pickup_history(self):
        username = self._ensure_user()
        if username is None:
            return

        with self.client.get(
            f"/users/{username}/pickup-history",
            name="GET /users/{username}/pickup-history",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return

            if response.status_code == 404:
                self._remove_user_from_pool(username)
                response.success()
                return

            response.failure(f"Unexpected status getting pickup history: {response.status_code} {response.text}")


class Shape(BaxbenchShape):
    pass
'''

SCENARIO = Scenario(
    id='''ParcelPinLockerPickup''',
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    short_app_description='''A web backend where users create accounts, register numbered parcel lockers with fixed capacities, add parcels to lockers with recipient usernames and pickup PINs, let recipients claim parcels by locker number and PIN, and query per-locker occupancy plus per-user pickup history.''',
    needs_db=True,
    needs_secret=False,
    scenario_instructions='''''',
    functional_tests=[func_test_create_user_and_register_locker, func_test_add_parcels_and_query_occupancy, func_test_claim_parcel_and_verify_pickup_history, func_test_pickup_history_for_user_with_no_pickups],
    security_tests=[],
    locustfile=_LOCUSTFILE.strip() or None,
)
