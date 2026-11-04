"use strict";

const TANK_BOTTOM = 362;
const TANK_PX_PER_PCT = 3;
const SETPOINTS = [
  ["high_high", "HH", "alarm-high", "HH"],
  ["high", "H", "alarm-medium", "H"],
  ["stop", "Stop", "", null],
  ["start", "Start", "", null],
  ["low", "L", "alarm-medium", "L"],
  ["low_low", "LL", "alarm-high", "LL"],
];
const SETPOINT_NAMES = {
  high_high: "High high alarm",
  high: "High alarm",
  stop: "Pump stop",
  start: "Pump start",
  low: "Low alarm",
  low_low: "Low low alarm",
};
const HIGH_PRIORITY = new Set(["LL", "HH", "PUMP", "SENSOR"]);
const SPEEDS = [1, 5, 20, 60];
const TREND_SPAN_S = 7200;
const SVG_NS = "http://www.w3.org/2000/svg";

const $ = (id) => document.getElementById(id);
let state = null;
let history = [];
const spEdited = new Set(); // setpoints the operator changed and has not applied yet
let spProblem = null; // { message, keys } from the last Apply
let demo = null; // set when the simulator runs in the browser (GitHub Pages)

const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/";
const DEMO_MODULES = ["__init__", "process", "plc", "sim", "commands"];
const DEMO_GLUE = `
import json, sys
sys.path.insert(0, "/home/pyodide")
from tanksim.sim import Simulator
from tanksim.commands import CommandError, apply_command

sim = Simulator()

def step(real_s):
    if not sim.paused:
        sim.step(min(real_s, 1.0) * sim.speed)

def snapshot():
    return json.dumps(sim.snapshot())

def history():
    return json.dumps(sim.history_list())

def command(body):
    try:
        apply_command(sim, json.loads(body))
    except CommandError as err:
        return str(err)
    return ""
`;

// ---- helpers ---------------------------------------------------------

function hhmm(minute) {
  const m = Math.floor(minute) % 1440;
  return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
}

function hhmmss(clock) {
  const s = Math.floor(clock) % 86400;
  return hhmm(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// Rebuild a block only when its data changed. Replacing it on every snapshot
// restarts the alarm flash before it shows, and loses any text selection.
const drawn = new Map();

function changed(block, data) {
  const key = JSON.stringify(data);
  if (drawn.get(block) === key) return false;
  drawn.set(block, key);
  return true;
}

function setText(id, text) {
  const el = $(id);
  if (el.textContent !== String(text)) el.textContent = text;
}

function svg(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}

// Sensor noise around an empty tank must not read -0.0 % or -1 mm.
const unsigned = (value) => Math.max(0, value);

function levelY(pct) {
  return TANK_BOTTOM - Math.max(0, Math.min(100, pct)) * TANK_PX_PER_PCT;
}

async function send(cmd, extra = {}) {
  if (demo) {
    const error = demo.command(JSON.stringify({ cmd, ...extra }));
    if (error) throw new Error(error);
    render(JSON.parse(demo.snapshot()));
    return;
  }
  const res = await fetch("api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, ...extra }),
  }).catch(() => { throw new Error("No connection to tanksim"); });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(body.error);
  }
}

// A button press: the operator sees why it failed, for example when another
// client switched the pump back to auto a moment ago.
function act(cmd, extra) {
  if (!state) return;
  const notice = $("cmd-error");
  send(cmd, extra).then(
    () => { notice.hidden = true; },
    (err) => { notice.textContent = err.message; notice.hidden = false; },
  );
}

// Controls stay locked until live data arrives and while the link is down.
function setLive(live) {
  document.body.classList.toggle("waiting", !live);
  for (const el of document.querySelectorAll("main, .sim")) el.inert = !live;
}

// ---- static parts ----------------------------------------------------

function buildScale() {
  const g = $("scale");
  g.setAttribute("class", "scale");
  for (let pct = 0; pct <= 100; pct += 25) {
    const y = levelY(pct);
    g.append(svg("line", { x1: 520, x2: 528, y1: y, y2: y }));
    g.append(svg("text", { x: 531, y: y + 4 }, String(pct)));
  }
}

function buildSetpointForm() {
  const grid = $("sp-grid");
  for (const [key] of SETPOINTS) {
    const id = "sp-" + key;
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = SETPOINT_NAMES[key];
    const input = document.createElement("input");
    // step "any": Modbus can write 20.3, and that must not make the form invalid.
    Object.assign(input, { id, type: "number", min: 0, max: 100, step: "any" });
    input.dataset.key = key;
    input.addEventListener("input", () => {
      if (spNumber(input) === state.setpoints[key]) spEdited.delete(key);
      else spEdited.add(key);
      if (spProblem) spProblem = setpointProblem(spValues());
      renderSetpointForm(state);
    });
    const live = document.createElement("span");
    live.id = id + "-live";
    live.className = "sp-live";
    input.setAttribute("aria-describedby", live.id);
    grid.append(label, input, live);
  }
}

const spInputs = () => [...$("sp-form").querySelectorAll("input")];
const spNumber = (input) => (input.value === "" ? NaN : Number(input.value));
const spValues = () => Object.fromEntries(spInputs().map((i) => [i.dataset.key, spNumber(i)]));

// Same rules and words as Plc.set_setpoints, which has the last word. Asking
// here first gives the answer while typing, without a failed request.
function setpointProblem(values) {
  const bottomUp = SETPOINTS.map(([key]) => key).reverse();
  for (const key of bottomUp) {
    if (!(values[key] >= 0 && values[key] <= 100)) {
      return { message: `${SETPOINT_NAMES[key]} must be a number from 0 to 100`, keys: [key] };
    }
  }
  for (let i = 0; i + 1 < bottomUp.length; i++) {
    const [lower, upper] = [bottomUp[i], bottomUp[i + 1]];
    if (values[lower] >= values[upper]) {
      return {
        message: `${SETPOINT_NAMES[lower]} (${values[lower]} %) must be below ${SETPOINT_NAMES[upper].toLowerCase()} (${values[upper]} %)`,
        keys: [lower, upper],
      };
    }
  }
  return null;
}

function buildSpeeds() {
  const box = $("speeds");
  for (const s of SPEEDS) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s + "×";
    b.dataset.speed = s;
    b.setAttribute("aria-label", s + " times");
    b.addEventListener("click", () => act("speed", { value: s }));
    box.append(b);
  }
}

// ---- rendering -------------------------------------------------------

function renderMimic(s) {
  const y = levelY(s.level);
  const water = $("tank-water");
  water.setAttribute("y", y);
  water.setAttribute("height", TANK_BOTTOM - y);

  const pump = $("pump");
  const fault = s.alarms.some((a) => a.tag === "PUMP" && a.active);
  pump.classList.toggle("running", s.pump_running);
  pump.classList.toggle("fault", fault);
  $("v-pump").textContent = fault ? "Fault" : s.pump_running ? "Running" : s.pump_cmd ? "Starting" : "Stopped";
  $("v-delay").textContent = s.restart_delay > 0 && !s.pump_cmd ? `Restart allowed in ${Math.ceil(s.restart_delay)} s` : "";

  $("pipe-in").classList.toggle("flowing", s.inflow > 1);
  $("pipe-out").classList.toggle("flowing", s.outflow > 1);
  $("v-inflow").textContent = s.inflow.toFixed(1) + " L/min";
  $("v-outflow").textContent = s.outflow.toFixed(1) + " L/min";
  $("v-level").textContent = unsigned(s.level).toFixed(1) + " %";
  $("v-mm").textContent = Math.round(unsigned(s.level_mm)) + " mm";
  $("v-volume").textContent = Math.round(unsigned(s.capacity_l * s.level / 100)) + " L";
  $("v-overflow").textContent = s.overflow > 0 ? `Overflowing ${s.overflow.toFixed(0)} L/min` : "";

  const active = new Set(s.alarms.filter((a) => a.active).map((a) => a.tag));
  if (!changed("marks", [s.setpoints, [...active]])) return;
  const marks = $("setpoint-marks");
  marks.replaceChildren();
  for (const [key, label, cls, tag] of SETPOINTS) {
    const my = levelY(s.setpoints[key]);
    const g = svg("g", { class: `sp-mark ${cls} ${tag && active.has(tag) ? "active" : ""}` });
    g.append(svg("line", { x1: 362, x2: 518, y1: my, y2: my }));
    g.append(svg("path", { d: `M348 ${my - 5} L358 ${my} L348 ${my + 5} Z` }));
    g.append(svg("text", { x: 344, y: my + 4, "text-anchor": "end" }, label));
    marks.append(g);
  }
}

function renderFaceplate(s) {
  const fault = s.alarms.some((a) => a.tag === "PUMP" && a.active);
  const fp = $("fp-state");
  fp.textContent = fault ? "Fault, reset to restart" : s.pump_running ? "Running" : s.pump_cmd ? "Starting" : "Stopped";
  fp.classList.toggle("fault", fault);

  $("btn-auto").setAttribute("aria-pressed", s.auto);
  $("btn-manual").setAttribute("aria-pressed", !s.auto);
  $("btn-start").disabled = s.auto;
  $("btn-stop").disabled = s.auto;
  $("btn-start").setAttribute("aria-pressed", !s.auto && s.manual_run);
  $("btn-stop").setAttribute("aria-pressed", !s.auto && !s.manual_run);
  $("cmd-hint").textContent = s.auto
    ? "In auto the PLC starts the pump at the start level and stops it at the stop level."
    : "In manual you run the pump. The high high interlock still stops it.";

  $("v-starts").textContent = s.starts;
  const min = Math.floor(s.run_s / 60);
  $("v-runtime").textContent = `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
  $("btn-ack").disabled = !s.alarms.some((a) => !a.acked);
  $("btn-reset").disabled = !s.alarms.some((a) => (a.tag === "PUMP" || a.tag === "SENSOR") && a.active);

  renderSetpointForm(s);
}

// A field the operator changed keeps the entry, shows the value the PLC runs
// on and counts as edited until the PLC reports the same number. The others
// follow the PLC. The tank markers always show the PLC's values.
function renderSetpointForm(s) {
  for (const input of spInputs()) {
    const key = input.dataset.key;
    const live = s.setpoints[key];
    if (spNumber(input) === live) spEdited.delete(key);
    else if (!spEdited.has(key)) input.value = live;
    const edited = spEdited.has(key);
    input.classList.toggle("pending", edited);
    input.setAttribute("aria-invalid", Boolean(spProblem && spProblem.keys.includes(key)));
    setText(input.id + "-live", edited ? `now ${live}` : "");
  }
  if (!spEdited.size) spProblem = null;
  $("sp-apply").disabled = $("sp-discard").disabled = !spEdited.size;
  $("sp-message").classList.toggle("error", Boolean(spProblem));
  setText("sp-message", spProblem
    ? `${spProblem.message}. Nothing was applied.`
    : spEdited.size ? "The PLC keeps its setpoints until you apply the changes." : "");
}

function renderAlarms(s) {
  const summary = $("alarm-summary");
  const n = s.alarms.length;
  const unacked = s.alarms.filter((a) => !a.acked).length;
  const high = s.alarms.some((a) => HIGH_PRIORITY.has(a.tag));
  setText("alarm-summary", n === 0 ? "No alarms" : `${n} alarm${n > 1 ? "s" : ""}` + (unacked ? `, ${unacked} not acknowledged` : ""));
  summary.className = "alarm-summary" + (n ? (high ? " has-high" : " has-medium") : "") + (unacked ? " unacked" : "");

  if (changed("alarms", s.alarms)) {
    const rows = $("alarm-rows");
    rows.replaceChildren();
    const sorted = [...s.alarms].sort((a, b) => b.since - a.since);
    for (const a of sorted) {
      const tr = document.createElement("tr");
      tr.className = [HIGH_PRIORITY.has(a.tag) ? "high" : "medium", a.acked ? "acked" : "unacked", a.active ? "active" : "cleared"].join(" ");
      const stateText = a.active ? (a.acked ? "Active" : "Active, not acknowledged") : "Cleared, not acknowledged";
      for (const [text, cls] of [[hhmmss(a.since)], [a.tag], [a.text], [stateText, "state-cell"]]) {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls) td.className = cls;
        tr.append(td);
      }
      rows.append(tr);
    }
    $("alarm-empty").hidden = n > 0;
  }

  if (changed("events", s.events)) {
    const list = $("events");
    list.replaceChildren();
    for (const [clock, kind, text] of [...s.events].reverse()) {
      const li = document.createElement("li");
      li.className = kind;
      for (const [t, cls] of [[hhmmss(clock), "time"], [kind, "kind"], [text, "text"]]) {
        const span = document.createElement("span");
        span.className = cls;
        span.textContent = t;
        li.append(span);
      }
      list.append(li);
    }
  }
}

function renderSim(s) {
  for (const b of $("speeds").children) b.setAttribute("aria-pressed", Number(b.dataset.speed) === s.sim.speed);
  $("btn-pause").setAttribute("aria-pressed", s.sim.paused);
  $("btn-pause").textContent = s.sim.paused ? "Resume" : "Pause";
  const slider = $("demand");
  if (document.activeElement !== slider) {
    slider.value = Math.round(s.sim.demand_scale * 100);
    $("demand-out").textContent = slider.value + " %";
  }
  const faults = { pump: s.sim.pump_broken, sensor: s.sim.sensor_stuck, leak: s.sim.leak_lpm > 0 };
  for (const b of document.querySelectorAll("[data-fault]")) b.setAttribute("aria-pressed", faults[b.dataset.fault]);
  $("v-true").textContent = s.level_true.toFixed(1) + " %";
}

function drawTrend() {
  const canvas = $("trend");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!state) return;

  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--ink").trim();
  const muted = css.getPropertyValue("--muted").trim();
  const left = 34, right = w - 8, top = 8, bottom = h - 22;
  const now = state.clock;
  const x = (t) => left + (right - left) * (1 - (now - t) / TREND_SPAN_S);
  const y = (pct) => bottom - (bottom - top) * pct / 100;

  ctx.save();
  ctx.font = "11px " + css.getPropertyValue("--font");
  ctx.fillStyle = muted;
  ctx.strokeStyle = "#c3c8cc";
  ctx.lineWidth = 1;
  for (let pct = 0; pct <= 100; pct += 25) {
    ctx.beginPath(); ctx.moveTo(left, y(pct)); ctx.lineTo(right, y(pct)); ctx.stroke();
    ctx.fillText(String(pct), 6, y(pct) + 4);
  }
  const firstTick = Math.ceil((now - TREND_SPAN_S) / 1800) * 1800;
  for (let t = firstTick; t <= now; t += 1800) {
    ctx.beginPath(); ctx.moveTo(x(t), top); ctx.lineTo(x(t), bottom); ctx.stroke();
    ctx.fillText(hhmm(t / 60), x(t) - 14, h - 6);
  }

  ctx.beginPath();
  ctx.rect(left, top, right - left, bottom - top);
  ctx.clip();

  // pump running band along the bottom
  ctx.fillStyle = "#a3abb2";
  for (let i = 1; i < history.length; i++) {
    if (history[i][2] > 1) ctx.fillRect(x(history[i - 1][0]), bottom - 8, x(history[i][0]) - x(history[i - 1][0]) + 0.5, 8);
  }

  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = muted;
  for (const key of ["start", "stop"]) {
    ctx.beginPath(); ctx.moveTo(left, y(state.setpoints[key])); ctx.lineTo(right, y(state.setpoints[key])); ctx.stroke();
  }
  ctx.setLineDash([]);

  ctx.strokeStyle = ink;
  ctx.lineWidth = 2;
  ctx.beginPath();
  history.forEach(([t, level], i) => (i ? ctx.lineTo(x(t), y(level)) : ctx.moveTo(x(t), y(level))));
  ctx.stroke();
  ctx.restore();
}

function setHistory(samples, now) {
  history = samples.filter(([t]) => now - t <= TREND_SPAN_S);
}

async function loadHistory() {
  const res = await fetch("api/history");
  if (!res.ok) throw new Error(res.statusText);
  const samples = await res.json();
  setHistory(samples, samples.length ? samples[samples.length - 1][0] : 0);
}

function render(s) {
  if (!state) setLive(true);
  state = s;
  $("clock").textContent = hhmm(s.minute);
  let last = history[history.length - 1];
  if (last && s.clock < last[0]) {  // a restarted server begins a new day
    history = [];
    last = null;
  }
  if (!last || s.clock - last[0] >= 10) {
    history.push([s.clock, s.level, s.inflow, s.outflow]);
    setHistory(history, s.clock);
  }
  renderMimic(s);
  renderFaceplate(s);
  renderAlarms(s);
  renderSim(s);
  drawTrend();
}

// ---- wiring ----------------------------------------------------------

function wire() {
  $("btn-auto").addEventListener("click", () => act("auto", { value: true }));
  $("btn-manual").addEventListener("click", () => act("auto", { value: false }));
  $("btn-start").addEventListener("click", () => act("manual_run", { value: true }));
  $("btn-stop").addEventListener("click", () => act("manual_run", { value: false }));
  $("btn-ack").addEventListener("click", () => act("ack"));
  $("btn-reset").addEventListener("click", () => act("reset"));
  $("alarm-summary").addEventListener("click", () => $("alarms-title").scrollIntoView({ behavior: "smooth" }));
  $("btn-pause").addEventListener("click", () => act("pause", { value: $("btn-pause").getAttribute("aria-pressed") !== "true" }));

  const slider = $("demand");
  slider.addEventListener("input", () => { $("demand-out").textContent = slider.value + " %"; });
  slider.addEventListener("change", () => act("demand", { value: Number(slider.value) }));

  for (const b of document.querySelectorAll("[data-fault]")) {
    b.addEventListener("click", () => act("fault", { name: b.dataset.fault, value: b.getAttribute("aria-pressed") !== "true" }));
  }

  // Send only what the operator changed, so a value that moved over Modbus
  // in the meantime is not written back.
  $("sp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!spEdited.size) return;
    spProblem = setpointProblem(spValues());
    if (!spProblem) {
      const values = spValues();
      const value = Object.fromEntries([...spEdited].map((key) => [key, values[key]]));
      await send("setpoints", { value }).catch((err) => { spProblem = { message: err.message, keys: [] }; });
    }
    renderSetpointForm(state);
  });

  $("sp-discard").addEventListener("click", () => {
    spEdited.clear();
    spProblem = null;
    renderSetpointForm(state);
  });

  window.addEventListener("resize", drawTrend);
}

async function start() {
  buildScale();
  buildSetpointForm();
  buildSpeeds();
  wire();
  // The Pages build marks itself, so it never probes for a server.
  const server = !document.documentElement.hasAttribute("data-demo")
    && await loadHistory().then(() => true, () => false);
  if (server) {
    const stream = new EventSource("api/stream");
    stream.onmessage = (e) => {
      if (!$("link-lost").hidden) {
        $("link-lost").hidden = true;
        setLive(true);
        loadHistory().catch(() => {});  // fill the gap, keep what we have if it fails
      }
      render(JSON.parse(e.data));
    };
    stream.onerror = () => {
      $("link-lost").hidden = false;
      setLive(false);
    };
  } else {
    await startDemo();
  }
}

// No tanksim server behind this page: run the same Python code in the
// browser with Pyodide. Everything except the Modbus server works.
async function startDemo() {
  const note = $("demo-note");
  note.hidden = false;
  note.textContent = "Loading the simulator into your browser. This takes a few seconds…";
  try {
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = PYODIDE + "pyodide.js";
      script.onload = resolve;
      script.onerror = () => reject(new Error("Could not load Pyodide"));
      document.head.append(script);
    });
    const py = await loadPyodide({ indexURL: PYODIDE });
    py.FS.mkdirTree("/home/pyodide/tanksim");
    for (const name of DEMO_MODULES) {
      const res = await fetch(`py/tanksim/${name}.py`);
      if (!res.ok) throw new Error(`Could not load ${name}.py`);
      py.FS.writeFile(`/home/pyodide/tanksim/${name}.py`, await res.text());
    }
    const ns = py.globals.get("dict")();
    py.runPython(DEMO_GLUE, { globals: ns });
    demo = {
      step: ns.get("step"),
      snapshot: ns.get("snapshot"),
      command: ns.get("command"),
    };
    history = JSON.parse(ns.get("history")());
  } catch (err) {
    note.textContent = `The demo could not start: ${err.message}. Install tanksim with pip to run it locally.`;
    return;
  }
  note.innerHTML = 'Live demo: the PLC program runs in your browser. ' +
    '<a href="https://github.com/A2M33/tanksim#run-it">Install tanksim</a> to get the Modbus TCP server too.';

  let last = performance.now();
  setInterval(() => {
    const now = performance.now();
    demo.step((now - last) / 1000);
    last = now;
  }, 100);
  setInterval(() => render(JSON.parse(demo.snapshot())), 250);
}

start();
