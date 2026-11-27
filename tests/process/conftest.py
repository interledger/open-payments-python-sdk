import pytest


@pytest.fixture
def browser_driver(request, live_settings):
    if not request.config.getoption("--run-browser"):
        pytest.skip("Requires --run-live --run-browser")
    required = (
        "TEST_BUYER_WALLET",
        "TEST_BUYER_LOGIN",
        "TEST_BUYER_PASSWORD",
        "TEST_REDIRECT_URI",
    )
    missing = [name for name in required if not getattr(live_settings, name)]
    if missing:
        pytest.skip("Browser account configuration missing: " + ", ".join(missing))
    webdriver = pytest.importorskip(
        "selenium.webdriver",
        reason="Install browser test dependencies with poetry install --with live,browser",
    )
    driver = webdriver.Chrome()
    try:
        yield driver
    finally:
        driver.quit()
