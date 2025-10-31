import logging
import pathlib
from typing import TypedDict

import requests

from scenarios.base import AppInstance, Scenario

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
    """Test user CRUD and login operations"""
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
    security_tests=[],  # TODO
)
