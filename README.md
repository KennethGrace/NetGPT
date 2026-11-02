# NetGPT: The Network Engineering GPT

[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](https://mit-license.org)
[![Author](https://img.shields.io/badge/Author-Kenneth_J_Grace-orange?style=flat-square)](https://kennethjeffersongrace.com)

NetGPT gives language models read-only access to network devices. It is an [MCP](https://modelcontextprotocol.io) server: any MCP client, such as Claude Code or Claude Desktop, can be the chat interface, and the client runs the tool-calling loop. `netgpt-chat` does the same with a local model served by [Ollama](https://ollama.com), so device output never leaves the machine.

The tools return typed records grouped by network layer (LLDP adjacencies, MAC tables, ARP bindings, routes, reachability). That makes them useful to a model and also to anything else that wants to draw the network.

## Tools

| Tool             | What it returns                                                                         |
|------------------|-----------------------------------------------------------------------------------------|
| `list_devices`   | The devices in the inventory, with platform, address, and role.                         |
| `show`           | The text output of one read-only CLI command on one device.                             |
| `lldp_neighbors` | LLDP adjacencies across many devices.                                                   |
| `interfaces`     | Each logical interface's network instance, whether it routes or bridges, its addresses, and its state. |
| `mac_table`      | Layer 2 forwarding entries across many devices, or those for a MAC, a VLAN, or a port.  |
| `arp_table`      | ARP bindings across many devices, or those for an address, a network, a MAC, or a port. |
| `routes`         | The routing table of a network instance (VRF) across many devices: IPv4, and IPv6 where the platform reads it. Or each device's route for a destination, and the routes inside it. |
| `bgp_sessions`   | Each device's BGP sessions: the peer and local addresses and AS numbers, the state, the last error, the address families, and the prefixes received. |
| `ospf_neighbors` | Each device's OSPF neighbors: the interface and area, the neighbor's router ID and address, and the adjacency's state. |
| `ospf_interfaces`| The interfaces each device runs OSPF on: area and its type, network type, cost, passive or not, authentication, hello and dead intervals, MTU, and the DR and BDR. For each network two routers share, the settings their ends disagree on, and how the other interfaces are set. |
| `topology`       | The network as one graph at layer 2, at layer 3, or as OSPF sees it, merged from every device's tables, with the evidence for each link. Or the part of it around a device, a host, a MAC, a network, or a link. |
| `forwarding_path`| The path packets take from a source to a destination, hop by hop, read from each device's routing table. |
| `ping`           | Replies to ICMP echo requests sent from a device, inside one of its network instances.  |
| `ping_sweep`     | The addresses that answer ICMP in a network, pinged from a device (inside one of its network instances) or from the NetGPT host itself. |
| `interface_traffic` | Each port's traffic in and out (Mbit/s, and percent of its speed), its average and busiest interval over the window, and the errors, discards, and link flaps counted, from counters NetGPT polls. |

The multi-device tools query devices concurrently. A device that fails shows up as an error next to the other devices' results rather than failing the whole call. A device can be named without its domain (`core1` for `core1.example.net`) when only one device's name starts that way.

On a real network one table from every device can be more than a small model can read. Four Nexus switches' MAC tables are 1,348 entries and more than 64K tokens, and qwen3.8 on one 24 GB GPU has 64K of context. So the tables take filters, written as an engineer or a model would write them:

- `mac`: any part of a MAC address, in any spelling (`0200.c0ff.ee01`, `02:00:C0:FF:EE:01`, or `c0ff`).
- `interface`: a port, by its name or an abbreviation (`Eth1/9`, `Po2`, `Gi1/0/51`), with its subinterfaces.
- `address`: an address or a network (`192.0.2.0/24`, or `192.0.2.0 255.255.255.0`). On `interfaces`, it matches the interfaces on it, so a host's address finds its gateway.
- `vlan`.
- `destination` on `routes`: each device's route for it (the longest match, or a default route), and every route inside it.
- `protocol` on `routes`.

`topology` takes `focus`: the devices, hosts (by address or MAC), networks, or links a question is about. Then it answers with those, every link that touches them, and the nodes at the far ends, without the far nodes' evidence.

An answer bigger than the server allows (`--max-answer`, 20,000 tokens by default for `netgpt-mcp`, a quarter of the context for `netgpt-chat`) isn't sent. The tool refuses it with what it holds and how to ask for less:

> The answer is about 78,169 tokens, more than the 16,384 one answer may have here, so none of it was sent. It holds 1,348 MAC entries: core1.example.net 440, core2.example.net 440, dc1.example.net 235, dc2.example.net 233; by VLAN, 30 136, 254 92, 252 64, 255 58, 80 56, 89 50, 1 46, 2500 36, and 79 others. Ask again for less, with mac (a MAC address or any part of one, in any spelling), vlan, interface, or fewer devices.

Tokens are counted with each digit as one, since qwen's and gemma's tokenizers split numbers into digits and addresses are mostly digits. A table of addresses is 1.6 to 3 characters a token.

`topology` draws the network at one of three layers. At layer 2 (the default) it is the cabling. Each cable is reconciled from both ends, and a link only one end reports is flagged. Each host hangs off the port it is plugged into: ARP says which port an address was heard on, and where a switch is in the way, the switch's MAC table says which of its ports the address is behind. If a switch has forgotten a MAC, the host is drawn behind the switch with no port, and the evidence says why. At layer 3 the graph is the routing in one network instance: routers, the networks their interfaces are on, and the hosts on those networks. Switches drop out, and a network between just two routers is drawn as a link between them. The OSPF view is the routing protocol: the routers that run it and the areas each is in, an adjacency for each pair of neighbors with each end's area, cost, and state, and the networks advertised from passive interfaces. Two OSPF interfaces on one network that aren't full neighbors are drawn as a link too, with what keeps them apart: areas, area types, timers, network types, authentication, or MTUs that differ, a passive end, or a router that doesn't run OSPF there. Two routers that are both passive on a network, such as a pair of gateways for its hosts, both advertise it and aren't meant to be neighbors, so it is drawn as a network they advertise. Where the setting is an interface's own, it says how the other interfaces have it, so the end set unlike the rest is named. Every layer keeps the management network apart from the data plane, and a port that hears several LLDP neighbors (such as a management bridge) becomes one shared segment instead of a mesh of links.

`forwarding_path` walks the routing tables the way the devices do. At each hop it takes the longest matching route, resolves the route's next hop down to an interface (a static route's next hop is often just an address), and follows the cable on that interface to the next device, through any switches in the way. Between two hosts on the same network, no router is involved: the path goes straight across the switch. It reports where the path ends: delivered, or lost, with the reason (no route, a route that drops the packet, a loop, a next hop nothing answers ARP for, or a device it couldn't read). It sends nothing on the network.

`interface_traffic` reads from a poller, not from the devices directly. While a client is connected, NetGPT reads every device's interface counters every 30 seconds (`--poll-interval`; 0 turns polling and the tool off) and keeps the last hour in memory. With `--store postgresql://netgpt@127.0.0.1:5432/netgpt` it keeps them in TimescaleDB instead, across restarts (`pip install -e '.[store]'`; `dev/compose.yml` runs a TimescaleDB, and its password goes in `~/.pgpass`, never in the URL): every poll for a week, 5-minute averages for 90 days, and hourly ones for two years. A rate is the difference between two readings over the time between them. The second poll comes 5 seconds after the first, so there are rates almost at once. A counter that goes backwards was reset (the device restarted, or someone cleared it), and that interval isn't measured, nor is one more than three polls long. A window too long to read poll by poll (past 400 polls, over 3 hours at the default interval) is read from the averages, in 5-minute steps or longer, and each step keeps its busiest interval, so a peak survives the averaging.

## Safety model

- **Inventory only.** Tools take device names from the inventory. The model cannot point NetGPT at any other host. `ping_sweep` only covers networks listed under `sweep_networks`, up to /22 from the NetGPT host and /24 from a device.
- **Read-only CLI.** `show` accepts a command only if it starts with an allowed verb, every pipe leads to an allowed filter, and it contains no control characters or chaining syntax. On Cisco IOS this refuses `| redirect`, `| tee`, and `| append`. On SR Linux it refuses `>` redirection. On Junos it refuses `| save` and `| request`. On NX-OS it refuses `>` redirection, `;` between commands, and `| email`, `| tee`, and `| python`. Use a read-only account on the device as well; the policy is the second line of defence, not the first.
- **Every way in has the same limits.** The tools, the page, and the API's commands all go through one engine, so the guard, the inventory, and the sweep limits are the same for an engineer at a frontend as for a model.
- **Credentials stay on the server.** The inventory names an environment variable or a `chmod 600` file for each password, or a `chmod 600` SSH private key. Secrets never appear in tool arguments or results. Over SSH, NetGPT checks each device's host key against a `known_hosts` file and refuses to log in to one that doesn't match.

## Quick start

NetGPT needs Python 3.12 or newer.

```bash
git clone https://github.com/KennethGrace/NetGPT.git
cd NetGPT
pyenv virtualenv 3.12.9 netgpt && pyenv local netgpt
pip install -e '.[dev]'
pytest
```

### A lab to talk to

[`lab/`](lab/) holds a [containerlab](https://containerlab.dev) topology: one Nokia SR Linux spine and two leaves, with static routing across the fabric. A Linux host hangs off leaf1. Behind leaf2 is switch1, an SR Linux node that only bridges, with two hosts on its LAN. The hosts keep traffic flowing with iperf3 ([`lab/traffic.sh`](lab/traffic.sh)), from host1 to host2 and from host3 to host1, at a rate that changes every minute, so the interface counters have something to count. It needs [containerlab](https://containerlab.dev/install/) and Docker:

```bash
sudo containerlab deploy -t lab/netgpt.clab.yml
sudo containerlab deploy -t lab/netgpt.clab.yml --reconfigure   # after changing the topology
```

A freshly deployed lab has empty ARP and MAC tables, so the hosts aren't on the map until something talks to them. Ask NetGPT to sweep the host networks from their leaves ("sweep 192.168.12.0/24 from leaf2"): the sweep fills the tables the map is drawn from. A /24 takes about 45 seconds from a device, since each ping is a command on it. A sweep from the NetGPT host itself is much faster but reaches only the lab's management network, and from there a data-plane network just looks empty.

Use Docker, not podman. Under podman, SR Linux 25.10.5 crashes during boot, and containerlab no longer tests its podman runtime. On RHEL, Docker CE can run alongside podman: remove only the `podman-docker` package (the `docker` alias for podman), not podman itself.

[`lab/inventory.yml`](lab/inventory.yml) already describes the lab. It reads SR Linux's default lab password from a file:

```bash
mkdir -p ~/.config/netgpt && printf 'NokiaSrl1!' > ~/.config/netgpt/srlinux-lab.password
chmod 600 ~/.config/netgpt/srlinux-lab.password
```

A second lab, [`lab/wan/`](lab/wan/), is a WAN routed by OSPF: three sites, HQ, Ops, and a branch, joined by links of 25G, 10G, and 1G. HQ and Ops each have a campus core with a datacenter router off it, and HQ's users sit behind an access switch. Link costs follow speed, so the branch reaches Ops through HQ rather than over its own 1G link. Each site's users send iperf3 traffic to the servers. It runs beside the first lab, on its own management network, and [`lab/wan/inventory.yml`](lab/wan/inventory.yml) describes it:

```bash
sudo containerlab deploy -t lab/wan/wan.clab.yml
netgpt-chat --inventory lab/wan/inventory.yml
```

To see whether NetGPT can troubleshoot, let a gremlin loose in the lab. [`lab/gremlin.py`](lab/gremlin.py) breaks one thing at random: a port shut, an address mistyped, a host's switch port pulled from its LAN, a blackhole route, a routing loop, or a missing route back. It prints only the complaint a user would make ("host2 and host3 can't reach host1"). Several faults share a complaint, so the model has to investigate rather than guess. In the WAN lab, its faults are OSPF's: a cost that sends traffic over the backup link, an area mismatch, an MTU mismatch that leaves an adjacency stuck in exstart, a passive interface, mismatched hello intervals, and a static route that beats OSPF's. Harder ones leave every adjacency up and hide a network with a summary, or break an adjacency in a way only the configuration shows (a stub flag, authentication), and the game can break two at once. Each WAN fault has a usual place and variants elsewhere (either end of any adjacency, another router's static route), each with the complaint its symptoms would bring, and `--anywhere` picks one at random, so a model can't remember its way to the answer. The gremlin writes configuration through the lab's own login; NetGPT stays read-only.

```bash
python lab/gremlin.py break     # then ask NetGPT about the complaint
python lab/gremlin.py reveal    # what the gremlin broke
python lab/gremlin.py repair    # put it back
python lab/gremlin.py check     # dry-run every fault; nothing is committed
python lab/gremlin.py --lab wan break   # OSPF faults in the WAN lab
python lab/gremlin.py --lab wan break --pair   # two at once
python lab/gremlin.py --lab wan break --anywhere   # a fault at a random one of its places
```

[`lab/game.py`](lab/game.py) plays the gremlin's game with a model and keeps score. Each round, the gremlin breaks a fault, and the model gets the complaint in a fresh conversation and investigates with NetGPT's tools. The game then puts the lab back and waits for it to answer again. A judge model reads each answer beside what the gremlin broke. It names the devices the answer blames, and whether it hedges between causes, then decides: found, partly, or missed. An answer that blames another device, or hedges, has missed, however much it says along the way. One that blames the right device and an innocent one as well is at most partly right. The judge can be wrong too, so the scoreboard keeps every answer and the judge's reasons, for reading, and each answered round's whole conversation goes to `lab/.transcripts/`.

```bash
python lab/game.py --model gemma4:26b --ollama http://127.0.0.1:11434   # each fault once, in random order
python lab/game.py --scoreboard --answers
python lab/game.py --rejudge --model gemma4:26b --ollama http://127.0.0.1:11434   # judge every round again
```

### Connect a client

The repository's [`.mcp.json`](.mcp.json) registers the server with Claude Code when it runs from the repository root. For other clients, the command is:

```bash
python -m netgpt --inventory lab/inventory.yml
```

### Chat with a local model

`netgpt-chat` builds the server in-process and runs the tool-calling loop against a model served by Ollama. Tool calls show up as they happen, and errors (such as a refused command) are always shown.

```bash
netgpt-chat --inventory lab/inventory.yml                               # interactive; /reset clears, Ctrl-D quits
netgpt-chat --inventory lab/inventory.yml "What is cabled to spine1?"   # one question per argument
```

The chat counts the conversation against its context (`--context`, in tokens). One tool answer may have a quarter of it. If an answer would leave the model no room to reply, the chat drops it and tells the model to ask for less, rather than sending Ollama a conversation it would cut the question out of.

The default model, `gemma4:e4b`, runs on CPU alone: about 14 tokens/s on 16 threads of a Xeon Gold 6242, using 10 GB of RAM at its full 128K context. `--model` picks any other model with tool support.

The chat lets the model think before each reply when the model supports it. That is slow on CPU, but small models need it for anything multi-step. On the lab, gemma4:e4b answered "find the host behind leaf2 and check that leaf1 can reach it" and "trace the path from host1 to host2" correctly 4 times out of 4 with thinking, taking about 3 minutes per question, and 0 times out of 4 without, at about 25 seconds per question. `--no-think` is fine for single lookups such as "what is cabled to spine1?".

To keep Ollama off the GPUs entirely, for example while they're busy training, run a separate CPU-only instance. Hide the GPUs from both CUDA and Vulkan, since Ollama finds them through either:

```bash
CUDA_VISIBLE_DEVICES=-1 GGML_VK_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=false \
  OLLAMA_HOST=127.0.0.1:11435 ollama serve   # OLLAMA_MODELS=... to share another instance's models
netgpt-chat --ollama http://127.0.0.1:11435 --threads 16 --inventory lab/inventory.yml
```

### In a browser

`netgpt-chat --web [HOST:]PORT` serves the same chat as a web page. The page is a drafting table with the network laid out on it, and the conversation is a sheet of paper lying on the table's left. The model's thinking streams in as it happens, each tool call shows what it asked of which devices, and a reload picks the conversation back up. The page loads nothing from outside the server, fonts included, so it works on an air-gapped network.

```bash
export NETGPT_WEB_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
netgpt-chat --inventory lab/inventory.yml --web 10.0.0.5:8080
```

Anyone who can load the page can query the network, so every request needs a token: `$NETGPT_WEB_TOKEN` if set, otherwise a random one. At startup, netgpt-chat prints a link that carries the token. Opening it swaps the token for an HttpOnly cookie. The page is plain HTTP, so bind it to an address only trusted machines can reach, such as a VPN address or 127.0.0.1 behind an SSH tunnel. There is one conversation, and it answers one question at a time.

The **(i)** beside the model's name says which NetGPT this is and what it runs: the version, the model and its context, and the devices by platform. Its **Report an issue** link opens a GitHub issue with those already filled in. The model knows too: ask it what this is, and it says which NetGPT and which model you are talking to.

To work on the page without a model, `python lab/preview_page.py` serves it against a real inventory (the WAN lab's, by default) with a scripted model, whose canned answers call the real tools and stream at a reading pace.

#### The map and the conversation, tied together

Every device has a color: seven tones in a fixed order, from a palette checked for color-blind readers against both the light table and the dark one. Devices are colored tokens on the table, and what hangs off a device (its hosts, its LLDP neighbors, its networks) wears the device's color. Every node is labeled too, so color is never the only way to tell them apart.

Wherever the conversation names something the map has (a device, a host's address, a network, a MAC address in any spelling, a port written after its device, or a link written `a--b`), the name wears its color. Point at the name and a thread runs across the page to the thing on the map, which lights up while the rest of the map steps back, its neighbors half lit. Click it to select it. Point at a node on the map and threads run the other way, to every mention of it in sight, and the rail beside the conversation marks where the rest are, a click away. The inspector says how often its subject came up, and offers to ask about it or to focus the map on it.

While a tool call runs, the devices it is asking ripple on the map, and its card in the conversation says what it is doing.

#### The map

When the page opens, before you ask anything, the map shows the whole network, read from [the API](#the-api) like any other frontend would, at the layer you pick: **Cabling** (layer 2), **Routing** (layer 3), or **OSPF**. After that, the model draws on it, with a tool only the page offers: `display`. When a picture helps, such as for the topology, where a host is attached, or the path between two points, the model calls `display`. It draws the whole network from the devices' own tables, at layer 2 (cables, switches, and the port each host is on), at layer 3 (routers and the networks between them), or as OSPF sees it (each end of an adjacency labeled with its cost, each area but the backbone in its own color, and a link with a problem dashed in red), and it chooses which parts to focus on and highlight. Focused nodes move to the middle, and the rest of the network stays on the map, dimmed and pushed aside. What the model highlights is marked in highlighter. The model can't draw anything the devices didn't report, and every name it uses is checked against the graph first. Click a node or link to see the evidence for it, on a card edged in its color. Each view leaves a card in the conversation, with the view in miniature, that brings it back.

On a big network the map shows only the devices' names, and what is highlighted, pointed at, or selected; the rest are named when the pointer is on them, along with their neighbors.

On a wide screen the map is the table beside the conversation. On a phone they take turns on the whole screen: the header's **Map** button switches to the map, and **Chat** switches back. A new view doesn't take over the screen while an answer is coming. A dot on the button says something new is on the other side.

Given a path (a source and a destination), `display` traces it through the routing tables and highlights it hop by hop, numbering the hops on the map, with the route each device used. At layer 2 the path runs through the switches, out the port each one bridges it to. At layer 3 it crosses the networks instead. Where a path is lost, the device that loses it turns red, with the reason.

The layout is a force simulation ([d3-force](https://d3js.org/d3-force), included in the package): nodes repel each other and links act as springs. A view changes the forces, not the nodes, so moving from one view to the next glides the network into its new shape.

You can pin nodes to lay the map out your way: say, a border leaf on top with the other leaves in a row below. A node you drag stays where you drop it, and the forces arrange everything else around it. Dropped near another pinned node's row or column, it snaps into line. Double-click a node to pin it where it is, or to let it go; the inspector has the same switch, plus "Unpin all". Pins are yours, not the model's: they hold across views, are kept per plane, and are saved in your browser.

#### Charts

When NetGPT polls (it does unless `--poll-interval 0`), the page also offers `chart`. Asked how busy something is, the model charts ports' traffic, utilization, or errors over the last minutes, or days with TimescaleDB, right in the conversation. A chart of a long window plots averages (5-minute ones over 6 hours, 30-minute ones over a week), and its caption says which. It names ports as `device:port`, a link as `leaf1--spine1`, or a device for all its ports. Each port's line wears the color of the device at the far end of its link, as that device is on the map (or, when two lead to one device, a color of its own), with in drawn solid and out dashed. A chart keeps updating with every poll while it is on the page. Hovering shows every line's value at one poll, and the Data table under each chart has the same numbers.

#### Traffic on the map

While NetGPT polls, the map draws each link's traffic the way a network weathermap does. Beside the link runs a lane for each direction, on the right as the traffic travels, ending in an arrowhead. A lane's color is its share of the port's speed, from light (under 10%) to dark (75% or more), and focused links are labeled with their rates. A port that counted errors in the last 5 minutes gets ⚠ and the count beside its name. Select a link to read both directions in words, with a chart of the port; select a device to chart its ports on the map. The **Traffic** button hides it all, and the page remembers that choice.

## Inventory

```yaml
credentials:
  lab-readonly:
    username: netgpt
    password_env: NETGPT_LAB_PASSWORD        # or: password_file: ~/.config/netgpt/lab.password
  junos-key:
    username: netgpt
    ssh_key_file: ~/.config/netgpt/netgpt_ed25519
devices:
  - name: core1
    host: 10.0.0.1
    platform: nokia_srlinux
    credentials: lab-readonly
    role: core
    tls_ca: certs/lab-ca.pem                 # optional; relative paths resolve against this file
  - name: access1
    host: 10.0.0.2
    platform: juniper_junos
    credentials: junos-key
    known_hosts: ~/.config/netgpt/known_hosts  # optional; the default is ~/.ssh/known_hosts
sweep_networks:
  - 10.0.0.0/24
```

## Platforms

| Platform        | Transport           | Status                                                                 |
|-----------------|---------------------|------------------------------------------------------------------------|
| `nokia_srlinux` | JSON-RPC over HTTPS | All tools verified against SR Linux 25.10.5 in the lab. The tests replay responses recorded there. `bgp_sessions` isn't implemented yet; the OSPF tools are, against the WAN lab. |
| `juniper_junos` | NETCONF over SSH    | Every tool but `ping`, `ping_sweep`, and the OSPF tools, verified against Junos 23.4R2 on a QFX5120 and two EX4400s. The tests replay replies recorded there, renamed. |
| `cisco_nxos`    | CLI over SSH, `\| json` | Every tool but `ping`, `ping_sweep`, and `bgp_sessions`, verified against NX-OS 7.0(3)I7(7) on two Nexus 9396PXs and 7.3(8)N1(1) on two Nexus 5548UPs. The tests replay replies recorded there, trimmed and made anonymous. |

Platform names match containerlab node kinds where there is one. `juniper_junos` and `cisco_nxos` are for hardware, which has none.

NetGPT keeps no more than two SSH connections open to one device at a time, so a graph that asks every table of every device at once doesn't take a shared switch's VTY lines from the people who use it.

On Junos, give NetGPT a user in the built-in `read-only` class, with an SSH key:

```
set system login user netgpt class read-only
set system login user netgpt authentication ssh-ed25519 "ssh-ed25519 AAAA... netgpt@host"
```

That class can't read the configuration, so the device refuses `show configuration`. To let NetGPT read it, give the user a class with `view-configuration` as well:

```
set system login class netgpt-ro permissions [ view view-configuration ]
set system login user netgpt class netgpt-ro
```

Junos hides secret statements, such as passwords and keys, from that class, but not comments. The Mist agent writes its device secret into a comment in the configuration, so a model reading a Mist-connected switch's configuration reads that secret too.

Interfaces and routes include IPv6 on Junos, but `topology` and `forwarding_path` still work in IPv4 only. Junos's NETCONF `<command>` ignores pipes, so NetGPT applies `match`, `except`, `find`, `count`, `last`, and `trim` to the output itself, and asks Junos for XML or JSON for `| display xml` and `| display json`.

To record new fixtures from Junos devices, list them in an inventory, and list each real name and address with a stand-in in a YAML file kept outside the repository:

```bash
python lab/record_junos.py --inventory my-junos.yml --rename my-renames.yml
```

On NX-OS, the built-in `network-operator` role reads everything but the configuration. To let NetGPT read that too, and to keep it from opening sessions to other hosts, give it a role of its own, with an SSH key (NX-OS checks the highest-numbered rule first):

```
role name netgpt-ro
  rule 1 permit read
  rule 2 permit command show running-config
  rule 3 permit command show running-config *
  rule 4 permit command show startup-config
  rule 5 deny command telnet *
  rule 6 deny command ssh *
username netgpt role netgpt-ro
username netgpt sshkey ssh-rsa AAAA... netgpt
```

The role refuses `ping` until a rule permits it, which is why `ping` isn't on NX-OS yet. NX-OS answers most show commands as JSON, and runs each command on an exec channel of its own, so nothing is scraped from a prompt. Some releases (7.0(3)I7 on a Nexus 9000) can't turn every reply into JSON, so NetGPT then asks for `| xml`, which the JSON is made from. NX-OS keeps its VLANs in the default network instance, so a switch port bridges there, and each MAC entry names its VLAN. A port in a port-channel names it (`aggregate`), and the topology follows a MAC table's port-channel to the cables of its members, where LLDP is heard.

To record fixtures from NX-OS devices, list them in an inventory, and their names with stand-ins in a YAML file kept outside the repository. The network may be one other people use, so the recorder keeps a few rows of each kind in each table, moves every address into 198.18.0.0/15 and every MAC to a locally administered one, calls every other system neighbor-N, and blanks descriptions:

```bash
python lab/record_nxos.py --inventory my-nexus.yml --rename my-renames.yml
```

## The schema

[`schema/netgpt.schema.json`](schema/netgpt.schema.json) is the JSON Schema of every record NetGPT sends outside itself: the views the map draws, charts and the map's traffic, and the records the tools return, each under `$defs` by name. It is for programs that read NetGPT without being written in Python, such as a frontend in another repository: generate types from it instead of writing them by hand. Every property is always sent, so the schema marks every one required, and one that can be empty is null. Its `version` is the NetGPT version that wrote it.

`netgpt-schema` writes it from the same pydantic models that make the records, and a test fails when the repository's copy and the code differ, so a change to what NetGPT sends shows up in a diff:

```bash
netgpt-schema --out schema/netgpt.schema.json
```

Over MCP, each tool also publishes its own output schema, from the same models.

## The API

The engine's API is at `/api/v1/`, for programs: a frontend in another repository, or a script. `netgpt-engine` serves it with no model behind it, polling the counters for as long as it runs:

```bash
export NETGPT_ENGINE_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
netgpt-engine --inventory lab/wan/inventory.yml --listen 127.0.0.1:8081   # --store postgresql://... to keep the counters
```

`netgpt-chat --web` serves the same API beside its page, with the page's token. Every route answers with a record from [the schema](#the-schema) (reads answer GET, and commands POST), and needs the token, sent as `Authorization: Bearer <token>` (`$NETGPT_ENGINE_TOKEN` for `netgpt-engine`, or a random one it prints at startup). The signed-in page's cookie works too. Both poll, so give each its own `--store`: two pollers on one store count errors twice.

| Route | Answers with |
|---|---|
| `devices` | The inventory's devices. |
| `topology?layer=l2\|l3\|ospf&plane=data\|management\|all&network_instance=default&focus=…` | `Topology`: the network as one graph, or the part of it around the nodes in focus. |
| `path?source=…&destination=…&network_instance=default` | `ForwardingPath`: the way packets go, read from the routing tables. |
| `view?layer=…&focus=…&highlight=…&source=…&destination=…&title=…` | `View`: the graph with your focus and highlight (each repeated for several names), and a path if you give its two ends. |
| `tables/<table>?device=…` | One table's rows from every device, or those named: `lldp_neighbors`, `interfaces`, `mac_table`, `arp_table`, `routes` (with `network_instance`), `bgp_sessions`, `ospf_neighbors`, or `ospf_interfaces`. Each takes its tool's filters, each repeated for several values: `mac_table?vlan=80&interface=Eth1/32`. |
| `traffic?device=…&minutes=5` | `Traffic`: every polled port's latest rates and recent errors. |
| `chart?port=…&metric=traffic\|percent\|errors&minutes=15` | `Chart`: ports' rates over the window, with links named `device--device`. |
| `schema` | The schema itself. |

```bash
curl -H "Authorization: Bearer $NETGPT_ENGINE_TOKEN" 'http://127.0.0.1:8081/api/v1/path?source=branch&destination=10.20.100.10'
```

Devices' answers are shared: one is reused for up to a poll interval (30 seconds), requests that arrive together wait for one read, and the `Age` header says how old the oldest answer behind a response is. `Cache-Control: no-cache` reads every device again. A frontend redrawing every poll then doesn't log in to every device each time; the model's tools always read live. A request NetGPT won't answer gets `{"error": …}` saying why, with the names it would accept: 400 for the request, 401 without the token, and 404 for a route that isn't one. There is no CORS: a browser app on another origin should reach the API through its own backend, which keeps the token out of the browser.

Commands answer POST, with a JSON body, and run every time they're sent, through the same guard and limits as the model's tools: an engineer at any frontend can do what the model can.

| Route | Body | Answers with |
|---|---|---|
| `show` | `device`, `command` | `CommandOutput`: one read-only command's output. The guard refuses anything else, before the device is asked. |
| `ping` | `device`, `destination`, `count` (3), `network_instance` | `PingResult`. |
| `ping_sweep` | `network`, `device` (or none, for the NetGPT host), `network_instance`, `count`, `timeout` | The addresses that answered, as `{items, errors}`. Only networks inside the inventory's `sweep_networks`. |

```bash
curl -H "Authorization: Bearer $NETGPT_ENGINE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"device": "branch", "destination": "10.20.100.10"}' http://127.0.0.1:8081/api/v1/ping
```

A command the guard or the limits refuse is a 400, a device that fails it a 502, and one whose platform can't run it yet (such as `ping` on Junos) a 501. A command's body has to be JSON, so a form on another site can't send one.

## Legacy web app (v0.3)

The original 2023 NetGPT, a React chat interface and a FastAPI backend built on OpenAI's first function-calling API, with Keycloak authentication, is kept for reference at the tag [`v0.3.6g`](https://github.com/KennethGrace/NetGPT/tree/v0.3.6g). **Do not deploy it:** the v0.3 API accepts requests with any bearer token, because token validation errors are logged but never rejected.
