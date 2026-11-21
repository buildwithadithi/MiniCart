from uuid import uuid4

from generated import product_pb2


def _register_and_login(client, credentials, security_records):
    registration = client.post("/auth/register", json=credentials)
    assert registration.status_code == 201, registration.text
    user_id = registration.json()["id"]
    security_records["user_ids"].add(user_id)

    login = client.post(
        "/auth/login",
        json={
            "email": credentials["email"],
            "password": credentials["password"],
        },
    )
    assert login.status_code == 200, login.text
    return user_id, login.json()["access_token"]


def test_gateway_authentication_and_order_ownership(
    security_client,
    product_stub,
    user_credentials,
    security_records,
):
    owner_id, owner_token = _register_and_login(
        security_client,
        user_credentials,
        security_records,
    )
    other_credentials = {
        "name": "Second Security Test User",
        "email": f"security-other-{uuid4().hex}@example.com",
        "password": f"Security-password-{uuid4().hex}",
    }
    other_id, other_token = _register_and_login(
        security_client,
        other_credentials,
        security_records,
    )

    product = product_stub.AddProduct(
        product_pb2.AddProductRequest(
            name=f"Ownership test {uuid4().hex}",
            description="Created by the MiniCart ownership integration test.",
            price="7.25",
            initial_stock=5,
        ),
        timeout=5,
    ).product
    security_records["product_id"] = product.id

    create_response = security_client.post(
        "/orders",
        headers={
            "Authorization": f"Bearer {owner_token}",
            "x-user-id": str(other_id),
        },
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert create_response.status_code == 201, create_response.text
    order_id = create_response.json()["id"]
    security_records["order_id"] = order_id

    own_order = security_client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert own_order.status_code == 200, own_order.text
    assert own_order.json()["id"] == order_id

    other_users_order = security_client.get(
        f"/orders/{order_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert other_users_order.status_code == 404
    assert str(order_id) not in other_users_order.text

    spoofed_owner = security_client.post(
        "/orders",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "user_id": other_id,
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert spoofed_owner.status_code == 422

    from sqlalchemy import text
    from database import SessionLocal

    with SessionLocal() as session:
        persisted_owner_id = session.execute(
            text("SELECT user_id FROM orders WHERE id = :order_id"),
            {"order_id": order_id},
        ).scalar_one()

    assert persisted_owner_id == owner_id
