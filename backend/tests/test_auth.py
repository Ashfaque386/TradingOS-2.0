from httpx import AsyncClient


async def _register(
    client: AsyncClient,
    email: str = "first@example.com",
    password: str = "supersecret1",
):
    return await client.post("/api/v1/auth/register", json={"email": email, "password": password})


async def test_register_first_user_becomes_system_administrator(client: AsyncClient):
    resp = await _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "first@example.com"
    assert body["role"] == "SystemAdministrator"
    assert body["is_active"] is True
    assert "hashed_password" not in body


async def test_register_second_user_is_read_only_auditor(client: AsyncClient):
    await _register(client, email="first@example.com")
    resp = await _register(client, email="second@example.com")
    assert resp.status_code == 201
    assert resp.json()["role"] == "ReadOnlyAuditor"


async def test_register_duplicate_email_rejected(client: AsyncClient):
    await _register(client, email="dupe@example.com")
    resp = await _register(client, email="dupe@example.com")
    assert resp.status_code == 400


async def test_register_client_cannot_choose_role(client: AsyncClient):
    await _register(client, email="admin@example.com")
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": "supersecret1",
            "role": "SystemAdministrator",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "ReadOnlyAuditor"


async def test_login_success_returns_token_pair(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_wrong_password_rejected(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_login_unknown_email_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever1"}
    )
    assert resp.status_code == 401


async def test_me_requires_bearer_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code in (401, 403)


async def test_me_returns_current_user(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    access_token = login.json()["access_token"]

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "user@example.com"


async def test_refresh_rotates_tokens(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    refresh_token_1 = login.json()["refresh_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token_1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["refresh_token"] != refresh_token_1
    assert body["access_token"] != login.json()["access_token"]


async def test_refresh_reuse_detection_revokes_family(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    refresh_token_1 = login.json()["refresh_token"]

    first_rotation = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token_1}
    )
    assert first_rotation.status_code == 200
    refresh_token_2 = first_rotation.json()["refresh_token"]

    # Reusing the already-redeemed token 1 is a reuse attempt.
    reuse_attempt = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token_1}
    )
    assert reuse_attempt.status_code == 401

    # The entire family — including the legitimately-rotated token 2 — must
    # now be revoked as a consequence of the reuse attempt.
    second_rotation = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token_2}
    )
    assert second_rotation.status_code == 401


async def test_refresh_with_garbage_token_rejected(client: AsyncClient):
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401


async def test_refresh_with_access_token_rejected(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    access_token = login.json()["access_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient):
    await _register(client, email="user@example.com", password="correct-horse-1")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "correct-horse-1"}
    )
    refresh_token = login.json()["refresh_token"]

    logout_resp = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 204

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401
