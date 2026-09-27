"""T4: API paging and privacy, tokens, webhooks, certificates, widget, import/export, offline assets."""

import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import ADMIN, ORGANIZER, PARTICIPANT, ROOT
from helpers import client, event_with_entries, person, sql


# ------------------------------------------------------------------ API

def test_cursor_paging_survives_new_submissions(server):
    e = event_with_entries(4)
    first = client().get(f"/api/v1/events/{e['slug']}/projects?limit=2").json()
    extra = person(f"late.{e['tag']}@example.org")
    client(extra).post(f"/events/{e['slug']}/teams", json={"name": f"Late {e['tag']}"})
    client(extra).post("/projects/new", json={"event": e["slug"], "title": "Late entry", "action": "submit"})
    seen = [p["id"] for p in first["projects"]]
    cursor = first["next_cursor"]
    while cursor:
        page = client().get(f"/api/v1/events/{e['slug']}/projects?limit=2&cursor={cursor}").json()
        seen += [p["id"] for p in page["projects"]]
        cursor = page["next_cursor"]
    originals = {t["project"] for t in e["teams"]}
    assert len(seen) == len(set(seen)) and originals <= set(seen)


def test_hidden_projects_look_exactly_like_missing_ones(server):
    e = event_with_entries(1)
    draft_owner = person(f"d.{e['tag']}@example.org")
    client(draft_owner).post(f"/events/{e['slug']}/teams", json={"name": f"Draft {e['tag']}"})
    pid = client(draft_owner).post("/projects/new", json={"event": e["slug"], "title": "wip"}).json()["id"]
    hidden, missing = client().get(f"/api/v1/projects/{pid}"), client().get("/api/v1/projects/prj_nope")
    assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()
    assert client(draft_owner).get(f"/api/v1/projects/{pid}").status_code == 200    # the team sees its own
    assert client(PARTICIPANT).get(f"/api/v1/projects/{pid}").status_code == 404


def test_personal_tokens(server):
    tok = person("tokens.person@example.org")
    made = client(tok).post("/api/v1/tokens", json={"label": "script"}).json()
    assert client(made["token"]).get("/api/v1/me").json()["email"] == "tokens.person@example.org"
    assert client(made["token"]).delete(f"/api/v1/tokens/{made['id']}").status_code == 200
    assert client(made["token"]).get("/api/v1/me").status_code == 401
    assert client(PARTICIPANT).delete(f"/api/v1/tokens/{made['id']}").status_code == 404   # not yours


def test_api_docs_are_local(server):
    assert client().get("/api/docs").status_code == 200
    spec = client().get("/api/openapi.json").json()
    assert "/api/v1/events/{event}/projects" in spec["paths"] and "/api/v1/certificates/verify" in spec["paths"]


def test_no_external_assets_anywhere():
    """Offline: nothing loads from another host."""
    pat = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", re.I)
    files = list((ROOT / "src/dogfood/templates").rglob("*.html")) + list((ROOT / "src/dogfood/static").rglob("*"))
    offenders = [str(f) for f in files if f.is_file() and pat.search(f.read_text(errors="ignore"))]
    offenders += [str(f) for f in (ROOT / "src/dogfood/static").rglob("*.css") if "@import" in f.read_text()]
    assert offenders == []


# ------------------------------------------------------------------ webhooks

class Receiver:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.calls.append({"body": body, "headers": dict(self.headers)})
                code = outer.codes.pop(0) if outer.codes else 200
                self.send_response(code)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"


def _wait(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_webhook_retries_signs_and_keeps_its_id(server):
    from dogfood.services.webhooks import verify
    e = event_with_entries(1)
    rx = Receiver([500, 503, 200])
    hook = client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks", json={"url": rx.url}).json()
    client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks/ping")
    assert _wait(lambda: len(rx.calls) >= 3)
    ids = {c["headers"]["X-Dogfood-Event-Id"] for c in rx.calls}
    assert len(ids) == 1                                                        # same envelope on every retry
    body = rx.calls[-1]["body"]
    sig = rx.calls[-1]["headers"]["X-Dogfood-Signature"]
    assert verify(hook["secret"], body, sig)
    assert not verify(hook["secret"], body.replace(b'"ping"', b'"pong"'), sig)            # one byte changed
    assert not verify(hook["secret"], json.dumps(json.loads(body), indent=2).encode(), sig)  # re-serialised
    assert not verify("whsec_wrong", body, sig)
    ts = int(sig.split(",")[0][2:])
    assert not verify(hook["secret"], body, sig, now=ts + 3600)                  # a replay an hour later
    assert _wait(lambda: sql("SELECT status FROM webhook_deliveries WHERE webhook_id = %s", (hook["id"],))[0]["status"]
                 == "delivered")


def test_webhook_sequence_and_give_up(server):
    e = event_with_entries(1)
    rx = Receiver([500] * 50)
    hook = client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks", json={"url": rx.url}).json()
    for _ in range(2):
        client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks/ping")
    assert _wait(lambda: len(rx.calls) >= 2)
    seqs = [json.loads(c["body"])["sequence"] for c in rx.calls[:2]]
    assert seqs[0] < seqs[1]
    sql("UPDATE webhook_deliveries SET attempts = 5 WHERE webhook_id = %s", (hook["id"],))   # skip to the last try
    assert _wait(lambda: {r["status"] for r in sql("SELECT status FROM webhook_deliveries WHERE webhook_id = %s",
                                                     (hook["id"],))} == {"failed"})


def test_webhook_refuses_private_addresses(monkeypatch):
    from dogfood import config
    from dogfood.errors import HTTPError
    from dogfood.services.webhooks import check_url
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE", False)
    for url in ("http://127.0.0.1/x", "http://localhost:5432/", "http://169.254.169.254/latest", "http://10.1.2.3/",
                "ftp://example.org/"):
        with pytest.raises(HTTPError):
            check_url(url)


def test_submission_emits_webhook_in_same_transaction(server):
    e = event_with_entries(1)
    rx = Receiver([])
    client(ORGANIZER).post(f"/organize/{e['slug']}/webhooks", json={"url": rx.url})
    tok = person(f"wh.{e['tag']}@example.org")
    client(tok).post(f"/events/{e['slug']}/teams", json={"name": f"WH {e['tag']}"})
    client(tok).post("/projects/new", json={"event": e["slug"], "title": "Hooked", "action": "submit"})
    assert _wait(lambda: any(json.loads(c["body"])["type"] == "project.submitted" for c in rx.calls))


# ------------------------------------------------------------------ results, certificates

@pytest.fixture(scope="module")
def published(server):
    e = event_with_entries(4, reviews_per_project=1)
    j = client(ORGANIZER).post(f"/organize/{e['slug']}/people", json={"email": f"j.{e['tag']}@example.org"}).json()
    from helpers import token_for
    jt = token_for(f"j.{e['tag']}@example.org")
    client(ORGANIZER).post(f"/organize/{e['slug']}/assign")
    for i, a in enumerate(client(jt).get("/api/v1/judge/assignments").json()["assignments"]):
        client(jt).put(f"/api/v1/assignments/{a['id']}/score",
                       json={"criteria": {"functionality": 5 - i % 5, "quality": 3, "innovation": 2 + i % 3}})
    sql("UPDATE events SET closes_at = now() - interval '1 minute' WHERE id = %s", (e["id"],))
    assert client(ORGANIZER).post(f"/organize/{e['slug']}/publish").status_code == 201
    assert client(ORGANIZER).post(f"/organize/{e['slug']}/certificates").status_code == 201
    return {**e, "judge": j}


def test_certificates_sign_and_verify(published, tmp_path):
    rows = client(ORGANIZER).get(f"/organize/{published['slug']}/certificates").json()["certificates"]
    kinds = {r["kind"] for r in rows}
    assert {"winner", "participant"} <= kinds
    doc = client().get(f"/api/v1/certificates/{rows[0]['id']}").json()
    ok = client().post("/api/v1/certificates/verify", json={"record": doc["record"], "signature": doc["signature"]}).json()
    assert ok == {"valid": True, "matches_current_results": True}
    forged = {**doc["record"], "recipient": doc["record"]["recipient"] + "x"}
    assert client().post("/api/v1/certificates/verify", json={"record": forged, "signature": doc["signature"]}).json() \
        == {"valid": False}
    # Offline, with a re-formatted file: formatting does not matter, content does.
    rec, keys = tmp_path / "rec.json", tmp_path / "keys.json"
    rec.write_text(json.dumps(doc, indent=4, ensure_ascii=False), encoding="utf-8")
    keys.write_text(json.dumps(client().get("/.well-known/dogfood-keys.json").json()), encoding="utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "tools/verify_record.py"), str(rec), str(keys)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.startswith("VALID"), run.stdout
    doc["record"]["team"] = "Someone else"
    rec.write_text(json.dumps(doc), encoding="utf-8")
    assert subprocess.run([sys.executable, str(ROOT / "tools/verify_record.py"), str(rec), str(keys)],
                          capture_output=True).returncode == 1


def test_certificates_are_immutable_and_block_retraction(published):
    import psycopg
    with pytest.raises(psycopg.Error) as e:
        sql("UPDATE certificates SET record = '{}' WHERE event_id = %s", (published["id"],))
    assert e.value.sqlstate == "DF006"
    r = client(ORGANIZER).post(f"/organize/{published['slug']}/retract", json={"reason": "oops"})
    assert r.status_code == 409 and r.json()["error"] == "certificates_issued"
    assert client(ORGANIZER).post(f"/organize/{published['slug']}/certificates").json()["error"] == "already_issued"


# ------------------------------------------------------------------ widget

def test_widget_is_the_only_frameable_page(published):
    w = client().get(f"/embed/{published['slug']}?view=results&track=%3Cscript%3Ealert(1)%3C/script%3E")
    assert w.status_code == 200 and "X-Frame-Options" not in w.headers
    assert "frame-ancestors *" in w.headers["Content-Security-Policy"] and "script-src 'none'" in w.headers["Content-Security-Policy"]
    assert "<script>alert(1)" not in w.text
    normal = client().get(f"/events/{published['slug']}")
    assert normal.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in normal.headers["Content-Security-Policy"]
    assert client().get("/embed/no-such-event").status_code == 404


# ------------------------------------------------------------------ import / export

def _shape(doc):
    """Export content without ids, for comparing an event with its copy."""
    title = {p["id"]: p["title"] for p in doc["projects"]}
    team = {t["id"]: t["name"] for t in doc["teams"]}
    track = {t["id"]: t["name"] for t in doc["tracks"]}
    return {
        "criteria": doc["criteria"], "settings": doc["event"]["settings"],
        "tracks": sorted(track.values()),
        "teams": sorted((t["name"], tuple(t["members"])) for t in doc["teams"]),
        "judges": sorted((j["email"], tuple(sorted(track[t] for t in j["tracks"]))) for j in doc["judges"]),
        "projects": sorted((p["title"], team[p["team"]], track.get(p["track"]), p["status"],
                            title.get(p["duplicate_of"]), p["submitted_at"]) for p in doc["projects"]),
        "scores": sorted((s["judge"], title[s["project"]], json.dumps(s["criteria"], sort_keys=True), s["comment"])
                         for s in doc["scores"]),
        "assignments": sorted((a["judge"], title[a["project"]]) for a in doc["assignments"]),
    }


def test_export_import_export_round_trip(server):
    first = client(ADMIN).get("/api/v1/events/evt_01/export").json()
    assert client(ADMIN).post("/api/v1/events/import", json=first).json()["error"] == "already_imported"
    r = client(ADMIN).post("/api/v1/events/import?as_copy=true", json=first)
    assert r.status_code == 201, r.text
    second = client(ADMIN).get(f"/api/v1/events/{r.json()['event_id']}/export").json()
    a, b = _shape(first), _shape(second)
    # Team names that the fixture import had to make unique carry the original team id; compare the rest.
    strip = lambda n: re.sub(r" \(tm_\w+\)$", "", n)  # noqa: E731
    for d in (a, b):
        d["teams"] = sorted((strip(n), m) for n, m in d["teams"])
        d["projects"] = sorted((t, strip(tm), *rest) for t, tm, *rest in d["projects"])
    assert a == b


@pytest.mark.parametrize("mutate, why", [
    (lambda d: d["projects"][0].update(team="tm_missing"), "unknown team"),
    (lambda d: d["scores"][0].update(judge="jdg_missing"), "unknown judge"),
    (lambda d: d["projects"][0].update(track="trk_missing"), "unknown track"),
    (lambda d: d["projects"].append(dict(d["projects"][0])), "duplicate id"),
    (lambda d: d["teams"].append(dict(d["teams"][0])), "duplicate team id"),
    (lambda d: d["judges"].append({**d["judges"][0], "id": "jdg_x"}), "one email, two judges"),
    (lambda d: d["scores"].append(dict(d["scores"][0])), "judge reviews twice"),
    (lambda d: d["scores"][0]["criteria"].update(quality=10 ** 27), "huge score"),
    (lambda d: d["scores"][0]["criteria"].update(quality=6), "off the scale"),
    (lambda d: d["scores"][0]["criteria"].update(quality=True), "boolean score"),
    (lambda d: d["scores"][0]["criteria"].update(quality=3.5), "fractional score"),
    (lambda d: d["projects"][0].update(status="winner"), "unknown status"),
    (lambda d: d["projects"][0].update(status="duplicate", duplicate_of=d["projects"][0]["id"]), "duplicate of itself"),
    (lambda d: d["event"].update(submissions_close="next tuesday"), "bad date"),
    (lambda d: d["event"].setdefault("settings", {}).update(max_team_size=10 ** 30), "huge team size"),
    (lambda d: d.update(projects="nope"), "not a list"),
])
def test_invalid_imports_write_nothing(server, mutate, why):
    data = json.loads((ROOT / "fixtures.json").read_text())
    data["event"]["id"] = "evt_bad_import"
    mutate(data)
    before = sql("SELECT count(*) AS n FROM events")[0]["n"]
    r = client(ADMIN).post("/api/v1/events/import", json=data)
    assert r.status_code == 400, (why, r.text)
    assert sql("SELECT count(*) AS n FROM events")[0]["n"] == before


def test_import_needs_admin_and_json(server):
    assert client(ORGANIZER).post("/api/v1/events/import", json={}).status_code == 403
    r = client(ADMIN).post("/api/v1/events/import", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_fixture_file_in_repo_is_untouched():
    import hashlib
    assert hashlib.sha256((ROOT / "fixtures.json").read_bytes()).hexdigest().startswith("252896b")
    assert hashlib.sha256((ROOT / "run.py").read_bytes()).hexdigest().startswith("aa98963")
    assert Path(ROOT / "run.py").exists()
