# Edge cases: the policy we chose and the test that proves it

The acceptance checker (`run.py`) verifies T1 and T2. This file is the evidence for everything else.
Every row names a policy and a test in `tests/`. Run all of them with
`docker compose exec app pytest -q`.

## Submissions and teams (T1)

| case | policy | test |
|---|---|---|
| submit after the deadline, by page or API | 403 `submissions_closed`; the database trigger decides with its own clock | `test_submissions.py`, `test_event_lifecycle.py::test_moderation_after_deadline_is_status_only` |
| two entries from one team (fixture prj_41) | the second is stored as `duplicate`, is never in the gallery, and is never ranked | `test_submissions.py`, `test_judging_math.py::test_fixture_results` |
| duplicate team names in the fixture | renamed with the team id on import | importer, `test_export_import_export_round_trip` |
| team is full; two people race for the last seat | 409 `team_full`; the team row lock lets exactly one in | `test_event_lifecycle.py::test_team_rules`, `test_cross_feature.py::test_two_people_racing_for_the_last_seat` |
| joining a second team | 409 `already_on_team` | `test_team_rules` |
| a judge or organizer joining a team in their own event | 403 `role_conflict` | `test_team_rules` |
| team changes after the deadline | refused: teams lock with submissions | `test_cross_feature.py::test_teams_lock_at_the_deadline` |
| organizer withdraws or restores an entry after the deadline | allowed, status-only; content stays frozen | `test_moderation_after_deadline_is_status_only` |

## Judging (T2)

| case | policy | test |
|---|---|---|
| peer judge reads scores by id, by `?judge=`, or with a made-up id | 403 every time | `test_isolation.py` |
| judge writes to another judge's assignment | 403 | `test_isolation.py::test_judge_cannot_write_a_peers_assignment` |
| judge removed after scoring | reviews kept; open assignments freed; editing old reviews refused | `test_cross_feature.py::test_removed_judge_keeps_reviews_but_loses_access` |
| flat scorer (fixture jdg_07: 4/4/4) | flagged; the maths gives their reviews little influence | `test_judging_math.py::test_fixture_results` |
| no weights in the fixture | 0.40 / 0.35 / 0.25, reported and editable | JUDGING.md |
| a judge on the team they would review | never assigned (conflicts) | `test_plan_never_breaks_a_conflict_and_reports_shortfalls` |
| not enough judges | shortfalls reported, not hidden | same |
| scarce track judge | hardest project first | `test_plan_hardest_first` |
| planner run twice | fills gaps only | `test_planner_respects_tracks_and_is_rerunnable` |
| publish before submissions close / with missing reviews | 409 `still_open` / 409 `incomplete` unless confirmed | `test_publish_rules_and_frozen_results` |
| editing a score after publishing, via the API or directly in SQL | 403 `judging_closed` / SQLSTATE DF004 | same |
| rewriting a published snapshot | DF003 | same |
| retract | reason required; reopens judging; creates a new version on republish | same |

## Voting, comments, abuse (T3)

| case | policy | test |
|---|---|---|
| vote before opening / at or after closing | 403 `voting_closed`; half-open window `opens <= t < closes`; database trigger | `test_voting.py::test_voting_window_is_server_side` |
| the same vote 10× at once | exactly one vote | `test_same_vote_ten_times_at_once_is_one_vote` |
| 3 votes left, 20 parallel requests | exactly 3 stored, the rest 409 | `test_budget_holds_under_concurrency` |
| ballot order | a stable personal permutation: every project once | `test_ballot_is_a_stable_personal_permutation` |
| vote for own team (account or email voter) | 403 `own_team` | `test_no_votes_for_own_team`, `test_email_voter_who_is_on_the_team_is_refused` |
| project from another event | 404 | `test_other_events_projects_are_not_on_the_ballot` |
| counts while voting is open, for anyone including admin | 403 `tallies_sealed`; the dashboard shows no counts | `test_tallies_sealed_for_everyone_until_close` |
| project withdrawn after receiving votes | votes kept, not tallied, and given back to voters | `test_withdrawn_project_gives_the_vote_back` |
| `" Guest@Example.COM "` vs `guest@example.com` | one voter | `test_email_links` |
| link replayed, altered, or opened by a mail scanner (GET) | 404 / 404 / not used | `test_email_links` |
| email voter's form without their CSRF token | 403 | `test_email_links` |
| empty-looking comments (spaces, U+2003, zero-width, BOM) | 400 `empty_comment` | `test_comments_and_abuse.py::test_empty_looking_comments_are_refused` |
| 2000 vs 2001 characters; Tamil and emoji | characters, not bytes; 2000 allowed | `test_length_limit_counts_characters` |
| `<script>`, `onerror`, `{{ 7*7 }}` | shown as text, never run or evaluated | `test_unicode_is_stored_exactly_and_markup_is_inert` |
| hide a comment | organizers, with a reason; authors may delete their own | `test_moderation` |
| comment flood | 6th comment in a minute: 429 | `test_comment_rate_limit` |
| IPv4-mapped IPv6, IPv6 /64 | normalized to one client | `test_network_normalization` |
| window edge | a slot frees strictly after 60 s | `test_sliding_window_edges` |
| a classroom behind one IP | 60 voters in a minute all pass | `test_shared_network_does_not_lock_out_a_classroom` |
| password guessing | after 10 failures, even the right password gets 429 | `test_login_lockout_blocks_even_the_right_password` |
| votes in the audit log | never | `test_cross_feature.py::test_votes_are_not_in_the_organizer_readable_audit_log` |

## API and integrations (T4)

| case | policy | test |
|---|---|---|
| project submitted between page 1 and page 2 | keyset cursor: no duplicates, nothing skipped | `test_integrations.py::test_cursor_paging_survives_new_submissions` |
| draft / hidden project by id | same 404 body as a missing id; the team still sees its own | `test_hidden_projects_look_exactly_like_missing_ones` |
| revoked token | 401 at once; revoking someone else's: 404 | `test_personal_tokens` |
| webhook receiver fails 500, 503, then succeeds | retried; same envelope id every time | `test_webhook_retries_signs_and_keeps_its_id` |
| forged, edited, re-serialised or replayed body | the signature check fails | same |
| ordering | `sequence` strictly increasing | `test_webhook_sequence_and_give_up` |
| receiver never recovers | `failed` after 6 attempts, visible to organizers | same |
| webhook to localhost, 10.x, metadata IP, ftp | refused (SSRF) | `test_webhook_refuses_private_addresses` |
| webhook for a rolled-back change | impossible: written in the same transaction | `test_submission_emits_webhook_in_same_transaction` |
| certificate edited by one character | invalid | `test_certificates_sign_and_verify` |
| certificate file re-formatted | still valid (canonical JSON) | same, via `tools/verify_record.py` |
| retract after certificates | 409 `certificates_issued` (admin can force) | `test_certificates_are_immutable_and_block_retraction` |
| widget framed; hostile `?track=` | only `/embed/*` frameable, no scripts, nothing reflected | `test_widget_is_the_only_frameable_page` |
| export → import → export | identical content | `test_export_import_export_round_trip` |
| 16 kinds of broken import file | 400 and nothing written | `test_invalid_imports_write_nothing` |
| same event imported twice | 409 unless `as_copy` | `test_export_import_export_round_trip` |
| external scripts, fonts or styles | none anywhere (works offline) | `test_no_external_assets_anywhere` |
| cookie write without CSRF | 403 | `test_cross_feature.py::test_cookie_writes_need_csrf` |

## Bugs the tests found during the build

1. The layout template reused the variable name `p`, so every project page lost its title. Found by
   the XSS test in stage 4; now `test_project_page_shows_the_project`.
2. The login limit counted failures but still let a correct password through after the limit.
   Fixed with a check *before* verifying the password; now
   `test_login_lockout_blocks_even_the_right_password`.
3. The rate-limit window freed a slot at exactly 60 s instead of strictly after; now
   `test_sliding_window_edges`.
4. The webhook sender first held one transaction for a whole batch, so one slow receiver stalled all
   others. It now uses one delivery per transaction.
