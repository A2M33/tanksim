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
