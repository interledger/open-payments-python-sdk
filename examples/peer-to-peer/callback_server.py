"""
Callback server for the Open Payments interactive grant flow.

The auth server redirects the user's browser here after grant approval,
appending `interact_ref` and `hash` as query parameters.

Routes:
  GET /        – receives the redirect and serves a success page to the browser
  GET /result  – long-polls until the callback arrives, then returns
                 interact_ref and hash as JSON (consumed by run.py)
"""

import queue

import flask

app = flask.Flask(__name__)
result_queue: queue.Queue = queue.Queue(maxsize=1)

CALLBACK_HTML = """
<html>
  <body style="font-family: monospace; padding: 2rem; text-align: center;">
    <img src="https://raw.githubusercontent.com/interledger/open-payments/main/docs/public/img/logo.svg"
         width="300" alt="Open Payments" style="max-width: 100%; margin-bottom: 2rem;">
    <h1>Authentication successful</h1>
    <p>You can close this window and return to your terminal.</p>
  </body>
</html>
"""


@app.route("/health")
def health():
    return flask.jsonify({"status": "ok"})


@app.route("/")
def callback():
    result_queue.put({
        "interact_ref": flask.request.args.get("interact_ref"),
        "hash": flask.request.args.get("hash"),
    })
    return CALLBACK_HTML


@app.route("/result")
def result():
    try:
        data = result_queue.get(timeout=300)
        return flask.jsonify(data)
    except queue.Empty:
        return flask.jsonify({"error": "timeout"}), 408


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3999)
