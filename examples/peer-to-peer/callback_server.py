"""Local callback relay with one active, expiring interaction session."""

import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from threading import Event, Lock

import flask

app = flask.Flask(__name__)
SESSION_LIFETIME = 300


@dataclass
class CallbackSession:
    token: str = field(repr=False)
    expires_at: float
    ready: Event = field(default_factory=Event)
    callback: dict | None = field(default=None, repr=False)
    polling: bool = False


session_lock = Lock()
active_session: CallbackSession | None = None

CALLBACK_HTML = """
<html>
  <body style="font-family: monospace; padding: 2rem; text-align: center;">
    <h1>Callback received</h1>
    <p>Return to your terminal to see the payment result.</p>
  </body>
</html>
"""


def expire_session():
    """Called while holding session_lock; wake a waiter when a session expires."""
    global active_session
    if active_session is not None and time.monotonic() >= active_session.expires_at:
        active_session.ready.set()
        active_session = None


def find_session(token):
    expire_session()
    if (
        active_session is not None
        and token.isascii()
        and hmac.compare_digest(active_session.token, token)
    ):
        return active_session
    return None


@app.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/health")
def health():
    return flask.jsonify({"status": "ok"})


@app.post("/session")
def create_session():
    global active_session
    with session_lock:
        expire_session()
        if active_session is not None:
            return flask.jsonify({"error": "session_in_progress"}), 409
        active_session = CallbackSession(
            token=secrets.token_urlsafe(32),
            expires_at=time.monotonic() + SESSION_LIFETIME,
        )
        return flask.jsonify({"session_id": active_session.token}), 201


@app.get("/callback/<token>")
def callback(token):
    with session_lock:
        session = find_session(token)
        if session is None:
            return flask.jsonify({"error": "unknown_session"}), 404
        if session.callback is not None:
            return flask.jsonify({"error": "callback_already_received"}), 409
        values = {}
        for name in ("interact_ref", "hash"):
            items = flask.request.args.getlist(name)
            if len(items) != 1 or not items[0]:
                return flask.jsonify({"error": "invalid_callback"}), 400
            values[name] = items[0]
        if "error" in flask.request.args:
            return flask.jsonify({"error": "invalid_callback"}), 400
        session.callback = values
        session.ready.set()
    return CALLBACK_HTML


@app.get("/result/<token>")
def result(token):
    global active_session
    with session_lock:
        session = find_session(token)
        if session is None:
            return flask.jsonify({"error": "unknown_session"}), 404
        if session.polling:
            return flask.jsonify({"error": "result_already_requested"}), 409
        session.polling = True
        remaining = max(0, session.expires_at - time.monotonic())
    session.ready.wait(timeout=remaining)
    with session_lock:
        if active_session is not session:
            return flask.jsonify({"error": "unknown_session"}), 404
        active_session = None
        if session.callback is None:
            return flask.jsonify({"error": "timeout"}), 408
        return flask.jsonify(session.callback)


@app.delete("/session/<token>")
def close_session(token):
    global active_session
    with session_lock:
        session = find_session(token)
        if session is None:
            return flask.jsonify({"error": "unknown_session"}), 404
        active_session = None
        session.ready.set()
    return "", 204


if __name__ == "__main__":
    # Callback query strings and session paths contain temporary capabilities.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=3999)
