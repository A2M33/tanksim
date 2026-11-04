"""Drives the HMI in a real browser. Skipped where Playwright is missing:

    pip install playwright && playwright install chromium
"""

import json

import pytest

from tanksim.process import Process
from tanksim.sim import Simulator
from tanksim.web import HmiServer

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        for options in ({}, {"channel": "msedge"}):  # Windows always has Edge
            try:
                browser = p.chromium.launch(headless=True, **options)
                break
            except pw.Error:
                continue
        else:
            pytest.skip("no browser to launch")
        yield browser
        browser.close()


@pytest.fixture
def server():
    srv = HmiServer(Simulator(Process(seed=1)), port=0)
    srv.sim.step(1)
    srv.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def open_page(browser, server, width=1440, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.errors = []
    page.on("console", lambda m: m.type == "error" and page.errors.append(m.text))
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
    return page


@pytest.fixture
def page(browser, server):
    page = open_page(browser, server)
    page.wait_for_function("state !== null")
    yield page
    page.close()
    assert page.errors == []


def next_frame(page):
    """Wait for two snapshots, so the page has seen what the test just did."""
    page.evaluate("""() => new Promise((resolve) => {
        const seen = state;
        const poll = () => {
            if (state !== seen) { resolve(); } else { setTimeout(poll, 20); }
        };
        poll();
    })""")
    page.wait_for_timeout(300)


def test_unacknowledged_alarm_row_really_flashes(page, server):
    server.sim.process.volume_l = server.sim.process.capacity_l * 0.1
    server.sim.step(1)  # transmitter picks up the new level
    server.sim.step(1)  # PLC sees it on the next scan
    page.wait_for_selector("tr.unacked .state-cell")
    cell = page.query_selector("tr.unacked .state-cell")
    colours = page.evaluate("""async (cell) => {
        const seen = new Set();
        for (let i = 0; i < 30; i++) {
            seen.add(getComputedStyle(cell).backgroundColor);
            await new Promise((r) => setTimeout(r, 50));
        }
        return [...seen];
    }""", cell)
    assert len(colours) == 2  # plain and alarm colour, on the same element


def test_controls_stay_locked_until_data_arrives(browser, server):
    page = browser.new_page()
    page.route("**/api/stream", lambda route: None)  # the stream never answers
    posts = []
    page.on("request", lambda r: r.method == "POST" and posts.append(r.url))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('main').inert")
    page.locator("#btn-pause").dispatch_event("click")
    page.locator("#btn-manual").dispatch_event("click")
    page.wait_for_timeout(200)
    assert posts == [] and errors == []
    page.close()


def test_refused_command_is_shown_to_the_operator(page):
    page.route("**/api/command", lambda route: route.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"error": "Switch the pump to manual first"}),
    ))
    page.click("#btn-manual")
    page.wait_for_selector("#cmd-error:not([hidden])")
    assert page.text_content("#cmd-error") == "Switch the pump to manual first"
    page.errors.clear()  # the browser itself logs the 400
    page.unroute("**/api/command")
    page.click("#btn-manual")
    page.wait_for_selector("#cmd-error", state="hidden")


def test_link_loss_locks_the_controls_until_the_stream_is_back(page, server):
    server.stopping.set()  # the stream handler hangs up on every reconnect
    page.wait_for_selector("#link-lost:not([hidden])")
    assert page.evaluate("document.querySelector('main').inert")
    server.stopping.clear()
    page.wait_for_selector("#link-lost", state="hidden", timeout=15000)
    assert not page.evaluate("document.querySelector('main').inert")
    page.errors[:] = [e for e in page.errors if "api/stream" not in e and "ERR_" not in e]
