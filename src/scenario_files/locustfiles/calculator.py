# locustfile.py
import random

from locust import FastHttpUser, task, events

from _baxbench_shape import BaxbenchShape, baxbench_wait_time


class CalculatorUser(FastHttpUser):
    wait_time = baxbench_wait_time()

    @task
    def calculate(self):
        task = [
            "1 + 2*3",
            "10 - 15",
            "1 * 1",
            "10 / 2",
            "1203 - 21 * 2"
        ]

        self.client.post("/calculator", name="POST /calculator", json={"expression": task[random.randint(0,len(task)-1)]})


class Shape(BaxbenchShape):
    pass