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


def test_trend_keeps_running_when_the_plant_clock_goes_back(page, server):
    server.sim.step(120)
    next_frame(page)
    server.sim.process.clock_s -= 3600  # what a restarted server looks like
    server.sim.step(30)
    next_frame(page)
    newest, now = page.evaluate("[history[history.length - 1][0], state.clock]")
    assert newest <= now
    server.sim.step(30)
    next_frame(page)
    assert page.evaluate("history[history.length - 1][0]") > newest


MARKER_Y = "[...document.querySelectorAll('.sp-mark text')].map((t) => t.getAttribute('y'))"


def test_refused_setpoint_stays_visible_and_says_why(page, server):
    before = page.evaluate(MARKER_Y)
    posts = []
    page.on("request", lambda r: r.method == "POST" and posts.append(r.url))
    page.fill("#sp-start", "85")
    page.click("text=Apply setpoints")
    assert page.text_content("#sp-message") == (
        "Pump start (85 %) must be below pump stop (80 %). Nothing was applied."
    )
    assert posts == []  # refused in the page, so no failed request in the console
    next_frame(page)
    assert page.input_value("#sp-start") == "85"  # the entry survives the next snapshot
    assert page.text_content("#sp-start-live") == "now 20"
    assert page.get_attribute("#sp-start", "aria-invalid") == "true"
    assert page.get_attribute("#sp-stop", "aria-invalid") == "true"
    assert page.get_attribute("#sp-low", "aria-invalid") == "false"
    assert page.evaluate(MARKER_Y) == before  # the markers show what the PLC runs on
    assert server.sim.plc.setpoints["start"] == 20

    page.fill("#sp-stop", "88")  # fixing it clears the error while typing
    assert page.text_content("#sp-message") == "The PLC keeps its setpoints until you apply the changes."
    page.click("text=Discard changes")
    assert page.input_value("#sp-start") == "20"
    assert page.input_value("#sp-stop") == "80"
    assert page.text_content("#sp-message") == ""
    assert page.is_disabled("#sp-apply")


def test_applied_setpoint_moves_the_tank_marker(page, server):
    before = page.evaluate(MARKER_Y)
    page.fill("#sp-start", "30")
    page.click("text=Apply setpoints")
    page.wait_for_function(f"{MARKER_Y}.join() !== {json.dumps(','.join(before))}")
    assert server.sim.plc.setpoints["start"] == 30
    assert page.input_value("#sp-start") == "30"
    assert page.text_content("#sp-start-live") == ""
    assert page.is_disabled("#sp-apply")


def test_apply_does_not_undo_a_value_written_by_another_client(page, server):
    page.fill("#sp-start", "25")
    server.sim.plc.set_setpoint("stop", 70)
    next_frame(page)
    assert page.input_value("#sp-stop") == "70"  # untouched fields stay live
    assert page.input_value("#sp-start") == "25"
    page.click("text=Apply setpoints")
    page.wait_for_function("state.setpoints.start === 25")
    assert server.sim.plc.setpoints["stop"] == 70


def test_setpoint_from_modbus_with_tenths_keeps_the_form_usable(page, server):
    server.sim.write_holding_registers(2, [203])  # 20.3 %
    next_frame(page)
    assert page.input_value("#sp-start") == "20.3"
    page.fill("#sp-stop", "70")
    page.click("text=Apply setpoints")
    page.wait_for_function("state.setpoints.stop === 70")


def test_empty_setpoint_is_explained_in_the_form(page):
    page.fill("#sp-start", "")
    page.click("text=Apply setpoints")
    assert page.text_content("#sp-message") == "Pump start must be a number from 0 to 100. Nothing was applied."


def test_empty_tank_does_not_read_negative(page, server):
    process = server.sim.process
    process.sensor_stuck = True
    process._sensor = -0.04
    server.sim.step(1)
    next_frame(page)
    assert page.text_content("#v-level") == "0.0 %"
    assert page.text_content("#v-mm") == "0 mm"
    assert page.text_content("#v-volume") == "0 L"


def test_phone_layout_is_readable_and_fits(browser, server):
    page = open_page(browser, server, width=375, height=812)
    page.wait_for_function("state !== null")
    server.sim.step(1)
    next_frame(page)
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    assert page.is_visible("#m-level")
    assert not page.is_visible("#v-level")
    assert page.text_content("#m-level") == page.text_content("#v-level")
    smallest = page.evaluate("""() => {
        const svg = document.getElementById("mimic");
        const scale = svg.getBoundingClientRect().width / 660;
        const sizes = [...svg.querySelectorAll("text")]
            .filter((t) => t.textContent && getComputedStyle(t).display !== "none"
                && t.closest("g.readout, .value") === null)
            .map((t) => parseFloat(getComputedStyle(t).fontSize) * scale);
        return Math.min(...sizes);
    }""")
    assert smallest >= 9
    heights = page.evaluate(
        "[...document.querySelectorAll('button')].filter((b) => b.offsetParent)"
        ".map((b) => b.getBoundingClientRect().height)"
    )
    assert min(heights) >= 44
    assert page.errors == []
    page.close()


def test_faceplate_says_why_the_pump_is_held_off(page, server):
    page.click("#btn-manual")
    page.click("#btn-start")
    process = server.sim.process
    process.volume_l = process.capacity_l * 0.97
    server.sim.step(1)
    server.sim.step(1)
    page.wait_for_function("document.getElementById('cmd-hint').textContent.includes('interlock holds')")
    assert page.is_disabled("#btn-start")
    assert page.get_attribute("#btn-start", "aria-pressed") == "false"


def test_simulator_explains_a_fault_the_plc_cannot_see_yet(page):
    page.click("text=Break the pump")
    page.wait_for_function("document.getElementById('sim-hint').textContent !== ''")
    assert "next start" in page.text_content("#sim-hint")


def test_paused_plant_says_so_in_the_header(page):
    page.click("#btn-pause")
    page.wait_for_function("document.getElementById('clock').textContent.endsWith('paused')")


def test_no_state_is_claimed_before_data_arrives(browser, server):
    page = browser.new_page()
    page.route("**/api/stream", lambda route: None)  # the stream never answers
    page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
    page.wait_for_timeout(300)
    assert page.text_content("#alarm-summary") == "No data yet"
    assert page.text_content("#fp-state") == "--"
    page.close()
