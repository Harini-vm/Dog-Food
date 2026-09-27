"""T3 comments and anti-abuse: empty and oversized text, Unicode, hostile markup, rate limits."""

import pytest

from conftest import ORGANIZER
from helpers import client, event_with_entries, person


@pytest.fixture(scope="module")
def ev(server):
    return event_with_entries(1)


@pytest.fixture(autouse=True)
def _fresh_limits():
    from dogfood import ratelimit
    ratelimit.reset()


def _post(tok, pid, body):
    from dogfood import ratelimit
    ratelimit.reset()
    return client(tok).post(f"/projects/{pid}/comments", json={"body": body})


@pytest.mark.parametrize("body", ["", "   ", "\t\n", "  ", "​​", "﻿", " ‍ ⁠ "])
def test_empty_looking_comments_are_refused(ev, body):
    tok = person(f"c.{ev['tag']}.{abs(hash(body))}@example.org")
    r = _post(tok, ev["teams"][0]["project"], body)
    assert r.status_code == 400 and r.json()["error"] == "empty_comment"


def test_length_limit_counts_characters(ev):
    tok = person(f"len.{ev['tag']}@example.org")
    pid = ev["teams"][0]["project"]
    assert _post(tok, pid, "a" * 1999).status_code == 201
    assert _post(tok, pid, "அ" * 2000).status_code == 201                          # 2000 Tamil letters: allowed
    assert _post(tok, pid, "😀" * 2001).json()["error"] == "comment_too_long"


def test_unicode_is_stored_exactly_and_markup_is_inert(ev):
    tok = person(f"u.{ev['tag']}@example.org")
    pid = ev["teams"][0]["project"]
    text = "நன்று 👍🏽 很好 é <script>alert(1)</script> <img src=x onerror=alert(1)> {{ 7*7 }}"
    assert _post(tok, pid, text).status_code == 201
    got = client().get(f"/api/v1/projects/{pid}/comments").json()["comments"][-1]["body"]
    import unicodedata
    assert got == unicodedata.normalize("NFC", text)
    html = client().get(f"/projects/{pid}").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html and "{{ 7*7 }}" in html                     # template syntax is never evaluated


def test_moderation(ev):
    author = person(f"m.{ev['tag']}@example.org")
    stranger = person(f"s.{ev['tag']}@example.org")
    pid = ev["teams"][0]["project"]
    cid = _post(author, pid, "hello").json()["id"]
    assert client(stranger).post(f"/comments/{cid}/hide", json={}).status_code == 403
    assert client(ORGANIZER).post(f"/comments/{cid}/hide", json={}).status_code == 400          # reason needed
    assert client(ORGANIZER).post(f"/comments/{cid}/hide", json={"reason": "spam"}).status_code == 200
    ids = [c["id"] for c in client().get(f"/api/v1/projects/{pid}/comments").json()["comments"]]
    assert cid not in ids
    assert client(ORGANIZER).post(f"/comments/{cid}/restore", json={}).status_code == 200
    assert client(author).post(f"/comments/{cid}/hide", json={}).status_code == 200            # author deletes own


def test_comment_rate_limit(ev):
    tok = person(f"rl.{ev['tag']}@example.org")
    pid = ev["teams"][0]["project"]
    codes = [client(tok).post(f"/projects/{pid}/comments", json={"body": f"n{i}"}).status_code for i in range(6)]
    assert codes == [201] * 5 + [429]


def test_network_normalization():
    from dogfood.ratelimit import network
    assert network("::ffff:192.0.2.7") == network("192.0.2.7") == "192.0.2.7"
    assert network("2001:db8::1") == network("2001:0db8:0000:0000:ffff::9") == "2001:db8::/64"
    assert network("garbage") == "unknown"


def test_sliding_window_edges():
    from dogfood import ratelimit
    from dogfood.errors import HTTPError
    for i in range(10):
        ratelimit.hit("vote", "edge", now=1000.0 + i)
    with pytest.raises(HTTPError) as e:
        ratelimit.hit("vote", "edge", now=1030.0)
    assert e.value.status == 429
    with pytest.raises(HTTPError):
        ratelimit.hit("vote", "edge", now=1060.0)          # the first hit is exactly 60 s old: still counted
    ratelimit.hit("vote", "edge", now=1060.001)            # strictly after 60 s a slot frees


def test_shared_network_does_not_lock_out_a_classroom(ev):
    from dogfood import ratelimit
    for i in range(60):
        ratelimit.hit("vote_net", "10.0.0.1")               # 60 voters on one Wi-Fi in a minute: fine
        ratelimit.hit("vote", f"voter{i}")


def test_login_lockout_blocks_even_the_right_password(ev):
    email = f"lock.{ev['tag']}@example.org"
    person(email)
    c = client()
    codes = [c.post("/login", json={"email": email, "password": "wrong"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert c.post("/login", json={"email": email, "password": "longenough"}).status_code == 429



def test_project_page_shows_the_project(ev):
    """Regression: the layout once reused the variable name `p`, so every project page showed no title."""
    t = ev["teams"][0]
    html = client().get(f"/projects/{t['project']}").text
    assert f"<h1>Entry 0 {ev['tag']}</h1>" in html and "built-in method" not in html
