# locustfile.py
import random
from datetime import date

from locust import FastHttpUser, task, between, events


class ClickCountUser(FastHttpUser):
    wait_time = between(0.5, 0.8)

    @task(1)
    def click(self):
        self.client.post("/click", name="POST /click")

    @task(100)
    def get_clicks(self):
        day = "2000-01-01"
        direction = ">="

        self.client.get(f"/clicks", params={"date": day, "direction": direction}, name="GET /clicks")