"""Reads bounded mail metadata without interpreting silence as completion.

The mailbox records review, reading and retirement of ACK expectations. It
does not record native dispatch receipts, acceptance of a task, or terminal
reports correlated to a message. Those facts remain unknown. A read timestamp
may come from a checkpoint, an explicit read, or an operator ACK; it therefore
cannot prove that a native agent accepted a request. This reader never reads
message content, credentials or native session transcripts, nor retries mail.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import time
from pathlib import Path

from agent_parley import roster, store
from agent_parley.state import BridgeError

MAX_MESSAGES = 100
ACK_SEMANTICS = "explicit_review_not_task_acceptance"


def _base(recipient: str) -> dict:
    """Creates the metadata fields whose evidence is absent in this protocol."""
    return {
        "recipient": recipient,
        "recipient_id": None,
        "message_id": None,
        "schedule_id": None,
        "thread_id": None,
        "claim_id": None,
        "sender": None,
        "read_ts": None,
        "ack_ts": None,
        "ack_deadline_ts": None,
        "ack_retired_ts": None,
        "cancelled_ts": None,
        "ack_semantics": ACK_SEMANTICS,
        "dispatch_state": "unknown",
        "task_state": "unknown",
        "session_generation": None,
        "delivery_uncertain": True,
        "late_ack": False,
    }


def _message(row: sqlite3.Row, now: float) -> dict:
    """Derives review and expiration only from this recipient's stored facts."""
    result = _base(row["recipient"])
    result.update(dict(row))
    result.update(
        id=f"message:{row['message_id']}",
        evidence="message_recipients",
        ack_required=bool(row["ack_required"]),
        mailbox_state="read" if row["read_ts"] is not None else "unread",
    )
    deadline = row["ack_deadline_ts"]
    ack = row["ack_ts"]
    retired = row["ack_retired_ts"]
    result["late_ack"] = ack is not None and (
        (deadline is not None and ack > deadline)
        or (retired is not None and ack > retired)
    )
    expired = (
        deadline is not None
        and now > deadline
        and (ack is None or result["late_ack"])
    ) or (retired is not None and (ack is None or result["late_ack"]))
    result["ack_state"] = (
        "expired"
        if expired
        else "reviewed"
        if ack is not None
        else "pending"
        if row["ack_required"]
        else "not_requested"
    )
    result["status"] = (
        "cancelled"
        if row["cancelled_ts"] is not None
        else "expired"
        if expired
        else "acknowledged"
        if ack is not None
        else "unknown"
        if row["read_ts"] is not None
        else "queued"
    )
    return result


def _schedule_names(
    home: Path, root: str, routing_name: str
) -> tuple[str, ...]:
    """Maps the registered routing identity to its existing stable lane key."""
    directory = roster.locate(home, root)
    if directory is None:
        return (routing_name,)
    manifest = roster.read(directory)
    return tuple(
        name
        for name, participant in manifest["participants"].items()
        if participant["display"] == routing_name
    )


def _schedules(
    db: sqlite3.Connection, root: str, names: tuple[str, ...], recipient: str
) -> list[dict]:
    """Reads unmailed scheduled items and persisted cancellation outcomes."""
    if not names:
        return []
    marks = ",".join("?" for _ in names)
    rows = db.execute(
        "SELECT s.id AS schedule_id,s.created_ts AS queued_ts,"
        "s.not_before,s.cancelled_ts,s.ack_required,"
        "(s.cancelled_reason IS NOT NULL) AS refused "
        "FROM scheduled_deliveries s JOIN projects p ON p.id=s.project_id "
        f"WHERE p.human_key=? AND s.recipient IN ({marks}) "
        "AND s.kind='message' AND s.delivered_ts IS NULL "
        "ORDER BY s.id DESC LIMIT ?",
        (root, *names, MAX_MESSAGES),
    )
    result = []
    for row in rows:
        item = _base(recipient)
        item.update(
            id=f"schedule:{row['schedule_id']}",
            schedule_id=row["schedule_id"],
            queued_ts=row["queued_ts"],
            not_before=row["not_before"],
            cancelled_ts=row["cancelled_ts"],
            ack_required=bool(row["ack_required"]),
            ack_state="not_yet_requested",
            status=(
                "failed"
                if row["refused"]
                else "cancelled"
                if row["cancelled_ts"] is not None
                else "queued"
            ),
            mailbox_state="not_enqueued",
            evidence="scheduled_deliveries",
        )
        result.append(item)
    return result


def messages(
    home: Path,
    project_root: str,
    routing_name: str,
    *,
    now: float | None = None,
) -> list[dict]:
    """Returns the newest persisted mail facts for one recipient.

    Args:
        home: Existing private coordination state root.
        project_root: Exact canonical project identity stored in the database.
        routing_name: Registered mailbox identity, distinct from editable UI
            labels. Scheduled items use the existing roster's stable lane key.
        now: Unix clock used to evaluate ACK deadlines; current time by default.

    Returns:
        At most MAX_MESSAGES metadata records, newest first. Mailbox IDs and
        thread IDs remain unchanged across reconnections. ACK means explicit
        review of coordination mail, not task acceptance. Deadline expiry is
        preserved after retirement and late ACKs cannot erase it. No terminal
        result is inferred from replies, reports, presence or zero pending ACKs.

    Raises:
        BridgeError: If the store cannot be read, or the clock is invalid.
    """
    stamp = time.time() if now is None else now
    if not math.isfinite(stamp):
        raise BridgeError("Mail history requires a finite Unix clock.")
    path = home / store.DATABASE
    if not path.exists():
        return []
    try:
        names = _schedule_names(home, project_root, routing_name)
        with contextlib.closing(
            sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5
            )
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT m.id AS message_id,m.thread_id,m.claim_id,"
                "a.id AS recipient_id,a.name AS recipient,s.name AS sender,"
                "unixepoch(m.created_ts) AS queued_ts,m.ack_required,"
                "unixepoch(r.read_ts) AS read_ts,"
                "unixepoch(r.ack_ts) AS ack_ts,"
                "unixepoch(m.ack_deadline_ts) AS ack_deadline_ts,"
                "unixepoch(m.ack_retired_ts) AS ack_retired_ts,"
                "unixepoch(r.superseded_ts) AS cancelled_ts "
                "FROM messages m JOIN message_recipients r "
                "ON r.message_id=m.id JOIN agents a ON a.id=r.agent_id "
                "JOIN agents s ON s.id=m.sender_id "
                "JOIN projects p ON p.id=m.project_id "
                "WHERE p.human_key=? AND a.name=? "
                "ORDER BY m.id DESC LIMIT ?",
                (project_root, routing_name, MAX_MESSAGES),
            ).fetchall()
            result = [
                *(_message(row, stamp) for row in rows),
                *_schedules(db, project_root, names, routing_name),
            ]
    except (OSError, sqlite3.Error) as exc:
        raise BridgeError(
            "Mail history unavailable; check state access and schema with "
            f"agent-parley doctor: {exc}"
        ) from exc
    return sorted(
        result,
        key=lambda item: (
            item["queued_ts"] or 0,
            item["message_id"] or item["schedule_id"] or 0,
        ),
        reverse=True,
    )[:MAX_MESSAGES]
