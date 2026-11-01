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
| `mac_table`      | Layer 2 forwarding entries across many devices.                                         |
| `arp_table`      | ARP bindings across many devices.                                                       |
| `routes`         | The routing table of a network instance (VRF) across many devices: IPv4, and IPv6 where the platform reads it. |
| `bgp_sessions`   | Each device's BGP sessions: the peer and local addresses and AS numbers, the state, the last error, the address families, and the prefixes received. |
| `ospf_neighbors` | Each device's OSPF neighbors: the interface and area, the neighbor's router ID and address, and the adjacency's state. |
| `ospf_interfaces`| The interfaces each device runs OSPF on: area and its type, network type, cost, passive or not, authentication, hello and dead intervals, MTU, and the DR and BDR. For each network two routers share, the settings their ends disagree on, and how the other interfaces are set. |
| `topology`       | The network as one graph at layer 2, at layer 3, or as OSPF sees it, merged from every device's tables, with the evidence for each link. |
| `forwarding_path`| The path packets take from a source to a destination, hop by hop, read from each device's routing table. |
| `ping`           | Replies to ICMP echo requests sent from a device, inside one of its network instances.  |
| `ping_sweep`     | The addresses that answer ICMP in a network, pinged from a device (inside one of its network instances) or from the NetGPT host itself. |
| `interface_traffic` | Each port's traffic in and out (Mbit/s, and percent of its speed), its average and busiest interval over the window, and the errors, discards, and link flaps counted, from counters NetGPT polls. |

The multi-device tools query devices concurrently. A device that fails shows up as an error next to the other devices' results rather than failing the whole call.

`topology` draws the network at one of three layers. At layer 2 (the default) it is the cabling. Each cable is reconciled from both ends, and a link only one end reports is flagged. Each host hangs off the port it is plugged into: ARP says which port an address was heard on, and where a switch is in the way, the switch's MAC table says which of its ports the address is behind. If a switch has forgotten a MAC, the host is drawn behind the switch with no port, and the evidence says why. At layer 3 the graph is the routing in one network instance: routers, the networks their interfaces are on, and the hosts on those networks. Switches drop out, and a network between just two routers is drawn as a link between them. The OSPF view is the routing protocol: the routers that run it and the areas each is in, an adjacency for each pair of neighbors with each end's area, cost, and state, and the networks advertised from passive interfaces. Two OSPF interfaces on one network that aren't full neighbors are drawn as a link too, with what keeps them apart: areas, area types, timers, network types, authentication, or MTUs that differ, a passive end, or a router that doesn't run OSPF there. Where the setting is an interface's own, it says how the other interfaces have it, so the end set unlike the rest is named. Every layer keeps the management network apart from the data plane, and a port that hears several LLDP neighbors (such as a management bridge) becomes one shared segment instead of a mesh of links.

`forwarding_path` walks the routing tables the way the devices do. At each hop it takes the longest matching route, resolves the route's next hop down to an interface (a static route's next hop is often just an address), and follows the cable on that interface to the next device, through any switches in the way. Between two hosts on the same network, no router is involved: the path goes straight across the switch. It reports where the path ends: delivered, or lost, with the reason (no route, a route that drops the packet, a loop, a next hop nothing answers ARP for, or a device it couldn't read). It sends nothing on the network.

`interface_traffic` reads from a poller, not from the devices directly. While a client is connected, NetGPT reads every device's interface counters every 30 seconds (`--poll-interval`; 0 turns polling and the tool off) and keeps the last hour in memory. With `--store postgresql://netgpt@127.0.0.1:5432/netgpt` it keeps them in TimescaleDB instead, across restarts (`pip install -e '.[store]'`; `dev/compose.yml` runs a TimescaleDB, and its password goes in `~/.pgpass`, never in the URL): every poll for a week, 5-minute averages for 90 days, and hourly ones for two years. A rate is the difference between two readings over the time between them. The second poll comes 5 seconds after the first, so there are rates almost at once. A counter that goes backwards was reset (the device restarted, or someone cleared it), and that interval isn't measured, nor is one more than three polls long. A window too long to read poll by poll (past 400 polls, over 3 hours at the default interval) is read from the averages, in 5-minute steps or longer, and each step keeps its busiest interval, so a peak survives the averaging.

## Safety model

- **Inventory only.** Tools take device names from the inventory. The model cannot point NetGPT at any other host. `ping_sweep` only covers networks listed under `sweep_networks`, up to /22 from the NetGPT host and /24 from a device.
- **Read-only CLI.** `show` accepts a command only if it starts with an allowed verb, every pipe leads to an allowed filter, and it contains no control characters or chaining syntax. On Cisco IOS this refuses `| redirect`, `| tee`, and `| append`. On SR Linux it refuses `>` redirection. On Junos it refuses `| save` and `| request`. Use a read-only account on the device as well; the policy is the second line of defence, not the first.
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

The default model, `gemma4:e4b`, runs on CPU alone: about 14 tokens/s on 16 threads of a Xeon Gold 6242, using 10 GB of RAM at its full 128K context. `--model` picks any other model with tool support.

The chat lets the model think before each reply when the model supports it. That is slow on CPU, but small models need it for anything multi-step. On the lab, gemma4:e4b answered "find the host behind leaf2 and check that leaf1 can reach it" and "trace the path from host1 to host2" correctly 4 times out of 4 with thinking, taking about 3 minutes per question, and 0 times out of 4 without, at about 25 seconds per question. `--no-think` is fine for single lookups such as "what is cabled to spine1?".

To keep Ollama off the GPUs entirely, for example while they're busy training, run a separate CPU-only instance. Hide the GPUs from both CUDA and Vulkan, since Ollama finds them through either:

```bash
CUDA_VISIBLE_DEVICES=-1 GGML_VK_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=false \
  OLLAMA_HOST=127.0.0.1:11435 ollama serve   # OLLAMA_MODELS=... to share another instance's models
netgpt-chat --ollama http://127.0.0.1:11435 --threads 16 --inventory lab/inventory.yml
```

### In a browser

`netgpt-chat --web [HOST:]PORT` serves the same chat as a web page. The model's thinking streams in as it happens, each tool call opens to show its result, and a reload picks the conversation back up. The page loads nothing from outside the server, so it works on an air-gapped network.

```bash
export NETGPT_WEB_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
netgpt-chat --inventory lab/inventory.yml --web 10.0.0.5:8080
```

Anyone who can load the page can query the network, so every request needs a token: `$NETGPT_WEB_TOKEN` if set, otherwise a random one. At startup, netgpt-chat prints a link that carries the token. Opening it swaps the token for an HttpOnly cookie. The page is plain HTTP, so bind it to an address only trusted machines can reach, such as a VPN address or 127.0.0.1 behind an SSH tunnel. There is one conversation, and it answers one question at a time.

#### The map

The page also has a map, drawn by a tool only the page offers: `display`. When a picture helps, such as for the topology, where a host is attached, or the path between two points, the model calls `display`. It draws the whole network from the devices' own tables, at layer 2 (cables, switches, and the port each host is on), at layer 3 (routers and the networks between them), or as OSPF sees it (each end of an adjacency labeled with its cost, each area but the backbone in its own color, and a link with a problem dashed in red), and it chooses which parts to focus on and highlight. Focused nodes move to the middle, and the rest of the network stays on the map, dimmed and pushed aside. The model can't draw anything the devices didn't report, and every name it uses is checked against the graph first. Click a node or link to see the evidence for it. Each view stays in the conversation as a button that brings it back.

On a wide screen the map sits beside the conversation. On a phone they take turns on the whole screen: the header's **Map** button switches to the map, and **Chat** switches back. A new view doesn't take over the screen while an answer is coming. A dot on the button says something new is on the other side.

Given a path (a source and a destination), `display` traces it through the routing tables and highlights it hop by hop, with the route each device used. At layer 2 the path runs through the switches, out the port each one bridges it to. At layer 3 it crosses the networks instead. Where a path is lost, the device that loses it turns red, with the reason.

The layout is a force simulation ([d3-force](https://d3js.org/d3-force), included in the package): nodes repel each other and links act as springs. A view changes the forces, not the nodes, so moving from one view to the next glides the network into its new shape.

You can pin nodes to lay the map out your way: say, a border leaf on top with the other leaves in a row below. A node you drag stays where you drop it, and the forces arrange everything else around it. Dropped near another pinned node's row or column, it snaps into line. Double-click a node to pin it where it is, or to let it go; the inspector has the same switch, plus "Unpin all". Pins are yours, not the model's: they hold across views, are kept per plane, and are saved in your browser.

#### Charts

When NetGPT polls (it does unless `--poll-interval 0`), the page also offers `chart`. Asked how busy something is, the model charts ports' traffic, utilization, or errors over the last minutes, or days with TimescaleDB, right in the conversation. A chart of a long window plots averages (5-minute ones over 6 hours, 30-minute ones over a week), and its caption says which. It names ports as `device:port`, a link as `leaf1--spine1`, or a device for all its ports. Each port gets its own color, with in drawn solid and out dashed. A chart keeps updating with every poll while it is on the page. Hovering shows every line's value at one poll, and the Data table under each chart has the same numbers.

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

Platform names match containerlab node kinds where there is one. `juniper_junos` is for Junos on hardware, which has none.

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

## Legacy web app (v0.3)

[`api/`](api/) and [`www/`](www/) contain the original 2023 NetGPT: a React chat interface and a FastAPI backend built on OpenAI's first function-calling API, with Keycloak authentication. They are kept for reference. **Do not deploy them:** the v0.3 API accepts requests with any bearer token, because token validation errors are logged but never rejected.
