# locustfile.py
import random
from datetime import date

from locust import FastHttpUser, task, between, events


class ClickCountUser(FastHttpUser):
    wait_time = between(0.5, 0.8)

    @task
    def click(self):
        self.client.post("/click", name="POST /click")

    @task
    def get_clicks(self):
        ops = ["<", ">", "<=", ">="]

        today = date.today().strftime("%Y-%m-%d")
        direction = ops[random.randint(0,len(ops)-1)]

        self.client.get(f"/clicks?date={today}&direction={direction}", name="GET /clicks")