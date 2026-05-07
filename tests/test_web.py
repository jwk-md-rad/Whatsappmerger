from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from whatsapp_photos import auth as auth_mod
from whatsapp_photos.ingest import ingest
from whatsapp_photos.web import create_app

from .fixtures import build_fixture


def _ingested_db(tmp_path: Path) -> Path:
    chat_db, media_root = build_fixture(tmp_path)
    out = tmp_path / "photos.db"
    ingest(chat_db, media_root, out)
    return out


def test_index_renders(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "WhatsApp Photo Archive" in r.text


def test_search_endpoint(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"q": "pizza"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["results"]) == 2
    assert all("thumb_url" in h for h in data["results"])


def test_thumb_endpoint_generates(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    # Get any photo id
    r = client.get("/api/search", params={"limit": 1})
    pid = r.json()["results"][0]["id"]
    t = client.get(f"/api/thumb/{pid}", params={"size": 96})
    assert t.status_code == 200
    assert t.headers["content-type"] == "image/jpeg"
    assert len(t.content) > 0


def test_photo_endpoint_serves_original(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"limit": 1})
    pid = r.json()["results"][0]["id"]
    p = client.get(f"/api/photo/{pid}")
    assert p.status_code == 200
    assert p.headers["content-type"].startswith("image/")


def test_chats_and_senders_endpoints(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    chats = client.get("/api/chats").json()
    assert any(c["jid"] == "333-group@g.us" for c in chats)
    senders = client.get("/api/senders").json()
    assert any(s["sender_name"] == "Carol" for s in senders)


def test_auth_required_when_password_set(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        auth_mod.set_password(conn, "secret123")
    finally:
        conn.close()

    app = create_app(db)
    client = TestClient(app)

    # Without credentials: 401
    r = client.get("/api/search")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")

    # Wrong password: 401
    bad = base64.b64encode(b"user:wrongpw").decode()
    r = client.get("/api/search", headers={"Authorization": f"Basic {bad}"})
    assert r.status_code == 401

    # Correct password: 200 (username is ignored)
    good = base64.b64encode(b"user:secret123").decode()
    r = client.get("/api/search", headers={"Authorization": f"Basic {good}"})
    assert r.status_code == 200


def test_auth_off_when_no_password(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search")
    assert r.status_code == 200
