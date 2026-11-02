"use strict";

// The NetGPT chat page. The server streams each answer as newline-delimited JSON
// events (thinking, content, calling, tool, answer, error) and replays the same
// events for the conversation so far when the page loads.
//
// The page is a table with the network laid out on it (map.js), and the conversation
// a sheet of paper lying on the table's left. They speak one language of color: each
// device has a tone (registry.js), and wherever the conversation names something,
// the name wears its tone and a thread runs from it to the thing on the map
// (threads.js). Tool calls are cards that name what they asked of which devices,
// and while a call runs, the devices it is reading ripple on the map. Each view the
// model draws leaves a card in the conversation, with the view in miniature, that
// brings it back.
//
// Model output can echo device data, so it only ever reaches the page as text:
// through textContent, or through renderMarkdown, which escapes before it formats.
//
// Until the model draws something, the map shows the whole network, read from the
// engine's API (/api/v1/view) like any other frontend's, at the layer the viewer
// picks. A wide screen shows the map beside the conversation. A narrow one has no
// room for both, so they take turns on the whole screen, and the masthead's button
// switches between them. A new view doesn't take the screen in the middle of an
// answer: the button is marked instead, and the view's card opens it.

const conversation = document.getElementById("conversation");
const composer = document.getElementById("composer");
const question = document.getElementById("question");
const send = document.getElementById("send");
const status = document.getElementById("status");
const reset = document.getElementById("reset");
const model = document.getElementById("model");
const mapToggle = document.getElementById("map-toggle");
const layerButtons = [...document.querySelectorAll("#map-layers button")];
const narrow = matchMedia("(max-width: 999px)");

const EXAMPLES = [
  "Show me the network.",
  "How busy are the links between the sites?",
  "Is every OSPF adjacency up?",
];

// What each tool does, said as done and as under way.
const TOOLS = {
  list_devices: ["Listed the devices", "Listing the devices"],
  lldp_neighbors: ["Read LLDP neighbors", "Reading LLDP neighbors"],
  interfaces: ["Read interfaces", "Reading interfaces"],
  mac_table: ["Read MAC tables", "Reading MAC tables"],
  arp_table: ["Read ARP tables", "Reading ARP tables"],
  routes: ["Read routing tables", "Reading routing tables"],
  bgp_sessions: ["Read BGP sessions", "Reading BGP sessions"],
  ospf_neighbors: ["Read OSPF neighbors", "Reading OSPF neighbors"],
  ospf_interfaces: ["Read OSPF interfaces", "Reading OSPF interfaces"],
  interface_traffic: ["Read the traffic", "Reading the traffic"],
  topology: ["Merged the topology", "Merging the topology"],
  forwarding_path: ["Traced the path", "Tracing the path"],
  display: ["Drew on the map", "Drawing on the map"],
  chart: ["Charted", "Charting"],
  show: ["Ran", "Running"],
  ping: ["Pinged", "Pinging"],
  ping_sweep: ["Swept", "Sweeping"],
};
// Tools that read every device when none are named.
const EVERY_DEVICE = new Set([
  "lldp_neighbors", "interfaces", "mac_table", "arp_table", "routes", "bgp_sessions", "ospf_neighbors",
  "ospf_interfaces", "interface_traffic", "topology",
]);
// Arguments that name things, and arguments that are only how the call was asked.
const NAMING = ["devices", "device", "source", "destination", "focus", "highlight", "ports", "path"];
const QUIET = new Set(["title", "notes", "plane"]);
const LAYERS = { l2: "Cabling", l3: "Routing", ospf: "OSPF" };

let turn = null; // the reply being built, until its answer or error arrives
let timer = null;
let shown = ""; // the view on the map, as JSON, so a reload doesn't redraw it
let latest = null; // the newest view replayed on load: [view, card]
let network = null; // the request for the whole network, until it is drawn or overtaken
let showingNetwork = false; // the map shows the whole network, not a view from the model
let layer = "l2"; // the layer the whole network is drawn at
let left = { scroll: 0, atEnd: true }; // where the conversation was when the map took the screen

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

// ---- Markdown: a small, safe subset. Everything is escaped before it is formatted.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (text) => text.replace(/[&<>"']/g, (character) => ESCAPES[character]);
// Small models write arrows in LaTeX ($\to$), which the page has no renderer for.
const ARROWS = {
  to: "→", rightarrow: "→", leftarrow: "←", leftrightarrow: "↔", Rightarrow: "⇒", Leftarrow: "⇐", Leftrightarrow: "⇔",
};

function inline(text) {
  // Code spans become numbered placeholders, so nothing inside them is formatted
  // and emphasis can still wrap them (**`leaf1`**).
  const spans = [];
  const marked = text
    .replace(/\u0000/g, "")
    .replace(/`([^`\n]+)`/g, (_, code) => `\u0000${spans.push(code) - 1}\u0000`);
  return escapeHtml(marked)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,:;!?]|$)/g, "$1<em>$2</em>")
    .replace(/\$\\(to|rightarrow|leftarrow|leftrightarrow|Rightarrow|Leftarrow|Leftrightarrow)\$/g, (_, name) => ARROWS[name])
    .replace(/\u0000(\d+)\u0000/g, (_, index) => `<code>${escapeHtml(spans[Number(index)])}</code>`);
}

function renderMarkdown(source) {
  const html = [];
  let paragraph = [];
  let list = null;
  let table = null;
  let code = null;
  const flush = () => {
    if (paragraph.length) html.push(`<p>${inline(paragraph.join(" "))}</p>`);
    if (list) html.push(`</${list}>`);
    if (table) html.push(`<table>${table.join("")}</table>`);
    paragraph = [];
    list = null;
    table = null;
  };
  for (const raw of source.replace(/\r/g, "").split("\n")) {
    if (code) {
      if (/^\s*```/.test(raw)) {
        html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = null;
      } else {
        code.push(raw);
      }
      continue;
    }
    const line = raw.trim();
    let match;
    if (line.startsWith("```")) {
      flush();
      code = [];
    } else if (!line) {
      flush();
    } else if ((match = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flush();
      const level = Math.min(match[1].length + 2, 6);
      html.push(`<h${level}>${inline(match[2])}</h${level}>`);
    } else if (/^([-*_])(\s*\1){2,}$/.test(line)) {
      flush();
      html.push("<hr>");
    } else if (line.startsWith("|")) {
      const cells = line.replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      if (cells.every((cell) => /^:?-{2,}:?$/.test(cell))) continue; // the row under the header
      if (!table) {
        flush();
        table = [];
      }
      const tag = table.length ? "td" : "th";
      table.push(`<tr>${cells.map((cell) => `<${tag}>${inline(cell)}</${tag}>`).join("")}</tr>`);
    } else if ((match = /^(?:[-*+]|(\d+)[.)])\s+(.*)$/.exec(line))) {
      const kind = match[1] ? "ol" : "ul";
      if (list !== kind) {
        flush();
        html.push(kind === "ol" ? `<ol start="${Number(match[1])}">` : "<ul>");
        list = kind;
      }
      html.push(`<li>${inline(match[2])}</li>`);
    } else {
      if (list || table) flush();
      paragraph.push(line);
    }
  }
  if (code) html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  flush();
  return html.join("");
}

// ---- What a tool call asked of which devices

function names(value) {
  // The names an argument gives, whatever shape the model sent it in.
  if (value == null) return [];
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(names);
  if (typeof value === "object") return Object.values(value).flatMap(names);
  return [];
}

function touched(name, args) {
  // The nodes a tool call reads or names, as the registry knows them.
  const found = new Set();
  for (const key of NAMING) {
    for (const written of names(args[key])) {
      const id = netregistry.nodeNamed(written) || netregistry.nodeNamed(written.split(":")[0]);
      if (id) found.add(id);
    }
  }
  if (!found.size && EVERY_DEVICE.has(name)) for (const id of netregistry.deviceIds()) found.add(id);
  return [...found];
}

function summary(event) {
  // What came back, in a few words: rows, errors, or why it was refused.
  if (event.is_error) return event.result.replace(/^Error executing tool \w+: /, "").split(/(?<=[.:])\s/)[0];
  let body;
  try {
    body = JSON.parse(event.result);
  } catch {
    return ["display", "chart"].includes(event.name) ? "drawn" : "";
  }
  const plural = (count, word) => `${count.toLocaleString()} ${word}${count === 1 ? "" : "s"}`;
  const parts = [];
  if (Array.isArray(body.items)) parts.push(plural(body.items.length, "row"));
  else if (Array.isArray(body.result)) parts.push(plural(body.result.length, "row"));
  else if (Array.isArray(body.nodes)) parts.push(`${plural(body.nodes.length, "node")}, ${plural(body.links.length, "link")}`);
  else if (body.outcome) parts.push(body.outcome);
  else if (body.received !== undefined) parts.push(`${body.received} of ${body.sent} replies`);
  else if (typeof body.output === "string") parts.push(plural(body.output.split("\n").length, "line"));
  if (Array.isArray(body.errors) && body.errors.length) parts.push(plural(body.errors.length, "error"));
  return parts.join(" · ");
}

function argumentChips(name, args) {
  // How the call was asked, beyond which devices: the command, the filters, the layer.
  const chips = node("span", "args");
  if (name === "show" && args.command) {
    chips.append(node("code", "command", args.command));
    if (args.device) chips.append(" on ", args.device);
    return chips;
  }
  const asked = names(args.devices).concat(names(args.device), names(args.source), names(args.destination), names(args.path));
  for (const written of asked) chips.append(node("span", "who", written), " ");
  for (const [key, value] of Object.entries(args)) {
    if (NAMING.includes(key) || QUIET.has(key) || value === null || value === "" || (Array.isArray(value) && !value.length)) continue;
    const shownValue = Array.isArray(value) ? value.join(", ") : typeof value === "object" ? JSON.stringify(value) : String(value);
    chips.append(node("span", "filter", `${key} ${shownValue}`), " ");
  }
  for (const written of names(args.focus).concat(names(args.highlight), names(args.ports))) chips.append(node("span", "who", written), " ");
  return chips;
}

// ---- Rendering events

function reply() {
  if (!turn) {
    turn = node("div", "message assistant");
    conversation.append(turn);
  }
  return turn;
}

function lastBlock(kind) {
  const last = turn && turn.lastElementChild;
  return last && last.dataset.kind === kind ? last : null;
}

function closeThinking() {
  const block = lastBlock("thinking");
  if (block) {
    block.open = false;
    block.querySelector("summary").textContent = "Thought it through";
    block.classList.remove("live");
    netthreads.mark(block.querySelector(".thinking-text"));
  }
}

function pretty(result) {
  try {
    return JSON.stringify(JSON.parse(result), null, 2);
  } catch {
    return result;
  }
}

function toolCard(name, args) {
  const card = node("details", "tool live");
  card.dataset.kind = "tool";
  card.dataset.name = name;
  const head = node("summary");
  head.append(node("span", `glyph glyph-${name}`), node("span", "verb", (TOOLS[name] || [name, name])[1]));
  head.append(argumentChips(name, args), node("span", "meta", "…"));
  card.append(head);
  netthreads.mark(head.querySelector(".args"));
  card.touches = touched(name, args);
  card.addEventListener("pointerenter", () => {
    netmap.spotlight(card.touches);
    const verb = card.querySelector(".verb");
    netthreads.drawAll(card.touches.map((id) => [verb, { kind: "node", id }]));
  });
  card.addEventListener("pointerleave", () => {
    netmap.spotlight(null);
    netthreads.drawAll([]);
  });
  return card;
}

function finishCard(card, event) {
  card.classList.remove("live");
  card.classList.toggle("failed", event.is_error);
  card.querySelector(".verb").textContent = (TOOLS[event.name] || [event.name])[0];
  card.querySelector(".meta").textContent = summary(event) || "done";
  card.append(node("pre", "raw", pretty(event.result)));
}

function viewCard(view) {
  const card = node("button", "view-card");
  card.type = "button";
  const words = node("span", "words");
  words.append(node("span", "eyebrow", `${LAYERS[view.layer] || view.layer} · the model's view`), node("strong", "", view.title));
  card.append(netmap.thumbnail(view), words);
  card.addEventListener("click", () => {
    showView(view, card);
    if (narrow.matches) setScreen(true);
  });
  return card;
}

function mentionedIn(element) {
  // The nodes an element's mentions name: a link's two ends, a port's device.
  return [...element.querySelectorAll(".mention")].flatMap((mention) => (mention.dataset.nodes ? mention.dataset.nodes.split(" ") : [mention.dataset.id]));
}

function castOf(answer) {
  // Under an answer, what it names, and a way to see just those on the map with the links between them.
  const named = [...new Set(mentionedIn(answer))].filter((id) => netregistry.has(id));
  if (named.length < 2) return;
  const cast = node("div", "cast");
  cast.append(node("span", "eyebrow", `Named here · ${named.length}`));
  const list = node("span", "who");
  for (const id of named) list.append(node("span", "", id), " ");
  cast.append(list);
  netthreads.mark(list);
  const show = node("button", "", "Show these on the map");
  show.type = "button";
  show.addEventListener("click", () => {
    if (narrow.matches) setScreen(true);
    if (!netmap.focusOn(named, "Named in the answer")) return;
    for (const other of conversation.querySelectorAll(".view-card.active")) other.classList.remove("active");
  });
  cast.append(show);
  answer.after(cast);
}

function handle(event, live) {
  switch (event.type) {
    case "user": {
      conversation.querySelector(".intro")?.remove();
      const note = node("div", "message user", event.text);
      conversation.append(note);
      netthreads.mark(note);
      turn = null;
      break;
    }
    case "thinking": {
      let block = lastBlock("thinking");
      if (!block) {
        block = node("details", "thinking");
        block.dataset.kind = "thinking";
        block.open = live;
        if (live) block.classList.add("live");
        block.append(node("summary", "", live ? "Thinking" : "Thought it through"), node("div", "thinking-text"));
        reply().append(block);
      }
      block.querySelector(".thinking-text").textContent += event.text;
      if (!live) netthreads.mark(block.querySelector(".thinking-text"));
      break;
    }
    case "content": {
      closeThinking();
      let block = lastBlock("draft");
      if (!block) {
        block = node("div", "draft");
        block.dataset.kind = "draft";
        reply().append(block);
      }
      block.textContent += event.text;
      break;
    }
    case "calling": {
      closeThinking();
      narrated();
      const card = toolCard(event.name, event.arguments || {});
      reply().append(card);
      netmap.reading(card.touches.filter((id) => netregistry.node(id)?.data.kind === "device"));
      setActivity((TOOLS[event.name] || [event.name, event.name])[1]);
      break;
    }
    case "tool": {
      closeThinking();
      narrated();
      // Live, the card went up when the call started; replayed, it goes up now.
      let card = live ? [...reply().querySelectorAll("details.tool.live")].find((open) => open.dataset.name === event.name) : null;
      if (!card) {
        card = toolCard(event.name, event.arguments || {});
        reply().append(card);
      }
      finishCard(card, event);
      netmap.reading(null);
      setActivity("");
      if (event.chart) netchart.add(reply(), event.chart, { toneOf: netregistry.farTone });
      if (event.view) {
        netregistry.learn(event.view);
        const viewed = viewCard(event.view);
        reply().append(viewed);
        if (live) {
          showView(event.view, viewed);
          if (!onMap()) markNew();
        } else {
          latest = [event.view, viewed];
        }
      }
      break;
    }
    case "answer": {
      closeThinking();
      const block = lastBlock("draft") || reply().appendChild(node("div"));
      block.className = "answer";
      block.dataset.kind = "answer";
      block.innerHTML = renderMarkdown(event.text);
      netthreads.mark(block);
      netmap.mention(mentionedIn(block));
      castOf(block);
      turn = null;
      if (live && onMap()) markNew();
      break;
    }
    case "error": {
      closeThinking();
      narrated();
      reply().append(node("div", "error", event.text));
      netmap.reading(null);
      turn = null;
      if (live && onMap()) markNew();
      break;
    }
  }
}

function narrated() {
  // Text the model wrote before calling a tool was narration, not the answer.
  const draft = lastBlock("draft");
  if (draft) {
    draft.dataset.kind = "note";
    draft.className = "note";
    netthreads.mark(draft);
  }
}

function following(render) {
  // Keep the newest output in view, unless the reader has scrolled up.
  const atEnd = conversation.scrollTop + conversation.clientHeight >= conversation.scrollHeight - 120;
  render();
  if (atEnd) conversation.scrollTop = conversation.scrollHeight;
}

// ---- The map

function showView(view, card) {
  const key = JSON.stringify(view);
  if (key !== shown) netmap.show(view, "Drawn by the model");
  shown = key;
  showingNetwork = false;
  for (const other of conversation.querySelectorAll(".view-card.active")) other.classList.remove("active");
  card.classList.add("active");
  document.body.classList.remove("map-hidden");
  mapToggle.hidden = false;
  pressLayer(null);
  labelToggle();
}

async function showNetwork(at = layer) {
  // The whole network at a layer: before the model draws anything, or when the viewer
  // picks a layer. A view from the model that lands first wins.
  const asked = (network = {});
  layer = at;
  pressLayer(at);
  let view;
  try {
    const query = new URLSearchParams({ layer: at, title: "The whole network" });
    const response = await fetch(`api/v1/view?${query}`);
    if (!response.ok) return; // The map stays as it is until the model draws on it.
    view = await response.json();
  } catch {
    return;
  }
  if (network !== asked) return;
  netmap.show(view, "The whole network");
  shown = JSON.stringify(view);
  showingNetwork = true;
  for (const other of conversation.querySelectorAll(".view-card.active")) other.classList.remove("active");
  mapToggle.hidden = false;
  labelToggle();
  inventoryIntro();
}

function pressLayer(at) {
  for (const button of layerButtons) button.setAttribute("aria-pressed", String(button.dataset.layer === at));
}

for (const button of layerButtons) {
  button.addEventListener("click", () => {
    network = null;
    showNetwork(button.dataset.layer);
  });
}

function clearMap() {
  netmap.clear();
  netthreads.clear();
  shown = "";
  network = null;
  showingNetwork = false;
  setScreen(false);
  mapToggle.hidden = true;
  mapToggle.classList.remove("fresh");
  document.body.classList.remove("map-hidden");
  labelToggle();
}

// A mention of something the map isn't drawing: draw the whole network at a layer that has it.
netthreads.whenAbsent((target) => {
  const known = netregistry.node(target.id);
  if (!known) return false;
  const at = ["l2", "l3", "ospf"].find((candidate) => known.layers.has(candidate));
  if (!at) return false;
  network = null;
  showNetwork(at);
  return true;
});

// What the inspector says about the conversation: how often a thing comes up, and a way to ask about it.
netmap.on("inspect", (id, panel) => {
  const box = node("div", "talk");
  const count = netthreads.count(id);
  const label = netregistry.label(id);
  const isLink = !netregistry.has(id);
  if (count) {
    const where = node("button", "", `Mentioned ${count === 1 ? "once" : `${count} times`} · show`);
    where.type = "button";
    where.addEventListener("click", () => {
      if (narrow.matches) setScreen(false);
      netthreads.revealLast(id);
    });
    box.append(where);
  }
  const ask = node("button", "ask", isLink ? "Ask about this link" : `Ask about ${label}`);
  ask.type = "button";
  ask.addEventListener("click", () => {
    const view = netmap.view();
    const link = isLink && view && view.links.find((candidate) => candidate.id === id);
    const about = link ? `the link between ${link.a.node} ${link.a.interface || ""} and ${link.b.node} ${link.b.interface || ""}` : label;
    question.value = `Tell me about ${about.replace(/\s+/g, " ").trim()}.`;
    if (narrow.matches) setScreen(false);
    question.focus();
    grow();
  });
  box.append(ask);
  if (!isLink) {
    const around = node("button", "", "Focus here");
    around.type = "button";
    around.addEventListener("click", () => netmap.focusOn([id]));
    box.append(around);
  }
  panel.append(box);
});

const onMap = () => document.body.classList.contains("map-full");

function labelToggle() {
  // Wide, the button shows or hides the map beside the conversation. Narrow, it names
  // the screen it leads to, so it isn't a toggle with a pressed state.
  const fresh = mapToggle.classList.contains("fresh");
  const name = narrow.matches && onMap() ? "Chat" : "Map";
  mapToggle.textContent = name;
  if (fresh) mapToggle.setAttribute("aria-label", `${name}, with something new`);
  else mapToggle.removeAttribute("aria-label");
  if (narrow.matches) mapToggle.removeAttribute("aria-pressed");
  else mapToggle.setAttribute("aria-pressed", String(!document.body.classList.contains("map-hidden")));
}

function markNew() {
  // On a narrow screen, what arrives on the screen out of sight marks the button that leads to it.
  if (!narrow.matches) return;
  mapToggle.classList.add("fresh");
  labelToggle();
}

function setScreen(map) {
  // On a narrow screen: give the whole screen to the map, or back to the conversation.
  if (map === onMap()) return;
  if (map) left = { scroll: conversation.scrollTop, atEnd: conversation.scrollTop + conversation.clientHeight >= conversation.scrollHeight - 120 };
  document.body.classList.toggle("map-full", map);
  mapToggle.classList.remove("fresh");
  labelToggle();
  if (!map) conversation.scrollTop = left.atEnd ? conversation.scrollHeight : left.scroll;
  // Charts drawn while out of sight had no width to fit; this redraws them.
  window.dispatchEvent(new Event("resize"));
}

mapToggle.addEventListener("click", () => {
  if (narrow.matches) {
    setScreen(!onMap());
    return;
  }
  document.body.classList.toggle("map-hidden");
  netthreads.drawAll([]);
  labelToggle();
});

narrow.addEventListener("change", () => {
  // Widening the window puts the map back beside the conversation.
  if (!narrow.matches) {
    setScreen(false);
    mapToggle.classList.remove("fresh");
  }
  labelToggle();
});

// On a narrow screen, choosing a mention in the conversation goes to the map to show it.
netmap.on("select", (id) => {
  if (id && narrow.matches && !onMap() && document.activeElement?.closest?.(".mention")) setScreen(true);
});

// ---- Status and controls

let activity = "";

function setActivity(text) {
  activity = text;
}

function setBusy(busy, label = "Answering") {
  send.disabled = busy;
  reset.disabled = busy;
  mapToggle.classList.toggle("busy", busy);
  clearInterval(timer);
  status.classList.toggle("live", busy);
  status.textContent = "";
  if (!busy) return;
  const started = Date.now();
  const tick = () => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    status.textContent = activity ? `${activity}… ${clock}` : `${label}… ${clock}`;
  };
  tick();
  timer = setInterval(tick, 1000);
}

function showIntro() {
  const intro = node("div", "intro");
  intro.append(node("p", "eyebrow", "Read-only, on every device in the inventory"));
  intro.append(node("h2", "", "Ask the network."));
  intro.append(
    node("p", "lede", "Every name in the conversation is tied to the map: point at one to see where it is, and point at the map to see where it came up."),
  );
  intro.append(node("div", "table-of-contents"));
  const examples = node("div", "examples");
  for (const example of EXAMPLES) {
    const button = node("button", "", example);
    button.type = "button";
    button.addEventListener("click", () => {
      question.value = example;
      question.focus();
      grow();
    });
    examples.append(button);
  }
  intro.append(examples);
  conversation.append(intro);
  inventoryIntro();
}

function inventoryIntro() {
  // The devices on the table, each a mention of itself, so the threads work before any question.
  const contents = conversation.querySelector(".intro .table-of-contents");
  if (!contents) return;
  const devices = netregistry.deviceIds();
  contents.replaceChildren();
  if (!devices.length) return;
  contents.append(node("span", "eyebrow", "On the table"));
  const list = node("p", "devices");
  for (const id of devices) list.append(node("span", "", id), " ");
  contents.append(list);
  netthreads.mark(list);
}

function grow() {
  question.style.height = "auto";
  question.style.height = `${question.scrollHeight}px`;
}

async function ask(text) {
  following(() => handle({ type: "user", text }, true));
  setBusy(true);
  let finished = false;
  try {
    const response = await fetch("api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text }),
    });
    if (response.status === 401) {
      location.reload();
      return;
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      handle({ type: "error", text: body.error || `HTTP ${response.status}` }, true);
      finished = true;
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        finished ||= event.type === "answer" || event.type === "error";
        following(() => handle(event, true));
      }
    }
  } catch (error) {
    handle({ type: "error", text: `Lost the connection: ${error.message}. Reload to see whether the answer finished.` }, true);
    finished = true;
  } finally {
    if (!finished) handle({ type: "error", text: "The answer stopped before it finished; the netgpt-chat log says why." }, true);
    setActivity("");
    setBusy(false);
    question.focus();
  }
}

async function load() {
  const response = await fetch("api/conversation");
  if (response.status === 401) {
    location.reload();
    return;
  }
  const data = await response.json();
  model.textContent = data.think ? `${data.model} · thinking` : data.model;
  // The whole network first, so the conversation's names have something to be tied to.
  if (!showingNetwork && !shown) await showNetwork(layer);
  conversation.replaceChildren();
  turn = null;
  latest = null;
  for (const event of data.events) handle(event, false);
  if (!data.events.length) showIntro();
  if (latest) showView(...latest);
  conversation.scrollTop = conversation.scrollHeight;
  if (data.busy) {
    // Another page is waiting on an answer; show it when it lands.
    setBusy(true, "Answering a question from another page");
    setTimeout(load, 5000);
  } else {
    setBusy(false);
  }
}

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = question.value.trim();
  if (!text || send.disabled) return;
  question.value = "";
  grow();
  ask(text);
});

question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

question.addEventListener("input", grow);

reset.addEventListener("click", async () => {
  const response = await fetch("api/reset", { method: "POST" });
  if (response.ok) {
    conversation.replaceChildren();
    turn = null;
    clearMap();
    showIntro();
    showNetwork(layer);
    question.focus();
  } else {
    const body = await response.json().catch(() => ({}));
    status.textContent = body.error || `HTTP ${response.status}`;
  }
});

load();
