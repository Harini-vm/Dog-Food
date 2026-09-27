"""Public comments on projects. Plain text only; the templates escape everything they print."""

import re
import unicodedata

from .. import audit, db
from ..errors import bad, forbidden, not_found
from ..security import new_id

LIMIT = 2000
# Characters that take no space on screen. " ​ " would otherwise pass as a non-empty comment.
INVISIBLE = re.compile("[​‌‍⁠﻿­᠎]")


def clean(text: str) -> str:
    text = INVISIBLE.sub("", unicodedata.normalize("NFC", text or ""))
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")   # control chars
    return text.strip()                         # str.strip() also removes Unicode spaces such as U+2003


def add(conn, user, project_id: str, body: str) -> str:
    p = db.one(conn, """SELECT p.*, e.comments_open FROM projects p JOIN events e ON e.id = p.event_id
                        WHERE p.id = %s""", (project_id,))
    if p is None or p["status"] != "submitted":
        raise not_found("no such project")
    if not p["comments_open"]:
        raise forbidden("comments are closed for this event", "comments_closed")
    text = clean(body)
    if not text:
        raise bad("write something first", "empty_comment")
    if len(text) > LIMIT:                       # code points, the same unit as Postgres char_length
        raise bad(f"comments are limited to {LIMIT} characters (this one has {len(text)})", "comment_too_long")
    cid = new_id("cmt")
    conn.execute("INSERT INTO comments (id, event_id, project_id, user_id, body) VALUES (%s, %s, %s, %s, %s)",
                 (cid, p["event_id"], project_id, user.id, text))
    audit.log(conn, user, "comment.add", p["event_id"], cid)
    return cid


def visible(conn, project_id: str) -> list:
    return db.rows(conn, """SELECT c.id, c.body, c.created_at, c.user_id, u.name FROM comments c
                            JOIN users u ON u.id = c.user_id WHERE c.project_id = %s AND c.hidden_at IS NULL
                            ORDER BY c.created_at""", (project_id,))


def moderate(conn, user, comment_id: str, hide: bool, reason: str = "") -> dict:
    from .. import authz
    c = db.one(conn, "SELECT * FROM comments WHERE id = %s FOR UPDATE", (comment_id,))
    if c is None:
        raise not_found("no such comment")
    is_author = c["user_id"] == user.id
    is_org = bool(authz.roles(conn, user, c["event_id"]) & {"organizer", "admin"})
    if not (is_org or (is_author and hide)):   # authors may remove their own; only organizers restore
        raise forbidden("only organizers can moderate comments")
    if hide and is_org and not is_author and not reason.strip():
        raise bad("give a reason; it goes in the audit log")
    conn.execute("UPDATE comments SET hidden_at = CASE WHEN %s THEN now() END, hidden_by = CASE WHEN %s THEN %s END, "
                 "hidden_reason = CASE WHEN %s THEN %s END WHERE id = %s",
                 (hide, hide, user.id, hide, reason.strip()[:300] or ("removed by author" if is_author else None),
                  comment_id))
    audit.log(conn, user, "comment.hide" if hide else "comment.restore", c["event_id"], comment_id,
              {"reason": reason.strip()[:300]})
    return c
