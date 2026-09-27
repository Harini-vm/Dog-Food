"""Regression tests for the findings of an independent adversarial review (see docs/edge-cases.md)."""

import re
import secrets

import httpx
import pytest

from conftest import ADMIN, BASE, ORGANIZER
from helpers import client, close_voting, event_with_entries, open_voting, person, sql


def _browser(email, password="longenough"):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=20)
    assert c.post("/login", data={"email": email, "password": password, "next": "/"}).status_code == 200
    return c


def test_organizer_of_another_event_cannot_take_over_a_pending_judge(server):
    a = event_with_entries(1)
    victim = f"pending.{a['tag']}@example.org"
    first = client(ORGANIZER).post(f"/organize/{a['slug']}/people", json={"email": victim}).json()
    assert first["link"]                                           # A's own invitation
    # the admin creates event B and makes an attacker its organizer
    b = client(ADMIN).post("/organize/events", json={"name": f"Other {a['tag']}", "closes_at": "2030-01-01T00:00"}).json()
    attacker = person(f"evil.{a['tag']}@example.org")
    sql("INSERT INTO memberships SELECT %s, id, 'organizer' FROM users WHERE email = %s", (b["id"], f"evil.{a['tag']}@example.org"))
    out = client(attacker).post(f"/organize/{b['slug']}/people", json={"email": victim})
    assert out.status_code == 409 and out.json()["error"] == "pending_elsewhere"   # B cannot touch A's pending account
    # the real judge activates with A's link; any other open link dies with it
    c = httpx.Client(base_url=BASE, timeout=20)
    assert c.post(first["link"], data={"name": "Real Judge", "password": "correcthorse"}).status_code == 303
    again = httpx.Client(base_url=BASE, timeout=20).post(first["link"], data={"password": "hijacked!!"})
    assert again.status_code == 400                                # an invitation is never a password reset
    assert _browser(victim, "correcthorse").get("/judge").status_code == 200


def test_logged_in_people_can_use_forms_that_had_no_csrf_field(server):
    e = event_with_entries(1)
    open_voting(e)
    email = f"csrf.{e['tag']}@example.org"
    person(email)
    b = _browser(email)
    link = client().post(f"/events/{e['slug']}/vote/link", json={"email": f"other.{e['tag']}@example.org"}).json()["link"]
    page = b.get("/" + link.split("/", 3)[3]).text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert token and b.post("/" + link.split("/", 3)[3], data={"csrf": token}).status_code == 200
    login_page = b.get("/login").text
    assert 'name="csrf"' in login_page


def test_account_and_email_link_share_one_budget(server):
    e = event_with_entries(4)
    open_voting(e, votes=2)
    email = f"both.{e['tag']}@example.org"
    tok = person(email)
    for t in e["teams"][:2]:
        assert client(tok).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": t["project"]}).status_code == 200
    link = client().post(f"/events/{e['slug']}/vote/link", json={"email": email.upper()}).json()["link"]
    b = httpx.Client(base_url=BASE, timeout=20)
    b.post("/" + link.split("/", 3)[3])
    page = b.get(f"/events/{e['slug']}/vote").text
    assert "0 of 2" in page                                        # the same person, the same spent budget
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = b.post(f"/events/{e['slug']}/vote", data={"csrf": csrf, "project_id": e["teams"][2]["project"]})
    assert r.status_code == 409


def test_votes_cannot_be_unsealed_by_clearing_or_reopening_voting(server):
    e = event_with_entries(2)
    open_voting(e)
    v = person(f"seal.{e['tag']}@example.org")
    client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": e["teams"][0]["project"]})
    r = client(ORGANIZER).post(f"/organize/{e['slug']}/settings", json={"voting_opens_at": "", "voting_closes_at": ""})
    assert r.status_code == 409 and r.json()["error"] == "votes_exist"
    assert client().get(f"/api/v1/events/{e['slug']}/tally").status_code == 403
    close_voting(e)
    r = client(ORGANIZER).post(f"/organize/{e['slug']}/settings", json={"voting_closes_at": "2030-01-01T00:00"})
    assert r.status_code == 409


def test_a_draft_cannot_be_turned_into_a_submission_by_moderation(server):
    e = event_with_entries(1)
    tok = person(f"draft.{e['tag']}@example.org")
    client(tok).post(f"/events/{e['slug']}/teams", json={"name": f"Drafty {e['tag']}"})
    pid = client(tok).post("/projects/new", json={"event": e["slug"], "title": "only a draft"}).json()["id"]
    r = client(ORGANIZER).post(f"/organize/{e['slug']}/projects/{pid}/status", json={"status": "withdrawn", "reason": "x"})
    assert r.status_code == 400
    assert sql("SELECT status FROM projects WHERE id = %s", (pid,))[0]["status"] == "draft"


def test_webhook_redirects_are_not_followed(server):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    import time
    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/internal")
            self.end_headers()

        def log_message(self, *a):
            pass

    hop = HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=hop.serve_forever, daemon=True).start()
    e = event_with_entries(1)
    hook = client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks", json={"url": f"http://127.0.0.1:{hop.server_port}/"}).json()
    client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks/ping")
    for _ in range(60):
        row = sql("SELECT attempts, last_error FROM webhook_deliveries WHERE webhook_id = %s", (hook["id"],))
        if row and row[0]["attempts"]:
            break
        time.sleep(0.05)
    assert hits == [] and "redirect" in (row[0]["last_error"] or "")


@pytest.mark.parametrize("path, body", [
    ("/login", {"email": 5, "password": 5}),
    ("/signup", {"email": 5, "name": 5, "password": 12345678}),
])
def test_json_numbers_never_cause_a_500(server, path, body):
    assert client().post(path, json=body).status_code < 500


def test_json_numbers_in_organizer_and_comment_routes(server):
    e = event_with_entries(1)
    tok = person(f"num.{e['tag']}@example.org")
    assert client(tok).post(f"/projects/{e['teams'][0]['project']}/comments", json={"body": 5}).status_code == 201
    assert client(ORGANIZER).post(f"/organize/{e['slug']}/settings", json={"name": 12345}).status_code == 200
    assert client(ORGANIZER).post(f"/organize/{e['slug']}/retract", json={"reason": 5}).status_code == 409
    assert client(ORGANIZER).post(f"/organize/{e['slug']}/pairwise", json={"per_judge": 3}).status_code < 500


def test_admin_organize_page_title_is_clean(server):
    html = client(ADMIN).get("/organize", headers={"Accept": "text/html"})
    page = httpx.Client(base_url=BASE, follow_redirects=True)
    page.post("/login", data={"email": "admin@dogfood.local", "password": "dogfood", "next": "/"})
    text = page.get("/organize").text
    assert "<title>Organize · DOGFOOD</title>" in text and text.count("Import an event") == 1
    assert html.status_code == 200


def test_team_names_unique_in_the_database(server):
    import psycopg
    e = event_with_entries(1)
    with pytest.raises(psycopg.Error):
        sql("INSERT INTO teams (id, event_id, name, invite_code) VALUES (%s, %s, %s, %s)",
            ("tm_dup_" + secrets.token_hex(3), e["id"], e["teams"][0]["name"].upper(), secrets.token_hex(6)))


def test_attacker_who_invites_first_cannot_take_over_later_roles(server):
    tag = secrets.token_hex(3)
    b = client(ADMIN).post("/organize/events", json={"name": f"Evil {tag}", "closes_at": "2030-01-01T00:00"}).json()
    attacker = person(f"evil2.{tag}@example.org")
    sql("INSERT INTO memberships SELECT %s, id, 'organizer' FROM users WHERE email = %s", (b["id"], f"evil2.{tag}@example.org"))
    target = f"future.{tag}@example.org"
    link = client(attacker).post(f"/organize/{b['slug']}/people", json={"email": target}).json()["link"]
    a = event_with_entries(1)
    r = client(ORGANIZER).post(f"/organize/{a['slug']}/people", json={"email": target})
    assert r.status_code == 409 and r.json()["error"] == "pending_elsewhere"   # A never grants a role to it
    assert httpx.Client(base_url=BASE, timeout=20).post(link, data={"password": "attacker!!"}).status_code == 303
    roles = sql("SELECT event_id FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.email = %s", (target,))
    assert {r["event_id"] for r in roles} == {b["id"]}                          # the attacker got nothing of A's


def test_activation_is_refused_if_the_account_became_shared(server):
    """Defense in depth: even if a role elsewhere appears after a link was issued, the link stops working."""
    tag = secrets.token_hex(3)
    a = event_with_entries(1)
    target = f"shared.{tag}@example.org"
    link = client(ORGANIZER).post(f"/organize/{a['slug']}/people", json={"email": target}).json()["link"]
    sql("INSERT INTO memberships SELECT 'evt_01', id, 'judge' FROM users WHERE email = %s", (target,))
    r = httpx.Client(base_url=BASE, timeout=20).post(link, data={"password": "whatever1"})
    assert r.status_code == 400


@pytest.mark.parametrize("path, body", [
    ("/login", {"email": {"a": 1}, "password": ["x"]}),
    ("/signup", {"email": ["a@b.co"], "name": {"x": 1}, "password": "longenough"}),
])
def test_json_objects_and_lists_never_cause_a_500(server, path, body):
    assert client().post(path, json=body).status_code < 500


def test_uploaded_file_in_a_text_field_never_causes_a_500(server):
    r = httpx.Client(base_url=BASE, timeout=20).post("/login", files={"email": ("x.txt", b"hello"), "password": (None, "x")})
    assert r.status_code < 500


def test_nat64_and_mapped_private_addresses_are_refused(monkeypatch):
    from dogfood import config
    from dogfood.errors import HTTPError
    from dogfood.services.webhooks import check_url
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE", False)
    for url in ("http://[64:ff9b::a9fe:a9fe]/", "http://[::ffff:10.0.0.1]/", "http://[::1]/"):
        with pytest.raises(HTTPError):
            check_url(url)


@pytest.mark.parametrize("method, path, kw", [
    ("get", "/projects?q=a%00b", {}),
    ("get", "/projects/a%00b", {}),
    ("get", "/events/a%00b", {}),
    ("get", "/api/v1/events/evt_01/projects?cursor=a%00", {}),
    ("post", "/login", {"data": {"email": "a\x00@b.co", "password": "x"}}),
    ("post", "/login", {"json": {"email": "a\u0000@b.co", "password": "x"}}),
    ("post", "/api/v1/certificates/verify", {"json": [1]}),
    ("post", "/api/v1/certificates/verify", {"json": "x"}),
])
def test_hostile_input_is_a_4xx_never_a_500(server, method, path, kw):
    r = getattr(httpx.Client(base_url=BASE, timeout=20), method)(path, **kw)
    assert 400 <= r.status_code < 500, (path, r.status_code)


def test_broken_flash_cookie_does_not_break_pages(server):
    assert httpx.get(BASE + "/", cookies={"df_flash": "notjson"}).status_code == 200


def test_webhook_urls_with_credentials_or_ipv4_compatible_addresses_are_refused(monkeypatch):
    from dogfood import config
    from dogfood.errors import HTTPError
    from dogfood.services.webhooks import check_url
    with pytest.raises(HTTPError):
        check_url("http://user:pw@example.org/hook")
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE", False)
    with pytest.raises(HTTPError):
        check_url("http://[::127.0.0.1]/")
