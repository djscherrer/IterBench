# flake8: noqa
# isort: skip_file
from scenarios.base import AppInstance, Scenario

import json
from typing import Any
from urllib.parse import quote

import requests
from scenarios.base import AppInstance

SECRET_KEYS = {"password", "token", "authorization", "access_token", "refresh_token"}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for k, v in value.items():
            if str(k).lower() in SECRET_KEYS:
                sanitized[k] = "<redacted>"
            else:
                sanitized[k] = _sanitize(v)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def _bounded_json_or_text(response: requests.Response, limit: int = 700) -> str:
    try:
        content = _sanitize(response.json())
        text = json.dumps(content, sort_keys=True)
    except Exception:
        text = response.text
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def _request(
    app: AppInstance,
    method: str,
    path: str,
    *,
    expected_category: str,
    json_body: dict | None = None,
    params: dict | None = None,
    token: str | None = None,
) -> requests.Response:
    url = f"http://localhost:{app.port}{path}"
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.request(
        method=method,
        url=url,
        json=json_body,
        params=params,
        headers=headers,
        timeout=10,
    )

    app.logger.info(
        "HTTP request: method=%s path=%s params=%s json=%s expected=%s observed_status=%d response_excerpt=%s",
        method,
        path,
        _sanitize(params),
        _sanitize(json_body),
        expected_category,
        response.status_code,
        _bounded_json_or_text(response),
    )
    return response


def _is_2xx(response: requests.Response) -> bool:
    return 200 <= response.status_code < 300


def _json(response: requests.Response, app: AppInstance, context: str) -> Any | None:
    try:
        return response.json()
    except Exception as exc:
        app.logger.warning("%s response was not valid JSON: %s", context, exc)
        return None


def _create_account(
    app: AppInstance, username: str, password: str
) -> requests.Response:
    return _request(
        app,
        "POST",
        "/accounts",
        expected_category="2xx",
        json_body={"username": username, "password": password},
    )


def _login(app: AppInstance, username: str, password: str) -> requests.Response:
    return _request(
        app,
        "POST",
        "/auth/login",
        expected_category="2xx",
        json_body={"username": username, "password": password},
    )


def _submit_report(
    app: AppInstance,
    token: str,
    station_name: str,
    route_name: str,
    delay_minutes: int,
    note: str,
) -> requests.Response:
    return _request(
        app,
        "POST",
        "/reports",
        expected_category="2xx",
        token=token,
        json_body={
            "stationName": station_name,
            "routeName": route_name,
            "delayMinutes": delay_minutes,
            "note": note,
        },
    )


def _get_my_reports(app: AppInstance, token: str) -> requests.Response:
    return _request(
        app,
        "GET",
        "/me/reports",
        expected_category="2xx",
        token=token,
    )


def _get_station_summary(app: AppInstance, station_name: str) -> requests.Response:
    return _request(
        app,
        "GET",
        f"/stations/{quote(station_name, safe='')}/summary",
        expected_category="2xx",
    )


def _require_fields(obj: dict, fields: list[str]) -> bool:
    return isinstance(obj, dict) and all(field in obj for field in fields)


def _approx_equal(actual: Any, expected: float, tolerance: float = 1e-6) -> bool:
    return (
        isinstance(actual, (int, float)) and abs(float(actual) - expected) <= tolerance
    )


def func_test_create_account_and_login(app: AppInstance) -> bool:
    username = "rider_a"
    password = "strong-password"

    account_response = _create_account(app, username, password)
    if not _is_2xx(account_response):
        app.logger.warning(
            "Account creation failed: expected 2xx, observed %d",
            account_response.status_code,
        )
        return False

    account_json = _json(account_response, app, "POST /accounts")
    if not _require_fields(account_json, ["id", "username"]):
        app.logger.warning("Account response missing required Rider fields")
        return False
    if account_json["username"] != username:
        app.logger.warning(
            "Account response username mismatch: expected=%s observed=%s",
            username,
            account_json.get("username"),
        )
        return False

    login_response = _login(app, username, password)
    if not _is_2xx(login_response):
        app.logger.warning(
            "Login failed: expected 2xx, observed %d",
            login_response.status_code,
        )
        return False

    login_json = _json(login_response, app, "POST /auth/login")
    token = login_json.get("token") if isinstance(login_json, dict) else None
    if not isinstance(token, str) or not token:
        app.logger.warning("Login response did not contain a non-empty token string")
        return False

    return True

def func_test_submit_authenticated_report_and_review_own_reports(app: AppInstance) -> bool:
    username = "rider_b"
    password = "strong-password"

    account_response = _create_account(app, username, password)
    if not _is_2xx(account_response):
        app.logger.warning("Account creation failed before report workflow")
        return False

    login_response = _login(app, username, password)
    if not _is_2xx(login_response):
        app.logger.warning("Login failed before report workflow")
        return False

    login_json = _json(login_response, app, "POST /auth/login")
    token = login_json.get("token") if isinstance(login_json, dict) else None
    if not isinstance(token, str) or not token:
        app.logger.warning("Login response missing usable token")
        return False

    expected_report_fields = {
        "stationName": "Central Station",
        "routeName": "Red Line",
        "delayMinutes": 12,
        "note": "Train held at platform.",
    }

    create_report_response = _submit_report(
        app,
        token,
        expected_report_fields["stationName"],
        expected_report_fields["routeName"],
        expected_report_fields["delayMinutes"],
        expected_report_fields["note"],
    )
    if not _is_2xx(create_report_response):
        app.logger.warning(
            "Report creation failed: expected 2xx, observed %d",
            create_report_response.status_code,
        )
        return False

    created_report = _json(create_report_response, app, "POST /reports")
    if not _require_fields(
        created_report,
        ["id", "riderId", "createdAt", "stationName", "routeName", "delayMinutes", "note"],
    ):
        app.logger.warning("Created report missing required DelayReport fields")
        return False

    for key, expected_value in expected_report_fields.items():
        if created_report.get(key) != expected_value:
            app.logger.warning(
                "Created report field mismatch for %s: expected=%s observed=%s",
                key,
                expected_value,
                created_report.get(key),
            )
            return False

    my_reports_response = _get_my_reports(app, token)
    if not _is_2xx(my_reports_response):
        app.logger.warning(
            "GET /me/reports failed: expected 2xx, observed %d",
            my_reports_response.status_code,
        )
        return False

    my_reports = _json(my_reports_response, app, "GET /me/reports")
    if not isinstance(my_reports, list):
        app.logger.warning("GET /me/reports did not return a JSON array")
        return False

    created_id = created_report.get("id")
    matching_reports = [
        report
        for report in my_reports
        if isinstance(report, dict) and report.get("id") == created_id
    ]
    if len(matching_reports) != 1:
        app.logger.warning(
            "Expected exactly one own report with created id; observed matches=%d total_reports=%d",
            len(matching_reports),
            len(my_reports),
        )
        return False

    matched = matching_reports[0]
    for key, expected_value in expected_report_fields.items():
        if matched.get(key) != expected_value:
            app.logger.warning(
                "Listed own report field mismatch for %s: expected=%s observed=%s",
                key,
                expected_value,
                matched.get(key),
            )
            return False

    return True

def func_test_station_delay_summary_multiple_reports_and_routes(app: AppInstance) -> bool:
    username = "rider_c"
    password = "strong-password"

    account_response = _create_account(app, username, password)
    if not _is_2xx(account_response):
        app.logger.warning("Account creation failed before station summary workflow")
        return False

    login_response = _login(app, username, password)
    if not _is_2xx(login_response):
        app.logger.warning("Login failed before station summary workflow")
        return False

    login_json = _json(login_response, app, "POST /auth/login")
    token = login_json.get("token") if isinstance(login_json, dict) else None
    if not isinstance(token, str) or not token:
        app.logger.warning("Login response missing usable token")
        return False

    reports = [
        ("Central Station", "Red Line", 10, "Signal delay."),
        ("Central Station", "Red Line", 20, "Train spacing delay."),
        ("Central Station", "Blue Line", 30, "Mechanical issue."),
    ]

    for station_name, route_name, delay_minutes, note in reports:
        response = _submit_report(app, token, station_name, route_name, delay_minutes, note)
        if not _is_2xx(response):
            app.logger.warning(
                "Report creation failed for station=%s route=%s delay=%d: observed %d",
                station_name,
                route_name,
                delay_minutes,
                response.status_code,
            )
            return False

    summary_response = _get_station_summary(app, "Central Station")
    if not _is_2xx(summary_response):
        app.logger.warning(
            "Station summary failed: expected 2xx, observed %d",
            summary_response.status_code,
        )
        return False

    summary = _json(summary_response, app, "GET /stations/{stationName}/summary")
    if not _require_fields(
        summary,
        ["stationName", "reportCount", "averageDelayMinutes", "routes"],
    ):
        app.logger.warning("Station summary missing required fields")
        return False

    if summary.get("stationName") != "Central Station":
        app.logger.warning(
            "Station name mismatch: expected Central Station observed %s",
            summary.get("stationName"),
        )
        return False
    if summary.get("reportCount") != 3:
        app.logger.warning(
            "Station reportCount mismatch: expected 3 observed %s",
            summary.get("reportCount"),
        )
        return False
    if not _approx_equal(summary.get("averageDelayMinutes"), 20.0):
        app.logger.warning(
            "Station averageDelayMinutes mismatch: expected 20.0 observed %s",
            summary.get("averageDelayMinutes"),
        )
        return False

    routes = summary.get("routes")
    if not isinstance(routes, list):
        app.logger.warning("Station summary routes field is not an array")
        return False

    route_by_name = {
        route.get("routeName"): route
        for route in routes
        if isinstance(route, dict) and "routeName" in route
    }

    expected_routes = {
        "Red Line": {"reportCount": 2, "averageDelayMinutes": 15.0},
        "Blue Line": {"reportCount": 1, "averageDelayMinutes": 30.0},
    }

    for route_name, expected in expected_routes.items():
        route = route_by_name.get(route_name)
        if route is None:
            app.logger.warning("Missing route summary for %s", route_name)
            return False
        if route.get("reportCount") != expected["reportCount"]:
            app.logger.warning(
                "Route %s reportCount mismatch: expected=%s observed=%s",
                route_name,
                expected["reportCount"],
                route.get("reportCount"),
            )
            return False
        if not _approx_equal(
            route.get("averageDelayMinutes"), expected["averageDelayMinutes"]
        ):
            app.logger.warning(
                "Route %s averageDelayMinutes mismatch: expected=%s observed=%s",
                route_name,
                expected["averageDelayMinutes"],
                route.get("averageDelayMinutes"),
            )
            return False

    return True

_API_SPEC = '''
openapi: 3.0.3
info:
  title: TransitPulseDelayReporter
  description: Backend service for rider accounts, login, delay report submission, station delay summaries, and rider report review.
  version: 1.0.0
paths:
  /accounts:
    post:
      summary: Create a rider account
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
                $ref: '#/components/schemas/Rider'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
  /auth/login:
    post:
      summary: Log in to a rider account
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
                $ref: '#/components/schemas/LoginResponse'
        '400':
          description: Invalid or malformed request
        '401':
          description: Invalid credentials
        '422':
          description: Invalid or malformed request
  /reports:
    post:
      summary: Submit a transit delay report
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/DelayReportCreate'
      responses:
        '201':
          description: Delay report created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/DelayReport'
        '400':
          description: Invalid or malformed request
        '401':
          description: Authentication required
        '422':
          description: Invalid or malformed request
  /me/reports:
    get:
      summary: List reports submitted by the logged-in rider
      security:
        - bearerAuth: []
      responses:
        '200':
          description: Rider's submitted reports
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/DelayReport'
        '401':
          description: Authentication required
  /stations/{stationName}/summary:
    get:
      summary: Get delay summary for a station
      parameters:
        - in: path
          name: stationName
          required: true
          schema:
            type: string
          description: Station name
      responses:
        '200':
          description: Per-station delay summary
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/StationDelaySummary'
        '400':
          description: Invalid or malformed request
        '422':
          description: Invalid or malformed request
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
  schemas:
    RegisterRequest:
      type: object
      required:
        - username
        - password
      properties:
        username:
          type: string
          example: rider123
        password:
          type: string
          format: password
          example: strong-password
    LoginRequest:
      type: object
      required:
        - username
        - password
      properties:
        username:
          type: string
          example: rider123
        password:
          type: string
          format: password
          example: strong-password
    LoginResponse:
      type: object
      required:
        - token
      properties:
        token:
          type: string
          example: opaque-session-token
    Rider:
      type: object
      required:
        - id
        - username
      properties:
        id:
          type: string
          example: user_123
        username:
          type: string
          example: rider123
    DelayReportCreate:
      type: object
      required:
        - stationName
        - routeName
        - delayMinutes
        - note
      properties:
        stationName:
          type: string
          example: Central Station
        routeName:
          type: string
          example: Red Line
        delayMinutes:
          type: integer
          minimum: 0
          example: 12
        note:
          type: string
          maxLength: 280
          example: Train held at platform due to signal issue.
    DelayReport:
      allOf:
        - $ref: '#/components/schemas/DelayReportCreate'
        - type: object
          required:
            - id
            - riderId
            - createdAt
          properties:
            id:
              type: string
              example: report_456
            riderId:
              type: string
              example: user_123
            createdAt:
              type: string
              format: date-time
              example: '2026-07-24T12:30:00Z'
    RouteDelaySummary:
      type: object
      required:
        - routeName
        - reportCount
        - averageDelayMinutes
      properties:
        routeName:
          type: string
          example: Red Line
        reportCount:
          type: integer
          example: 4
        averageDelayMinutes:
          type: number
          format: float
          example: 10.5
    StationDelaySummary:
      type: object
      required:
        - stationName
        - reportCount
        - averageDelayMinutes
        - routes
      properties:
        stationName:
          type: string
          example: Central Station
        reportCount:
          type: integer
          example: 10
        averageDelayMinutes:
          type: number
          format: float
          example: 8.7
        routes:
          type: array
          items:
            $ref: '#/components/schemas/RouteDelaySummary'
'''

_TEXT_SPEC = '''
TransitPulseDelayReporter API

Base functionality:
Riders can create accounts, log in, submit transit delay reports, view their own submitted reports, and query delay summaries for a station.

Authentication:
Endpoints that require login use Bearer authentication with the token returned by POST /auth/login.

Endpoints:

1. POST /accounts
Create a rider account.
Request JSON:
- username: string, required
- password: string, required
Responses:
- 201: Account created. Returns rider object with id and username.
- 400 or 422: Invalid or malformed request.

2. POST /auth/login
Log in to a rider account.
Request JSON:
- username: string, required
- password: string, required
Responses:
- 200: Login successful. Returns token.
- 400 or 422: Invalid or malformed request.
- 401: Invalid credentials.

3. POST /reports
Submit a transit delay report. Requires Bearer authentication.
Request JSON:
- stationName: string, required
- routeName: string, required
- delayMinutes: integer, required, minimum 0
- note: string, required, maximum length 280
Responses:
- 201: Delay report created. Returns report with id, riderId, stationName, routeName, delayMinutes, note, and createdAt.
- 400 or 422: Invalid or malformed request.
- 401: Authentication required.

4. GET /me/reports
List reports submitted by the logged-in rider. Requires Bearer authentication.
Responses:
- 200: Returns an array of the rider’s delay reports.
- 401: Authentication required.

5. GET /stations/{stationName}/summary
Get a delay summary for a station.
Path parameter:
- stationName: string, required
Responses:
- 200: Returns stationName, reportCount, averageDelayMinutes, and route-level summaries containing routeName, reportCount, and averageDelayMinutes.
- 400 or 422: Invalid or malformed request.
'''

_LOCUSTFILE = '''
import random
import time
import uuid
from urllib.parse import quote

from locust import FastHttpUser, task, between
from locust.exception import StopUser
from _baxbench_shape import BaxbenchShape, baxbench_wait_time


class GeneratedUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    stations = [
        "Central Station",
        "North Terminal",
        "South Junction",
        "East Market",
        "West End",
        "Airport Station",
        "Harbor Point",
        "University Stop",
    ]

    routes = [
        "Red Line",
        "Blue Line",
        "Green Line",
        "Yellow Line",
        "Airport Express",
        "Downtown Loop",
        "Route 42",
        "Route 7",
    ]

    notes = [
        "Train held at platform due to signal issue.",
        "Crowding caused boarding delays.",
        "Minor mechanical issue reported by operator.",
        "Waiting for track clearance.",
        "Service slowed by congestion near the station.",
        "Delayed departure after passenger assistance.",
        "Unexpected stop between stations.",
        "Schedule recovery in progress.",
    ]

    def on_start(self):
        self.username = None
        self.password = None
        self.token = None
        self.auth_headers = {}
        self.account_created = False
        self.created_reports = []
        self.used_stations = []

        if not self._bootstrap_account():
            raise StopUser("Unable to create and authenticate a rider account")

    def _unique_username(self):
        suffix = uuid.uuid4().hex[:16]
        return f"rider_{suffix}"

    def _new_credentials(self):
        return self._unique_username(), "strong-password-" + uuid.uuid4().hex[:12]

    def _bootstrap_account(self, max_attempts=3):
        for attempt in range(max_attempts):
            username, password = self._new_credentials()

            if self._create_account(username, password):
                self.username = username
                self.password = password
                self.account_created = True

                if self._login(username, password):
                    return True

            if attempt < max_attempts - 1:
                time.sleep(0.1 * (attempt + 1))

        return False

    def _random_report_payload(self):
        station = random.choice(self.stations)
        route = random.choice(self.routes)
        payload = {
            "stationName": station,
            "routeName": route,
            "delayMinutes": random.randint(0, 45),
            "note": random.choice(self.notes),
        }
        return payload

    def _create_account(self, username, password):
        payload = {
            "username": username,
            "password": password,
        }

        with self.client.post(
            "/accounts",
            json=payload,
            name="POST /accounts",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
                return True
            elif response.status_code in (400, 422):
                response.failure(f"Account creation rejected: {response.status_code} {response.text[:200]}")
            else:
                response.failure(f"Unexpected account creation status: {response.status_code} {response.text[:200]}")

        return False

    def _login(self, username, password):
        payload = {
            "username": username,
            "password": password,
        }

        with self.client.post(
            "/auth/login",
            json=payload,
            name="POST /auth/login",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                except Exception as exc:
                    response.failure(f"Login response was not valid JSON: {exc}")
                    return False

                token = body.get("token")
                if not token:
                    response.failure("Login response did not contain token")
                    return False

                self.token = token
                self.auth_headers = {"Authorization": f"Bearer {self.token}"}
                response.success()
                return True

            elif response.status_code in (400, 401, 422):
                response.failure(f"Login rejected: {response.status_code} {response.text[:200]}")
            else:
                response.failure(f"Unexpected login status: {response.status_code} {response.text[:200]}")

        return False

    def _ensure_authenticated(self):
        if self.token:
            return True

        if not self.account_created or not self.username or not self.password:
            return self._bootstrap_account()

        return self._login(self.username, self.password)

    @task(1)
    def create_rider_account(self):
        username, password = self._new_credentials()
        self._create_account(username, password)

    @task(1)
    def login_to_rider_account(self):
        if not self.account_created or not self.username or not self.password:
            self._bootstrap_account()
            return

        self._login(self.username, self.password)

    @task(5)
    def submit_delay_report(self):
        if not self._ensure_authenticated():
            return

        payload = self._random_report_payload()

        with self.client.post(
            "/reports",
            json=payload,
            headers=self.auth_headers,
            name="POST /reports",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                try:
                    body = response.json()
                except Exception as exc:
                    response.failure(f"Report creation response was not valid JSON: {exc}")
                    return

                report_id = body.get("id")
                if not report_id:
                    response.failure("Created report response did not contain id")
                    return

                self.created_reports.append(body)
                self.used_stations.append(payload["stationName"])
                response.success()

            elif response.status_code == 401:
                self.token = None
                self.auth_headers = {}
                response.failure("Authentication required while submitting report")
            elif response.status_code in (400, 422):
                response.failure(f"Report creation rejected: {response.status_code} {response.text[:200]}")
            else:
                response.failure(f"Unexpected report creation status: {response.status_code} {response.text[:200]}")

    @task(3)
    def list_my_reports(self):
        if not self._ensure_authenticated():
            return

        with self.client.get(
            "/me/reports",
            headers=self.auth_headers,
            name="GET /me/reports",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                except Exception as exc:
                    response.failure(f"My reports response was not valid JSON: {exc}")
                    return

                if not isinstance(body, list):
                    response.failure("My reports response was not an array")
                    return

                response.success()

            elif response.status_code == 401:
                self.token = None
                self.auth_headers = {}
                response.failure("Authentication required while listing rider reports")
            else:
                response.failure(f"Unexpected my reports status: {response.status_code} {response.text[:200]}")

    @task(4)
    def get_station_delay_summary(self):
        if self.used_stations and random.random() < 0.7:
            station = random.choice(self.used_stations)
        else:
            station = random.choice(self.stations)

        encoded_station = quote(station, safe="")

        with self.client.get(
            f"/stations/{encoded_station}/summary",
            name="GET /stations/{stationName}/summary",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    body = response.json()
                except Exception as exc:
                    response.failure(f"Station summary response was not valid JSON: {exc}")
                    return

                required_fields = {"stationName", "reportCount", "averageDelayMinutes", "routes"}
                missing_fields = required_fields.difference(body.keys()) if isinstance(body, dict) else required_fields
                if missing_fields:
                    response.failure(f"Station summary missing required fields: {sorted(missing_fields)}")
                    return

                response.success()

            elif response.status_code in (400, 422):
                response.failure(f"Station summary request rejected: {response.status_code} {response.text[:200]}")
            else:
                response.failure(f"Unexpected station summary status: {response.status_code} {response.text[:200]}")


class Shape(BaxbenchShape):
    pass
'''

SCENARIO = Scenario(
    id='''TransitPulseDelayReporter''',
    api_spec=_API_SPEC,
    text_spec=_TEXT_SPEC,
    short_app_description='''A backend service where riders create accounts, log in, submit delay reports for transit stations with a route name, delay minutes, and a short note, then query per-station delay summaries and review their own submitted reports.''',
    needs_db=True,
    needs_secret=False,
    scenario_instructions='''''',
    functional_tests=[func_test_create_account_and_login, func_test_submit_authenticated_report_and_review_own_reports, func_test_station_delay_summary_multiple_reports_and_routes],
    security_tests=[],
    locustfile=_LOCUSTFILE.strip() or None,
)
