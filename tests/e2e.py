"""End-to-end check for the whole stack, run against a live `docker compose up`.

Uses only the standard library, so it runs with the system Python without
installing anything. Every request goes through the gateway, the same path a
real client takes.

    python3 tests/e2e.py
"""

import json
import sys
import time
import urllib.error
import urllib.request

GATEWAY = "http://localhost:8000"


def call(method: str, path: str, body=None, token=None):
    """Send one request through the gateway and return (status, parsed body)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(GATEWAY + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def wait_for_stack(timeout: int = 120) -> None:
    """Block until the gateway answers and all four services are registered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, services = call("GET", "/registry")
            healthy = {s["service"] for s in services if s["status"] == "passing"}
            if status == 200 and healthy >= {
                "gateway",
                "user-service",
                "product-service",
                "order-service",
            }:
                return
        except Exception:  # noqa: BLE001 - stack still booting
            pass
        time.sleep(2)
    sys.exit("stack did not become ready in time; check `docker compose ps`")


def check(label: str, condition: bool, detail: str = "") -> None:
    """Assert one expectation, reporting which step failed."""
    if not condition:
        raise AssertionError(f"{label} FAILED {detail}")
    print(f"  ok  {label}")


def stock_of(product_id: int) -> int:
    """Read current stock straight from the catalog."""
    status, product = call("GET", f"/api/products/{product_id}")
    assert status == 200, product
    return product["stock"]


def main() -> None:
    print("waiting for the stack...")
    wait_for_stack()

    # Unique username so the script can be run repeatedly without cleanup.
    username = f"e2e_{int(time.time())}"

    print("\n[1] registration and login")
    status, user = call("POST", "/api/users", {
        "username": username, "email": f"{username}@its.ac.id", "password": "rahasia123",
    })
    check("register returns 201", status == 201, str(user))
    status, _ = call("POST", "/api/users", {
        "username": username, "email": "x@y.z", "password": "rahasia123",
    })
    check("duplicate username returns 409", status == 409)
    status, session = call("POST", "/api/auth/login", {
        "username": username, "password": "rahasia123",
    })
    check("login returns a token", status == 200 and "access_token" in session, str(session))
    token = session["access_token"]

    print("\n[2] catalog")
    status, cheap = call("POST", "/api/products", {"name": "E2E Mug", "price": 10000, "stock": 5})
    check("create product returns 201", status == 201, str(cheap))
    status, scarce = call("POST", "/api/products", {"name": "E2E Rare", "price": 25000, "stock": 1})
    check("create scarce product returns 201", status == 201, str(scarce))

    print("\n[3] successful order")
    status, order = call("POST", "/api/orders", {
        "items": [{"product_id": cheap["id"], "quantity": 2}],
    }, token=token)
    check("order returns 201", status == 201, str(order))
    check("total is computed from catalog price", order["total"] == 20000, str(order))
    check("stock dropped by the ordered amount", stock_of(cheap["id"]) == 3)
    status, fetched = call("GET", f"/api/orders/{order['id']}", token=token)
    check("order can be read back", status == 200 and fetched["total"] == 20000, str(fetched))

    print("\n[4] authentication is enforced at the gateway")
    status, _ = call("POST", "/api/orders", {"items": [{"product_id": cheap["id"], "quantity": 1}]})
    check("order without a token returns 401", status == 401)

    print("\n[5] one user cannot read another user's order")
    other = f"{username}_other"
    call("POST", "/api/users", {
        "username": other, "email": f"{other}@its.ac.id", "password": "rahasia123",
    })
    _, other_session = call("POST", "/api/auth/login", {
        "username": other, "password": "rahasia123",
    })
    status, _ = call(
        "GET", f"/api/orders/{order['id']}", token=other_session["access_token"]
    )
    check("another user's order is reported as missing", status == 404)

    print("\n[6] failed order compensates reserved stock")
    before_cheap, before_scarce = stock_of(cheap["id"]), stock_of(scarce["id"])
    status, failed = call("POST", "/api/orders", {
        "items": [
            {"product_id": cheap["id"], "quantity": 1},
            {"product_id": scarce["id"], "quantity": 99},
        ],
    }, token=token)
    check("order with insufficient stock returns 409", status == 409, str(failed))
    check(
        "stock of the first item was released again",
        stock_of(cheap["id"]) == before_cheap,
        f"expected {before_cheap}, got {stock_of(cheap['id'])}",
    )
    check("scarce item stock untouched", stock_of(scarce["id"]) == before_scarce)

    print("\n[7] routing")
    status, _ = call("GET", "/api/invoices")
    check("unknown prefix returns 404", status == 404)
    status, _ = call("GET", "/api/users/999999")
    check("missing user returns 404 from the owning service", status == 404)

    print("\nall checks passed")


if __name__ == "__main__":
    main()
