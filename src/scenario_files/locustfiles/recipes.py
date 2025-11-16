# locustfile.py
import os
import random
import threading
from typing import Optional, List

from faker import Faker
from locust import FastHttpUser, LoadTestShape, task, between

# -----------------------------
# Config via environment vars
# -----------------------------
# Popularity: lower alpha (~1.0-1.5) => more skew; higher => flatter
SKEW_ALPHA = float(os.getenv("SKEW_ALPHA", "1.2"))

# Probability a user comments/ratings the most recently viewed recipe
P_INTERACT_LAST_VIEWED = float(os.getenv("P_INTERACT_LAST_VIEWED", "0.7"))

# Task weights (Locust @task weights)
W_BROWSE  = int(os.getenv("W_BROWSE",  "20"))
W_VIEW    = int(os.getenv("W_VIEW",    "50"))
W_COMMENT = int(os.getenv("W_COMMENT", "10"))
W_RATE    = int(os.getenv("W_RATE",    "15"))
W_UPLOAD  = int(os.getenv("W_UPLOAD",  "1"))

# If pool is empty during early ramp, optionally create instead of skipping
P_CREATE_WHEN_EMPTY = float(os.getenv("P_CREATE_WHEN_EMPTY", "1"))

fake = Faker()

class SteadyShape(LoadTestShape):
    user_count = 1800
    spawn_rate = 10
    time_limit = user_count // spawn_rate

    def tick(self):
        run_time = self.get_run_time()
        if run_time < self.time_limit:
            return self.user_count, self.spawn_rate
        return None

# -----------------------------------------
# Thread-safe pool with weighted sampling
# -----------------------------------------
class WeightedRecipePool:
    """
    Global store of recipe IDs with static 'popularity' weights.
    Sampling uses random.choices(ids, weights=weights, k=1).
    """
    def __init__(self):
        self._ids: List[str] = []
        self._weights: List[float] = []
        self._seen = set()
        self._lock = threading.RLock()

    def add(self, recipe_id: str, weight: Optional[float] = None) -> None:
        if not recipe_id:
            return
        if weight is None or weight <= 0:
            # Pareto(alpha): smaller alpha (~1.0) => heavier tail (more skew)
            weight = random.paretovariate(SKEW_ALPHA)
        with self._lock:
            if recipe_id in self._seen:
                return
            self._seen.add(recipe_id)
            self._ids.append(recipe_id)
            self._weights.append(max(weight, 1e-9))  # guard against zeros

    def size(self) -> int:
        with self._lock:
            return len(self._ids)

    def sample(self) -> Optional[str]:
        with self._lock:
            if not self._ids:
                return None
            # random.choices returns a list; we take one
            return random.choices(self._ids, weights=self._weights, k=1)[0]

recipe_pool = WeightedRecipePool()
# _seed_done = False
# _seed_lock = threading.Lock()

# -----------------------------------------
# Payload helpers
# -----------------------------------------
def make_recipe_payload():
    title = f"{fake.word().title()} {fake.word().title()}"
    ingredients = [fake.word().title() for _ in range(random.randint(3, 7))]
    instructions = fake.paragraph(nb_sentences=random.randint(5, 15))
    return {"title": title, "ingredients": ingredients, "instructions": instructions}

def make_comment_payload():
    return {"comment": fake.sentence(nb_words=random.randint(4, 14))}

def sample_rating() -> int:
    # Slightly optimistic distribution
    return random.choices([1, 2, 3, 4, 5], weights=[5, 10, 20, 30, 35], k=1)[0]

# -----------------------------------------
# Locust user behavior
# -----------------------------------------
class RecipeUser(FastHttpUser):
    """
    Users:
      - browse overview
      - view weighted-popular recipes
      - comment/rate (biased toward last viewed)
      - occasionally upload
    """
    wait_time = between(0.5, 3.0)

    def on_start(self):
        # global _seed_done
        # if not _seed_done:
        #     with _seed_lock:
        #         if not _seed_done:
        #             for _ in range(20):
        #                 self.create_recipe()
        #             _seed_done = True
        self.last_viewed_id: Optional[str] = None

    @task(W_BROWSE)
    def browse_overview(self):
        with self.client.get("/recipes", name="GET /recipes", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"Expected 200, got {r.status_code}")

    @task(W_VIEW)
    def view_recipe(self):
        rid = recipe_pool.sample()
        if rid is None:
            if random.random() < P_CREATE_WHEN_EMPTY:
                self.create_recipe()
            return
        with self.client.get(f"/recipes/{rid}", name="GET /recipes/{id}", catch_response=True) as r:
            if r.status_code == 200:
                self.last_viewed_id = rid
            elif r.status_code == 404:
                r.failure("Recipe not found (404)")
            else:
                r.failure(f"Expected 200/404, got {r.status_code}")

    @task(W_COMMENT)
    def comment_on_recipe(self):
        rid = self._pick_interaction_target()
        if rid is None:
            if random.random() < P_CREATE_WHEN_EMPTY:
                self.create_recipe()
            return
        payload = make_comment_payload()
        with self.client.post(
            f"/recipes/{rid}/comments",
            json=payload,
            name="POST /recipes/{id}/comments",
            catch_response=True,
        ) as r:
            if r.status_code != 201:
                r.failure(f"Comment expected 201, got {r.status_code}")

    @task(W_RATE)
    def rate_recipe(self):
        rid = self._pick_interaction_target()
        if rid is None:
            if random.random() < P_CREATE_WHEN_EMPTY:
                self.create_recipe()
            return
        payload = {"rating": sample_rating()}
        with self.client.post(
            f"/recipes/{rid}/ratings",
            json=payload,
            name="POST /recipes/{id}/ratings",
            catch_response=True,
        ) as r:
            if r.status_code != 201:
                r.failure(f"Rating expected 201, got {r.status_code}")

    @task(W_UPLOAD)
    def create_recipe(self):
        payload = make_recipe_payload()
        with self.client.post(
            "/recipes/upload",
            json=payload,
            name="POST /recipes/upload",
            catch_response=True,
        ) as r:
            if r.status_code == 201:
                # Expect JSON with 'id'
                try:
                    rid = r.json().get("id")
                except Exception:
                    rid = None
                if rid:
                    recipe_pool.add(rid)  # assign popularity weight on add
                else:
                    r.failure("201 without JSON id")
            elif r.status_code == 400:
                r.failure("Upload failed: 400 Invalid input")
            else:
                r.failure(f"Unexpected status: {r.status_code}")

    # ---------- internals ----------
    def _pick_interaction_target(self) -> Optional[str]:
        if self.last_viewed_id and random.random() < P_INTERACT_LAST_VIEWED:
            return self.last_viewed_id
        return recipe_pool.sample()
