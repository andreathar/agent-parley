"""Checks bounded operator replies without issuing an operator credential."""

import json
import threading
from pathlib import Path

import httpx
import pytest

from agent_parley import cli, roster, server, store
from agent_parley.state import BridgeError


@pytest.fixture
def exchange(bridge, repo, paired):
    store.initialize(bridge.home)
    registrations = {
        name: store.register(bridge.home, paired["root"], name)
        for name in ("claude", "codex")
    }
    actors = {
        name: store.authenticate(bridge.home, item["registration_token"])
        for name, item in registrations.items()
    }
    request = bridge.say(repo, "claude", "Read one local reference.", ack=True)
    return {
        "root": paired["root"],
        "actors": actors,
        "tokens": registrations,
        "request": request,
        "lane": Path(paired["lanes"]["claude"]),
    }


def reply(exchange, **extra):
    return {
        "to": [roster.OPERATOR],
        "subject": "Diagnostic result",
        "body_md": "Read-only reference checked.",
        "idempotency_key": "diagnostic-reply",
        "reply_to": exchange["request"]["id"],
        **extra,
    }


@pytest.mark.parametrize("correlation", ["reply_to", "thread_id"])
def test_authenticated_recipient_returns_private_mail_without_operator_token(
    bridge, repo, exchange, correlation
):
    args = reply(exchange)
    if correlation == "thread_id":
        args.pop("reply_to")
        args["thread_id"] = exchange["request"]["thread_id"]
    sent = store.call(
        bridge.home, exchange["actors"]["claude"], "send_message", args
    )
    assert sent["thread_id"] == exchange["request"]["thread_id"]
    assert "feed" not in sent
    page = bridge.mail(
        repo, "thread", thread=sent["thread_id"], participant=roster.OPERATOR
    )
    assert [m["id"] for m in page["messages"]] == [
        exchange["request"]["id"],
        sent["id"],
    ]
    assert page["messages"][-1]["sender"] == "claude"
    assert bridge.mail(repo, "list", participant=roster.OPERATOR)["messages"]
    with store.connect(bridge.home) as db:
        assert (
            db.execute(
                "SELECT token_digest FROM agents WHERE name=?",
                (roster.OPERATOR,),
            ).fetchone()[0]
            is None
        )
        assert db.execute(
            "SELECT read_ts,ack_ts FROM message_recipients WHERE message_id=?",
            (exchange["request"]["id"],),
        ).fetchone()[:] == (None, None)
    assert (
        store.call(
            bridge.home,
            exchange["actors"]["codex"],
            "read_thread",
            {"thread_id": sent["thread_id"]},
        )["messages"]
        == []
    )
    assert (
        store.search_decisions(bridge.home, exchange["root"], "codex")[
            "messages"
        ]
        == []
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"reply_to": None},
        {"ack_required": True},
        {"decision": True},
        {"to": [roster.OPERATOR, "codex"]},
        {"to": [roster.OPERATOR, roster.OPERATOR]},
    ],
)
def test_operator_exception_does_not_allow_unsolicited_or_expanded_mail(
    bridge, exchange, extra
):
    args = reply(exchange, **extra)
    if args["reply_to"] is None:
        args.pop("reply_to")
    with pytest.raises(BridgeError, match="operator"):
        store.call(
            bridge.home, exchange["actors"]["claude"], "send_message", args
        )
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_other_lane_cannot_claim_a_known_operator_thread(bridge, exchange):
    args = reply(exchange)
    args.pop("reply_to")
    args["thread_id"] = exchange["request"]["thread_id"]
    with pytest.raises(BridgeError, match="operator"):
        store.call(
            bridge.home, exchange["actors"]["codex"], "send_message", args
        )


def test_same_lane_name_in_other_project_cannot_answer_operator_thread(
    bridge, exchange
):
    other = store.register(bridge.home, "/other", "claude")
    actor = store.authenticate(bridge.home, other["registration_token"])
    store.speak(bridge.home, "/other", "claude", "Other", "Body", "other")
    args = reply(exchange)
    args.pop("reply_to")
    args["thread_id"] = exchange["request"]["thread_id"]
    with pytest.raises(BridgeError, match="operator"):
        store.call(bridge.home, actor, "send_message", args)


def test_peer_mail_does_not_authorize_an_operator_reply(bridge, exchange):
    peer = store.call(
        bridge.home,
        exchange["actors"]["codex"],
        "send_message",
        {
            "to": ["claude"],
            "subject": "Peer request",
            "body_md": "Private peer body.",
            "idempotency_key": "peer",
        },
    )
    with pytest.raises(BridgeError, match="operator"):
        store.call(
            bridge.home,
            exchange["actors"]["claude"],
            "send_message",
            reply(exchange, reply_to=peer["id"]),
        )


def test_public_operator_decision_does_not_authorize_private_reply(
    bridge, repo, exchange
):
    decision = bridge.decide(repo, "Shared routing decision.")
    args = reply(exchange)
    args.pop("reply_to")
    args["thread_id"] = decision["thread_id"]
    with pytest.raises(BridgeError, match="operator"):
        store.call(
            bridge.home, exchange["actors"]["claude"], "send_message", args
        )


def test_concurrent_replies_with_one_key_store_one_message(bridge, exchange):
    results = []
    failures = []
    barrier = threading.Barrier(2)

    def send():
        try:
            barrier.wait(timeout=5)
            results.append(
                store.call(
                    bridge.home,
                    exchange["actors"]["claude"],
                    "send_message",
                    reply(exchange),
                )
            )
        except Exception as error:
            failures.append(error)

    workers = [threading.Thread(target=send) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not failures
    assert len(results) == 2
    assert len({item["id"] for item in results}) == 1
    assert sum(bool(item.get("duplicate")) for item in results) == 1


def test_operator_reply_cannot_route_to_feed_even_with_base_topic(
    bridge, exchange
):
    sent = store.call(
        bridge.home,
        exchange["actors"]["claude"],
        "send_message",
        reply(exchange, topic=store.BASE_TOPIC),
    )
    assert "feed" not in sent
    assert store.feed(bridge.home, exchange["root"])["items"] == []
    assert (
        store.read_message(
            bridge.home, exchange["root"], roster.OPERATOR, sent["id"]
        )["body_md"]
        == "Read-only reference checked."
    )


def test_expired_ack_and_reconnection_preserve_one_operator_reply(
    bridge, exchange
):
    request = exchange["request"]
    with store.connect(bridge.home, write=True) as db:
        db.execute(
            "UPDATE messages SET ack_deadline_ts=datetime('now','-60 seconds'),"
            "ack_retired_ts=CURRENT_TIMESTAMP WHERE id=?",
            (request["id"],),
        )
    old_token = exchange["tokens"]["claude"]["registration_token"]
    store.revoke(bridge.home, exchange["root"], "claude")
    assert store.authenticate(bridge.home, old_token) is None
    new = store.register(bridge.home, exchange["root"], "claude")
    actor = store.authenticate(bridge.home, new["registration_token"])
    sent = store.call(bridge.home, actor, "send_message", reply(exchange))
    again = store.call(bridge.home, actor, "send_message", reply(exchange))
    assert again["duplicate"] is True
    assert again["id"] == sent["id"]
    with store.connect(bridge.home) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT ack_ts FROM message_recipients WHERE message_id=?",
                (request["id"],),
            ).fetchone()[0]
            is None
        )
        assert (
            db.execute(
                "SELECT ack_retired_ts FROM messages WHERE id=?",
                (request["id"],),
            ).fetchone()[0]
            is not None
        )
    with pytest.raises(BridgeError, match="already names"):
        store.call(
            bridge.home,
            actor,
            "send_message",
            reply(exchange, body_md="Different result."),
        )


def test_retired_lane_is_still_refused_as_operator_reply_recipient(
    bridge, exchange
):
    store.revoke(bridge.home, exchange["root"], "codex")
    with pytest.raises(BridgeError, match="cannot receive mail"):
        store.call(
            bridge.home,
            exchange["actors"]["claude"],
            "send_message",
            reply(exchange, to=["codex"]),
        )


def test_operator_cannot_be_registered_or_impersonated_by_a_lane_cli(
    bridge, repo, exchange, monkeypatch
):
    with pytest.raises(BridgeError, match="cannot.*registered"):
        store.register(bridge.home, exchange["root"], roster.OPERATOR)
    monkeypatch.setenv(
        "AGENT_PARLEY_TOKEN",
        exchange["tokens"]["claude"]["registration_token"],
    )
    with pytest.raises(BridgeError, match="operator authority"):
        bridge.decide(exchange["lane"], "Not an operator decision.")
    with pytest.raises(BridgeError, match="operator authority"):
        bridge.mail(repo, "list", participant=roster.OPERATOR)
    monkeypatch.delenv("AGENT_PARLEY_TOKEN")
    assert bridge.decide(repo, "Real operator decision.")["decision"] is True


def test_native_mcp_transport_authenticates_reply_and_operator_cli_reads_it(
    bridge, repo, exchange, monkeypatch, capsys
):
    with server.Server(bridge.home, {"token": "health", "port": 0}) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            response = httpx.post(
                f"http://127.0.0.1:{service.server_port}/mcp/",
                headers={
                    "Authorization": "Bearer "
                    + exchange["tokens"]["claude"]["registration_token"]
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "send_message",
                        "arguments": reply(exchange),
                    },
                },
                trust_env=False,
            )
            unauthenticated = httpx.post(
                f"http://127.0.0.1:{service.server_port}/mcp/",
                json={},
                trust_env=False,
            )
        finally:
            service.shutdown()
            thread.join(timeout=2)
    result = response.json()["result"]
    assert not result.get("isError")
    assert unauthenticated.status_code == 401
    sent = json.loads(result["content"][0]["text"])
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-parley",
            "--home",
            str(bridge.home),
            "mail",
            "thread",
            sent["thread_id"],
            "--as",
            roster.OPERATOR,
            "--repo",
            str(repo),
            "--json",
        ],
    )
    cli.main()
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "mail_thread"
    assert shown["messages"][-1]["id"] == sent["id"]
