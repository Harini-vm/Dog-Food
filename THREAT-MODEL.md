# Threat model

What we protect, from whom, how, and what we accept. It is written for one self-hosted event
portal run by a small organizing team.

## What is worth attacking

| asset | why someone wants it |
|---|---|
| unpublished scores | a participant wants to know where they stand; a judge wants to align with peers |
| the ranking | winning is worth prize money |
| vote counts during voting | bandwagon effects, targeted vote buying |
| accounts (especially organizer, judge) | control over the above |
| personal data (emails, voter identity) | privacy |
| the server itself | a pivot into the host network |

## Actors

Visitors (anonymous), participants, judges, organizers, the admin, and anyone who can send HTTP
requests directly. **We assume every attacker uses curl, not our UI.**

## Threats and defenses

### Reading scores you shouldn't (spoofing, elevation of privilege)

- Every score read goes through `authz.score_scope`. A judge gets their own scores, an organizer
  gets their events' scores, everyone else gets 403.
- A peer judge gets **403, not an empty list**, so they cannot even tell whether a colleague has
  started. Real and made-up judge ids get the same answer.
- Assignment and score ids of other judges give the same 403 as ids that do not exist.
- Drafts, withdrawn and duplicate entries return the **same 404 as a missing id**, so nobody can
  probe for them.
- Tests: `test_isolation.py`, `test_cross_feature.py`, `test_integrations.py::test_hidden_projects_look_exactly_like_missing_ones`.

### Changing results (tampering)

- Deadlines, score ranges, the score lock after publishing, the voting window, and frozen results
  and certificates are **database triggers**, so a bug in one route cannot bypass them
  (ARCHITECTURE.md lists them).
- Published results are a snapshot with a SHA-256. Retracting needs a reason, keeps the old
  version, and is refused once certificates exist unless an admin forces it.
- Certificates are Ed25519-signed over canonical JSON. Anyone can verify them offline with
  `tools/verify_record.py`.
- The audit log is append-only and hash-chained. The organizer's audit page verifies the chain on
  every view.

### Vote manipulation

| attack | defense |
|---|---|
| double voting by replay, refresh or parallel requests | PK `(voter_id, project_id)` and a row lock on the voter |
| overspending the budget with parallel requests | voter row `FOR UPDATE`; the budget is counted from `votes` (tested with 20 parallel requests) |
| voting for your own team | checked against team membership **at vote time**, for accounts and for email voters (by email hash) |
| many email addresses | each address = one voter. Links are rate-limited per address and per network. We do not claim to stop someone who controls many mailboxes; a people's-choice vote is a popularity signal, not the judged ranking |
| link forwarding or replay | links are single-use, expire in 24 h, and are stored as digests. Several links for one address lead to the same voter |
| mail scanners burning the one-time link | the link page needs a click (POST); a GET never uses it |
| bandwagon voting | tallies are sealed until voting closes, for everyone including admins; ballot order is personal |
| position bias | each voter's ballot order is a keyed hash of (voter, project): stable for them, different for others, unpredictable |

### Account attacks

- Passwords: scrypt (N = 2^14). The login does the same work whether the account exists or not.
- Online guessing: after 10 failed logins for an address in 10 minutes, even the right password
  gets 429 until the window passes. Legitimate users can wait; attackers lose their throughput.
- Account takeover via signup: an email that was imported or invited cannot be claimed by signing
  up (`use_invite`). Only the invitation link can activate it.
- Session theft: cookies are `HttpOnly`, `SameSite=Lax` and `Secure` when `DOGFOOD_SECURE_COOKIES` is
  on. Sessions are server-side and die on logout. Tokens and sessions are stored as digests, so a
  database leak does not leak credentials.
- CSRF: every cookie-authenticated write must carry the session's CSRF token (form field or
  `X-CSRF-Token`). Bearer-token requests are not exposed to CSRF.

### Injection and XSS

- SQL: every query is parameterised; no string-built SQL.
- HTML: Jinja autoescaping everywhere. Comments are plain text, and `{{ 7*7 }}` stays literal.
- CSP: `default-src 'self'` with no inline scripts. `frame-ancestors 'none'` on every page except the
  widget (`/embed/*`), which has `script-src 'none'`. The widget is the only frameable page and
  cannot run script.
- Invisible and control characters are stripped before a comment's "is it empty" check.

### Server-side request forgery (webhooks)

An organizer sets a webhook URL. Without checks, that would let an organizer make our server call
`db:5432` or a cloud metadata endpoint. Receivers must resolve to **public** addresses.
`DOGFOOD_WEBHOOK_ALLOW_PRIVATE=true` exists for local testing only.

Accepted risk: DNS rebinding between our check and the request. A full fix needs pinning the
resolved IP per request. It is on the list, not done.

### Denial of service

- Per-person limits first (voter, account, address), loose per-network limits second. A school or
  conference Wi-Fi shares one IP, and 60 voters from one address in a minute all succeed. IPv4-mapped
  IPv6 is unwrapped, and IPv6 is grouped by /64.
- Imports are capped at 20 MB and fully validated before writing.
- Webhook sends run off the request path with a timeout, one delivery per transaction.

Accepted risk: limits are in process memory, so a restart resets them, and several processes would
each count separately. For one self-hosted event this is acceptable; a larger deployment would move
them to Postgres or Redis.

### Privacy

- Email voters are stored as an HMAC of their address (`DOGFOOD_SECRET_KEY`). The plain address
  exists only in the mail outbox.
- Votes and score values are never written to the audit log, which organizers can read.
- Public results show rank, score and review count, never judges or individual reviews.
- Judge comments are for organizers only.

## Configuration that matters in production

| setting | why |
|---|---|
| `DOGFOOD_DEMO=false` | revokes the fixed demo tokens and stops giving seeded accounts the password `dogfood` |
| `DOGFOOD_SECRET_KEY` | a long random value: it keys the voter email hashes and ballot order |
| `DOGFOOD_SECURE_COOKIES=true` | behind HTTPS |
| `DOGFOOD_TRUST_PROXY=true` | only behind a reverse proxy you control; otherwise `X-Forwarded-For` is ignored, because clients can forge it |
| the `/data` volume | holds the certificate signing key; back it up, and never commit it |
