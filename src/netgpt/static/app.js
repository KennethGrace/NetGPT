"use strict";

// The NetGPT chat page. The server streams each answer as newline-delimited JSON
// events (thinking, content, tool, answer, error) and replays the same events
// for the conversation so far when the page loads.
//
// Model output can echo device data, so it only ever reaches the page as text:
// through textContent, or through renderMarkdown, which escapes before it formats.
//
// When the model calls display, its tool event carries a view, which the map
// (map.js) draws. Each view leaves a chip in the conversation that brings it back.
// When it calls chart, the event carries a chart, drawn in the conversation (chart.js).
//
// A wide screen shows the map beside the conversation. A narrow one has no room for
// both, so they take turns on the whole screen, and the header's button switches
// between them. A new view doesn't take the screen in the middle of an answer:
// the button is marked instead, and the view's chip opens it.

const conversation = document.getElementById("conversation");
const composer = document.getElementById("composer");
const question = document.getElementById("question");
const send = document.getElementById("send");
const status = document.getElementById("status");
const reset = document.getElementById("reset");
const model = document.getElementById("model");
const mapToggle = document.getElementById("map-toggle");
const narrow = matchMedia("(max-width: 999px)");

const EXAMPLES = [
  "Show me the network.",
  "How busy are the fabric links?",
  "What is cabled to each device?",
];

let turn = null; // the reply being built, until its answer or error arrives
let timer = null;
let shown = ""; // the view on the map, as JSON, so a reload doesn't redraw it
let latest = null; // the newest view replayed on load: [view, chip]
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
    block.querySelector("summary").textContent = "Thinking";
  }
}

function pretty(result) {
  try {
    return JSON.stringify(JSON.parse(result), null, 2);
  } catch {
    return result;
  }
}

function handle(event, live) {
  switch (event.type) {
    case "user":
      conversation.querySelector(".intro")?.remove();
      conversation.append(node("div", "message user", event.text));
      turn = null;
      break;
    case "thinking": {
      let block = lastBlock("thinking");
      if (!block) {
        block = node("details", "thinking");
        block.dataset.kind = "thinking";
        block.open = live;
        block.append(node("summary", "", live ? "Thinking…" : "Thinking"), node("div", "thinking-text"));
        reply().append(block);
      }
      block.querySelector(".thinking-text").textContent += event.text;
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
    case "tool": {
      closeThinking();
      // Text the model wrote before calling a tool was narration, not the answer.
      const draft = lastBlock("draft");
      if (draft) {
        draft.dataset.kind = "note";
        draft.className = "note";
      }
      const step = node("details", event.is_error ? "tool failed" : "tool");
      step.dataset.kind = "tool";
      const args = Object.entries(event.arguments || {})
        .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
        .join(", ");
      step.append(node("summary", "", `${event.name}(${args})`), node("pre", "", pretty(event.result)));
      reply().append(step);
      if (event.chart) netchart.add(reply(), event.chart);
      if (event.view) {
        const chip = node("button", "view-chip", event.view.title);
        chip.type = "button";
        chip.addEventListener("click", () => {
          showView(event.view, chip);
          if (narrow.matches) setScreen(true);
        });
        reply().append(chip);
        if (live) {
          showView(event.view, chip);
          if (!onMap()) markNew();
        } else {
          latest = [event.view, chip];
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
      turn = null;
      if (live && onMap()) markNew();
      break;
    }
    case "error": {
      closeThinking();
      const draft = lastBlock("draft");
      if (draft) {
        draft.dataset.kind = "note";
        draft.className = "note";
      }
      reply().append(node("div", "error", event.text));
      turn = null;
      if (live && onMap()) markNew();
      break;
    }
  }
}

function following(render) {
  // Keep the newest output in view, unless the reader has scrolled up.
  const atEnd = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  render();
  if (atEnd) window.scrollTo(0, document.body.scrollHeight);
}

// ---- The map

function showView(view, chip) {
  const key = JSON.stringify(view);
  if (key !== shown) netmap.show(view);
  shown = key;
  for (const other of conversation.querySelectorAll(".view-chip.active")) other.classList.remove("active");
  chip.classList.add("active");
  document.body.classList.remove("map-hidden");
  mapToggle.hidden = false;
  labelToggle();
}

function clearMap() {
  netmap.clear();
  shown = "";
  setScreen(false);
  mapToggle.hidden = true;
  mapToggle.classList.remove("fresh");
  document.body.classList.remove("map-hidden");
  labelToggle();
}

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
  if (map) left = { scroll: window.scrollY, atEnd: window.innerHeight + window.scrollY >= document.body.scrollHeight - 120 };
  document.body.classList.toggle("map-full", map);
  mapToggle.classList.remove("fresh");
  labelToggle();
  if (!map) window.scrollTo(0, left.atEnd ? document.body.scrollHeight : left.scroll);
  // Charts drawn while out of sight had no width to fit; this redraws them.
  window.dispatchEvent(new Event("resize"));
}

mapToggle.addEventListener("click", () => {
  if (narrow.matches) {
    setScreen(!onMap());
    return;
  }
  document.body.classList.toggle("map-hidden");
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

// ---- Status and controls

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
    status.textContent = `${label}… ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  };
  tick();
  timer = setInterval(tick, 1000);
}

function showIntro() {
  const intro = node("div", "intro");
  intro.append(node("h2", "", "Ask about the network"));
  intro.append(node("p", "", "Read-only access to the devices in the inventory."));
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
  conversation.replaceChildren();
  turn = null;
  latest = null;
  for (const event of data.events) handle(event, false);
  if (!data.events.length) showIntro();
  if (latest) showView(...latest);
  else clearMap();
  window.scrollTo(0, document.body.scrollHeight);
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
    question.focus();
  } else {
    const body = await response.json().catch(() => ({}));
    status.textContent = body.error || `HTTP ${response.status}`;
  }
});

load();
