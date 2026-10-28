"use strict";

// Charts of ports' rates over time, drawn in the conversation when the model calls
// chart. Each chart asks the server for itself again whenever the poller has had
// time for new samples, so it stays live while it is on the page.
//
// Each port takes the next of six colors in a fixed order (--series-1 to -6 in
// app.css), so a port keeps its color as the chart updates. In is a solid line and
// out a dashed one. Hovering shows every line's value at the nearest poll, and a
// table under the chart holds the same numbers, for anyone who can't hover or can't
// tell the colors apart.
//
// Device data reaches the chart only as text: through textContent and attributes.

const netchart = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const HEIGHT = 200;
  const MARGIN = { top: 12, right: 14, bottom: 24, left: 46 };
  const SLOTS = 6;
  // Charts on the page, each with the data it last drew and when it last asked for more.
  const live = new Set();

  function element(tag, className, text) {
    const made = document.createElement(tag);
    if (className) made.className = className;
    if (text !== undefined) made.textContent = text;
    return made;
  }

  function shape(tag, className, attributes = {}) {
    const made = document.createElementNS(SVG, tag);
    if (className) made.setAttribute("class", className);
    for (const [name, value] of Object.entries(attributes)) made.setAttribute(name, value);
    return made;
  }

  // ---- Numbers and times

  function niceStep(span, count) {
    const raw = span / count;
    const power = 10 ** Math.floor(Math.log10(raw));
    const fraction = raw / power;
    return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power;
  }

  function number(value) {
    if (Math.abs(value) >= 1000) return `${Number((value / 1000).toPrecision(3))}K`;
    return String(Number(value.toPrecision(3)));
  }

  function clock(seconds, withSeconds) {
    const options = { hour: "2-digit", minute: "2-digit", hour12: false };
    if (withSeconds) options.second = "2-digit";
    return new Date(seconds * 1000).toLocaleTimeString([], options);
  }

  function timeTicks(start, end) {
    const span = end - start;
    const steps = [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
    const step = steps.find((candidate) => span / candidate <= 5) || 3600;
    const ticks = [];
    for (let tick = Math.ceil(start / step) * step; tick <= end; tick += step) ticks.push(tick);
    return { ticks, withSeconds: step < 60 };
  }

  // ---- Drawing

  function slot(chart, port) {
    return (chart.ports.indexOf(port) % SLOTS) + 1;
  }

  function lineKey(chart, line) {
    const key = shape("svg", "key", { viewBox: "0 0 18 6", width: "18", height: "6", "aria-hidden": "true" });
    key.append(shape("line", `line s${slot(chart, line.port)} ${line.direction}`, { x1: "1", y1: "3", x2: "17", y2: "3" }));
    return key;
  }

  function draw(entry) {
    const { figure, chart } = entry;
    const plot = figure.querySelector(".chart-plot");
    const width = Math.max(plot.clientWidth || 600, 280);
    const svg = shape("svg", "chart-svg", { viewBox: `0 0 ${width} ${HEIGHT}`, role: "img" });
    svg.setAttribute("aria-label", `${chart.title}, ${chart.unit}`);
    const inner = { left: MARGIN.left, right: width - MARGIN.right, top: MARGIN.top, bottom: HEIGHT - MARGIN.bottom };

    const points = chart.lines.flatMap((line) => line.points);
    if (!points.length) {
      svg.append(shape("text", "chart-empty", { x: String(width / 2), y: String(HEIGHT / 2) }));
      svg.lastChild.textContent = "No rates yet: waiting for the next poll.";
      plot.replaceChildren(svg);
      return;
    }
    const end = Math.max(...points.map(([time]) => time));
    const start = Math.max(Math.min(...points.map(([time]) => time)), end - chart.minutes * 60);
    const span = Math.max(end - start, chart.interval);
    const peak = Math.max(...points.map(([, value]) => value));
    const step = peak > 0 ? niceStep(peak, 4) : 1;
    const top = peak > 0 ? Math.ceil(peak / step) * step : 1;
    const x = (time) => inner.right - ((end - time) / span) * (inner.right - inner.left);
    const y = (value) => inner.bottom - (value / top) * (inner.bottom - inner.top);

    const grid = shape("g", "grid");
    for (let tick = 0; tick <= top + step / 2; tick += step) {
      grid.append(shape("line", "", { x1: String(inner.left), x2: String(inner.right), y1: String(y(tick)), y2: String(y(tick)) }));
      const label = shape("text", "tick y", { x: String(inner.left - 6), y: String(y(tick)) });
      label.textContent = number(tick);
      grid.append(label);
    }
    const { ticks, withSeconds } = timeTicks(end - span, end);
    for (const tick of ticks) {
      const label = shape("text", "tick x", { x: String(x(tick)), y: String(HEIGHT - 6) });
      label.textContent = clock(tick, withSeconds);
      grid.append(label);
    }
    svg.append(grid);

    const marks = shape("g", "marks");
    for (const line of chart.lines) {
      const shown = line.points.filter(([time]) => time >= end - span);
      if (!shown.length) continue;
      const path = shown.map(([time, value], index) => `${index ? "L" : "M"}${x(time).toFixed(1)},${y(value).toFixed(1)}`);
      marks.append(shape("path", `line s${slot(chart, line.port)} ${line.direction}`, { d: path.join("") }));
      const [time, value] = shown[shown.length - 1];
      marks.append(shape("circle", `end s${slot(chart, line.port)}`, { cx: String(x(time)), cy: String(y(value)), r: "4" }));
    }
    svg.append(marks);

    // The crosshair snaps to the nearest poll, and the readout lists every line there.
    const crosshair = shape("line", "crosshair", { y1: String(inner.top), y2: String(inner.bottom) });
    crosshair.setAttribute("visibility", "hidden");
    const target = shape("rect", "hover-target", {
      x: String(inner.left),
      y: String(inner.top),
      width: String(inner.right - inner.left),
      height: String(inner.bottom - inner.top),
    });
    svg.append(crosshair, target);
    const readout = figure.querySelector(".chart-readout");
    const times = [...new Set(points.map(([time]) => time))].sort((a, b) => a - b);
    target.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const at = end - ((inner.right - (event.clientX - box.left) * (width / box.width)) / (inner.right - inner.left)) * span;
      const nearest = times.reduce((best, time) => (Math.abs(time - at) < Math.abs(best - at) ? time : best));
      crosshair.setAttribute("x1", String(x(nearest)));
      crosshair.setAttribute("x2", String(x(nearest)));
      crosshair.setAttribute("visibility", "visible");
      showReadout(readout, chart, nearest, withSeconds || chart.interval < 60);
      const left = (x(nearest) / width) * box.width;
      readout.classList.toggle("flip", left > box.width / 2);
      readout.style.left = `${plot.offsetLeft + left}px`;
    });
    target.addEventListener("pointerleave", () => {
      crosshair.setAttribute("visibility", "hidden");
      readout.hidden = true;
    });
    plot.replaceChildren(svg);
  }

  function showReadout(readout, chart, time, withSeconds) {
    const rows = [element("div", "when", clock(time, withSeconds))];
    for (const line of chart.lines) {
      // A device's poll lands a moment before or after another's; within half an interval is the same poll.
      const near = line.points.find(([at]) => Math.abs(at - time) <= chart.interval / 2);
      if (!near) continue;
      const row = element("div", "row");
      row.append(lineKey(chart, line), element("strong", "", number(near[1])), element("span", "", ` ${line.port} ${line.direction}`));
      rows.push(row);
    }
    readout.replaceChildren(...rows);
    readout.hidden = false;
  }

  function legend(chart) {
    const items = element("div", "chart-legend");
    for (const port of chart.ports) {
      const item = element("span", "item");
      item.append(lineKey(chart, { port, direction: "in" }), document.createTextNode(port));
      items.append(item);
    }
    const ways = element("span", "item ways");
    ways.append(lineKey({ ports: [] }, { port: "", direction: "in" }), document.createTextNode("in"));
    ways.append(lineKey({ ports: [] }, { port: "", direction: "out" }), document.createTextNode("out"));
    items.append(ways);
    return items;
  }

  function table(chart) {
    const data = element("details", "chart-data");
    data.append(element("summary", "", "Data"));
    const grid = element("table");
    const head = element("tr");
    for (const title of ["Port", "", "Now", "Average", "Peak"]) head.append(element("th", "", title));
    grid.append(head);
    for (const line of chart.lines) {
      const values = line.points.map(([, value]) => value);
      const row = element("tr");
      const cells = values.length
        ? [values[values.length - 1], values.reduce((sum, value) => sum + value, 0) / values.length, Math.max(...values)].map(number)
        : ["–", "–", "–"];
      for (const cell of [line.port, line.direction, ...cells]) row.append(element("td", "", cell));
      grid.append(row);
    }
    data.append(grid);
    return data;
  }

  function fill(entry, note) {
    const { figure, chart } = entry;
    figure.querySelector(".chart-unit").textContent = chart.unit;
    figure.querySelector(".chart-legend").replaceWith(legend(chart));
    const open = figure.querySelector(".chart-data").open;
    const data = table(chart);
    data.open = open;
    figure.querySelector(".chart-data").replaceWith(data);
    const caption = figure.querySelector(".chart-caption");
    caption.classList.toggle("stale", Boolean(note));
    caption.textContent = note || `Updated ${clock(Date.now() / 1000, true)}, every ${chart.interval} s`;
    draw(entry);
  }

  async function refresh(entry) {
    const { chart } = entry;
    entry.asked = Date.now();
    try {
      const response = await fetch("api/chart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: chart.title, ports: chart.ports, metric: chart.metric, minutes: chart.minutes }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        fill(entry, `Not updating: ${body.error || `HTTP ${response.status}`}`);
        return;
      }
      entry.chart = body;
      fill(entry);
    } catch (error) {
      fill(entry, `Not updating: ${error.message}`);
    }
  }

  // Draw a chart into the conversation and keep it live.
  function add(parent, chart) {
    const figure = element("figure", "chart");
    const heading = element("figcaption", "chart-heading");
    heading.append(element("strong", "", chart.title), element("span", "chart-unit"));
    const plot = element("div", "chart-plot");
    const readout = element("div", "chart-readout");
    readout.hidden = true;
    figure.append(heading, plot, readout, element("div", "chart-legend"), element("details", "chart-data"), element("div", "chart-caption"));
    parent.append(figure);
    const entry = { figure, chart, asked: 0 };
    live.add(entry);
    fill(entry, "Fetching the latest samples…");
    refresh(entry);
    return figure;
  }

  setInterval(() => {
    for (const entry of live) {
      if (!entry.figure.isConnected) live.delete(entry);
      else if (Date.now() - entry.asked >= entry.chart.interval * 1000) refresh(entry);
    }
  }, 5000);

  let resizing = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizing);
    resizing = setTimeout(() => {
      for (const entry of live) if (entry.figure.isConnected) draw(entry);
    }, 150);
  });

  return { add };
})();
