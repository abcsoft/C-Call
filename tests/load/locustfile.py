# tests/load/locustfile.py
"""Locust load test script.

Simulates merchant order submission (API), customer order status checking (API),
and dashboard wallet balance checks (Web panel).

Run instructions:
    locust -f tests/load/locustfile.py --headless -u 1000 -r 50 --run-time 60s
"""
from __future__ import annotations

import os
import random
from locust import HttpUser, task, between

# Read API Key and Session credentials from environment or use test defaults
API_KEY = os.environ.get("LOCUST_API_KEY", "ccall_api_key_placeholder")
SESSION_COOKIE = os.environ.get("LOCUST_SESSION_COOKIE", "ccall_session_cookie_placeholder")


class OrderSubmitUser(HttpUser):
    # Wait between 1 and 3 seconds between tasks
    wait_time = between(1, 3)

    @task(10)
    def submit_order(self) -> None:
        """Task: POST order creation to the public REST API (weighted: 10)."""
        payload = {
            "order_id": f"ORD-{random.randint(1, 99999)}",
            "phone": f"017{random.randint(10000000, 99999999)}",
            "customer_name": f"Locust User {random.randint(1, 100)}",
            "amount": f"{random.randint(100, 10000)}.00",
            "items": f"{random.randint(1, 3)}x Panjabi",
            "store_name": "Locust Load Test Store",
            "webhook_url": "https://mystore.com/webhook/ccall"
        }
        self.client.post(
            "/api/v1/orders",
            headers={"X-API-Key": API_KEY},
            json=payload,
            name="/api/v1/orders (Create)"
        )

    @task(3)
    def check_order_status(self) -> None:
        """Task: GET order details from public REST API (weighted: 3)."""
        random_order_id = f"ORD-{random.randint(1, 1000)}"
        self.client.get(
            f"/api/v1/orders/{random_order_id}",
            headers={"X-API-Key": API_KEY},
            name="/api/v1/orders/:id (Status)"
        )

    @task(1)
    def get_wallet_balance(self) -> None:
        """Task: GET wallet balance page / transactions JSON (weighted: 1)."""
        headers = {}
        if SESSION_COOKIE:
            headers["Cookie"] = f"session={SESSION_COOKIE}"
            
        # Requests both the HTML wallet dashboard and the transactions JSON endpoint
        self.client.get(
            "/wallet",
            headers=headers,
            name="/wallet (Web Page)"
        )
        self.client.get(
            "/api/wallet/transactions",
            headers=headers,
            name="/api/wallet/transactions (JSON)"
        )
