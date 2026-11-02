"use strict";

// The registry: everything the page has seen of the network, from every view it has
// drawn (the whole network at any layer, and every view the model made), so the
// conversation and the map can name the same things alike.
//
// Each node gets a tone, the color it wears everywhere on the page: on the map, in
// the conversation where its name is written, and on the threads between them. An
// inventory device takes the next of seven tones in inventory order (--tone-1 to -7
// in app.css), and keeps it: color follows the device, never its place in a view. A
// host, LLDP neighbor, or network takes the tone of the one device it hangs off, so
// what belongs to a device shows at a glance; one shared by several devices, or
// devices past the seventh, are neutral (tone 0). Every node is labeled too, so
// color is never the only way to tell them apart.
//
// resolve() says what a word in the conversation names: a node by its ID, an
// address, a MAC in any spelling, or a device by its name without the domain; a
// port written device:port or after its device (hq-core ethernet-1/1, core2
// Eth1/24); or a link written a--b.

const netregistry = (() => {
  const TONES = 7;
  const nodes = new Map(); // by ID: {data, layers: Set, tone, provisional}
  const links = new Map(); // by ID: link data
  const index = new Map(); // lowercased ID, address, or short name -> node ID
  const macs = new Map(); // a MAC's hex digits -> node ID
  const ports = new Map(); // node ID -> Set of interface names seen on its links
  const devices = []; // inventory devices, in the order views list them
  let shortNames = new Map(); // a device or neighbor's name before its first dot -> node ID, when only one has it

  const macKey = (text) => (text || "").toLowerCase().replace(/[^0-9a-f]/g, "");
  const MAC_SPELLING = /^[0-9A-Fa-f]{2}([.:-]?[0-9A-Fa-f]{2}){5}$|^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$/;
  // A port's name is a word and a number: Eth1/24 is short for Ethernet1/24, and a subinterface's port is before its dot.
  const PORT_NAME = /^([A-Za-z][A-Za-z-]*?)(\d[\d/.:]*)$/;
  const portOf = (name) => name.replace(/\.\d+$/, "");

  function samePort(written, interfaceName) {
    const [a, b] = [written.toLowerCase(), interfaceName.toLowerCase()];
    if (a === b || a === portOf(b)) return true;
    const [short, full] = [PORT_NAME.exec(written), PORT_NAME.exec(portOf(interfaceName))];
    return Boolean(short && full && short[2] === full[2] && full[1].toLowerCase().startsWith(short[1].toLowerCase()));
  }

  function attached(view, id) {
    // The inventory devices a node is linked to in a view.
    const found = new Set();
    for (const link of view.links) {
      const other = link.a.node === id ? link.b.node : link.b.node === id ? link.a.node : null;
      if (other && nodes.get(other)?.data.kind === "device") found.add(other);
    }
    return [...found];
  }

  function learn(view) {
    if (!view || !Array.isArray(view.nodes)) return;
    for (const data of view.nodes) {
      const known = nodes.get(data.id);
      if (known) {
        // A later view can know more: addresses ARP has found since, a router ID.
        known.data = { ...known.data, ...data, addresses: [...new Set([...known.data.addresses, ...data.addresses])] };
        known.layers.add(view.layer);
      } else {
        nodes.set(data.id, { data, layers: new Set([view.layer]), tone: 0, provisional: true });
        if (data.kind === "device") devices.push(data.id);
      }
    }
    for (const link of view.links) {
      links.set(link.id, link);
      for (const end of [link.a, link.b]) {
        if (!end.interface) continue;
        if (!ports.has(end.node)) ports.set(end.node, new Set());
        ports.get(end.node).add(end.interface);
      }
    }
    // Devices first, in inventory order; then what hangs off them.
    for (const [position, id] of devices.entries()) {
      const node = nodes.get(id);
      node.tone = position < TONES ? position + 1 : 0;
      node.provisional = false;
    }
    for (const [id, node] of nodes) {
      if (node.data.kind === "device" || !node.provisional) continue;
      const kin = attached(view, id);
      if (!kin.length) continue; // Nothing to go by in this view; another may say.
      node.tone = kin.length === 1 ? nodes.get(kin[0]).tone : 0;
      // A host or neighbor is known by its cable, so layer 2 settles its tone; elsewhere it may still change once.
      node.provisional = view.layer !== "l2" && node.data.kind !== "subnet";
    }
    reindex();
  }

  function reindex() {
    index.clear();
    macs.clear();
    const firsts = new Map();
    for (const [id, { data }] of nodes) {
      index.set(id.toLowerCase(), id);
      if (data.label) index.set(data.label.toLowerCase(), id);
      for (const mac of data.macs || []) if (!macs.has(macKey(mac))) macs.set(macKey(mac), id);
      const first = id.split(".")[0].toLowerCase();
      if ((data.kind === "device" || data.kind === "neighbor") && first !== id.toLowerCase() && !/^[0-9a-f]+$/.test(first)) {
        firsts.set(first, firsts.has(first) ? null : id);
      }
    }
    for (const [id, { data }] of nodes) {
      for (const address of data.addresses || []) {
        const key = address.toLowerCase();
        if (!index.has(key)) index.set(key, id);
      }
    }
    shortNames = new Map([...firsts].filter(([, id]) => id));
    for (const [first, id] of shortNames) if (!index.has(first)) index.set(first, id);
  }

  function nodeNamed(text) {
    const key = text.toLowerCase();
    if (index.has(key)) return index.get(key);
    if (MAC_SPELLING.test(text)) return macs.get(macKey(text)) || null;
    // An address written with its prefix length: the address names its node.
    const address = /^(\d{1,3}(?:\.\d{1,3}){3})\/\d{1,2}$/.exec(text);
    if (address && index.has(address[1])) return index.get(address[1]);
    return null;
  }

  function portNamed(node, text) {
    const seen = ports.get(node);
    if (!seen || !PORT_NAME.test(text)) return null;
    for (const name of seen) if (samePort(text, name)) return name;
    return null;
  }

  function linkBetween(one, two) {
    for (const link of links.values()) {
      if ((link.a.node === one && link.b.node === two) || (link.a.node === two && link.b.node === one)) return link.id;
    }
    return null;
  }

  // What a word names, or null. after: the node named just before it in the same sentence, for a port written after its device.
  function resolve(word, after = null) {
    const node = nodeNamed(word);
    if (node) return { kind: "node", id: node };
    const ends = word.split(/--+|↔/);
    if (ends.length === 2) {
      const [one, two] = ends.map(nodeNamed);
      const link = one && two && linkBetween(one, two);
      if (link) return { kind: "link", id: link, nodes: [one, two] };
    }
    const colon = word.indexOf(":");
    if (colon > 0) {
      const owner = nodeNamed(word.slice(0, colon));
      const port = owner && portNamed(owner, word.slice(colon + 1));
      if (port) return { kind: "port", id: owner, port };
    }
    if (after) {
      const port = portNamed(after, word);
      if (port) return { kind: "port", id: after, port };
    }
    return null;
  }

  function farTone(port) {
    // The tone of the device at the far end of a port's link, for a port named device:port; 0 if the page doesn't know it.
    const colon = port.indexOf(":");
    if (colon < 0) return 0;
    const [owner, name] = [port.slice(0, colon), port.slice(colon + 1)];
    for (const link of links.values()) {
      for (const [here, there] of [[link.a, link.b], [link.b, link.a]]) {
        if (here.node === owner && here.interface && samePort(name, here.interface)) return nodes.get(there.node)?.tone ?? 0;
      }
    }
    return 0;
  }

  const node = (id) => nodes.get(id) || null;
  const tone = (id) => nodes.get(id)?.tone ?? 0;
  const label = (id) => nodes.get(id)?.data.label || id;
  const deviceIds = () => [...devices];
  const has = (id) => nodes.has(id);

  return { learn, resolve, node, tone, farTone, label, deviceIds, has, samePort, portOf, nodeNamed };
})();
