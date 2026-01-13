# locustfile.py
from locust import FastHttpUser, task, between, events
import requests
from locust.runners import MasterRunner, LocalRunner

# prime database with a single click to get answers earlier
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    if isinstance(environment.runner, (MasterRunner, LocalRunner)):
        requests.post(f"{environment.host}/click", name="POST /click")


class ClickCountUser(FastHttpUser):
    wait_time = between(0.5, 1.5)

    @task(1)
    def click(self):
        self.client.post("/click", name="POST /click")

    @task(100)
    def get_clicks(self):
        day = "2000-01-01"
        direction = ">="

        self.client.get(f"/clicks", params={"date": day, "direction": direction}, name="GET /clicks")