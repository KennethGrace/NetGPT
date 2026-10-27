# NetGPT: The Network Engineering GPT

[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](https://mit-license.org)
[![Author](https://img.shields.io/badge/Author-Kenneth_J_Grace-orange?style=flat-square)](https://kennethjeffersongrace.com)

NetGPT gives language models read-only access to network devices. It is an [MCP](https://modelcontextprotocol.io) server: any MCP client, such as Claude Code or Claude Desktop, can be the chat interface, and the client runs the tool-calling loop.

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
