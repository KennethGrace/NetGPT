"use strict";

// The NetGPT chat page. The server streams each answer as newline-delimited JSON
// events (thinking, content, tool, answer, error) and replays the same events
// for the conversation so far when the page loads.
//
// Model output can echo device data, so it only ever reaches the page as text:
// through textContent, or through renderMarkdown, which escapes before it formats.

const conversation = document.getElementById("conversation");
const composer = document.getElementById("composer");
const question = document.getElementById("question");
const send = document.getElementById("send");
const status = document.getElementById("status");
const reset = document.getElementById("reset");
const model = document.getElementById("model");

const EXAMPLES = [
  "Which devices are in the inventory?",
  "What is cabled to each device?",
  "Show me every device's routing table.",
];

let turn = null; // the reply being built, until its answer or error arrives
let timer = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

// ---- Markdown: a small, safe subset. Everything is escaped before it is formatted.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (text) => text.replace(/[&<>"']/g, (character) => ESCAPES[character]);

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
    .replace(/\$\\(?:to|rightarrow)\$/g, "→")
    .replace(/\$\\leftarrow\$/g, "←")
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
      break;
    }
    case "answer": {
      closeThinking();
      const block = lastBlock("draft") || reply().appendChild(node("div"));
      block.className = "answer";
      block.dataset.kind = "answer";
      block.innerHTML = renderMarkdown(event.text);
      turn = null;
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

// ---- Status and controls

function setBusy(busy, label = "Answering") {
  send.disabled = busy;
  reset.disabled = busy;
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
  for (const event of data.events) handle(event, false);
  if (!data.events.length) showIntro();
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
    showIntro();
    question.focus();
  } else {
    const body = await response.json().catch(() => ({}));
    status.textContent = body.error || `HTTP ${response.status}`;
  }
});

load();
