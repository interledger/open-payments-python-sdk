"""Browser interaction state and callback correlation without Selenium or I/O."""

from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit, urlunsplit

import pytest

from tests.process import test_process as browser

CALLBACK = "https://app.example/callback/01ARZ3NDEKTSV4RRFFQ69G5FAV"
REFERENCE = "opaque/reference"
HASH = "synthetic+callback/hash=="
SETTINGS = SimpleNamespace(
    TEST_BUYER_LOGIN="synthetic-login", TEST_BUYER_PASSWORD="synthetic-password"
)


def callback_url(base=CALLBACK, parameters=None):
    parts = urlsplit(base)
    result = urlencode(
        parameters
        if parameters is not None
        else [("hash", HASH), ("interact_ref", REFERENCE)]
    )
    query = parts.query + ("&" if parts.query else "") + result
    return urlunsplit(parts._replace(query=query))


@pytest.mark.parametrize(
    "expected, response",
    [
        (CALLBACK, callback_url()),
        ("https://APP.EXAMPLE:443/callback/01ARZ3NDEKTSV4RRFFQ69G5FAV", callback_url()),
        (
            "http://localhost:8080/finish/id",
            callback_url("http://localhost:8080/finish/id"),
        ),
        ("com.example.app:/finish/id", callback_url("com.example.app:/finish/id")),
        (
            "https://app.example/finish?tenant=%2f&payment=id",
            callback_url("https://app.example/finish?tenant=%2f&payment=id"),
        ),
        (CALLBACK + "#client-state", callback_url(CALLBACK + "#client-state")),
    ],
)
def test_callback_matches_transaction_path_query_and_fragment(expected, response):
    assert browser.interaction_callback(response, expected) == (REFERENCE, HASH)


@pytest.mark.parametrize(
    "response",
    [
        callback_url("https://other.example/callback/01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        callback_url(CALLBACK.replace("https:", "http:")),
        callback_url(CALLBACK + "/different"),
        callback_url(CALLBACK + "#different"),
        CALLBACK,
        callback_url(parameters=[("hash", HASH)]),
        "/relative",
        None,
        {},
    ],
)
def test_unrelated_or_incomplete_callbacks_are_not_accepted(response):
    assert browser.interaction_callback(response, CALLBACK) is None


def test_callback_cannot_replace_saved_query_state():
    expected = "https://app.example/finish?payment=original"
    response = callback_url("https://app.example/finish?payment=other")
    assert browser.interaction_callback(response, expected) is None


@pytest.mark.parametrize(
    "parameters",
    [
        [("hash", ""), ("interact_ref", REFERENCE)],
        [("hash", HASH), ("interact_ref", "")],
        [("hash", HASH), ("hash", "DO-NOT-PRINT-extra"), ("interact_ref", REFERENCE)],
        [
            ("hash", HASH),
            ("interact_ref", REFERENCE),
            ("interact_ref", "DO-NOT-PRINT-extra"),
        ],
    ],
)
def test_callback_rejects_blank_or_competing_protocol_values(parameters):
    with pytest.raises(
        ValueError, match="Invalid interaction callback parameters"
    ) as caught:
        browser.interaction_callback(callback_url(parameters=parameters), CALLBACK)
    assert "DO-NOT-PRINT" not in str(caught.value) + repr(caught.value.args)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert 0 <= seconds <= 0.2
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(browser, "time", value)
    return value


class Element:
    def __init__(self, driver, role):
        self.driver, self.role = driver, role

    def is_displayed(self):
        return True

    def is_enabled(self):
        if self.role in ("accept", "decline") and self.driver.delay:
            self.driver.delay -= 1
            return False
        return True

    def send_keys(self, value):
        self.driver.actions.append(("keys", self.role, value))

    def click(self):
        self.driver.actions.append(("click", self.role))
        if self.driver.uncertain == self.role:
            raise RuntimeError("Synthetic uncertain browser click")
        if self.role == "submit":
            self.driver.stage = "consent"
        else:
            self.driver.stage = "waiting" if self.driver.never_callback else "callback"


class Driver:
    def __init__(self, *, login=True, delay=0, never_callback=False, uncertain=None):
        self.stage = "login" if login else "consent"
        self.delay, self.never_callback, self.uncertain = (
            delay,
            never_callback,
            uncertain,
        )
        self.actions, self.opened, self.page_timeouts, self.implicit_timeouts = (
            [],
            [],
            [],
            [],
        )

    def set_page_load_timeout(self, value):
        self.page_timeouts.append(value)

    def implicitly_wait(self, value):
        self.implicit_timeouts.append(value)

    def get(self, value):
        self.opened.append(value)

    @property
    def current_url(self):
        return (
            callback_url()
            if self.stage == "callback"
            else "https://auth.example/" + self.stage
        )

    def find_elements(self, by, value):
        if self.stage == "login":
            roles = {
                ("name", "email"): "email",
                ("name", "password"): "password",
                ("css selector", "button[type='submit']"): "submit",
            }
        elif self.stage == "consent":
            roles = {
                ("css selector", "button[aria-label='accept']"): "accept",
                ("css selector", "button[aria-label='decline']"): "decline",
            }
        else:
            roles = {}
        role = roles.get((by, value))
        return [Element(self, role)] if role else []


@pytest.mark.parametrize("login", [False, True])
@pytest.mark.parametrize("accept", [False, True])
def test_browser_waits_for_the_expected_callback_and_clicks_each_action_once(
    clock, login, accept
):
    driver = Driver(login=login)
    response = browser.interactive_payment_approval(
        "https://auth.example/interaction",
        driver,
        SETTINGS,
        accept,
        callback_uri=CALLBACK,
        timeout=1,
    )
    assert response == callback_url()
    assert driver.opened == ["https://auth.example/interaction"]
    assert driver.page_timeouts == [1.0] and driver.implicit_timeouts == [0]
    clicks = [action for action in driver.actions if action[0] == "click"]
    expected = [("click", "submit")] if login else []
    expected.append(("click", "accept" if accept else "decline"))
    assert clicks == expected
    keys = [action for action in driver.actions if action[0] == "keys"]
    assert keys == (
        [
            ("keys", "email", SETTINGS.TEST_BUYER_LOGIN),
            ("keys", "password", SETTINGS.TEST_BUYER_PASSWORD),
        ]
        if login
        else []
    )


def test_disabled_consent_is_waited_for_without_resubmitting_login(clock):
    driver = Driver(delay=2)
    browser.interactive_payment_approval(
        "https://auth.example/interaction",
        driver,
        SETTINGS,
        callback_uri=CALLBACK,
        timeout=1,
    )
    assert len(clock.sleeps) == 2
    assert driver.actions.count(("click", "submit")) == 1
    assert driver.actions.count(("click", "accept")) == 1


def test_missing_callback_times_out_without_replaying_consent(clock):
    driver = Driver(login=False, never_callback=True)
    with pytest.raises(TimeoutError, match="expected interaction callback"):
        browser.interactive_payment_approval(
            "https://auth.example/interaction",
            driver,
            SETTINGS,
            callback_uri=CALLBACK,
            timeout=1,
        )
    assert clock.now == 1 and driver.actions == [("click", "accept")]


@pytest.mark.parametrize("role", ["submit", "accept"])
def test_uncertain_browser_clicks_are_not_retried(clock, role):
    driver = Driver(uncertain=role)
    with pytest.raises(RuntimeError, match="uncertain browser click"):
        browser.interactive_payment_approval(
            "https://auth.example/interaction",
            driver,
            SETTINGS,
            callback_uri=CALLBACK,
            timeout=1,
        )
    assert driver.actions.count(("click", role)) == 1


@pytest.mark.parametrize(
    "timeout", [True, False, 0, -1, float("inf"), float("nan"), "1", None, 10**400]
)
def test_invalid_timeouts_fail_before_browser_navigation(clock, timeout):
    driver = Driver()
    with pytest.raises(ValueError, match="positive finite interaction timeout"):
        browser.interactive_payment_approval(
            "https://auth.example/interaction",
            driver,
            SETTINGS,
            callback_uri=CALLBACK,
            timeout=timeout,
        )
    assert not driver.opened and not driver.actions


@pytest.mark.parametrize(
    "callback", [None, "/relative", {}, "https://app.example/DO-NOT-PRINT%ZZ"]
)
def test_invalid_callback_configuration_fails_before_browser_navigation(
    clock, callback
):
    driver = Driver()
    with pytest.raises(ValueError, match="valid callback URI") as caught:
        browser.interactive_payment_approval(
            "https://auth.example/interaction",
            driver,
            SETTINGS,
            callback_uri=callback,
            timeout=1,
        )
    assert not driver.opened and not driver.actions
    assert "DO-NOT-PRINT" not in str(caught.value) + repr(caught.value.args)
