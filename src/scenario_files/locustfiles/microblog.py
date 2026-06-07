import os
import random
import threading
from typing import Optional, List, Dict

from faker import Faker
from locust import FastHttpUser, task

from _baxbench_shape import BaxbenchShape, baxbench_wait_time

SKEW_ALPHA = float(os.getenv("SKEW_ALPHA", "1.2"))
P_CREATE_WHEN_EMPTY = 1.0
P_SECOND_PAGE = 0.1

# Task weights
W_REGISTER = 1
W_POST     = 5
W_FOLLOW   = 5
W_FEED     = 50
W_LIKE     = 30
W_TRENDING = 10

fake = Faker()

class GlobalPool:
    def __init__(self):
        self.usernames: List[str] = []
        self.post_ids: List[int] = []
        self.user_weights: List[float] = []
        self.post_weights: List[float] = []
        self._lock = threading.RLock()

    def add_user(self, username: str):
        with self._lock:
            if username not in self.usernames:
                self.usernames.append(username)
                self.user_weights.append(random.paretovariate(SKEW_ALPHA))

    def add_post(self, post_id: int):
        with self._lock:
            if post_id not in self.post_ids:
                self.post_ids.append(post_id)
                self.post_weights.append(random.paretovariate(SKEW_ALPHA))

    def sample_user(self) -> Optional[str]:
        with self._lock:
            if not self.usernames: return None
            return random.choices(self.usernames, weights=self.user_weights, k=1)[0]

    def sample_post(self) -> Optional[int]:
        with self._lock:
            if not self.post_ids: return None
            return random.choices(self.post_ids, weights=self.post_weights, k=1)[0]

pool = GlobalPool()

class MicroBlogUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    def on_start(self):
        self.username = f"user_{random.randint(1, 1000000)}_{threading.get_ident()}"
        self.registered = False

    def ensure_registered(self):
        if not self.registered:
            payload = {
                "username": self.username,
                "full_name": fake.name(),
                "bio": fake.text(max_nb_chars=100)
            }
            with self.client.post("/users", json=payload, name="POST /users", catch_response=True) as r:
                if r.status_code in [201, 400]: # 400 might mean already exists
                    self.registered = True
                    pool.add_user(self.username)
                    r.success()

    @task(W_REGISTER)
    def register_other(self):
        uname = f"user_{random.randint(1, 1000000)}"
        payload = {"username": uname, "full_name": fake.name(), "bio": fake.text()}
        with self.client.post("/users", json=payload, name="POST /users", catch_response=True) as r:
            if r.status_code == 201:
                pool.add_user(uname)
                r.success()
            elif r.status_code == 400:
                r.success()

    @task(W_POST)
    def create_post(self):
        self.ensure_registered()
        payload = {"username": self.username, "content": fake.text(max_nb_chars=200)}
        with self.client.post("/posts", json=payload, name="POST /posts") as r:
            if r.status_code == 201:
                try:
                    pid = r.json().get("id")
                    if pid: pool.add_post(pid)
                except: pass

    @task(W_FOLLOW)
    def follow_user(self):
        self.ensure_registered()
        target = pool.sample_user()
        if target and target != self.username:
            payload = {"follower_username": self.username, "following_username": target}
            self.client.post("/follow", json=payload, name="POST /follow")

    @task(W_FEED)
    def view_feed(self):
        self.ensure_registered()
        page = 2 if random.random() < P_SECOND_PAGE else 1
        url = f"/feed?username={self.username}" if page == 1 else f"/feed?username={self.username}&page={page}"
        with self.client.get(url, name="GET /feed") as r:
            if r.status_code == 200:
                try:
                    posts = r.json().get("items", [])
                    for p in posts[:5]: # Sample some posts from feed to like later
                        pool.add_post(p["id"])
                except: pass

    @task(W_LIKE)
    def like_post(self):
        self.ensure_registered()
        pid = pool.sample_post()
        if pid:
            payload = {"username": self.username}
            self.client.post(f"/posts/{pid}/like", json=payload, name="POST /posts/{id}/like")

    @task(W_TRENDING)
    def view_trending(self):
        page = 2 if random.random() < P_SECOND_PAGE else 1
        url = "/trending" if page == 1 else f"/trending?page={page}"
        self.client.get(url, name="GET /trending")


class Shape(BaxbenchShape):
    pass
