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
    assert "WhatsApp Archive" in r.text


def test_search_endpoint(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"q": "pizza"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["results"]) == 2
    # Both pizza hits are images (Alice + Carol's captions) — they have
    # thumb_url. Text-only hits wouldn't.
    assert all(h.get("thumb_url") for h in data["results"])


def test_search_returns_text_messages(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"q": "sunscreen"})
    data = r.json()
    assert data["total"] == 1
    hit = data["results"][0]
    assert hit["type"] == "text"
    assert "thumb_url" not in hit
    assert hit["body"] == "anyone bringing sunscreen?"


def test_thumb_endpoint_generates(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"limit": 60, "type": "image"})
    pid = r.json()["results"][0]["id"]
    t = client.get(f"/api/thumb/{pid}", params={"size": 96})
    assert t.status_code == 200
    assert t.headers["content-type"] == "image/jpeg"
    assert len(t.content) > 0


def test_photo_endpoint_serves_original(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"limit": 60, "type": "image"})
    pid = r.json()["results"][0]["id"]
    p = client.get(f"/api/photo/{pid}")
    assert p.status_code == 200
    assert p.headers["content-type"].startswith("image/")


def test_photo_endpoint_404_for_text(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    app = create_app(db)
    client = TestClient(app)
    r = client.get("/api/search", params={"limit": 60, "type": "text"})
    tid = r.json()["results"][0]["id"]
    p = client.get(f"/api/photo/{tid}")
    assert p.status_code == 404


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


def test_static_files_require_auth_when_password_set(tmp_path: Path) -> None:
    """/static/* used to be mounted outside the dependency tree, so JS
    source (and the route map it encodes) was readable by anyone who
    could reach the server — even with a password set on every other
    route."""
    db = _ingested_db(tmp_path)
    conn = sqlite3.connect(db)
    auth_mod.set_password(conn, "secret")
    conn.close()

    client = TestClient(create_app(db))
    assert client.get("/static/chats.js").status_code == 401

    creds = base64.b64encode(b"x:secret").decode()
    r = client.get("/static/chats.js", headers={"Authorization": f"Basic {creds}"})
    assert r.status_code == 200
    assert "chat-row" in r.text


def test_static_files_reject_path_traversal(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path)
    client = TestClient(create_app(db))
    # Without traversal protection this would land on /src/.../auth.py.
    r = client.get("/static/../auth.py")
    assert r.status_code == 404


def test_thread_page_message_count_matches_api(tmp_path: Path) -> None:
    """The thread-page header showed COUNT(*) including NULL-sent_at
    rows, but /api/thread filters them out — leading to "8 messages"
    in the header while only 3 are ever rendered."""
    db = _ingested_db(tmp_path)
    # Inject NULL-sent_at rows into chat 1.
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO messages (chat_id, chat_jid, chat_name, is_group, "
        "sender_jid, sender_name, is_from_me, stanza_id, sent_at, type, body) "
        "VALUES (1, '111@s.whatsapp.net', 'Alice', 0, '111@s.whatsapp.net', "
        "'Alice', 0, ?, NULL, 'text', 'ghost')",
        [("ghost-1",), ("ghost-2",), ("ghost-3",)],
    )
    conn.commit(); conn.close()

    client = TestClient(create_app(db))
    page = client.get("/thread/1").text
    import re
    m = re.search(r"(\d+(?:,\d+)*)\s+messages", page)
    assert m, "header line not found"
    header_count = int(m.group(1).replace(",", ""))
    api_count = len(client.get("/api/thread/1?limit=500").json()["messages"])
    assert header_count == api_count, (header_count, api_count)
