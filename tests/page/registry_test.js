// Tests for the page's registry (src/netgpt/static/registry.js): the names a mention
// in the conversation resolves to, and the tones nodes wear. Run by tests/test_page.py,
// with Node's own test runner: node --test tests/page/registry_test.js
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function registry() {
  // A fresh registry, loaded as the page loads it: a script that defines netregistry.
  const source = fs.readFileSync(path.join(__dirname, "../../src/netgpt/static/registry.js"), "utf8");
  const context = vm.createContext({});
  vm.runInContext(`${source}\nthis.netregistry = netregistry;`, context);
  return context.netregistry;
}

const node = (id, kind, extra = {}) => ({ id, kind, label: id, addresses: [], macs: [], ...extra });
const end = (nodeId, port = null) => ({ node: nodeId, interface: port });
const link = (a, b) => ({ id: `${a.node}--${b.node}`, a, b });

// Two sites' cores (a device has its domain), a switch with a host behind it, and an LLDP neighbor.
const LAYER2 = {
  layer: "l2",
  nodes: [
    node("core1.example.net", "device", { addresses: ["192.0.2.1"] }),
    node("core2.example.net", "device", { addresses: ["192.0.2.2"] }),
    node("sw1", "device"),
    node("198.51.100.7", "host", { addresses: ["198.51.100.7"], macs: ["02:00:c0:ff:ee:01"] }),
    node("server1", "neighbor"),
  ],
  links: [
    link(end("core1.example.net", "Ethernet1/1"), end("core2.example.net", "Ethernet1/1")),
    link(end("core1.example.net", "Ethernet1/24"), end("sw1", "ethernet-1/49")),
    link(end("198.51.100.7"), end("sw1", "ethernet-1/2")),
    link(end("server1", "eth0"), end("core2.example.net", "Ethernet1/9")),
  ],
};

test("devices take the tones in inventory order, and what hangs off one wears its tone", () => {
  const known = registry();
  known.learn(LAYER2);
  assert.deepEqual(["core1.example.net", "core2.example.net", "sw1"].map(known.tone), [1, 2, 3]);
  assert.equal(known.tone("198.51.100.7"), 3); // behind sw1
  assert.equal(known.tone("server1"), 2); // cabled to core2
});

test("a node is named by its ID, its address, or a MAC in any spelling", () => {
  const known = registry();
  known.learn(LAYER2);
  for (const word of ["sw1", "192.0.2.2", "198.51.100.7", "0200.c0ff.ee01", "02-00-C0-FF-EE-01"]) {
    assert.equal(known.resolve(word)?.kind, "node", word);
  }
  assert.equal(known.resolve("192.0.2.1/24").id, "core1.example.net");
  assert.equal(known.resolve("hello"), null);
});

test("a device is named without its domain when only one has that name", () => {
  const known = registry();
  known.learn(LAYER2);
  assert.equal(known.resolve("core1").id, "core1.example.net");
  assert.equal(known.resolve("CORE2").id, "core2.example.net");
});

test("a port is its device's when written device:port or after the device, by any abbreviation", () => {
  const known = registry();
  known.learn(LAYER2);
  assert.deepEqual({ ...known.resolve("core1:Eth1/24") }, { kind: "port", id: "core1.example.net", port: "Ethernet1/24" });
  assert.deepEqual({ ...known.resolve("ethernet-1/2", "sw1") }, { kind: "port", id: "sw1", port: "ethernet-1/2" });
  // A port the device doesn't have is no mention.
  assert.equal(known.resolve("ethernet-1/7", "sw1"), null);
  assert.equal(known.resolve("Ethernet1/24"), null);
});

test("a link is named by its two ends, either way round", () => {
  const known = registry();
  known.learn(LAYER2);
  const found = known.resolve("core2--core1");
  assert.equal(found.kind, "link");
  assert.equal(found.id, "core1.example.net--core2.example.net");
});

test("a port's far end gives a chart line its tone", () => {
  const known = registry();
  known.learn(LAYER2);
  assert.equal(known.farTone("core1.example.net:Ethernet1/24"), 3); // to sw1
  assert.equal(known.farTone("core2.example.net:Eth1/9"), 2); // server1 wears core2's tone
  assert.equal(known.farTone("sw1:ethernet-1/9"), 0);
});

test("a device keeps its tone in every later view", () => {
  const known = registry();
  known.learn(LAYER2);
  known.learn({ layer: "l3", nodes: [node("sw1", "device"), node("core2.example.net", "device")], links: [] });
  assert.deepEqual(["core1.example.net", "core2.example.net", "sw1"].map(known.tone), [1, 2, 3]);
});
