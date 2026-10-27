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
| `mac_table`      | Layer 2 forwarding entries across many devices.                                         |
| `arp_table`      | ARP bindings across many devices.                                                       |
| `routes`         | The IPv4 routing table of a network instance (VRF) across many devices.                 |
| `ping`           | Replies to ICMP echo requests sent from a device, inside one of its network instances.  |
| `ping_sweep`     | The addresses that answer ICMP in a network, pinged from the NetGPT host itself.        |

The multi-device tools query devices concurrently. A device that fails shows up as an error next to the other devices' results rather than failing the whole call.

## Safety model

- **Inventory only.** Tools take device names from the inventory. The model cannot point NetGPT at any other host. `ping_sweep` only covers networks listed under `sweep_networks`, up to /22.
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

[`lab/`](lab/) holds a [containerlab](https://containerlab.dev) topology: one Nokia SR Linux spine, two leaves, and a Linux host behind each leaf, with static routing across the fabric. It needs [containerlab](https://containerlab.dev/install/) and Docker:

```bash
sudo containerlab deploy -t lab/netgpt.clab.yml
```

Use Docker, not podman. Under podman, SR Linux 25.10.5 crashes during boot, and containerlab no longer tests its podman runtime. On RHEL, Docker CE can run alongside podman: remove only the `podman-docker` package (the `docker` alias for podman), not podman itself.

[`lab/inventory.yml`](lab/inventory.yml) already describes the lab. It reads SR Linux's default lab password from a file:

```bash
mkdir -p ~/.config/netgpt && printf 'NokiaSrl1!' > ~/.config/netgpt/srlinux-lab.password
chmod 600 ~/.config/netgpt/srlinux-lab.password
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
