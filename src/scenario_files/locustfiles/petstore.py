# locustfile.py
import random
import threading
from enum import Enum
from typing import Optional, List, Dict

from faker import Faker
from locust import FastHttpUser, task, between, events


# -----------------------------
# Performance Tests
# -----------------------------
fake = Faker()


class Sendable:
    def __init__(self):
        pass

    def as_payload(self) -> dict:
        pass

    def random_update(self):
        pass


class Pet(Sendable):
    def __init__(self, id: int, data: dict = None):
        super().__init__()
        self.id: int = id
        if data is not None:
            self.name = data.get("name")
            self.photo_urls = data.get("photoUrl", [])
            self.status = data.get("status")
        else:
            self.random_update()

    def random_update(self):
        self.name: str = fake.name()
        self.photo_urls: List = []
        self.status: str = random.choice(["available", "pending", "sold"])

    def as_payload(self) -> dict:
        return {"id": self.id, "name": self.name, "photoUrls": self.photo_urls, "status": self.status}


class Order(Sendable):
    def __init__(self, id: int, data: dict = None):
        super().__init__()
        self.id = id
        if data is not None:
            self.pet_id = data.get("petId")
            self.quantity = data.get("quantity")
        else:
            self.random_update()

    def random_update(self):
        self.pet_id = pet_store.sample_id(PetStoreData.Types.PET)
        self.quantity = random.randint(1, 10)

    def as_payload(self) -> dict:
        return {"id": self.id, "petId": self.pet_id, "quantity": self.quantity}


class PetStoreData:
    class Types(Enum):
        PET = 1
        ORDER = 2
        USER = 3

    def __init__(self):
        self.data: Dict[int, Optional[Sendable]] = {}
        self.lock: threading.RLock = threading.RLock()
        self.next_id: int = 1


class PetStore:
    """
    Global store of everything that should be stored in the petstore app.
    """

    def __init__(self):
        self._table: Dict[PetStoreData.Types, PetStoreData] = {
            PetStoreData.Types.PET: PetStoreData(),
            PetStoreData.Types.ORDER: PetStoreData(),
            PetStoreData.Types.USER: PetStoreData(),
        }

    def reserve(self, t: PetStoreData.Types) -> int:
        with self._table[t].lock:
            id = self._table[t].next_id
            self._table[t].next_id += 1
            return id

    def release(self, t: PetStoreData.Types, v: int):
        with self._table[t].lock:
            if v in self._table[t].data:
                del self._table[t].data[v]

    def add(self, t: PetStoreData.Types, v: int, data: Sendable):
        with self._table[t].lock:
            self._table[t].data[v] = data

    def sample_id(self, t: PetStoreData.Types):
        with self._table[t].lock:
            active_ids = [k for k, v in self._table[t].data.items() if v is not None]
            if len(active_ids) == 0:
                return None

            return random.choice(active_ids)

    def sample(self, t: PetStoreData.Types) -> Optional[Sendable]:
        with self._table[t].lock:
            id = self.sample_id(t)
            if id is None:
                return None
            return self._table[t].data[id]


pet_store = PetStore()

# -----------------------------------------
# Environment setup
# -----------------------------------------
HEADER = {"api_key": "special-key", "Authorization": "Bearer 4b6fe5aa-e6e3-4f83-9187-28239c67faf6"}
import requests
from locust.runners import MasterRunner, LocalRunner


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    if isinstance(environment.runner, (MasterRunner, LocalRunner)):
        for i in range(100):
            id = pet_store.reserve(PetStoreData.Types.PET)
            pet = Pet(id)

            try:
                requests.post(f"{environment.host}/pet", name="POST /pet", json=pet.as_payload())
                pet_store.add(PetStoreData.Types.PET, id, pet)
            except Exception as e:
                pet_store.release(PetStoreData.Types.PET, id)


# -----------------------------------------
# Locust request functions
# -----------------------------------------

# -------------
# pet endpoints
# -------------
def get_pet(locust):
    id = pet_store.sample_id(PetStoreData.Types.PET)
    if id is None:
        return

    locust.client.get(f"/pet/{id}", name="GET /pet/{id}", headers=HEADER)


def get_by_status(locust):
    status = random.choice(["available", "pending", "sold"])
    locust.client.get(f"/pet/findByStatus?status={status}", name="GET /pet/findByStatus", headers=HEADER)


def create_pet(locust):
    id = pet_store.reserve(PetStoreData.Types.PET)
    pet = Pet(id)

    with locust.client.post("/pet", name="POST /pet", json=pet.as_payload(), headers=HEADER, catch_response=True) as r:
        if r.status_code == 200:
            r.success()
            pet_store.add(PetStoreData.Types.PET, id, pet)
        else:
            r.failure(f"Bad status code: {r.status_code}")
            pet_store.release(PetStoreData.Types.PET, id)


def update_pet(locust):
    pet = pet_store.sample(PetStoreData.Types.PET)
    if pet is not None:
        pet.random_update()

        locust.client.put("/pet", name="PUT /pet", json=pet.as_payload(), headers=HEADER)


# ---------------
# store endpoints
# ---------------
def browse_inventory(locust):
    locust.client.get(f"/store/inventory", name="GET /store/inventory", headers=HEADER)


def read_order(locust):
    id = pet_store.sample_id(PetStoreData.Types.ORDER)
    if id is None:
        return

    locust.client.get(f"/store/order/{id}", name="GET /store/order/{id}", headers=HEADER)


def create_order(locust):
    id = pet_store.reserve(PetStoreData.Types.ORDER)
    order = Order(id)
    with locust.client.post("/store/order", name="POST /store/order", json=order.as_payload(), headers=HEADER,
                            catch_response=True) as r:
        if r.status_code == 200:
            r.success()
            pet_store.add(PetStoreData.Types.ORDER, id, order)
        else:
            r.failure(f"Bad status code: {r.status_code}")
            pet_store.release(PetStoreData.Types.ORDER, id)


def delete_order(locust):
    id = pet_store.sample_id(PetStoreData.Types.ORDER)
    if id is None:
        return

    with locust.client.delete(f"/store/order/{id}", name="DELETE /store/order/{id}", headers=HEADER,
                              catch_response=True) as r:
        if r.status_code == 200:
            pet_store.release(PetStoreData.Types.ORDER, id)


# -----------------------------------------
# Locust user behavior
# -----------------------------------------
class MixedPetstoreUser(FastHttpUser):
    wait_time = between(0.5, 0.8)

    @task
    def get_pet(self):
        get_pet(self)

    @task
    def get_by_status(self):
        get_by_status(self)

    @task
    def create_pet(self):
        create_pet(self)

    @task
    def update_pet(self):
        update_pet(self)

    @task
    def browse_inventory(self):
        browse_inventory(self)

    @task
    def read_order(self):
        read_order(self)

    @task
    def create_order(self):
        create_order(self)

    @task
    def delete_order(self):
        delete_order(self)


class WritePetstoreUser(FastHttpUser):
    @task(1)
    def get_pet(self):
        get_pet(self)

    @task(10)
    def create_pet(self):
        create_pet(self)

    @task(10)
    def update_pet(self):
        update_pet(self)

    @task(1)
    def read_order(self):
        read_order(self)

    @task(10)
    def create_order(self):
        create_order(self)

    @task(5)
    def delete_order(self):
        delete_order(self)


class ReadPetstoreUser(FastHttpUser):
    @task
    def get_pet(self):
        get_pet(self)

    @task
    def get_by_status(self):
        get_by_status(self)

    @task
    def update_pet(self):
        update_pet(self)

    @task
    def browse_inventory(self):
        browse_inventory(self)

    @task
    def read_order(self):
        read_order(self)

