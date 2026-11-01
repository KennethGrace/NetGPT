"use strict";

// The network map: draws the views the model makes with the display tool.
//
// A view is the network graph, at layer 2 (cables and switches), at layer 3
// (routers, and the networks between them, drawn as pills), or as OSPF sees it
// (adjacencies labeled with each end's cost, each area but the backbone in a color
// of its own, and a link with a problem dashed in red), plus the model's choices.
// Every node and link is in focus, near the focus, or context, and some
// are highlighted. The layout is a force simulation (d3-force): nodes push each
// other apart like bubbles and links pull like springs. The view sets the forces. Focus is pulled to the middle in
// tiers (spines above leaves above hosts), near nodes ring it, and context is
// pushed out to the edge and dimmed. Showing another view changes the forces, not
// the nodes, so the network glides into its new shape and every node stays the
// node you were looking at.
//
// The viewer can pin nodes: a node dropped after a drag stays where it was put,
// and the forces arrange everything else around it. Pins are the viewer's layout,
// not the model's: they hold across views, per plane, and are kept in this
// browser's storage so a reload keeps them.
//
// While NetGPT polls, the links carry their traffic, drawn the way network
// weathermaps draw it: a lane for each direction beside the wire, on the right as
// the traffic travels, ending in an arrowhead. A lane's color is its share of the
// port's speed, from a one-hue ramp (--load-1 to -5 in app.css), and a focused
// link's lanes are labeled with their rates. A port that counted errors in the
// last few minutes gets a warning sign and the count beside its name. Selecting a
// link or a device charts its ports. Like the pins, the traffic is the viewer's
// to hide.
//
// Device data reaches the map only as text: through textContent and attributes.

const netmap = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const panel = document.getElementById("map");
  const svg = document.getElementById("map-graph");
  const heading = document.getElementById("map-title");
  const inspector = document.getElementById("map-inspector");
  const trafficButton = document.getElementById("map-traffic");
  const still = matchMedia("(prefers-reduced-motion: reduce)");

  // Layout units; the camera scales the drawing to fit the panel.
  const RADIUS = {
    device: { focus: 20, near: 15, context: 11 },
    neighbor: { focus: 16, near: 12, context: 9 },
    host: { focus: 10, near: 8, context: 6 },
    segment: { focus: 9, near: 8, context: 6 },
    subnet: { focus: 12, near: 10, context: 7 },
  };
  const TIERS = {
    core: 0, "super-spine": 0, superspine: 0,
    spine: 1, aggregation: 1, distribution: 1, border: 1,
    leaf: 2, edge: 2, tor: 2,
    access: 3,
  };
  const TIER_GAP = 120;
  const PULL = { focus: 0.14, near: 0.03, context: 0 }; // toward the middle, in tiers
  const RING = { focus: 0, near: 0.06, context: 0.4 }; // toward a ring around the focus
  const CHARGE = { focus: -520, near: -360, context: -200 };
  const SPRING = { focus: 0.8, near: 0.4, context: 0.1 };
  const LENGTH = { focus: 100, near: 120, context: 120 };
  const CHAR_WIDTH = 7; // roughly, for the 12–13px label and note text
  const BACKBONE = "0.0.0.0";
  const AREA_COLORS = 3; // --area-1 to -3 in app.css, used in turn
  const LAYER_NAMES = { l2: "", l3: " · layer 3", ospf: " · OSPF" };
  const SNAP = 8; // screen pixels: a dragged node this close to a pinned node's row or column joins it
  const DOUBLE_TAP = 400; // milliseconds
  const PINS = "netgpt.map.pins";
  const TRAFFIC = "netgpt.map.traffic";
  // A lane's color, by its share of the port's speed: under 10%, 10–25%, 25–50%, 50–75%, and 75% or more.
  const BANDS = [10, 25, 50, 75];
  const BAND_NAMES = ["under 10%", "10–25%", "25–50%", "50–75%", "75% or more"];
  const LANE = 4.5; // how far each direction's lane runs beside the wire
  const CHART_MINUTES = 15;
  const CHART_PORTS = 6; // as many as a chart has colors for
  const SHORT = [
    [/^ethernet-/i, "e"], [/^TenGigabitEthernet/i, "Te"], [/^GigabitEthernet/i, "Gi"],
    [/^FastEthernet/i, "Fa"], [/^HundredGigE/i, "Hu"], [/^Ethernet/i, "Eth"], [/^Port-channel/i, "Po"],
  ];

  const linkLayer = shape("g", { class: "links" });
  const guideLayer = shape("g", { class: "guides" });
  const nodeLayer = shape("g", { class: "nodes" });
  svg.append(linkLayer, guideLayer, nodeLayer);

  const nodes = new Map(); // by ID, kept across views so each node keeps its place
  const linkShapes = new Map();
  const pins = loadPins(); // {plane: {node ID: [x, y]}}
  let links = [];
  let current = null; // the view on the map
  let selected = null; // the ID of the node or link in the inspector
  let camera = null; // the viewBox, eased toward the drawing as it moves
  let dragging = null;
  let tapped = null; // the last node clicked without a drag, and when, to tell a double click
  let traffic = null; // the latest rates by device:port, from /api/v1/traffic
  let trafficShown = loadChoice();
  let polling = true; // until the server says NetGPT doesn't poll
  let pollEvery = 30; // seconds between polls, as the server says
  let errorMinutes = 5; // how far back the errors on the map go, as the server says
  let trafficAsked = 0;
  let trafficAt = 0;

  function loadPins() {
    try {
      const stored = JSON.parse(localStorage.getItem(PINS));
      return stored && typeof stored === "object" && !Array.isArray(stored) ? stored : {};
    } catch {
      return {};
    }
  }

  function savePins() {
    try {
      localStorage.setItem(PINS, JSON.stringify(pins));
    } catch {
      // Storage is off or full: the pins last until the page is reloaded.
    }
  }

  function loadChoice() {
    try {
      return localStorage.getItem(TRAFFIC) !== "hidden";
    } catch {
      return true;
    }
  }

  function saveChoice() {
    try {
      localStorage.setItem(TRAFFIC, trafficShown ? "shown" : "hidden");
    } catch {
      // As with the pins: the choice lasts until the page is reloaded.
    }
  }

  const simulation = d3.forceSimulation().alphaDecay(0.025).velocityDecay(0.35).on("tick", draw).on("end", () => aim(true));
  simulation.stop();

  function shape(tag, attributes = {}, text) {
    const element = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function html(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  const short = (name) => SHORT.reduce((text, [pattern, abbreviation]) => text.replace(pattern, abbreviation), name);

  function tiers(view) {
    // Devices in rows by role, with no empty rows for roles the view doesn't have.
    // Anything else sits a row below what it hangs off, except a management segment,
    // which goes above everything (it is out of band), and a network between routers
    // in two rows, which goes between them.
    const tier = new Map();
    const kinds = { device: 0, segment: 1, subnet: 2, neighbor: 3, host: 4 };
    const ordered = [...view.nodes].sort((a, b) => kinds[a.kind] - kinds[b.kind]);
    const role = (node) => TIERS[(node.role || "").toLowerCase()] ?? 1.5;
    const rows = [...new Set(ordered.filter((node) => node.kind === "device").map(role))].sort((a, b) => a - b);
    for (const node of ordered) {
      if (node.kind === "device") {
        tier.set(node.id, rows.indexOf(role(node)));
        continue;
      }
      const attached = view.links
        .flatMap((link) => (link.a.node === node.id ? [link.b.node] : link.b.node === node.id ? [link.a.node] : []))
        .map((id) => tier.get(id))
        .filter((value) => value !== undefined);
      const [low, high] = [Math.min(...attached), Math.max(...attached)];
      if (node.kind === "segment" && node.plane === "management") tier.set(node.id, Math.min(0, ...attached) - 1);
      else if (node.kind === "subnet" && high > low) tier.set(node.id, (low + high) / 2);
      else tier.set(node.id, attached.length ? high + 1 : 3);
    }
    return tier;
  }

  // How far a node's body reaches to the side: a network is drawn wider than it is tall.
  const reach = (node) => (node.data.kind === "subnet" ? node.radius * 1.6 : node.radius);

  function endLabel(end) {
    // In the OSPF view an end is its cost. At layer 3 an end is its address, just the host
    // part where the network is a /24 or smaller.
    if (end.cost !== null && end.cost !== undefined) return String(end.cost);
    if (end.address) {
      const [address, length] = end.address.split("/");
      return Number(length) >= 24 ? `.${address.split(".").pop()}` : address;
    }
    return end.interface ? short(end.interface) : "";
  }

  function nodeShape(data) {
    const group = shape("g", { class: `node kind-${data.kind}` });
    group.dataset.id = data.id;
    const body = {
      segment: () => shape("rect", { class: "body", transform: "rotate(45)" }),
      subnet: () => shape("rect", { class: "body" }),
    }[data.kind];
    group.append(body ? body() : shape("circle", { class: "body" }));
    if (data.kind === "device" && data.role) group.append(shape("text", { class: "glyph", dy: "0.35em" }, data.role[0].toUpperCase()));
    group.append(shape("text", { class: "label" }, data.label), shape("text", { class: "note" }), shape("circle", { class: "pin", r: 4 }));
    nodeLayer.append(group);
    return group;
  }

  function portLabel(end) {
    // The port's name, and room after it for a warning when the port counts errors.
    const label = shape("text", { class: "port" });
    label.append(shape("tspan", {}, endLabel(end)), shape("tspan", { class: "flag" }));
    return label;
  }

  function linkShape(data) {
    const group = shape("g", { class: "link" });
    group.dataset.id = data.id;
    group.append(
      shape("line", { class: "hit" }),
      shape("line", { class: "wire" }),
      shape("path", { class: "lane" }),
      shape("path", { class: "lane" }),
      portLabel(data.a),
      portLabel(data.b),
      shape("text", { class: "port network" }, data.subnet || ""),
      shape("text", { class: "rate" }),
      shape("text", { class: "rate" }),
      shape("text", { class: "note" }),
    );
    linkLayer.append(group);
    return group;
  }

  function relabel(element, data, layer) {
    // A link keeps its drawing from view to view, and the same link can be in another
    // layer's view (layer 3 and OSPF name a link between routers alike), so its labels
    // follow the view: costs or addresses at the ends, and the network between them
    // except in the OSPF view, where the inspector has it.
    const [a, b] = element.querySelectorAll(".port:not(.network)");
    a.firstChild.textContent = endLabel(data.a);
    b.firstChild.textContent = endLabel(data.b);
    element.querySelector(".port.network").textContent = layer === "ospf" ? "" : data.subnet || "";
  }

  function place(data, view, tier, middle) {
    // A node new to the map starts beside a neighbor already there. On an empty map,
    // nodes start in rows by tier, in the view's order (inventory order for devices),
    // so the first layout comes out the same way round every time.
    for (const link of view.links) {
      const other = link.a.node === data.id ? link.b.node : link.b.node === data.id ? link.a.node : null;
      const anchor = other && nodes.get(other);
      if (anchor && anchor.x !== undefined) return { x: anchor.x + (Math.random() - 0.5) * 40, y: anchor.y + (Math.random() - 0.5) * 40 };
    }
    const row = view.nodes.filter((node) => tier.get(node.id) === tier.get(data.id));
    const column = row.findIndex((node) => node.id === data.id);
    return { x: (column - (row.length - 1) / 2) * 150 + Math.random() - 0.5, y: (tier.get(data.id) - middle) * TIER_GAP };
  }

  function show(view) {
    current = view;
    selected = null;
    document.body.classList.add("map-open");
    panel.hidden = false;
    const planes = { data: "", management: " · management plane", all: " · all planes" }[view.plane];
    heading.textContent = `${view.title}${planes}${LAYER_NAMES[view.layer] ?? ""}`;

    const tier = tiers(view);
    const lit = new Set(view.highlight);
    const trouble = new Set(view.trouble);
    const focus = view.nodes.filter((node) => view.emphasis[node.id] === "focus");
    const middle = focus.reduce((sum, node) => sum + tier.get(node.id), 0) / (focus.length || 1);
    const core = 60 + 50 * Math.sqrt(focus.length);

    const pinned = pins[view.plane] || {};
    const next = new Map();
    for (const data of view.nodes) {
      const node = nodes.get(data.id) || { id: data.id, element: nodeShape(data), ...place(data, view, tier, middle) };
      Object.assign(node, {
        data,
        emphasis: view.emphasis[data.id],
        lit: lit.has(data.id),
        trouble: trouble.has(data.id),
        note: view.notes[data.id] || "",
        tierY: (tier.get(data.id) - middle) * TIER_GAP,
      });
      node.radius = RADIUS[data.kind][node.emphasis];
      const at = pinned[data.id];
      if (Array.isArray(at) && at.length === 2 && at.every(Number.isFinite)) {
        node.x = node.fx = at[0];
        node.y = node.fy = at[1];
      } else {
        node.fx = node.fy = null;
      }
      next.set(data.id, node);
    }
    for (const [id, node] of nodes) if (!next.has(id)) node.element.remove();
    nodes.clear();
    for (const [id, node] of next) nodes.set(id, node);

    const seen = new Set();
    links = view.links.map((data) => {
      seen.add(data.id);
      if (!linkShapes.has(data.id)) linkShapes.set(data.id, linkShape(data));
      relabel(linkShapes.get(data.id), data, view.layer);
      return {
        id: data.id,
        data,
        source: data.a.node,
        target: data.b.node,
        emphasis: view.emphasis[data.id],
        lit: lit.has(data.id),
        note: view.notes[data.id] || "",
        element: linkShapes.get(data.id),
      };
    });
    for (const [id, element] of linkShapes) {
      if (!seen.has(id)) {
        element.remove();
        linkShapes.delete(id);
      }
    }

    restyle();
    simulation
      .nodes([...nodes.values()])
      .force("link", d3.forceLink(links).id((node) => node.id).distance((link) => LENGTH[link.emphasis]).strength((link) => SPRING[link.emphasis]))
      .force("charge", d3.forceManyBody().strength((node) => CHARGE[node.emphasis]).distanceMax(600))
      .force("collide", d3.forceCollide((node) => reach(node) + 22))
      .force("x", d3.forceX(0).strength((node) => PULL[node.emphasis] / 2))
      .force("y", d3.forceY((node) => node.tierY).strength((node) => PULL[node.emphasis]))
      .force("ring", d3.forceRadial((node) => (node.emphasis === "near" ? core + 60 : core + 190)).strength((node) => RING[node.emphasis]))
      .force("labels", labelRoom);
    // After the link force, which turns each link's ends from names into nodes.
    retraffic();
    if (polling && trafficShown && Date.now() - trafficAsked >= pollEvery * 1000) refreshTraffic();
    settle();
    inspect(null);
  }

  function textWidth(node) {
    const note = node.emphasis === "context" ? 0 : node.note.length;
    return Math.max(node.data.label.length, note) * CHAR_WIDTH;
  }

  function labelRoom(alpha) {
    // Labels and notes sit to the right of their nodes. Push apart nodes on the same
    // line until each one's text clears the next node along.
    const shown = [...nodes.values()].filter((node) => node.emphasis !== "context");
    for (const left of shown) {
      for (const right of shown) {
        if (left === right || right.x < left.x || Math.abs(right.y - left.y) > 32) continue;
        const room = reach(left) + 12 + textWidth(left) + reach(right);
        const gap = right.x - left.x;
        if (gap < room) {
          const push = (room - gap) * alpha * 0.3;
          left.vx -= push;
          right.vx += push;
        }
      }
    }
  }

  function settle(alpha = 1) {
    if (still.matches) {
      simulation.stop().alpha(alpha);
      for (let tick = 0; tick < 400; tick += 1) simulation.tick();
      draw();
      aim(true);
    } else {
      simulation.alpha(Math.max(alpha, simulation.alpha())).restart();
    }
  }

  function restyle() {
    for (const node of nodes.values()) {
      const { element, data } = node;
      element.setAttribute(
        "class",
        [
          "node",
          `kind-${data.kind}`,
          node.emphasis,
          node.lit && "lit",
          node.trouble && "trouble",
          data.error && "failed",
          data.kind === "subnet" && areaClass(data.areas),
          isPinned(node) && "pinned",
          selected === node.id && "selected",
        ]
          .filter(Boolean)
          .join(" "),
      );
      const pin = element.querySelector(".pin");
      pin.setAttribute("cx", (-reach(node) * 0.75).toFixed(1));
      pin.setAttribute("cy", (-node.radius * 0.75).toFixed(1));
      const body = element.querySelector(".body");
      const box = (width, height, round = 0) => {
        const sizes = { x: -width / 2, y: -height / 2, width, height, rx: round };
        for (const [name, value] of Object.entries(sizes)) body.setAttribute(name, value);
      };
      if (data.kind === "segment") box(node.radius * 1.5, node.radius * 1.5);
      else if (data.kind === "subnet") box(reach(node) * 2, node.radius * 1.5, node.radius * 0.75);
      else body.setAttribute("r", node.radius);
      // Labels go to the right: in a tiered drawing, links mostly leave a node up or down.
      const label = element.querySelector(".label");
      label.setAttribute("x", reach(node) + 7);
      label.setAttribute("dy", "0.35em");
      const note = element.querySelector(".note");
      note.setAttribute("x", reach(node) + 7);
      note.setAttribute("y", 17);
      note.textContent = node.emphasis === "context" ? "" : node.note;
    }
    for (const link of links) {
      link.element.setAttribute(
        "class",
        [
          "link",
          link.emphasis,
          link.lit && "lit",
          link.data.one_sided && "one-sided",
          link.data.problems && link.data.problems.length && "problem",
          areaClass([...new Set([link.data.a.area, link.data.b.area].filter(Boolean))]),
          link.data.plane === "management" && "management",
          selected === link.id && "selected",
        ]
          .filter(Boolean)
          .join(" "),
      );
      link.element.querySelector(".note").textContent = link.emphasis === "context" ? "" : link.note;
    }
  }

  function draw() {
    const rates = []; // each rate label, its link, and where it could go
    for (const link of links) {
      const { source: a, target: b, element } = link;
      for (const line of element.querySelectorAll("line")) {
        line.setAttribute("x1", a.x.toFixed(1));
        line.setAttribute("y1", a.y.toFixed(1));
        line.setAttribute("x2", b.x.toFixed(1));
        line.setAttribute("y2", b.y.toFixed(1));
      }
      // Port names sit a little way along the link from each end; notes in the middle.
      const [nearA, nearB] = element.querySelectorAll(".port");
      const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      const along = (from, to, distance) => [from.x + ((to.x - from.x) * distance) / length, from.y + ((to.y - from.y) * distance) / length];
      const [ax, ay] = along(a, b, a.radius + 18);
      const [bx, by] = along(b, a, b.radius + 18);
      nearA.setAttribute("x", ax.toFixed(1));
      nearA.setAttribute("y", ay.toFixed(1));
      nearB.setAttribute("x", bx.toFixed(1));
      nearB.setAttribute("y", by.toFixed(1));
      const note = element.querySelector(".note");
      note.setAttribute("x", ((a.x + b.x) / 2).toFixed(1));
      note.setAttribute("y", ((a.y + b.y) / 2 - 8).toFixed(1));
      // A network's prefix sits beside its link, off the line and clear of the ends' addresses.
      let [across, down] = [-(b.y - a.y) / length, (b.x - a.x) / length];
      if (across < 0 || (across === 0 && down < 0)) [across, down] = [-across, -down];
      const network = element.querySelector(".network");
      network.setAttribute("x", ((a.x + b.x) / 2 + across * 10).toFixed(1));
      network.setAttribute("y", ((a.y + b.y) / 2 + down * 10 + 4).toFixed(1));
      network.classList.toggle("beside", across > 0.5);
      const [laneA, laneB] = element.querySelectorAll(".lane");
      const [rateA, rateB] = element.querySelectorAll(".rate");
      const [ux, uy] = [(b.x - a.x) / length, (b.y - a.y) / length];
      const flows = link.flows || [];
      rates.push([rateA, link, lane(laneA, a, b, ux, uy, length, flows[0])]);
      rates.push([rateB, link, lane(laneB, b, a, -ux, -uy, length, flows[1])]);
    }
    placeRates(rates);
    for (const node of nodes.values()) node.element.setAttribute("transform", `translate(${node.x.toFixed(1)},${node.y.toFixed(1)})`);
    aim(false);
  }

  function aim(immediately) {
    // Frame the whole drawing, easing the camera after it rather than jumping.
    if (dragging || !nodes.size) return;
    let left = Infinity;
    let right = -Infinity;
    let top = Infinity;
    let bottom = -Infinity;
    for (const node of nodes.values()) {
      // Room for the label and note on the right.
      left = Math.min(left, node.x - reach(node) - 30);
      right = Math.max(right, node.x + reach(node) + 12 + textWidth(node));
      top = Math.min(top, node.y - node.radius - 25);
      bottom = Math.max(bottom, node.y + node.radius + 30);
    }
    const width = Math.max(right - left, 420);
    const height = Math.max(bottom - top, 300);
    const target = { x: (left + right - width) / 2, y: (top + bottom - height) / 2, width, height };
    if (immediately || !camera) {
      camera = target;
    } else {
      for (const key of Object.keys(camera)) camera[key] += (target[key] - camera[key]) * 0.12;
    }
    svg.setAttribute("viewBox", `${camera.x.toFixed(1)} ${camera.y.toFixed(1)} ${camera.width.toFixed(1)} ${camera.height.toFixed(1)}`);
  }

  function edge(node, ux, uy) {
    // How far a node's body reaches in a direction: a circle, a network's wide box, or a segment's diamond.
    const [across, down] = [Math.abs(ux) || 1e-9, Math.abs(uy) || 1e-9];
    if (node.data.kind === "subnet") return Math.min(reach(node) / across, (node.radius * 0.75) / down);
    if (node.data.kind === "segment") return (node.radius * 1.06) / (across + down);
    return node.radius;
  }

  function crowding(x, y, self) {
    // How near a point comes to any other link's wire.
    let nearest = Infinity;
    for (const link of links) {
      if (link === self) continue;
      const { source: a, target: b } = link;
      const [dx, dy] = [b.x - a.x, b.y - a.y];
      const t = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / (dx * dx + dy * dy || 1)));
      nearest = Math.min(nearest, Math.hypot(a.x + t * dx - x, a.y + t * dy - y));
    }
    return nearest;
  }

  function lane(path, from, to, ux, uy, length, flow) {
    // One direction's lane runs on the right of the wire as it travels, from body to
    // body, and ends in a barb on the outside. Returns the places its rate could go:
    // beside it near its start, or near its end, leaving the middle of the link to
    // notes and network prefixes.
    const start = edge(from, ux, uy) + 3;
    const end = length - edge(to, -ux, -uy) - 3;
    if (!flow || end - start < 16) {
      path.setAttribute("d", "");
      return [];
    }
    const [nx, ny] = [-uy, ux];
    const at = (along, side) => [from.x + ux * along + nx * side, from.y + uy * along + ny * side];
    const point = (along, side) => at(along, side).map((value) => value.toFixed(1)).join(",");
    path.setAttribute("d", `M${point(start, LANE)}L${point(end, LANE)}M${point(end - 7, LANE + 4)}L${point(end, LANE)}`);
    const anchor = nx > 0.5 ? "start" : nx < -0.5 ? "end" : "middle";
    const baseline = ny > 0.5 ? "hanging" : ny < -0.5 ? "auto" : "middle";
    const along = Math.min(28, (end - start) * 0.3);
    return [start + along, end - along].map((distance) => {
      const [x, y] = at(distance, LANE + 7);
      return { x, y, anchor, baseline };
    });
  }

  function textBox(x, y, width, height, anchor = "middle", baseline = "middle") {
    const left = anchor === "start" ? x : anchor === "end" ? x - width : x - width / 2;
    const top = baseline === "hanging" ? y : baseline === "auto" ? y - height : y - height / 2;
    return { left, top, right: left + width, bottom: top + height };
  }

  const overlap = (one, two) =>
    Math.max(0, Math.min(one.right, two.right) - Math.max(one.left, two.left)) *
    Math.max(0, Math.min(one.bottom, two.bottom) - Math.max(one.top, two.top));

  function placeRates(rates) {
    // Each shown rate goes at whichever end of its lane covers least of what is already
    // drawn: nodes and their names, port names and prefixes, other links' wires, and
    // the rates placed before it. Text widths are estimated, which is close enough.
    const taken = [];
    for (const node of nodes.values()) {
      taken.push(textBox(node.x, node.y, reach(node) * 2, node.radius * 2));
      taken.push(textBox(node.x + reach(node) + 7, node.y, node.data.label.length * CHAR_WIDTH, 14, "start"));
    }
    for (const link of links) {
      const named = link.emphasis === "focus" && link.data.plane !== "management";
      for (const text of link.element.querySelectorAll(".port")) {
        if (!text.textContent || !(named || text.classList.contains("flagged"))) continue;
        const anchor = text.classList.contains("beside") ? "start" : "middle";
        taken.push(textBox(Number(text.getAttribute("x")), Number(text.getAttribute("y")), text.textContent.length * 6, 11, anchor));
      }
    }
    for (const [label, link, options] of rates) {
      if (!options.length) continue;
      const width = label.textContent.length * 6.5;
      const boxes = options.map(({ x, y, anchor, baseline }) => textBox(x, y, width, 11, anchor, baseline));
      const costs = boxes.map((box) => {
        const covered = taken.reduce((sum, other) => sum + overlap(box, other), 0);
        return covered + (crowding((box.left + box.right) / 2, (box.top + box.bottom) / 2, link) < 8 ? 200 : 0);
      });
      const best = link.emphasis === "focus" && costs[1] < costs[0] ? 1 : 0;
      const { x, y, anchor, baseline } = options[best];
      label.setAttribute("x", x.toFixed(1));
      label.setAttribute("y", y.toFixed(1));
      label.setAttribute("text-anchor", anchor);
      label.setAttribute("dominant-baseline", baseline);
      if (link.emphasis === "focus") taken.push(boxes[best]);
    }
  }

  // ---- Traffic

  const portOf = (name) => name.replace(/\.\d+$/, ""); // a subinterface's traffic is its port's
  const band = (percent) => (percent == null ? 0 : 1 + BANDS.filter((limit) => percent >= limit).length);

  function measured(end) {
    // The latest rates of the port at one end of a link, if NetGPT polls it.
    return traffic && end.interface ? traffic.get(`${end.node}:${portOf(end.interface)}`) : undefined;
  }

  function sending(from, to) {
    // What one end of a link sends: what left its port, or failing that, what arrived at the far end's.
    const here = measured(from);
    if (here) return { mbps: here.out_mbps, percent: here.out_percent, speed: here.speed };
    const there = measured(to);
    if (there) return { mbps: there.in_mbps, percent: there.in_percent, speed: there.speed };
    return null;
  }

  function rateText(mbps, long = false) {
    // 38.2M on the map, 38.2 Mbit/s in the inspector: always three figures, so 47.0M sits evenly beside 47.1M.
    if (!mbps) return long ? "0 bit/s" : "0";
    const [value, unit] = mbps >= 1e6 ? [mbps / 1e6, "T"] : mbps >= 1000 ? [mbps / 1000, "G"] : mbps >= 1 ? [mbps, "M"] : [mbps * 1000, "k"];
    const shown = value.toPrecision(3);
    return long ? `${shown} ${unit}bit/s` : `${shown}${unit}`;
  }

  const clock = (milliseconds) =>
    new Date(milliseconds).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  function retraffic() {
    // Work out each link's two directions from the latest rates, and flag the ports that counted errors.
    const shown = trafficShown && traffic;
    for (const link of links) {
      const { a, b } = link.data;
      link.flows = shown ? [sending(a, b), sending(b, a)] : [null, null];
      const lanes = link.element.querySelectorAll(".lane");
      const rates = link.element.querySelectorAll(".rate");
      link.flows.forEach((flow, index) => {
        lanes[index].setAttribute("class", flow ? `lane u${band(flow.percent)}` : "lane");
        rates[index].textContent = flow ? rateText(flow.mbps) : "";
      });
      const labels = link.element.querySelectorAll(".port");
      [a, b].forEach((end, index) => {
        const port = shown && measured(end);
        const errors = port ? port.in_errors + port.out_errors : 0;
        labels[index].querySelector(".flag").textContent = errors ? ` ⚠ ${errors}` : "";
        labels[index].classList.toggle("flagged", errors > 0);
      });
    }
    draw();
    const panel = inspector.querySelector(".traffic");
    if (panel) panel.replaceWith(trafficLines(selected));
  }

  async function refreshTraffic() {
    trafficAsked = Date.now();
    let body;
    try {
      const response = await fetch("api/v1/traffic");
      if (response.status === 404) {
        // NetGPT isn't polling: there is no traffic to show, now or later.
        polling = false;
        trafficButton.hidden = true;
        return;
      }
      if (!response.ok) return;
      body = await response.json();
    } catch {
      return; // Keep showing the last rates; the next try may get through.
    }
    const first = !traffic || !traffic.size;
    pollEvery = body.interval;
    errorMinutes = body.minutes;
    traffic = new Map(body.ports.map((port) => [`${port.device}:${port.interface}`, port]));
    trafficAt = Date.now();
    trafficButton.hidden = false;
    retraffic();
    // The first rates can chart whatever is already selected.
    if (first && current) inspect(selected);
  }

  function devicePorts(id) {
    // A device's ports that the map's links name and NetGPT polls.
    const found = new Map();
    for (const link of links) {
      for (const end of [link.data.a, link.data.b]) {
        const port = end.node === id && measured(end);
        if (port) found.set(`${port.device}:${port.interface}`, port);
      }
    }
    return [...found.values()];
  }

  function counted(panel, port) {
    // What a port counted going wrong in the last few minutes. Errors get the warning; discards are only told.
    const told = [
      [port.in_errors, "input error"],
      [port.out_errors, "output error"],
      [port.in_discards, "input discard"],
      [port.out_discards, "output discard"],
    ]
      .filter(([count]) => count > 0)
      .map(([count, name]) => `${count} ${name}${count === 1 ? "" : "s"}`);
    if (!told.length) return;
    const text = `${port.device} ${port.interface} counted ${told.join(", ")} in the last ${errorMinutes} minutes.`;
    const errors = port.in_errors + port.out_errors > 0;
    panel.append(html("p", errors ? "warned" : "kind", errors ? `⚠ ${text}` : text));
  }

  function ospfAreas() {
    // The areas in the OSPF view, the backbone first.
    if (!current || current.layer !== "ospf") return [];
    const areas = new Set(current.nodes.flatMap((node) => node.areas || []));
    const order = (a, b) => (a === BACKBONE ? -1 : b === BACKBONE ? 1 : a.localeCompare(b, undefined, { numeric: true }));
    return [...areas].sort(order);
  }

  function areaClass(areas) {
    // The color class of something in exactly one area other than the backbone.
    if (!areas || areas.length !== 1 || areas[0] === BACKBONE) return null;
    const index = ospfAreas().filter((area) => area !== BACKBONE).indexOf(areas[0]);
    return index < 0 ? null : `area-${(index % AREA_COLORS) + 1}`;
  }

  function areaLegend() {
    const panel = html("div", "ospf");
    panel.append(html("p", "hint", "The number at each end of a link is that end's OSPF cost, for traffic it sends across."));
    const row = html("p", "area-legend");
    row.append(html("span", "", "Areas:"));
    for (const area of ospfAreas()) {
      const item = html("span", "item");
      const name = area === BACKBONE ? `${area} (backbone)` : area;
      item.append(html("span", `swatch ${areaClass([area]) || ""}`), document.createTextNode(name));
      row.append(item);
    }
    panel.append(row);
    return panel;
  }

  function ospfLines(data) {
    // A link's two OSPF ends: area, cost, and how far each side's adjacency got; then its problems.
    const panel = html("div", "ospf");
    for (const side of [data.a, data.b]) {
      if (!side.area) continue;
      const state = side.state ? side.state : "hears no neighbor";
      panel.append(html("p", "", `${side.node} ${side.interface}: area ${side.area}, cost ${side.cost ?? "unknown"}, ${state}`));
    }
    for (const problem of data.problems || []) panel.append(html("p", "failed", problem));
    return panel;
  }

  function laneKey(level) {
    const key = shape("svg", { class: "lane-key", viewBox: "0 0 22 10", width: "22", height: "10", "aria-hidden": "true" });
    key.append(shape("path", { class: `lane u${level}`, d: "M2,6L20,6M13,2L20,6" }));
    return key;
  }

  function legend(panel) {
    panel.append(html("p", "hint", "Each link has a lane for each direction, on the right as the traffic travels."));
    const row = html("p", "traffic-legend");
    row.append(html("span", "", "Share of port speed:"));
    BAND_NAMES.forEach((name, index) => {
      const item = html("span", "item");
      item.append(laneKey(index + 1), document.createTextNode(name));
      row.append(item);
    });
    const flag = html("span", "item");
    flag.append(html("span", "flag", "⚠"), document.createTextNode(`errors, last ${errorMinutes} minutes`));
    row.append(flag, html("span", "", `Updated ${clock(trafficAt)}, every ${pollEvery} s.`));
    panel.append(row);
  }

  function trafficLines(id) {
    // The inspector's account of the traffic: a link's two directions, the errors at a
    // device's ports, or with nothing selected, how to read the lanes.
    const panel = html("div", "traffic");
    if (!trafficShown || !polling) return panel;
    if (!traffic) {
      panel.append(html("p", "hint", "Traffic: waiting for the first rates."));
      return panel;
    }
    const link = id && links.find((candidate) => candidate.id === id);
    if (link) {
      const { a, b } = link.data;
      link.flows.forEach((flow, index) => {
        if (!flow) return;
        const [from, to] = index ? [b.node, a.node] : [a.node, b.node];
        const share = flow.percent == null ? "" : `, ${Number(flow.percent.toPrecision(2))}% of ${flow.speed}`;
        panel.append(html("p", "", `${from} → ${to}: ${rateText(flow.mbps, true)}${share}`));
      });
      for (const port of new Set([measured(a), measured(b)])) if (port) counted(panel, port);
    } else if (id && nodes.has(id)) {
      for (const port of devicePorts(id)) counted(panel, port);
    } else {
      legend(panel);
    }
    return panel;
  }

  function chartOf(ports) {
    // The selected link's or device's ports over the last minutes, kept live by chart.js.
    if (!trafficShown || !ports.length) return false;
    const names = ports.slice(0, CHART_PORTS).map((port) => `${port.device}:${port.interface}`);
    const chart = { title: `Last ${CHART_MINUTES} minutes`, metric: "traffic", unit: "Mbit/s", minutes: CHART_MINUTES, interval: pollEvery };
    netchart.add(inspector, { ...chart, ports: names, lines: [] }, { height: 150 });
    return true;
  }

  trafficButton.setAttribute("aria-pressed", String(trafficShown));
  trafficButton.addEventListener("click", () => {
    trafficShown = !trafficShown;
    saveChoice();
    trafficButton.setAttribute("aria-pressed", String(trafficShown));
    retraffic();
    inspect(selected);
    if (trafficShown) refreshTraffic();
  });

  setInterval(() => {
    if (current && polling && trafficShown && !panel.hidden && Date.now() - trafficAsked >= pollEvery * 1000) refreshTraffic();
  }, 5000);

  // ---- Pinning

  const isPinned = (node) => node.fx != null;

  function pin(node, x, y) {
    // In whole units, so the node is where storage will put it back.
    node.fx = Math.round(x);
    node.fy = Math.round(y);
    (pins[current.plane] ||= {})[node.id] = [node.fx, node.fy];
    savePins();
  }

  function unpin(node) {
    node.fx = node.fy = null;
    if (pins[current.plane]) delete pins[current.plane][node.id];
    savePins();
  }

  function repinned() {
    // Let the rest of the drawing find its place around the change.
    settle(0.3);
    inspect(selected);
  }

  function togglePin(node) {
    if (isPinned(node)) unpin(node);
    else pin(node, node.x, node.y);
    repinned();
  }

  function unpinAll() {
    for (const node of nodes.values()) node.fx = node.fy = null;
    delete pins[current.plane];
    savePins();
    repinned();
  }

  function align(node, { x, y }) {
    // A node dragged near a pinned node's row or column snaps into line with it,
    // and a guide shows the line, so pinned nodes are easy to set out in rows.
    const reach = SNAP / svg.getScreenCTM().a;
    const nearest = (values, target) =>
      values.reduce((best, value) => (Math.abs(value - target) < Math.min(reach, Math.abs((best ?? Infinity) - target)) ? value : best), null);
    const others = [...nodes.values()].filter((other) => other !== node && isPinned(other));
    const column = nearest(others.map((other) => other.fx), x);
    const row = nearest(others.map((other) => other.fy), y);
    guideLayer.replaceChildren();
    if (column !== null) guideLayer.append(shape("line", { x1: column, x2: column, y1: camera.y, y2: camera.y + camera.height }));
    if (row !== null) guideLayer.append(shape("line", { x1: camera.x, x2: camera.x + camera.width, y1: row, y2: row }));
    return { x: column ?? x, y: row ?? y };
  }

  // ---- Selecting and dragging

  function inspect(id) {
    selected = id;
    restyle();
    inspector.replaceChildren();
    const node = id && nodes.get(id);
    const link = id && links.find((candidate) => candidate.id === id);
    let charted = false;
    if (node) {
      const data = node.data;
      const head = html("div", "head");
      const toggle = html("button", "", isPinned(node) ? "Unpin" : "Pin here");
      toggle.type = "button";
      toggle.addEventListener("click", () => togglePin(node));
      head.append(html("h3", "", data.label), toggle);
      inspector.append(head);
      const kind = data.kind === "device" ? [data.role, data.platform].filter(Boolean).join(" · ") || "device" : data.kind;
      inspector.append(html("p", "kind", kind));
      if (data.addresses.length) inspector.append(html("p", "addresses", data.addresses.join(", ")));
      if (data.router_id) inspector.append(html("p", "kind", `OSPF router ID ${data.router_id}`));
      const areas = data.areas || [];
      if (areas.length > 1) inspector.append(html("p", "kind", `Area border router: areas ${areas.join(", ")}`));
      else if (areas.length) inspector.append(html("p", "kind", `Area ${areas[0]}`));
      if (data.error) inspector.append(html("p", "failed", data.error));
      const hop = current.path && current.path.hops.find((candidate) => candidate.node === id);
      if (hop && hop.route) inspector.append(html("p", node.trouble ? "failed" : "remark", forwarding(hop)));
      else if (node.note) inspector.append(html("p", node.trouble ? "failed" : "remark", node.note));
      inspector.append(trafficLines(id));
      charted = chartOf(devicePorts(id));
      evidence(data.evidence);
    } else if (link) {
      const data = link.data;
      const end = (side) => [side.node, side.interface, side.address].filter(Boolean).join(" ");
      inspector.append(html("h3", "", `${end(data.a)} ↔ ${end(data.b)}`));
      inspector.append(html("p", "kind", [`${data.plane} plane`, data.subnet].filter(Boolean).join(" · ")));
      const bridging = [data.a, data.b].filter((side) => side.bridges && side.bridges.length);
      if (bridging.length) {
        inspector.append(html("p", "kind", bridging.map((side) => `${side.node} bridges it in ${side.bridges.join(", ")}`).join("; ") + "."));
      }
      if (data.one_sided) inspector.append(html("p", "failed", "Only one end reports this link, though both were asked."));
      if (current.layer === "ospf") inspector.append(ospfLines(data));
      if (link.note) inspector.append(html("p", "remark", link.note));
      inspector.append(trafficLines(id));
      // One port tells both directions; the far end's would only say the same again.
      charted = chartOf([measured(data.a) || measured(data.b)].filter(Boolean));
      evidence(data.evidence);
    } else if (current) {
      if (current.path) inspector.append(html("p", current.path.outcome === "delivered" ? "remark" : "failed", current.path.detail));
      for (const error of current.errors) inspector.append(html("p", "failed", `${error.device}: ${error.error}`));
      if (current.layer === "ospf") inspector.append(areaLegend());
      inspector.append(html("p", "hint", "Select a node or link for the evidence. Drag a node to pin it; double-click to pin or let go."));
      const count = [...nodes.values()].filter(isPinned).length;
      if (count) {
        const pinned = html("p", "hint", `${count} pinned. `);
        const release = html("button", "", "Unpin all");
        release.type = "button";
        release.addEventListener("click", unpinAll);
        pinned.append(release);
        inspector.append(pinned);
      }
      inspector.append(trafficLines(null));
    }
    inspector.classList.toggle("charted", charted);
  }

  function forwarding(hop) {
    // The route a hop used, its next hops, and the interfaces the packet leaves by.
    const { protocol, prefix, next_hops: nextHops } = hop.route;
    const via = nextHops.map((next) => (next.action === "forward" ? [next.address, next.interface].filter(Boolean).join(" on ") : next.action));
    let text = `${protocol} ${prefix}`;
    if (via.some(Boolean)) text += ` via ${via.filter(Boolean).join(", ")}`;
    if (hop.interfaces.length) text += `, out ${hop.interfaces.join(", ")}`;
    return hop.next.length || hop.note === `${protocol} ${prefix}` ? text : `${text}: ${hop.note}`;
  }

  function evidence(lines) {
    if (!lines.length) return;
    const list = html("ul", "evidence");
    for (const line of lines) list.append(html("li", "", line));
    inspector.append(list);
  }

  function point(event) {
    const matrix = svg.getScreenCTM().inverse();
    return new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix);
  }

  nodeLayer.addEventListener("pointerdown", (event) => {
    const element = event.target.closest(".node");
    if (!element) return;
    event.preventDefault();
    element.setPointerCapture(event.pointerId);
    const node = nodes.get(element.dataset.id);
    dragging = { node, x: event.clientX, y: event.clientY, moved: false, was: [node.fx, node.fy] };
  });

  nodeLayer.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    if (!dragging.moved && Math.hypot(event.clientX - dragging.x, event.clientY - dragging.y) < 4) return;
    dragging.moved = true;
    const node = dragging.node;
    const { x, y } = align(node, point(event));
    node.fx = x;
    node.fy = y;
    if (still.matches) {
      node.x = x;
      node.y = y;
      draw();
    } else {
      simulation.alphaTarget(0.3).restart();
    }
  });

  function release(event) {
    if (!dragging) return;
    const { node, moved, was } = dragging;
    dragging = null;
    guideLayer.replaceChildren();
    simulation.alphaTarget(0);
    if (moved) {
      // Where a dragged node is dropped, it stays; the forces arrange the rest around it.
      // A drag the browser cancels puts the node back as it was.
      if (event.type === "pointercancel") [node.fx, node.fy] = was;
      else pin(node, node.fx, node.fy);
      tapped = null;
      repinned();
    } else if (tapped && tapped.id === node.id && event.timeStamp - tapped.time < DOUBLE_TAP) {
      tapped = null;
      togglePin(node);
    } else {
      tapped = { id: node.id, time: event.timeStamp };
      inspect(node.id);
    }
  }
  nodeLayer.addEventListener("pointerup", release);
  nodeLayer.addEventListener("pointercancel", release);

  linkLayer.addEventListener("click", (event) => {
    const element = event.target.closest(".link");
    if (element) inspect(element.dataset.id);
  });

  svg.addEventListener("click", (event) => {
    if (event.target === svg) inspect(null);
  });

  function clear() {
    simulation.stop();
    nodeLayer.replaceChildren();
    linkLayer.replaceChildren();
    guideLayer.replaceChildren();
    dragging = tapped = null;
    nodes.clear();
    linkShapes.clear();
    links = [];
    current = null;
    camera = null;
    inspector.replaceChildren();
    panel.hidden = true;
    document.body.classList.remove("map-open");
  }

  return { show, clear };
})();
