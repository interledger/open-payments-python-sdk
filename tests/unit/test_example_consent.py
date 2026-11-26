"""Offline payment gating and the optional local callback relay."""

import copy
import importlib.util
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import AnyUrl

ROOT = Path(__file__).parents[2]
FLOW = json.loads((ROOT / "tests/fixtures/peer_to_peer.json").read_text())["responses"]
CLIENT_NONCE = "VJLO6A4CATR0KRO"
INTERACT_REF = "4IFWWIKYB2PQ6U56NL1"
CALLBACK_HASH = "x+gguKWTj8rQf7d7i3w3UhzvuJ5bpOlKyAlVpLxBffY="
SESSION_ID = "A" * 43
SERVER_URL = "http://callback.example"


def load_example(name, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sdk_example_" + name, ROOT / "examples/peer-to-peer" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def response(status, data, method, url):
    return httpx.Response(status, json=data, request=httpx.Request(method, url))


@pytest.fixture
def example(monkeypatch, tmp_path, keyid_private_key, op_client, http_mock):
    module = load_example("run", monkeypatch)
    monkeypatch.setattr(module, "load_environment", lambda: None)
    key = tmp_path / "unregistered-test-key.pem"
    key.write_text(keyid_private_key["private_key"])
    for name, value in {
        "OP_PRIVATE_KEY_PATH": str(key),
        "OP_KEY_ID": keyid_private_key["keyid"],
        "OP_WALLET_ADDRESS_URL": op_client.client_wallet_address,
        "SENDING_WALLET_ADDRESS_URL": FLOW[0]["json"]["id"],
        "RECEIVING_WALLET_ADDRESS_URL": FLOW[1]["json"]["id"],
        "CALLBACK_SERVER_URL": SERVER_URL,
    }.items():
        monkeypatch.setenv(name, value)
    constructor = Mock(return_value=op_client)
    monkeypatch.setattr(module, "OpenPaymentsClient", constructor)
    nonce = Mock(return_value=CLIENT_NONCE)
    monkeypatch.setattr(module, "secrets", SimpleNamespace(token_urlsafe=nonce))
    browser = Mock()
    monkeypatch.setattr(module.webbrowser, "open", browser)
    context = SimpleNamespace(
        module=module,
        http=http_mock,
        browser=browser,
        nonce=nonce,
        constructor=constructor,
        local_calls=[],
        result={"interact_ref": INTERACT_REF, "hash": CALLBACK_HASH},
        wallets=[],
    )
    original_get = op_client.wallet.get_wallet_address

    def get_wallet(url):
        result = original_get(url)
        context.wallets.append(result)
        return result

    monkeypatch.setattr(op_client.wallet, "get_wallet_address", get_wallet)

    def post(url, **kwargs):
        assert url == SERVER_URL + "/session"
        context.local_calls.append(("POST", url))
        return response(201, {"session_id": SESSION_ID}, "POST", url)

    def get(url, **kwargs):
        assert url == SERVER_URL + "/result/" + SESSION_ID
        context.local_calls.append(("GET", url))
        return response(200, context.result, "GET", url)

    def delete(url, **kwargs):
        assert url == SERVER_URL + "/session/" + SESSION_ID
        context.local_calls.append(("DELETE", url))
        return response(204, None, "DELETE", url)

    monkeypatch.setattr(module.httpx, "post", post)
    monkeypatch.setattr(module.httpx, "get", get)
    monkeypatch.setattr(module.httpx, "delete", delete)

    def prepare(count=9, overrides=None):
        items = copy.deepcopy(FLOW[:count])
        for index, value in (overrides or {}).items():
            items[index] = value
        http_mock.responses.extend(
            httpx.Response(item["status"], json=item["json"]) for item in items
        )

    context.prepare = prepare
    return context


def test_verified_example_completes_the_nine_request_flow_without_logging_credentials(
    example, caplog
):
    example.prepare()
    caplog.set_level(logging.INFO)

    payment = example.module.main()

    assert str(payment.id) == FLOW[-1]["json"]["id"]
    assert len(example.http.requests) == 9
    example.nonce.assert_called_once_with(32)
    finish = json.loads(example.http.requests[6].content)["interact"]["finish"]
    assert finish["uri"] == "http://localhost:3999/callback/" + SESSION_ID
    assert finish["nonce"] == CLIENT_NONCE
    assert example.local_calls == [
        ("POST", SERVER_URL + "/session"),
        ("GET", SERVER_URL + "/result/" + SESSION_ID),
        ("DELETE", SERVER_URL + "/session/" + SESSION_ID),
    ]
    assert (
        example.http.requests[7].headers["authorization"]
        == "GNAP example-continuation-secret"
    )
    assert (
        example.http.requests[8].headers["authorization"]
        == "GNAP example-outgoing-secret"
    )
    for value in (
        "example-incoming-secret",
        "example-quote-secret",
        "example-continuation-secret",
        "example-outgoing-secret",
        CLIENT_NONCE,
        CALLBACK_HASH,
        "MBDOFXG4Y5CVJCX821LH",
        "Q2FsbGJhY2tFeGFtcGxlU2VjcmV0",
    ):
        assert value not in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        {"interact_ref": INTERACT_REF},
        {"interact_ref": INTERACT_REF, "hash": None},
        {"interact_ref": INTERACT_REF, "hash": ""},
        {"interact_ref": INTERACT_REF, "hash": "malformed!"},
        {"interact_ref": INTERACT_REF, "hash": "A" * 43 + "="},
        {"interact_ref": INTERACT_REF, "hash": CALLBACK_HASH.replace("+", " ")},
        {"hash": CALLBACK_HASH},
        {"interact_ref": "wrong", "hash": CALLBACK_HASH},
        {"error": "denied", "interact_ref": INTERACT_REF, "hash": CALLBACK_HASH},
        [],
    ],
)
def test_invalid_example_callbacks_never_continue_or_create_outgoing(example, result):
    example.prepare(7)
    example.result = result

    with pytest.raises(ValueError, match="callback"):
        example.module.main()

    assert len(example.http.requests) == 7
    assert example.local_calls[-1] == ("DELETE", SERVER_URL + "/session/" + SESSION_ID)


def test_example_verifies_the_original_endpoint_after_browser_interaction(example):
    example.prepare()
    example.browser.side_effect = lambda _: setattr(
        example.wallets[0], "authServer", AnyUrl("https://new-auth.example/grants")
    )

    assert str(example.module.main().id) == FLOW[-1]["json"]["id"]
    assert str(example.http.requests[6].url) == "https://server.example.com/tx"


def test_example_does_not_create_outgoing_after_continuation_failure(example):
    example.prepare(8, {7: {"status": 500, "json": {"error": "uncertain"}}})

    with pytest.raises(httpx.HTTPStatusError):
        example.module.main()

    assert len(example.http.requests) == 8
    assert example.local_calls[-1][0] == "DELETE"


def test_registration_conflict_does_not_close_someone_elses_session(
    example, monkeypatch
):
    example.prepare(6)
    monkeypatch.setattr(
        example.module.httpx,
        "post",
        lambda url, **kwargs: response(
            409, {"error": "session_in_progress"}, "POST", url
        ),
    )
    cleanup = Mock()
    monkeypatch.setattr(example.module.httpx, "delete", cleanup)

    with pytest.raises(httpx.HTTPStatusError):
        example.module.main()

    cleanup.assert_not_called()
    example.browser.assert_not_called()
    assert len(example.http.requests) == 6


@pytest.mark.parametrize(
    "data", [{}, {"session_id": "../other"}, {"session_id": 42}, []]
)
def test_malformed_session_responses_never_start_an_interactive_grant(
    example, monkeypatch, data
):
    example.prepare(6)
    monkeypatch.setattr(
        example.module.httpx,
        "post",
        lambda url, **kwargs: response(201, data, "POST", url),
    )

    with pytest.raises(ValueError, match="session"):
        example.module.main()

    example.browser.assert_not_called()
    assert len(example.http.requests) == 6
    assert not example.local_calls


def test_cleanup_error_does_not_hide_a_created_payment_or_log_capabilities(
    example, monkeypatch, caplog
):
    example.prepare()
    monkeypatch.setattr(
        example.module.httpx,
        "delete",
        Mock(side_effect=httpx.ConnectError("private capability " + SESSION_ID)),
    )
    caplog.set_level(logging.INFO)

    assert str(example.module.main().id) == FLOW[-1]["json"]["id"]
    assert "cleanup failed" in caplog.text
    assert SESSION_ID not in caplog.text


def test_cli_reports_failure_without_printing_response_credentials(example, caplog):
    bad = copy.deepcopy(FLOW[2])
    del bad["json"]["access_token"]["manage"]
    example.prepare(3, {2: bad})
    caplog.set_level(logging.INFO)

    assert example.module.run_cli() == 1
    assert "ValidationError" in caplog.text
    assert "example-incoming-secret" not in caplog.text
    assert len(example.http.requests) == 3


@pytest.fixture
def relay(monkeypatch):
    pytest.importorskip(
        "flask",
        reason="Install optional example dependencies: poetry install --with examples",
    )
    module = load_example("callback_server", monkeypatch)
    module.app.config.update(TESTING=True)
    yield module
    with module.session_lock:
        if module.active_session is not None:
            module.active_session.ready.set()
            module.active_session = None


class TestCallbackServer:
    def session(self, relay):
        result = relay.app.test_client().post("/session")
        assert result.status_code == 201
        return result.get_json()["session_id"]

    def callback(self, client, token, **query):
        values = {"interact_ref": INTERACT_REF, "hash": CALLBACK_HASH, **query}
        return client.get("/callback/" + token, query_string=values)

    def test_session_callback_and_result_are_single_use(self, relay):
        token = self.session(relay)
        client = relay.app.test_client()
        assert self.callback(client, token).status_code == 200
        assert self.callback(client, token).status_code == 409
        result = client.get("/result/" + token)
        assert result.get_json() == {
            "interact_ref": INTERACT_REF,
            "hash": CALLBACK_HASH,
        }
        assert result.headers["Cache-Control"] == "no-store"
        assert result.headers["Referrer-Policy"] == "no-referrer"
        assert client.get("/result/" + token).status_code == 404
        assert self.callback(client, token).status_code == 404
        replacement = self.session(relay)
        assert replacement != token
        assert self.callback(client, token).status_code == 404

    def test_second_session_cannot_replace_the_first(self, relay):
        token = self.session(relay)
        client = relay.app.test_client()
        assert client.post("/session").status_code == 409
        assert self.callback(client, token).status_code == 200
        assert client.get("/result/" + token).status_code == 200

    @pytest.mark.parametrize(
        "query",
        [
            [("interact_ref", INTERACT_REF)],
            [("hash", CALLBACK_HASH)],
            [("interact_ref", INTERACT_REF), ("hash", "")],
            [
                ("interact_ref", INTERACT_REF),
                ("hash", CALLBACK_HASH),
                ("hash", CALLBACK_HASH),
            ],
            [
                ("interact_ref", INTERACT_REF),
                ("interact_ref", INTERACT_REF),
                ("hash", CALLBACK_HASH),
            ],
            [
                ("interact_ref", INTERACT_REF),
                ("hash", CALLBACK_HASH),
                ("error", "denied"),
            ],
        ],
    )
    def test_invalid_queries_do_not_consume_the_session(self, relay, query):
        token = self.session(relay)
        client = relay.app.test_client()
        assert client.get("/callback/" + token, query_string=query).status_code == 400
        assert self.callback(client, token).status_code == 200
        assert client.get("/result/" + token).status_code == 200

    def test_other_sessions_and_unbound_routes_are_rejected(self, relay):
        token = self.session(relay)
        client = relay.app.test_client()
        for url in (
            "/",
            "/result",
            "/callback/wrong",
            "/result/wrong",
            "/callback/\u00e9",
        ):
            assert client.get(url).status_code == 404
        assert client.delete("/session/wrong").status_code == 404
        assert self.callback(client, token).status_code == 200

    def test_expired_sessions_are_rejected_and_replaced(self, relay, monkeypatch):
        clock = {"now": 1.0}
        monkeypatch.setattr(
            relay, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        token = self.session(relay)
        clock["now"] += relay.SESSION_LIFETIME + 1
        client = relay.app.test_client()
        assert self.callback(client, token).status_code == 404
        assert client.get("/result/" + token).status_code == 404
        assert self.session(relay) != token

    def test_result_timeout_releases_the_session(self, relay, monkeypatch):
        token = self.session(relay)
        monkeypatch.setattr(relay.active_session.ready, "wait", lambda timeout: False)
        client = relay.app.test_client()
        assert client.get("/result/" + token).status_code == 408
        assert self.callback(client, token).status_code == 404
        assert self.session(relay) != token

    @pytest.mark.parametrize("cancel", [False, True])
    def test_one_waiter_gets_the_callback_or_cancellation(
        self, relay, monkeypatch, cancel
    ):
        token = self.session(relay)
        entered = Event()
        original_wait = relay.active_session.ready.wait

        def wait(timeout):
            entered.set()
            return original_wait(timeout=2)

        monkeypatch.setattr(relay.active_session.ready, "wait", wait)
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                lambda: relay.app.test_client().get("/result/" + token)
            )
            assert entered.wait(timeout=1)
            client = relay.app.test_client()
            assert client.get("/result/" + token).status_code == 409
            if cancel:
                assert client.delete("/session/" + token).status_code == 204
                assert result.result(timeout=3).status_code == 404
            else:
                assert self.callback(client, token).status_code == 200
                assert result.result(timeout=3).get_json()["hash"] == CALLBACK_HASH

    def test_full_example_uses_the_real_callback_routes(
        self, relay, example, monkeypatch
    ):
        client = relay.app.test_client()

        def local(method, url, **kwargs):
            path = urlsplit(url).path
            result = client.open(path, method=method)
            return response(
                result.status_code, result.get_json(silent=True), method, url
            )

        monkeypatch.setattr(
            example.module.httpx,
            "post",
            lambda url, **kwargs: local("POST", url, **kwargs),
        )
        monkeypatch.setattr(
            example.module.httpx,
            "get",
            lambda url, **kwargs: local("GET", url, **kwargs),
        )
        monkeypatch.setattr(
            example.module.httpx,
            "delete",
            lambda url, **kwargs: local("DELETE", url, **kwargs),
        )

        def browser_callback(_):
            finish = json.loads(example.http.requests[-1].content)["interact"]["finish"]
            assert (
                client.get(
                    urlsplit(finish["uri"]).path,
                    query_string={"interact_ref": INTERACT_REF, "hash": CALLBACK_HASH},
                ).status_code
                == 200
            )

        example.browser.side_effect = browser_callback
        example.prepare()
        assert str(example.module.main().id) == FLOW[-1]["json"]["id"]
        assert len(example.http.requests) == 9
        assert relay.active_session is None
