"use strict";

// The network map: draws the views the model makes with the display tool.
//
// A view is the network graph plus the model's choices. Every node and link is in
// focus, near the focus, or context, and some are highlighted. The layout is a
// force simulation (d3-force): nodes push each other apart like bubbles and links
// pull like springs. The view sets the forces. Focus is pulled to the middle in
// tiers (spines above leaves above hosts), near nodes ring it, and context is
// pushed out to the edge and dimmed. Showing another view changes the forces, not
// the nodes, so the network glides into its new shape and every node stays the
// node you were looking at.
//
// Device data reaches the map only as text: through textContent and attributes.

const netmap = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const panel = document.getElementById("map");
  const svg = document.getElementById("map-graph");
  const heading = document.getElementById("map-title");
  const inspector = document.getElementById("map-inspector");
  const still = matchMedia("(prefers-reduced-motion: reduce)");

  // Layout units; the camera scales the drawing to fit the panel.
  const RADIUS = {
    device: { focus: 20, near: 15, context: 11 },
    neighbor: { focus: 16, near: 12, context: 9 },
    host: { focus: 10, near: 8, context: 6 },
    segment: { focus: 9, near: 8, context: 6 },
  };
  const TIERS = {
    core: 0, "super-spine": 0, superspine: 0,
    spine: 1, aggregation: 1, distribution: 1, border: 1,
    leaf: 2, access: 2, edge: 2, tor: 2,
  };
  const TIER_GAP = 120;
  const PULL = { focus: 0.14, near: 0.03, context: 0 }; // toward the middle, in tiers
  const RING = { focus: 0, near: 0.06, context: 0.4 }; // toward a ring around the focus
  const CHARGE = { focus: -520, near: -360, context: -200 };
  const SPRING = { focus: 0.8, near: 0.4, context: 0.1 };
  const LENGTH = { focus: 100, near: 120, context: 120 };
  const CHAR_WIDTH = 7; // roughly, for the 12–13px label and note text
  const SHORT = [
    [/^ethernet-/i, "e"], [/^TenGigabitEthernet/i, "Te"], [/^GigabitEthernet/i, "Gi"],
    [/^FastEthernet/i, "Fa"], [/^HundredGigE/i, "Hu"], [/^Ethernet/i, "Eth"], [/^Port-channel/i, "Po"],
  ];

  const linkLayer = shape("g", { class: "links" });
  const nodeLayer = shape("g", { class: "nodes" });
  svg.append(linkLayer, nodeLayer);

  const nodes = new Map(); // by ID, kept across views so each node keeps its place
  const linkShapes = new Map();
  let links = [];
  let current = null; // the view on the map
  let selected = null; // the ID of the node or link in the inspector
  let camera = null; // the viewBox, eased toward the drawing as it moves
  let dragging = null;

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
    // Devices by role. Anything else sits a tier below what it hangs off, except a
    // management segment, which goes above everything: it is out of band.
    const tier = new Map();
    const kinds = { device: 0, segment: 1, neighbor: 2, host: 3 };
    const ordered = [...view.nodes].sort((a, b) => kinds[a.kind] - kinds[b.kind]);
    for (const node of ordered) {
      if (node.kind === "device") {
        tier.set(node.id, TIERS[(node.role || "").toLowerCase()] ?? 1.5);
        continue;
      }
      const attached = view.links
        .flatMap((link) => (link.a.node === node.id ? [link.b.node] : link.b.node === node.id ? [link.a.node] : []))
        .map((id) => tier.get(id))
        .filter((value) => value !== undefined);
      if (node.kind === "segment" && node.plane === "management") tier.set(node.id, Math.min(0, ...attached) - 1);
      else tier.set(node.id, attached.length ? Math.max(...attached) + 1 : 3);
    }
    return tier;
  }

  function nodeShape(data) {
    const group = shape("g", { class: `node kind-${data.kind}` });
    group.dataset.id = data.id;
    group.append(data.kind === "segment" ? shape("rect", { class: "body", transform: "rotate(45)" }) : shape("circle", { class: "body" }));
    if (data.kind === "device" && data.role) group.append(shape("text", { class: "glyph", dy: "0.35em" }, data.role[0].toUpperCase()));
    group.append(shape("text", { class: "label" }, data.label), shape("text", { class: "note" }));
    nodeLayer.append(group);
    return group;
  }

  function linkShape(data) {
    const group = shape("g", { class: "link" });
    group.dataset.id = data.id;
    group.append(
      shape("line", { class: "hit" }),
      shape("line", { class: "wire" }),
      shape("text", { class: "port" }, data.a.interface ? short(data.a.interface) : ""),
      shape("text", { class: "port" }, data.b.interface ? short(data.b.interface) : ""),
      shape("text", { class: "note" }),
    );
    linkLayer.append(group);
    return group;
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
    heading.textContent = { data: view.title, management: `${view.title} · management plane`, all: `${view.title} · all planes` }[view.plane];

    const tier = tiers(view);
    const lit = new Set(view.highlight);
    const trouble = new Set(view.trouble);
    const focus = view.nodes.filter((node) => view.emphasis[node.id] === "focus");
    const middle = focus.reduce((sum, node) => sum + tier.get(node.id), 0) / (focus.length || 1);
    const core = 60 + 50 * Math.sqrt(focus.length);

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
      next.set(data.id, node);
    }
    for (const [id, node] of nodes) if (!next.has(id)) node.element.remove();
    nodes.clear();
    for (const [id, node] of next) nodes.set(id, node);

    const seen = new Set();
    links = view.links.map((data) => {
      seen.add(data.id);
      if (!linkShapes.has(data.id)) linkShapes.set(data.id, linkShape(data));
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
      .force("collide", d3.forceCollide((node) => node.radius + 22))
      .force("x", d3.forceX(0).strength((node) => PULL[node.emphasis] / 2))
      .force("y", d3.forceY((node) => node.tierY).strength((node) => PULL[node.emphasis]))
      .force("ring", d3.forceRadial((node) => (node.emphasis === "near" ? core + 60 : core + 190)).strength((node) => RING[node.emphasis]))
      .force("labels", labelRoom);
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
        const room = left.radius + 12 + textWidth(left) + right.radius;
        const gap = right.x - left.x;
        if (gap < room) {
          const push = (room - gap) * alpha * 0.3;
          left.vx -= push;
          right.vx += push;
        }
      }
    }
  }

  function settle() {
    if (still.matches) {
      simulation.stop().alpha(1);
      for (let tick = 0; tick < 400; tick += 1) simulation.tick();
      draw();
      aim(true);
    } else {
      simulation.alpha(1).restart();
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
          selected === node.id && "selected",
        ]
          .filter(Boolean)
          .join(" "),
      );
      const body = element.querySelector(".body");
      if (data.kind === "segment") {
        const side = node.radius * 1.5;
        for (const [name, value] of Object.entries({ x: -side / 2, y: -side / 2, width: side, height: side })) body.setAttribute(name, value);
      } else {
        body.setAttribute("r", node.radius);
      }
      // Labels go to the right: in a tiered drawing, links mostly leave a node up or down.
      const label = element.querySelector(".label");
      label.setAttribute("x", node.radius + 7);
      label.setAttribute("dy", "0.35em");
      const note = element.querySelector(".note");
      note.setAttribute("x", node.radius + 7);
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
    }
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
      left = Math.min(left, node.x - node.radius - 30);
      right = Math.max(right, node.x + node.radius + 12 + textWidth(node));
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

  // ---- Selecting and dragging

  function inspect(id) {
    selected = id;
    restyle();
    inspector.replaceChildren();
    const node = id && nodes.get(id);
    const link = id && links.find((candidate) => candidate.id === id);
    if (node) {
      const data = node.data;
      inspector.append(html("h3", "", data.label));
      const kind = data.kind === "device" ? [data.role, data.platform].filter(Boolean).join(" · ") || "device" : data.kind;
      inspector.append(html("p", "kind", kind));
      if (data.addresses.length) inspector.append(html("p", "addresses", data.addresses.join(", ")));
      if (data.error) inspector.append(html("p", "failed", data.error));
      const hop = current.path && current.path.hops.find((candidate) => candidate.node === id);
      if (hop && hop.route) inspector.append(html("p", node.trouble ? "failed" : "remark", forwarding(hop)));
      else if (node.note) inspector.append(html("p", node.trouble ? "failed" : "remark", node.note));
      evidence(data.evidence);
    } else if (link) {
      const data = link.data;
      const end = (side) => (side.interface ? `${side.node} ${side.interface}` : side.node);
      inspector.append(html("h3", "", `${end(data.a)} ↔ ${end(data.b)}`));
      inspector.append(html("p", "kind", `${data.plane} plane`));
      if (data.one_sided) inspector.append(html("p", "failed", "Only one end reports this link, though both were asked."));
      if (link.note) inspector.append(html("p", "remark", link.note));
      evidence(data.evidence);
    } else if (current) {
      if (current.path) inspector.append(html("p", current.path.outcome === "delivered" ? "remark" : "failed", current.path.detail));
      for (const error of current.errors) inspector.append(html("p", "failed", `${error.device}: ${error.error}`));
      inspector.append(html("p", "hint", "Select a node or link to see the evidence for it. Drag a node to move it."));
    }
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
    dragging = { node: nodes.get(element.dataset.id), x: event.clientX, y: event.clientY, moved: false };
  });

  nodeLayer.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    if (!dragging.moved && Math.hypot(event.clientX - dragging.x, event.clientY - dragging.y) < 4) return;
    dragging.moved = true;
    const { x, y } = point(event);
    const node = dragging.node;
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

  function release() {
    if (!dragging) return;
    const { node, moved } = dragging;
    dragging = null;
    // Let go, and the forces take the node back: the view still decides the shape.
    node.fx = null;
    node.fy = null;
    simulation.alphaTarget(0);
    if (!moved) inspect(node.id);
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
