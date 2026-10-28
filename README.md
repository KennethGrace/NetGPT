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
| `routes`         | The IPv4 routing table of a network instance (VRF) across many devices.                 |
| `topology`       | The network as one graph at layer 2 or layer 3, merged from every device's tables, with the evidence for each link. |
| `forwarding_path`| The path packets take from a source to a destination, hop by hop, read from each device's routing table. |
| `ping`           | Replies to ICMP echo requests sent from a device, inside one of its network instances.  |
| `ping_sweep`     | The addresses that answer ICMP in a network, pinged from a device (inside one of its network instances) or from the NetGPT host itself. |

The multi-device tools query devices concurrently. A device that fails shows up as an error next to the other devices' results rather than failing the whole call.

`topology` draws the network at one of two layers. At layer 2 (the default) it is the cabling. Each cable is reconciled from both ends, and a link only one end reports is flagged. Each host hangs off the port it is plugged into: ARP says which port an address was heard on, and where a switch is in the way, the switch's MAC table says which of its ports the address is behind. If a switch has forgotten a MAC, the host is drawn behind the switch with no port, and the evidence says why. At layer 3 the graph is the routing in one network instance: routers, the networks their interfaces are on, and the hosts on those networks. Switches drop out, and a network between just two routers is drawn as a link between them. Both layers keep the management network apart from the data plane, and a port that hears several LLDP neighbors (such as a management bridge) becomes one shared segment instead of a mesh of links.

`forwarding_path` walks the routing tables the way the devices do. At each hop it takes the longest matching route, resolves the route's next hop down to an interface (a static route's next hop is often just an address), and follows the cable on that interface to the next device, through any switches in the way. Between two hosts on the same network, no router is involved: the path goes straight across the switch. It reports where the path ends: delivered, or lost, with the reason (no route, a route that drops the packet, a loop, a next hop nothing answers ARP for, or a device it couldn't read). It sends nothing on the network.

## Safety model

- **Inventory only.** Tools take device names from the inventory. The model cannot point NetGPT at any other host. `ping_sweep` only covers networks listed under `sweep_networks`, up to /22 from the NetGPT host and /24 from a device.
- **Read-only CLI.** `show` accepts a command only if it starts with an allowed verb, every pipe leads to an allowed filter, and it contains no control characters or chaining syntax. On Cisco IOS this refuses `| redirect`, `| tee`, and `| append`. On SR Linux it refuses `>` redirection. Use a read-only account on the device as well; the policy is the second line of defence, not the first.
- **Credentials stay on the server.** The inventory names an environment variable or a `chmod 600` file for each password. Passwords never appear in tool arguments or results.

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

[`lab/`](lab/) holds a [containerlab](https://containerlab.dev) topology: one Nokia SR Linux spine and two leaves, with static routing across the fabric. A Linux host hangs off leaf1. Behind leaf2 is switch1, an SR Linux node that only bridges, with two hosts on its LAN. It needs [containerlab](https://containerlab.dev/install/) and Docker:

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

To see whether NetGPT can troubleshoot, let a gremlin loose in the lab. [`lab/gremlin.py`](lab/gremlin.py) breaks one thing at random: a port shut, an address mistyped, a host's switch port pulled from its LAN, a blackhole route, a routing loop, or a missing route back. It prints only the complaint a user would make ("host2 and host3 can't reach host1"). Several faults share a complaint, so the model has to investigate rather than guess. The gremlin writes configuration through the lab's own login; NetGPT stays read-only.

```bash
python lab/gremlin.py break     # then ask NetGPT about the complaint
python lab/gremlin.py reveal    # what the gremlin broke
python lab/gremlin.py repair    # put it back
python lab/gremlin.py check     # dry-run every fault; nothing is committed
```

[`lab/game.py`](lab/game.py) plays the gremlin's game with a model and keeps score. Each round, the gremlin breaks a fault, and the model gets the complaint in a fresh conversation and investigates with NetGPT's tools. The game then puts the lab back and waits for it to answer again. A judge model reads each answer beside what the gremlin broke. It names the devices the answer blames, and whether it hedges between causes, then decides: found, partly, or missed. An answer that blames another device, or hedges, has missed, however much it says along the way. One that blames the right device and an innocent one as well is at most partly right. The judge can be wrong too, so the scoreboard keeps every answer and the judge's reasons, for reading.

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

The page also has a map, drawn by a tool only the page offers: `display`. When a picture helps, such as for the topology, where a host is attached, or the path between two points, the model calls `display`. It draws the whole network from the devices' own tables, at layer 2 (cables, switches, and the port each host is on) or at layer 3 (routers and the networks between them), and it chooses which parts to focus on and highlight. Focused nodes move to the middle, and the rest of the network stays on the map, dimmed and pushed aside. The model can't draw anything the devices didn't report, and every name it uses is checked against the graph first. Click a node or link to see the evidence for it. Each view stays in the conversation as a button that brings it back.

Given a path (a source and a destination), `display` traces it through the routing tables and highlights it hop by hop, with the route each device used. At layer 2 the path runs through the switches, out the port each one bridges it to. At layer 3 it crosses the networks instead. Where a path is lost, the device that loses it turns red, with the reason.

The layout is a force simulation ([d3-force](https://d3js.org/d3-force), included in the package): nodes repel each other and links act as springs. A view changes the forces, not the nodes, so moving from one view to the next glides the network into its new shape.

You can pin nodes to lay the map out your way: say, a border leaf on top with the other leaves in a row below. A node you drag stays where you drop it, and the forces arrange everything else around it. Dropped near another pinned node's row or column, it snaps into line. Double-click a node to pin it where it is, or to let it go; the inspector has the same switch, plus "Unpin all". Pins are yours, not the model's: they hold across views, are kept per plane, and are saved in your browser.

## Inventory

```yaml
credentials:
  lab-readonly:
    username: netgpt
    password_env: NETGPT_LAB_PASSWORD        # or: password_file: ~/.config/netgpt/lab.password
devices:
  - name: core1
    host: 10.0.0.1
    platform: nokia_srlinux
    credentials: lab-readonly
    role: core
    tls_ca: certs/lab-ca.pem                 # optional; relative paths resolve against this file
sweep_networks:
  - 10.0.0.0/24
```

## Platforms

| Platform        | Transport           | Status                                                                 |
|-----------------|---------------------|------------------------------------------------------------------------|
| `nokia_srlinux` | JSON-RPC over HTTPS | All tools verified against SR Linux 25.10.5 in the lab. The tests replay responses recorded there. |

Platform names match containerlab node kinds. Cisco IOS (via containerlab's `cisco_iol` kind) is next.

## Legacy web app (v0.3)

[`api/`](api/) and [`www/`](www/) contain the original 2023 NetGPT: a React chat interface and a FastAPI backend built on OpenAI's first function-calling API, with Keycloak authentication. They are kept for reference. **Do not deploy them:** the v0.3 API accepts requests with any bearer token, because token validation errors are logged but never rejected.
