"""Tests conservative mailbox evidence using disposable persisted state."""

import json
import sqlite3

import pytest

from agent_parley import mail_evidence, store
from agent_parley.state import BridgeError

ROOT = "/synthetic/project"
NAME = "opencode-routing"


@pytest.fixture
def mailbox(tmp_path):
    home = tmp_path / "mail state"
    home.mkdir()
    store.initialize(home)
    store.register(home, ROOT, NAME)
    store.register(home, ROOT, "other-routing")
    return home


def send(home, *, name=NAME, key="logical-request", ack=True):
    sent = store.speak(
        home, ROOT, name, "Private subject", "Private body", key, ack=ack
    )
    with store.connect(home, write=True) as db:
        db.execute(
            "UPDATE messages SET created_ts=datetime(100,'unixepoch'),"
            "ack_deadline_ts=? WHERE id=?",
            ("1970-01-01 00:03:20" if ack else None, sent["id"]),
        )
    return sent["id"]


def recipient(home, identifier, *, read=None, ack=None, cancelled=None):
    with store.connect(home, write=True) as db:
        db.execute(
            "UPDATE message_recipients SET read_ts=datetime(?,'unixepoch'),"
            "ack_ts=datetime(?,'unixepoch'),"
            "superseded_ts=datetime(?,'unixepoch') "
            "WHERE message_id=? AND agent_id=(SELECT id FROM agents "
            "WHERE name=?)",
            (read, ack, cancelled, identifier, NAME),
        )


def evidence(home, now=150):
    return mail_evidence.messages(home, ROOT, NAME, now=now)


def test_disconnection_before_delivery_preserves_queued_identity(mailbox):
    identifier = send(mailbox)
    before = evidence(mailbox)[0]
    store.revoke(mailbox, ROOT, NAME)
    store.register(mailbox, ROOT, NAME)
    assert evidence(mailbox)[0] == before
    assert before["id"] == f"message:{identifier}"
    with store.connect(mailbox) as db:
        thread = db.execute(
            "SELECT thread_id FROM messages WHERE id=?", (identifier,)
        ).fetchone()[0]
    assert before["thread_id"] == thread
    assert before["status"] == "queued"
    assert before["ack_state"] == "pending"
    assert before["session_generation"] is None


def test_read_does_not_prove_dispatch_or_task_acceptance(mailbox):
    identifier = send(mailbox)
    recipient(mailbox, identifier, read=120)
    before = evidence(mailbox)[0]
    store.revoke(mailbox, ROOT, NAME)
    store.register(mailbox, ROOT, NAME)
    assert evidence(mailbox)[0] == before
    assert before["status"] == "unknown"
    assert before["read_ts"] == 120
    assert before["dispatch_state"] == "unknown"
    assert before["task_state"] == "unknown"
    assert before["delivery_uncertain"]


def test_missing_ack_expires_at_fake_clock_and_survives_retirement(mailbox):
    send(mailbox)
    assert evidence(mailbox, now=200)[0]["ack_state"] == "pending"
    assert evidence(mailbox, now=201)[0]["status"] == "expired"
    identifier = evidence(mailbox)[0]["message_id"]
    assert store.retire_acknowledgement(mailbox, ROOT, identifier)
    item = evidence(mailbox, now=201)[0]
    assert not item["ack_required"]
    assert item["ack_ts"] is None
    assert item["ack_state"] == "expired"
    assert item["ack_deadline_ts"] == 200


@pytest.mark.parametrize("read,ack", [(130, 120), (120, 130)])
def test_out_of_order_review_is_acknowledged_without_completion(
    mailbox, read, ack
):
    identifier = send(mailbox)
    recipient(mailbox, identifier, read=read, ack=ack)
    item = evidence(mailbox, now=250)[0]
    assert item["status"] == "acknowledged"
    assert item["ack_state"] == "reviewed"
    assert item["ack_semantics"] == "explicit_review_not_task_acceptance"
    assert item["task_state"] == "unknown"
    assert not item["late_ack"]


def test_duplicate_ack_retains_original_timestamp(mailbox):
    identifier = send(mailbox)
    with store.connect(mailbox) as db:
        actor = dict(
            db.execute("SELECT * FROM agents WHERE name=?", (NAME,)).fetchone()
        )
    store.call(
        mailbox, actor, "acknowledge_message", {"message_id": identifier}
    )
    recipient(mailbox, identifier, read=110, ack=120)
    store.call(
        mailbox, actor, "acknowledge_message", {"message_id": identifier}
    )
    assert evidence(mailbox)[0]["ack_ts"] == 120


def test_late_ack_keeps_expiration_visible(mailbox):
    identifier = send(mailbox)
    recipient(mailbox, identifier, read=120, ack=201)
    item = evidence(mailbox, now=250)[0]
    assert item["ack_state"] == "expired"
    assert item["status"] == "expired"
    assert item["late_ack"]
    assert item["ack_ts"] == 201


def test_retirement_without_deadline_retains_expiration_after_late_ack(
    mailbox,
):
    identifier = send(mailbox)
    with store.connect(mailbox, write=True) as db:
        db.execute(
            "UPDATE messages SET ack_required=0,ack_deadline_ts=NULL,"
            "ack_retired_ts=datetime(200,'unixepoch') WHERE id=?",
            (identifier,),
        )
    recipient(mailbox, identifier, ack=201)
    assert evidence(mailbox, now=250)[0]["status"] == "expired"


def test_replies_and_unrelated_terminal_reports_cannot_complete_mail(mailbox):
    send(mailbox)
    store.speak(
        mailbox, ROOT, NAME, "completed", "Done message 1", "reply", ack=False
    )
    (mailbox / "opencode-routing-reports.jsonl").write_text(
        json.dumps({"kind": "report", "state": "ready", "at": 140}) + "\n"
    )
    assert all(item["task_state"] == "unknown" for item in evidence(mailbox))
    assert all(item["status"] != "completed" for item in evidence(mailbox))


def test_schedule_persists_across_simulated_restart_then_enqueues_once(
    mailbox,
):
    pending = store.schedule(
        mailbox,
        ROOT,
        {
            "recipient": NAME,
            "dedup_key": "schedule-key",
            "subject": "Private",
            "body_md": "Private body",
        },
    )
    before = evidence(mailbox)
    store.initialize(mailbox)
    assert evidence(mailbox) == before
    assert before[0]["id"] == f"schedule:{pending['id']}"
    store.deliver_schedule(mailbox, ROOT, pending["id"], NAME)
    store.deliver_schedule(mailbox, ROOT, pending["id"], NAME)
    assert len(evidence(mailbox)) == 1
    assert evidence(mailbox)[0]["message_id"] is not None
    assert evidence(mailbox)[0]["dispatch_state"] == "unknown"


def test_cancelled_and_failed_are_only_persisted_evidence(mailbox):
    cancelled = store.schedule(mailbox, ROOT, {"recipient": NAME})
    store.cancel_schedule(mailbox, ROOT, cancelled["id"])
    refused = store.schedule(
        mailbox, ROOT, {"recipient": NAME, "dedup_key": "refusal"}
    )
    store.revoke(mailbox, ROOT, NAME)
    store.deliver_schedule(mailbox, ROOT, refused["id"], NAME)
    states = {item["schedule_id"]: item["status"] for item in evidence(mailbox)}
    assert states == {cancelled["id"]: "cancelled", refused["id"]: "failed"}


def test_superseded_recipient_does_not_disappear(mailbox):
    identifier = send(mailbox)
    recipient(mailbox, identifier, cancelled=120)
    item = evidence(mailbox)[0]
    assert item["message_id"] == identifier
    assert item["status"] == "cancelled"
    assert item["ack_ts"] is None


def test_messages_are_isolated_per_recipient_and_project(mailbox):
    identifier = send(mailbox)
    send(mailbox, name="other-routing", key="other")
    store.register(mailbox, "/other/project", NAME)
    store.speak(mailbox, "/other/project", NAME, "Private", "Body", "other")
    assert [item["message_id"] for item in evidence(mailbox)] == [identifier]
    assert mail_evidence.messages(mailbox, ROOT, "absent", now=150) == []


def test_metadata_is_bounded_and_never_contains_content_or_credentials(
    mailbox,
):
    for number in range(mail_evidence.MAX_MESSAGES + 5):
        send(mailbox, key=f"key-{number}")
    items = evidence(mailbox)
    assert len(items) == mail_evidence.MAX_MESSAGES
    serialized = json.dumps(items)
    for private in ("Private", "body_md", "token", "subject", "dedup_key"):
        assert private not in serialized
    assert items[0]["message_id"] == mail_evidence.MAX_MESSAGES + 5


def test_roster_lane_and_routing_identity_share_schedule_history(
    mailbox, monkeypatch
):
    directory = mailbox / "projects" / "fixture"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(json.dumps({"root": ROOT}))
    participant = {
        "display": NAME,
        "name": "Operator chosen label",
        "role": "reviewer",
    }
    monkeypatch.setattr(
        mail_evidence.roster,
        "read",
        lambda directory: {"participants": {"stable-lane": participant}},
    )
    identifier = send(mailbox)
    store.schedule(mailbox, ROOT, {"recipient": "stable-lane"})
    before = evidence(mailbox)
    participant["name"] = "Renamed label"
    participant["role"] = "implementer"
    assert evidence(mailbox) == before
    assert len(before) == 2
    assert identifier in {item["message_id"] for item in before}


def test_reader_never_queries_message_content_or_token_columns(
    mailbox, monkeypatch
):
    send(mailbox)
    connect = sqlite3.connect
    forbidden = {"body_md", "subject", "token_digest", "dedup_key"}

    def protected(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and column in forbidden:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(mail_evidence.sqlite3, "connect", protected)
    assert evidence(mailbox)[0]["status"] == "queued"


def test_schedule_identity_alias_cannot_include_another_lanes_queue(
    mailbox, monkeypatch
):
    directory = mailbox / "projects" / "fixture"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(json.dumps({"root": ROOT}))
    monkeypatch.setattr(
        mail_evidence.roster,
        "read",
        lambda directory: {
            "participants": {
                "stable-lane": {"display": NAME},
                NAME: {"display": "other-routing"},
            }
        },
    )
    intended = store.schedule(mailbox, ROOT, {"recipient": "stable-lane"})
    store.schedule(mailbox, ROOT, {"recipient": NAME})
    assert [item["schedule_id"] for item in evidence(mailbox)] == [
        intended["id"]
    ]


def test_read_only_reader_cannot_create_or_mutate_database(mailbox, tmp_path):
    send(mailbox)
    before = (mailbox / store.DATABASE).read_bytes()
    evidence(mailbox)
    assert (mailbox / store.DATABASE).read_bytes() == before
    absent = tmp_path / "absent"
    assert mail_evidence.messages(absent, ROOT, NAME) == []
    assert not absent.exists()


def test_unusable_store_and_nonfinite_clock_report_actionable_errors(mailbox):
    with sqlite3.connect(mailbox / store.DATABASE) as db:
        db.execute("DROP TABLE message_recipients")
    with pytest.raises(BridgeError, match="agent-parley doctor"):
        evidence(mailbox)
    with pytest.raises(BridgeError, match="finite Unix clock"):
        evidence(mailbox, now=float("nan"))
