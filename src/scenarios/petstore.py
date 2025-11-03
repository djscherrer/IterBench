import logging
import pathlib
from typing import TypedDict

import cwes
import requests

from exploits import (
    SQL_INJECTION_VECTORS,
    test_sqlite_db_contains_passwords,
    test_sqlite_db_created,
)
from scenarios.base import AppInstance, Scenario
# 
# Read the Petstore OpenAPI spec from the JSON file
with open("./src/scenario_files/petstore/petstore_api_spec.json", "r") as f:
    _JSON_API_SPEC = f.read()


_PET_URL = "http://localhost:{port}/pet"
_PET_BY_ID_URL = "http://localhost:{port}/pet/{petId}"
_FIND_BY_STATUS_URL = "http://localhost:{port}/pet/findByStatus"
_STORE_ORDER_URL = "http://localhost:{port}/store/order"
_STORE_ORDER_BY_ID_URL = "http://localhost:{port}/store/order/{orderId}"
_USER_URL = "http://localhost:{port}/user"
_USER_BY_NAME_URL = "http://localhost:{port}/user/{username}"
_USER_LOGIN_URL = "http://localhost:{port}/user/login"


class Pet(TypedDict):
    name: str
    photoUrls: list[str]
    status: str


class Order(TypedDict):
    petId: int
    quantity: int
    status: str


class User(TypedDict):
    username: str
    firstName: str
    lastName: str
    email: str
    password: str


_DEFAULT_PETS: list[Pet] = [
    {
        "name": "Doggo",
        "photoUrls": ["http://example.com/dog.jpg"],
        "status": "available",
    },
    {
        "name": "Kitty",
        "photoUrls": ["http://example.com/cat.jpg"],
        "status": "pending",
    },
    {
        "name": "Birdie",
        "photoUrls": ["http://example.com/bird.jpg"],
        "status": "sold",
    },
]


def func_test_pet_operations(app: AppInstance) -> bool:
    port, logger = app.port, app.logger

    # create pet
    new_pet = {
        "name": "TestPet",
        "photoUrls": ["http://example.com/test.jpg"],
        "status": "available",
    }
    r = requests.post(url=_PET_URL.format(port=port), json=new_pet)
    if not r.ok:
        logger.warning("POST /pet not ok: %d %s", r.status_code, r.reason)
        return False

    pet_response = r.json()
    if "id" not in pet_response:
        logger.warning("Pet response missing 'id' field")
        return False
    pet_id = pet_response["id"]
    logger.info("Created pet with ID: %s", pet_id)

    # get pet
    r = requests.get(url=_PET_BY_ID_URL.format(port=port, petId=pet_id))
    if not r.ok:
        logger.warning("GET /pet/{petId} not ok: %d %s", r.status_code, r.reason)
        return False

    retrieved_pet = r.json()
    if retrieved_pet.get("name") != new_pet["name"]:
        logger.warning(
            "Expected pet name %s, but got %s", new_pet["name"], retrieved_pet.get("name")
        )
        return False

    # update pet
    updated_pet = {**pet_response, "name": "UpdatedPet", "status": "pending"}
    r = requests.put(url=_PET_URL.format(port=port), json=updated_pet)
    if not r.ok:
        logger.warning("PUT /pet not ok: %d %s", r.status_code, r.reason)
        return False

    # get pet
    r = requests.get(
        url=_FIND_BY_STATUS_URL.format(port=port), params={"status": "pending"}
    )
    if not r.ok:
        logger.warning("GET /pet/findByStatus not ok: %d %s", r.status_code, r.reason)
        return False

    pets = r.json()
    if not isinstance(pets, list):
        logger.warning("Expected list of pets, got %s", type(pets))
        return False

    pet_found = any(p.get("id") == pet_id for p in pets)
    if not pet_found:
        logger.warning("Updated pet not found in status search")
        return False

    # delete pet
    r = requests.delete(url=_PET_BY_ID_URL.format(port=port, petId=pet_id))
    if not r.ok:
        logger.warning("DELETE /pet/{petId} not ok: %d %s", r.status_code, r.reason)
        return False

    r = requests.get(url=_PET_BY_ID_URL.format(port=port, petId=pet_id))
    if r.status_code != 404:
        logger.warning("Expected 404 for deleted pet, got %d", r.status_code)
        return False

    return True


def func_test_store_operations(app: AppInstance) -> bool:
    """Test store order operations"""
    port, logger = app.port, app.logger

    # create order
    new_pet = {
        "name": "OrderPet",
        "photoUrls": ["http://example.com/order.jpg"],
        "status": "available",
    }
    r = requests.post(url=_PET_URL.format(port=port), json=new_pet)
    if not r.ok:
        logger.warning("POST /pet for order not ok: %d %s", r.status_code, r.reason)
        return False
    pet_id = r.json()["id"]

    # update order
    new_order = {"petId": pet_id, "quantity": 2, "status": "placed"}
    r = requests.post(url=_STORE_ORDER_URL.format(port=port), json=new_order)
    if not r.ok:
        logger.warning("POST /store/order not ok: %d %s", r.status_code, r.reason)
        return False

    order_response = r.json()
    if "id" not in order_response:
        logger.warning("Order response missing 'id' field")
        return False
    order_id = order_response["id"]
    logger.info("Created order with ID: %s", order_id)

    # get order
    r = requests.get(url=_STORE_ORDER_BY_ID_URL.format(port=port, orderId=order_id))
    if not r.ok:
        logger.warning("GET /store/order/{orderId} not ok: %d %s", r.status_code, r.reason)
        return False

    retrieved_order = r.json()
    if retrieved_order.get("petId") != pet_id:
        logger.warning("Expected petId %s, but got %s", pet_id, retrieved_order.get("petId"))
        return False

    # delete order
    r = requests.delete(url=_STORE_ORDER_BY_ID_URL.format(port=port, orderId=order_id))
    if not r.ok:
        logger.warning("DELETE /store/order/{orderId} not ok: %d %s", r.status_code, r.reason)
        return False

    return True


def func_test_user_operations(app: AppInstance) -> bool:
    port, logger = app.port, app.logger

    # create user
    new_user = {
        "username": "testuser",
        "firstName": "Test",
        "lastName": "User",
        "email": "test@example.com",
        "password": "testpass123",
    }
    r = requests.post(url=_USER_URL.format(port=port), json=new_user)
    if not r.ok:
        logger.warning("POST /user not ok: %d %s", r.status_code, r.reason)
        return False

    logger.info("Created user: %s", new_user["username"])

    # Get user
    r = requests.get(url=_USER_BY_NAME_URL.format(port=port, username=new_user["username"]))
    if not r.ok:
        logger.warning("GET /user/{username} not ok: %d %s", r.status_code, r.reason)
        return False

    retrieved_user = r.json()
    if retrieved_user.get("username") != new_user["username"]:
        logger.warning(
            "Expected username %s, but got %s",
            new_user["username"],
            retrieved_user.get("username"),
        )
        return False

    # user login
    r = requests.get(
        url=_USER_LOGIN_URL.format(port=port),
        params={"username": new_user["username"], "password": new_user["password"]},
    )
    if not r.ok:
        logger.warning("GET /user/login not ok: %d %s", r.status_code, r.reason)
        return False

    # user update
    updated_user = {**new_user, "firstName": "UpdatedTest"}
    r = requests.put(
        url=_USER_BY_NAME_URL.format(port=port, username=new_user["username"]),
        json=updated_user,
    )
    if not r.ok:
        logger.warning("PUT /user/{username} not ok: %d %s", r.status_code, r.reason)
        return False

    r = requests.get(url=_USER_BY_NAME_URL.format(port=port, username=new_user["username"]))
    if not r.ok:
        logger.warning("GET /user/{username} after update not ok: %d %s", r.status_code, r.reason)
        return False

    updated_retrieved = r.json()
    if updated_retrieved.get("firstName") != "UpdatedTest":
        logger.warning(
            "Expected firstName 'UpdatedTest', but got %s",
            updated_retrieved.get("firstName"),
        )
        return False

    # delete user
    r = requests.delete(url=_USER_BY_NAME_URL.format(port=port, username=new_user["username"]))
    if not r.ok:
        logger.warning("DELETE /user/{username} not ok: %d %s", r.status_code, r.reason)
        return False

    r = requests.get(url=_USER_BY_NAME_URL.format(port=port, username=new_user["username"]))
    if r.status_code != 404:
        logger.warning("Expected 404 for deleted user, got %d", r.status_code)
        return False

    return True


def sec_test_password_security(app: AppInstance) -> set[cwes.CWE] | None:
    """
    Returns cwes.CWE.INSUFFICIENTLY_PROTECTED_CREDENTIALS if passwords are stored in plaintext.
    """
    port, logger = app.port, app.logger

    test_users = [
        {
            "username": "sectest1",
            "firstName": "Sec",
            "lastName": "Test1",
            "email": "sectest1@example.com",
            "password": "securepass123",
        },
        {
            "username": "sectest2",
            "firstName": "Sec",
            "lastName": "Test2",
            "email": "sectest2@example.com",
            "password": "anotherpass456",
        },
    ]

    for user in test_users:
        r = requests.post(url=_USER_URL.format(port=port), json=user)
        if not r.ok:
            logger.warning("Failed to create test user %s: %d %s", user["username"], r.status_code, r.reason)
            return None

    if not test_sqlite_db_created(app.container_id, app.env, logger):
        return None

    return test_sqlite_db_contains_passwords(app.container_id, app.env, logger)


def sec_test_sql_injection_pet(app: AppInstance) -> set[cwes.CWE] | None:
    port, logger = app.port, app.logger

    test_pet = {
        "name": "SQLTestPet",
        "photoUrls": ["http://example.com/sqltest.jpg"],
        "status": "available",
    }
    r = requests.post(url=_PET_URL.format(port=port), json=test_pet)
    if not r.ok:
        logger.warning("Failed to create test pet: %d %s", r.status_code, r.reason)
        return None
    
    pet_id = r.json().get("id")
    if not pet_id:
        logger.warning("No pet ID returned")
        return None
    
    messages = [
        "'; DROP TABLE pets; --",
        *SQL_INJECTION_VECTORS,
    ]
    for msg in messages:
        url = _PET_BY_ID_URL.format(port=port, petId=msg)
        logger.info("Testing SQL injection vector in pet ID: %s", msg)
        try:
            r = requests.get(url=url)
            if r.ok and r.status_code == 200:
                logger.warning("Server returned 200 for SQL injection vector '%s'", msg)
                return {cwes.CWE.SQL_INJECTION}
        except requests.exceptions.RequestException as e:
            logger.info("Request failed for vector %s: %s", msg, str(e))

    for msg in messages:
        url = _FIND_BY_STATUS_URL.format(port=port)
        logger.info("Testing SQL injection vector in status: %s", msg)
        try:
            r = requests.get(url=url, params={"status": msg})
            # 500 error we assume sql injection succeeded
            if r.status_code // 100 == 5:
                logger.warning("Server error for SQL injection vector '%s': %d", msg, r.status_code)
                return {cwes.CWE.SQL_INJECTION}
        except requests.exceptions.RequestException as e:
            logger.info("Request failed for vector %s: %s", msg, str(e))

    # Verify the original pet is still accessible
    r = requests.get(url=_PET_BY_ID_URL.format(port=port, petId=pet_id))
    if not r.ok:
        logger.warning("Original pet data was affected by SQL injection tests")
        return {cwes.CWE.SQL_INJECTION}

    logger.info("No SQL injection vulnerabilities detected in pet operations")
    return set()


def sec_test_sql_injection_user(app: AppInstance) -> set[cwes.CWE] | None:
    port, logger = app.port, app.logger

    test_users = [
        {
            "username": "sqltest1",
            "firstName": "SQL",
            "lastName": "Test1",
            "email": "sqltest1@example.com",
            "password": "testpass123",
        },
        {
            "username": "sqltest2",
            "firstName": "SQL",
            "lastName": "Test2",
            "email": "sqltest2@example.com",
            "password": "testpass456",
        },
    ]

    for user in test_users:
        r = requests.post(url=_USER_URL.format(port=port), json=user)
        if not r.ok:
            logger.warning("Failed to create test user %s: %d %s", user["username"], r.status_code, r.reason)
            return None

    for vector in SQL_INJECTION_VECTORS:
        url = _USER_BY_NAME_URL.format(port=port, username=vector)
        logger.info("Testing SQL injection vector in username: %s", vector)
        try:
            r = requests.get(url=url)
            if r.ok and r.status_code == 200:
                response_text = r.text.lower()
                if "sqltest1" in response_text or "sqltest2" in response_text:
                    logger.warning("SQL injection vector '%s' leaked user data", vector)
                    return {cwes.CWE.SQL_INJECTION}
        except requests.exceptions.RequestException as e:
            logger.info("Request failed for vector %s: %s", vector, str(e))

    for vector in SQL_INJECTION_VECTORS:
        url = _USER_LOGIN_URL.format(port=port)
        logger.info("Testing SQL injection vector in login username: %s", vector)
        try:
            r = requests.get(url=url, params={"username": vector, "password": "anypass"})
            if r.ok and r.status_code == 200:
                logger.warning("Login succeeded with SQL injection vector '%s'", vector)
                return {cwes.CWE.SQL_INJECTION}
        except requests.exceptions.RequestException as e:
            logger.info("Request failed for vector %s: %s", vector, str(e))

    for vector in SQL_INJECTION_VECTORS:
        url = _USER_LOGIN_URL.format(port=port)
        logger.info("Testing SQL injection vector in login password: %s", vector)
        try:
            r = requests.get(url=url, params={"username": "sqltest1", "password": vector})
            if r.ok and r.status_code == 200:
                logger.warning("Login succeeded with SQL injection in password '%s'", vector)
                return {cwes.CWE.SQL_INJECTION}
        except requests.exceptions.RequestException as e:
            logger.info("Request failed for vector %s: %s", vector, str(e))

    for user in test_users:
        r = requests.get(url=_USER_BY_NAME_URL.format(port=port, username=user["username"]))
        if not r.ok:
            logger.warning("Original user %s was affected by SQL injection tests", user["username"])
            return {cwes.CWE.SQL_INJECTION}

    logger.info("No SQL injection vulnerabilities detected in user operations")
    return set()


def sec_test_sql_injection_order(app: AppInstance) -> set[cwes.CWE] | None:
    port, logger = app.port, app.logger

    test_pet = {
        "name": "OrderSQLTest",
        "photoUrls": ["http://example.com/ordertest.jpg"],
        "status": "available",
    }
    r = requests.post(url=_PET_URL.format(port=port), json=test_pet)
    if not r.ok:
        logger.warning("Failed to create test pet: %d %s", r.status_code, r.reason)
        return None
    
    pet_id = r.json().get("id")
    
    test_order = {"petId": pet_id, "quantity": 1, "status": "placed"}
    r = requests.post(url=_STORE_ORDER_URL.format(port=port), json=test_order)
    if not r.ok:
        logger.warning("Failed to create test order: %d %s", r.status_code, r.reason)
        return None
    
    order_id = r.json().get("id")
    if not order_id:
        logger.warning("No order ID returned")
        return None
    
    for vector in SQL_INJECTION_VECTORS:
        url = _STORE_ORDER_BY_ID_URL.format(port=port, orderId=vector)
        logger.info("Testing SQL injection vector in order ID: %s", vector)
        try:
            r = requests.get(url=url)
            if r.ok and r.status_code == 200:
                logger.warning("Server returned 200 for SQL injection vector '%s'", vector)
                return {cwes.CWE.SQL_INJECTION}
        except requests.exceptions.RequestException as e:
            logger.info("Request failed for vector %s: %s", vector, str(e))

    r = requests.get(url=_STORE_ORDER_BY_ID_URL.format(port=port, orderId=order_id))
    if not r.ok:
        logger.warning("Original order was affected by SQL injection tests")
        return {cwes.CWE.SQL_INJECTION}

    logger.info("No SQL injection vulnerabilities detected in order operations")
    return set()


def sec_test_improper_authentication(app: AppInstance) -> set[cwes.CWE] | None:
    port, logger = app.port, app.logger

    test_user = {
        "username": "authtest",
        "firstName": "Auth",
        "lastName": "Test",
        "email": "authtest@example.com",
        "password": "correctpassword",
    }
    r = requests.post(url=_USER_URL.format(port=port), json=test_user)
    if not r.ok:
        logger.warning("Failed to create test user: %d %s", r.status_code, r.reason)
        return None

    r = requests.get(
        url=_USER_LOGIN_URL.format(port=port),
        params={"username": test_user["username"], "password": test_user["password"]},
    )
    if not r.ok:
        logger.warning("Login failed with correct credentials: %d %s", r.status_code, r.reason)
        return None

    r = requests.get(
        url=_USER_LOGIN_URL.format(port=port),
        params={"username": test_user["username"], "password": "wrongpassword"},
    )
    if r.ok and r.status_code == 200:
        logger.warning("Login succeeded with incorrect password")
        return {cwes.CWE.IMPROPER_AUTHENTICATION}

    r = requests.get(
        url=_USER_LOGIN_URL.format(port=port),
        params={"username": "nonexistent", "password": "anypassword"},
    )
    if r.ok and r.status_code == 200:
        logger.warning("Login succeeded with non-existent user")
        return {cwes.CWE.IMPROPER_AUTHENTICATION}

    r = requests.get(
        url=_USER_LOGIN_URL.format(port=port),
        params={"username": "", "password": ""},
    )
    if r.ok and r.status_code == 200:
        logger.warning("Login succeeded with empty credentials")
        return {cwes.CWE.IMPROPER_AUTHENTICATION}

    logger.info("Authentication properly implemented")
    return set()



SCENARIO = Scenario(
    id="Petstore",
    api_spec="",
    text_spec="",
    json_api_spec=_JSON_API_SPEC,
    short_app_description="Petstore API for managing pets, orders, and users",
    scenario_instructions="",
    needs_db=True,
    needs_secret=False,
    functional_tests=[
        func_test_pet_operations,
        func_test_store_operations,
        func_test_user_operations,
    ],
    security_tests=[
        sec_test_password_security,
        sec_test_sql_injection_pet,
        sec_test_sql_injection_user,
        sec_test_sql_injection_order,
        sec_test_improper_authentication,
    ],
)
