// Draw callout traces the way a PCB router would: a horizontal run from the label,
// a 45-degree leg, then an optional straight tail into the part it names.
// Points can be numbers or a CSS selector (the centre of that element is used).
// Usage: traces([[from, to, {lit, tail}], ...]) where from/to are [x, y] or "selector".
function centre(p) {
  if (Array.isArray(p)) return p;
  const r = document.querySelector(p).getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2];
}
function traces(list) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "traces");
  svg.setAttribute("viewBox", "0 0 1280 640");
  for (const [from, to, opt = {}] of list) {
    const [x1, y1] = centre(from), [x2, y2] = centre(to);
    const tail = opt.tail || 0, ty = y2 - Math.sign(y2 - y1) * tail;
    const dy = ty - y1, dx = x2 - x1;
    const run = Math.sign(dx) * Math.max(0, Math.abs(dx) - Math.abs(dy));
    const g = document.createElementNS(ns, "g");
    if (opt.lit) g.setAttribute("class", "lit");
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", `M${x1} ${y1} H${x1 + run} L${x2} ${ty} V${y2}`);
    g.append(p);
    for (const [x, y, r, cls] of [[x1, y1, 3, "start"], [x2, y2, opt.r ?? 5, "end"]]) {
      if (!r) continue;
      const c = document.createElementNS(ns, "circle");
      c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", r); c.setAttribute("class", cls);
      g.append(c);
    }
    svg.append(g);
  }
  document.querySelector(".sheet").append(svg);
}
