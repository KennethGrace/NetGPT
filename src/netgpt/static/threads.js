"use strict";

// Threads: what ties the conversation to the map.
//
// Every name the registry knows (registry.js) is marked where the conversation writes
// it: devices, hosts, networks, MACs, ports (hq-core ethernet-1/1), and links
// (a--b). A mention wears its node's tone. Pointing at a mention runs a thread across
// the page to the thing on the map and lights it there; choosing one selects it in
// the map's inspector. Pointing at a node on the map runs threads the other way, to
// every mention of it in sight, and the rail beside the conversation marks where
// the rest are, so a thing's whole history in the conversation is a click away.
//
// Threads are drawn in a layer over the whole page that takes no pointer events, and
// follow their ends as the conversation scrolls and the map moves. Mentions are made
// from text nodes, so device data stays text.

const netthreads = (() => {
  const SVG = "http://www.w3.org/2000/svg";
  const layer = document.getElementById("threads");
  const conversation = document.getElementById("conversation");
  const rail = document.getElementById("rail");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const narrow = matchMedia("(max-width: 999px)");
  // A word that could be a name: letters, digits, and the punctuation names and addresses are made of.
  const WORD = /[A-Za-z0-9][A-Za-z0-9._:/-]*[A-Za-z0-9]/g;
  const BLOCK = "p, li, td, th, h3, h4, h5, h6, summary, .thinking-text, .note, .message.user, .draft, .args, .evidence li";
  const MOST_THREADS = 12;

  let threads = []; // {from: Element, to: target, tone, group, path, knots}
  let frame = null;
  let lit = null; // what the map points at: {id, by: "hover" | "select"}
  let chosen = null; // what the map has selected
  let held = null; // a mention chosen in the conversation, whose thread stays a moment
  let absent = () => false; // what to do with a mention of something the map isn't drawing; set by the page

  function shape(tag, attributes = {}) {
    const element = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    return element;
  }

  // ---- Marking names

  function toneOf(target) {
    if (target.kind === "link") {
      const tones = new Set(target.nodes.map(netregistry.tone));
      return tones.size === 1 ? [...tones][0] : 0;
    }
    return netregistry.tone(target.id);
  }

  function mentionOf(word, target) {
    const mention = document.createElement("span");
    mention.className = `mention kind-${target.kind} tone-${toneOf(target)}`;
    mention.textContent = word;
    mention.dataset.id = target.id;
    if (target.port) mention.dataset.port = target.port;
    if (target.nodes) mention.dataset.nodes = target.nodes.join(" ");
    mention.tabIndex = 0;
    mention.setAttribute("role", "button");
    return mention;
  }

  function mark(root, except = null) {
    // Wrap each name in root's text in a mention, except what names except. Returns the IDs named, in order.
    const named = [];
    if (!root) return named;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (text) => (text.parentElement.closest("pre, .mention, button, .raw") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const texts = [];
    while (walker.nextNode()) texts.push(walker.currentNode);
    let after = null;
    let block = null;
    for (const text of texts) {
      // A port is a device's when it follows the device in the same sentence or cell.
      const here = text.parentElement.closest(BLOCK);
      if (here !== block) {
        block = here;
        after = null;
      }
      const value = text.nodeValue;
      const parts = [];
      let start = 0;
      let since = 0;
      for (const match of value.matchAll(WORD)) {
        if (/[.!?;]\s/.test(value.slice(since, match.index))) after = null; // a new sentence
        since = match.index + match[0].length;
        const target = netregistry.resolve(match[0], after);
        if (!target || target.id === except) continue;
        if (target.kind === "node") after = target.id;
        parts.push(value.slice(start, match.index), mentionOf(match[0], target));
        named.push(target.id);
        start = match.index + match[0].length;
      }
      if (!parts.length) continue;
      parts.push(value.slice(start));
      text.replaceWith(...parts.filter((part) => part !== ""));
    }
    return named;
  }

  function targetOf(mention) {
    const { id, port, nodes } = mention.dataset;
    if (nodes) return { kind: "link", id, nodes: nodes.split(" ") };
    return port ? { kind: "port", id, port } : { kind: "node", id };
  }

  const spotted = (target) => (target.kind === "link" ? [target.id, ...target.nodes] : [target.id]);

  // ---- Drawing threads

  function thread(from, to, tone) {
    const group = shape("g", { class: `thread tone-${tone}` });
    const run = shape("path", { class: "run" });
    const shadow = shape("path", { class: "shadow" });
    const path = shape("path", { class: "string" });
    const start = shape("circle", { class: "knot", r: "3" });
    const end = shape("circle", { class: "knot end", r: "4.5" });
    group.append(run, shadow, path, start, end);
    layer.append(group);
    const entry = { from, to, tone, group, run, path, shadow, start, end, fresh: true };
    threads.push(entry);
    return entry;
  }

  function visible(element) {
    // Whether a mention is in sight: in the conversation's scrolled view, or wherever else it is shown.
    const box = element.getBoundingClientRect();
    if (!box.width && !box.height) return null;
    const scroller = element.closest("#conversation");
    if (scroller) {
      const view = scroller.getBoundingClientRect();
      if (box.bottom < view.top + 4 || box.top > view.bottom - 4) return null;
    }
    return box;
  }

  function place(entry) {
    const box = visible(entry.from);
    const end = netmap.anchor(entry.to);
    entry.group.classList.toggle("hidden", !box || !end);
    if (!box || !end) return;
    // On the sheet, the thread leaves along the gap under the mention's line, like its underline
    // run on, so it crosses no words, and leaps from the paper's edge to the thing on the table.
    // Elsewhere (the inspector's card) it goes straight from the side of the mention facing the thing.
    const sheet = entry.from.closest("#sheet, #masthead")?.getBoundingClientRect();
    const rightward = end.x >= box.left + box.width / 2;
    let [sx, sy] = [rightward ? box.right + 2 : box.left - 2, box.top + box.height / 2];
    let run = "";
    if (sheet && rightward && end.x > sheet.right) {
      [sx, sy] = [box.left + 1, box.bottom + 1.5];
      run = `M${sx.toFixed(1)},${sy.toFixed(1)} H${(sheet.right + 6).toFixed(1)}`;
    }
    const fromX = run ? sheet.right + 6 : sx;
    const [dx, dy] = [end.x - fromX, end.y - sy];
    const length = Math.hypot(dx, dy) || 1;
    const [ex, ey] = [end.x - (dx / length) * (end.radius + 3), end.y - (dy / length) * (end.radius + 3)];
    const bend = Math.max(Math.abs(ex - fromX) * 0.45, 30) * (rightward ? 1 : -1);
    const d = `M${fromX.toFixed(1)},${sy.toFixed(1)} C${(fromX + bend).toFixed(1)},${sy.toFixed(1)} ${(ex - bend * 0.6).toFixed(1)},${ey.toFixed(1)} ${ex.toFixed(1)},${ey.toFixed(1)}`;
    entry.run.setAttribute("d", run);
    entry.path.setAttribute("d", d);
    entry.shadow.setAttribute("d", d);
    entry.start.setAttribute("cx", fromX.toFixed(1));
    entry.start.setAttribute("cy", sy.toFixed(1));
    entry.end.setAttribute("cx", ex.toFixed(1));
    entry.end.setAttribute("cy", ey.toFixed(1));
    if (entry.fresh && !still.matches) {
      // Drawn out from the mention, the first time it is placed.
      const total = entry.path.getTotalLength();
      entry.path.style.setProperty("--length", `${total.toFixed(0)}`);
      entry.group.classList.add("drawing");
      entry.group.addEventListener("animationend", () => entry.group.classList.remove("drawing"), { once: true });
    }
    entry.fresh = false;
  }

  function follow() {
    frame = null;
    for (const entry of threads) place(entry);
    if (threads.length) frame = requestAnimationFrame(follow);
  }

  function drawAll(pairs) {
    // Replace the threads with these, as [from element, to target] pairs.
    for (const entry of threads) entry.group.remove();
    threads = [];
    if (narrow.matches) return; // On a narrow screen the map and the conversation aren't on screen together.
    for (const [from, to] of pairs.slice(0, MOST_THREADS)) thread(from, to, toneOf(to));
    if (threads.length && frame === null) frame = requestAnimationFrame(follow);
  }

  // ---- From the conversation to the map

  function point(mention) {
    const target = targetOf(mention);
    held = null;
    // Something the map isn't drawing now is marked so; choosing it draws a layer that has it.
    mention.classList.toggle("absent", !netmap.shows(target.id));
    if (!netmap.shows(target.id)) {
      drawAll([]);
      return;
    }
    netmap.spotlight(spotted(target));
    drawAll([[mention, target]]);
  }

  function unpoint() {
    if (held) return;
    drawAll([]);
    netmap.spotlight(null);
    if (lit) light(lit.id, lit.by);
  }

  function choose(mention) {
    // Select what a mention names on the map, and keep its thread up a moment.
    const target = targetOf(mention);
    if (!netmap.shows(target.id) && !absent(target)) return;
    const showing = () => {
      const port = target.kind === "port" ? netmap.anchor(target)?.link : null;
      netmap.select(port || target.id);
      netmap.spotlight(spotted(target));
      drawAll([[mention, target]]);
      held = mention;
      setTimeout(() => {
        if (held !== mention) return;
        held = null;
        unpoint();
      }, 2400);
    };
    // A map that has just been drawn needs a moment to place what the mention names.
    if (netmap.shows(target.id)) showing();
    else setTimeout(showing, 600);
  }

  const mentionAt = (event) => event.target.closest?.(".mention");

  function listen(root) {
    root.addEventListener("pointerover", (event) => {
      const mention = mentionAt(event);
      if (mention) point(mention);
    });
    root.addEventListener("pointerout", (event) => {
      const mention = mentionAt(event);
      if (mention && !mention.contains(event.relatedTarget)) unpoint();
    });
    root.addEventListener("click", (event) => {
      const mention = mentionAt(event);
      if (mention) choose(mention);
    });
    root.addEventListener("keydown", (event) => {
      const mention = mentionAt(event);
      if (mention && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        choose(mention);
      }
    });
    root.addEventListener("focusin", (event) => {
      const mention = mentionAt(event);
      if (mention) point(mention);
    });
    root.addEventListener("focusout", (event) => {
      if (mentionAt(event)) unpoint();
    });
  }

  // ---- From the map to the conversation

  function mentionsOf(id) {
    // Every mention in the conversation of a node, of a link, or of a link's ends.
    const view = netmap.view();
    const link = view && view.links.find((candidate) => candidate.id === id);
    const ids = new Set(link ? [id, link.a.node, link.b.node] : [id]);
    return [...conversation.querySelectorAll(".mention")].filter((mention) => ids.has(mention.dataset.id));
  }

  function light(id, by = "hover") {
    // Light every mention of what the map points at, with a thread to each one in sight.
    for (const glowing of conversation.querySelectorAll(".mention.glow")) glowing.classList.remove("glow");
    lit = id ? { id, by } : null;
    if (!id) {
      drawAll([]);
      ticks(chosen);
      return;
    }
    const found = mentionsOf(id);
    for (const mention of found) mention.classList.add("glow");
    if (by === "hover") {
      const inSight = found.filter((mention) => visible(mention)).reverse();
      drawAll(inSight.map((mention) => [mention, targetOf(mention)]));
    } else {
      drawAll([]); // A selection is marked on the mentions and the rail, without threads.
    }
    ticks(id);
  }

  function ticks(id) {
    // Mark on the rail where in the conversation a thing is mentioned.
    rail.replaceChildren();
    if (!id) return;
    const found = mentionsOf(id);
    const height = conversation.scrollHeight || 1;
    const top = conversation.getBoundingClientRect().top;
    rail.classList.toggle("shown", found.length > 0);
    for (const mention of found) {
      const at = mention.getBoundingClientRect().top - top + conversation.scrollTop;
      const tick = document.createElement("button");
      tick.type = "button";
      tick.className = `tick tone-${netregistry.tone(mention.dataset.id)}`;
      tick.style.setProperty("--at", `${((at / height) * 100).toFixed(2)}%`);
      tick.title = mention.textContent;
      tick.setAttribute("aria-label", `Mention of ${mention.textContent}`);
      tick.addEventListener("click", () => reveal(mention));
      rail.append(tick);
    }
  }

  function reveal(mention) {
    mention.scrollIntoView({ block: "center", behavior: still.matches ? "auto" : "smooth" });
    mention.classList.remove("flash");
    // Restart the flash: reading its size makes the browser start the animation again.
    void mention.offsetWidth;
    mention.classList.add("flash");
  }

  function revealLast(id) {
    const found = mentionsOf(id);
    if (found.length) reveal(found[found.length - 1]);
    return found.length;
  }

  netmap.on("hover", (id) => {
    if (held) return;
    if (id) light(id, "hover");
    else light(chosen, "select");
  });

  netmap.on("select", (id) => {
    chosen = id;
    for (const old of conversation.querySelectorAll(".mention.chosen")) old.classList.remove("chosen");
    if (id) for (const mention of mentionsOf(id)) mention.classList.add("chosen");
    if (!lit || lit.by === "select") light(id, "select");
    // The inspector's evidence names devices and ports too.
    mark(document.querySelector("#map-inspector .evidence"), id);
  });

  listen(conversation);
  listen(document.getElementById("map-inspector"));
  conversation.addEventListener("scroll", () => {
    if (lit && lit.by === "hover") light(lit.id, "hover");
  }, { passive: true });
  narrow.addEventListener("change", () => drawAll([]));

  function whenAbsent(handler) {
    absent = handler;
  }

  function count(id) {
    return mentionsOf(id).length;
  }

  function clear() {
    drawAll([]);
    rail.replaceChildren();
    lit = chosen = held = null;
  }

  return { mark, listen, light, ticks, count, revealLast, whenAbsent, clear, drawAll };
})();
