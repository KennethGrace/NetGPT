# NetGPT

NetGPT gives language models read-only access to network devices. It is an MCP
server (`netgpt-mcp`) with typed tools: LLDP, interfaces, ARP, MAC and routing
tables, a merged topology at layer 2 or 3, forwarding paths read from the routing
tables, and pings. `netgpt-chat` drives the same server from a local Ollama model,
in the terminal or as a web page (`--web`) with a map the model draws on.

## Commands

```bash
pip install -e '.[dev]'                 # Python 3.12+, in the pyenv virtualenv "netgpt"
pytest                                  # the whole suite; quick, needs no devices
netgpt-mcp --inventory lab/inventory.yml
netgpt-chat --inventory lab/inventory.yml --model gemma4:e4b --ollama http://127.0.0.1:11434
netgpt-chat ... --web 127.0.0.1:8080    # sign-in token from $NETGPT_WEB_TOKEN, or printed
python lab/record_fixtures.py           # re-record the lab's responses as test fixtures
python lab/gremlin.py break|reveal|repair|check   # break one thing in the lab on purpose, and put it back
python lab/game.py --model M --ollama URL         # the gremlin's game: break, ask the model why, keep score
```

## Layout

- `src/netgpt/primitives.py`: the typed records every tool returns, by layer. Each names the device that reported it.
- `src/netgpt/platforms/`: one driver per platform (`srlinux.py` speaks SR Linux JSON-RPC). `base.py` is the interface.
- `src/netgpt/guard.py`: the read-only CLI policy for `show`.
- `src/netgpt/inventory.py`: devices, credentials (env var or `chmod 600` file), and the networks `ping_sweep` may cover.
- `src/netgpt/topology.py`: merges the tables into one graph: `build_topology` (layer 2), `build_l3` (layer 3).
- `src/netgpt/forwarding.py`: walks routing tables hop by hop (`trace_path`). It sends nothing.
- `src/netgpt/views.py`: turns the model's choices into a `View` of the graph. `describe` is the text the model reads back.
- `src/netgpt/server.py`: the MCP tools. `display` exists only when `build_server(display=True)`.
- `src/netgpt/chat.py`, `web.py`: the Ollama tool loop, and the web page's server.
- `src/netgpt/static/`: the page. `map.js` is the map on vendored d3-force.
- `lab/`: the containerlab lab (SR Linux spine, two leaves, a bridging switch, Linux hosts) and its inventory.
- `api/`, `www/`: the 2023 v0.3 app, kept for reference only. Never deploy it: its API accepts any bearer token.

## Rules that hold everywhere

- **Read-only.** Every tool is annotated read-only and nothing writes to a device. `show` passes the guard first. Where a command is built for the device (ping), it is built from validated parts, never from model text.
- **Inventory only.** Tools reach only inventory devices, and sweeps only the inventory's `sweep_networks`.
- **Credentials never reach the model.** They are not in tool arguments, results, errors, or logs.
- **Devices supply facts; the model supplies choices.** The model picks what to focus on, highlight, and note. Every name it uses is checked against the graph, and a refusal lists the names that exist.
- **Evidence for everything.** Every node and link carries the lines of device output it came from.
- **One device's failure is not the answer's.** Multi-device tools return a `Collection` with per-device errors. A query a platform hasn't implemented raises `UnsupportedError`. The topology treats MAC tables and interfaces as optional.

## How it works, briefly

- **Layer 2:** LLDP gives cables. A port hearing several LLDP neighbors is a shared segment. For each ARP entry, `locate` follows the address from the port it was heard on through switches' MAC tables to the port it is plugged into. A forgotten MAC leaves the host behind the switch with no port.
- **Layer 3:** routers, subnets (from routed interfaces), and the hosts on them. Switches drop out. A /30 or /31 between two routers becomes a link with `subnet` set.
- **Forwarding walk:** longest match, recursive next-hop resolution, then the cable. `beyond` crosses shared segments and bridged ports, and each switch crossed becomes a hop. Hosts on the same network reach each other without the router.
- **Model and page:** a `display` result gives the model `describe(view)` as text and gives the page the whole view as structured content. The chat keeps it under `_structured` and strips that before Ollama sees the history.

## Small models

The chat is tuned against small local models (gemma4 e4b on CPU, 26b on a GPU or Apple silicon). What they taught:

- **Ollama's tool schemas have no `$ref`:** nested parameters arrive empty. `chat.plain_schema` inlines them; keep tool parameters flat where possible.
- **Accept the shapes models send instead of failing validation:** a string for a list, an object keyed by names (`as_names`), `[source, destination]` for a path, and link names with either end first or written `node:interface`.
- **Write errors that say what to do next:** "Name one of leaf2's addresses: ..." gets a correct retry.
- **Watch gemma4's narration:** it returns an empty turn after tool results when its tool-calling reply also narrated, so `Chat.ask` strips that text from history.
- **Don't throw away an investigation.** When every reply to a question has gone on tool calls, `Chat.ask` asks once more with no tools offered (`OUT_OF_TOOLS`), so what the model found becomes an answer.
- **A tool that flags one device gets that device blamed.** The forwarding walk once marked only where a loop closed, and the model blamed that router's correct route. Report every device a fact implicates, and the fact that decides between them: where the destination's network is actually connected.
- **Models guess Cisco commands.** `show interfaces` on SR Linux wastes a reply, then a variation wastes another. The driver adds working examples to a parse error.

## Testing

- **Parser tests** replay JSON-RPC responses recorded from the lab (`tests/fixtures/srlinux/`, through `fakes.replay`). The single files are leaf1's, for the parser tests. `lab/<device>/` holds every device's tables, and `fakes.recorded_lab()` parses them through the real driver.
- **After changing the lab,** re-record (`python lab/record_fixtures.py`) and read the diff. A redeploy changes MAC addresses, which some tests name, and timestamps change every time.
- **Synthetic networks:** `fakes.SwitchedNetwork` is a small routed network with a switch. `FakePlatform` backs the server, chat, and web tests. `ScriptedOllama` stands in for the model.
- **Check driver changes against the live lab** before calling them done. Recording the lab found that SR Linux spells a learned MAC `learnt`. SR Linux also reports an unanswered ping as either a result or an error.
- **The page has no JS test suite.** Check it in headless Firefox:
  - Build a static copy of the page whose stub `fetch` replays real views, and screenshot it.
  - `ui.prefersReducedMotion=1` in the profile's `user.js` makes the layout settle at once.
  - For interaction, inject a script that dispatches `PointerEvent`s, with `setPointerCapture` stubbed out.
  - Serve the copy over HTTP if it needs `localStorage`.
  - Once a page scrolls, Firefox draws its fixed and sticky layers about 110px off, so keep test pages short.

## The lab

- **Runtime:** containerlab on Docker, not podman: SR Linux 25.10.5 crashes during boot under podman. Shells may still export `DOCKER_HOST` pointing at podman; use `env -u DOCKER_HOST` for Docker commands.
- **Deploying needs root.** Ask the user to run `sudo containerlab deploy -t lab/netgpt.clab.yml` (`--reconfigure` after changing the topology).
- **A fresh deploy has empty ARP and MAC tables.** Sweep the host networks from their leaves (`ping_sweep` with `device`) before drawing the map or recording fixtures.
- **Dry-run SR Linux config** before asking for a redeploy. Over JSON-RPC `cli`, send `enter candidate private`, the `set` commands, `commit validate`, and `discard now`. Nothing is committed.
- **SR Linux details:**
  - Module prefixes are stripped from `get` results.
  - One `get` can fetch several narrow paths, which is cheaper than whole subtrees.
  - Management lives in network instance `mgmt`.
  - Asking for routes in an instance a device lacks returns an empty list, not an error.
  - Each ping is a CLI session; about 16 at once is the most a sweep gains from.
- **The web page reads its assets and inventory at startup.** Restart it after changing `static/` or `lab/inventory.yml`.
- **The gremlin** (`lab/gremlin.py`) is the one thing here that writes to devices. It is a lab tool outside `src/`, and the model never reaches it.
  - Each fault is SR Linux CLI: commands that break, commands that repair, the user's complaint, and the cause.
  - `check` dry-runs every fault in a private candidate. The break must change something, and the break plus the repair must leave an empty `diff`.
  - What it broke is kept in `lab/.gremlin.json` (gitignored) until it is repaired.
  - It breaks only a whole lab: `settle` waits until a set of pings from the leaves all answer. A repaired interface takes a moment to come back, and without the wait the lab shows symptoms of two faults. The pings also refill ARP and MAC tables.
  - Hosts aren't in the inventory, so NetGPT knows them only by address. The game's question gives the addresses (`gremlin.HOSTS`).
- **The game** (`lab/game.py`) runs the model in-process through `Chat`, with no `display` tool, a fresh conversation each round, and the lab always repaired in a `finally`. Rounds go to `lab/.scoreboard.jsonl` (gitignored), marked with the NetGPT commit (plus `+` and a fingerprint of uncommitted changes to `src/`) and the reply limit.
  - **Keywords can't tell blame.** A `loop` answer that names spine1 and says "loop" passed the keyword rubric while blaming leaf1. So a judge model scores each answer, and the rubric is kept beside it.
  - **The judge names the blamed device first,** through Ollama's `format` schema, and the code refuses `found` or `partly` unless that is the fault's device. Before that, the judge passed the same wrong answer.
  - **Write each fault's cause so blame is unambiguous** ("leaf1's route is as it should be"). The judge reads it literally.
  - **After changing the judge,** check it against answers with known verdicts, then `--rejudge`. A judge change does not replay the rounds.

## The page

- **The CSP is strict** (`script-src 'self'; style-src 'self'`). No inline scripts or styles. Style through classes in `app.css`, since a stylesheet rule beats an SVG presentation attribute.
- **Device data reaches the DOM only as text:** `textContent` and attributes, never HTML.
- **Pins are the viewer's, not the model's.** They are kept per plane in `localStorage`, and every access is wrapped in `try`.

## Style

- **Match the code around you:** type hints everywhere, `argparse` for command lines, `pathlib` for files. Docstrings say what a thing is for, and comments say why. Module docstrings open with "The X module ...". No linter is configured; keep lines under about 130 characters.
- **Name tests as sentences** about behavior: `test_hosts_on_one_network_reach_each_other_without_the_router`.
- **Write prose plainly:** README, docstrings, tool descriptions, and commit messages use plain words and concrete detail. Models read the tool descriptions, so say what a tool returns and when to use it.
- **Roadmaps are phases and tracks,** never durations.

## Where things stand

- **Lab track:**
  - Phase 1 is done: a bridging switch, and layer 2 and layer 3 views.
  - Phase 2 is a second vendor. Arista cEOS-lab is a container with a JSON API. Cisco IOL is legitimately available through CML-Free. This VM has no nested KVM, so images packaged as VMs (vJunos, Nexus 9000v, Cat8000v) need other hardware.
  - A second vendor will need port names normalized, so both ends of a cable match.
- **Gremlin track** (can a model troubleshoot with these tools?):
  - Phase 1 is done: `lab/gremlin.py`, six faults across all four devices, each checked live.
  - Phase 2 is done: `lab/game.py` breaks a fault, asks the model the complaint, has a judge score the answer against the cause, and keeps a scoreboard per model, NetGPT commit, and reply limit.
  - Phase 3: fix what the scoreboard shows the tools or the chat missing. The first games, with gemma4 26b and 2 rounds of each fault, showed:
    - With 10 replies (the page's default), nothing was found: 11 of 13 rounds ran out of replies. With 20 replies, 5 of 12 were found.
    - `port-down` and `renumbered` were never answered, even with 20 replies. Both take the hosts out of leaf2's ARP table.
    - 2 of 3 `loop` answers blamed leaf1's route. The forwarding walk flags leaf1 (where the packet comes back) as the trouble, not spine1.
    - When the model reaches for `show`, it guesses Cisco syntax (`show interfaces`, `show mac-address-table`) on SR Linux.
  - Phase 3, first pass: an answer when replies run out, loops that name every device and where the destination is connected, and SR Linux examples on a parse error. With 10 replies: 9 of 12 found, 2 partly, none unanswered (from 0 of 13). 8 of the 12 answers came on the last reply, with no tools offered. The three changes were played together, so their shares are unknown.
  - Still open:
    - The model queries devices one call at a time (`routes` three times), where one call with no devices queries all of them.
    - It read `network_instance: null` (a subinterface in no network instance) as an instance named "null".
    - Two rounds of each fault is a small sample; the same fault scored very differently from round to round.
- **Frontends track:**
  - Phase 1: `netgpt-mcp` over HTTP, behind a token.
  - Phase 2: one login for the page and the endpoint.
  - Phase 3: the map inside MCP clients (MCP Apps).
- **Known gaps:**
  - IPv4 only.
  - A path and its return path are separate views.
  - A sweep from the NetGPT host can't tell "unreachable" from "empty".
