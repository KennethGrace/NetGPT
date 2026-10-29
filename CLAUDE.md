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
netgpt-chat ... --poll-interval 10      # interface counters every 10 s (default 30; 0 turns polling off)
python lab/record_fixtures.py           # re-record the lab's responses as test fixtures
python lab/record_junos.py --inventory I --rename R   # record Junos devices' replies as renamed fixtures
python lab/gremlin.py break|reveal|repair|check   # break one thing in the lab on purpose, and put it back
python lab/game.py --model M --ollama URL         # the gremlin's game: break, ask the model why, keep score
```

## Layout

- `src/netgpt/primitives.py`: the typed records every tool returns, by layer. Each names the device that reported it.
- `src/netgpt/platforms/`: one driver per platform (`srlinux.py` speaks SR Linux JSON-RPC, `junos.py` NETCONF over SSH). `base.py` is the interface, and `connect` opens an inventory device with its driver.
- `src/netgpt/guard.py`: the read-only CLI policy for `show`.
- `src/netgpt/inventory.py`: devices, credentials (a password from an env var or `chmod 600` file, or a `chmod 600` SSH key), each device's `known_hosts`, and the networks `ping_sweep` may cover.
- `src/netgpt/topology.py`: merges the tables into one graph: `build_topology` (layer 2), `build_l3` (layer 3).
- `src/netgpt/forwarding.py`: walks routing tables hop by hop (`trace_path`). It sends nothing.
- `src/netgpt/statistics.py`: the `Poller`, which reads every device's interface counters on an interval, and the rates worked out from them.
- `src/netgpt/views.py`: turns the model's choices into a `View` of the graph. `describe` is the text the model reads back.
- `src/netgpt/server.py`: the MCP tools. `display` exists only when `build_server(display=True)`.
- `src/netgpt/chat.py`, `web.py`: the Ollama tool loop, and the web page's server.
- `src/netgpt/static/`: the page. `map.js` is the map on vendored d3-force, with each link's traffic, and `chart.js` draws charts in the conversation and the map's inspector.
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
- **Statistics:** while a client is connected, the server's lifespan runs the `Poller`: every device's interface counters every 30 seconds, the second poll 5 seconds after the first, and an hour of samples in memory. A rate is the difference of two readings over the time between them, and a counter that goes backwards is a reset, so that interval isn't measured. `interface_traffic` exists only when the server polls; the game's server doesn't. `chart` needs polling and the page: its structured content is a `Chart`, which the page draws in the conversation and asks `/api/chart` for again every poll. The map asks `/api/traffic` every poll for each port's latest rates and its errors over the last 5 minutes.
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
- **Saying what a tool can do doesn't break a habit.** With "one call covers every device" in every table tool's description, gemma4 26b still asked one device at a time (71 of 76 table calls named devices).
- **gemma4 makes one tool call per reply unless told it can make several.** In 46 game rounds it never made two. In one-reply tests it made several in 1 of 3 tries untold, and 2 of 3 told. `Chat.start` tells it, along with how many replies it has (`SEVERAL_CALLS`).

## Testing

- **Parser tests** replay JSON-RPC responses recorded from the lab (`tests/fixtures/srlinux/`, through `fakes.replay`). The single files are leaf1's, for the parser tests. `lab/<device>/` holds every device's tables, and `fakes.recorded_lab()` parses them through the real driver.
- **After changing the lab,** re-record (`python lab/record_fixtures.py`) and read the diff. A redeploy changes MAC addresses, which some tests name, and timestamps change every time.
- **Junos fixtures** (`tests/fixtures/junos/`) are real switches' replies: a QFX5120 core and two EX4400 office switches, recorded by `lab/record_junos.py` through the real driver. `replies.json` says which RPC each file answers, and `test_junos.Replay` replays them.
  - **The repository is public, and the switches are real.** The recorder replaces every name and address listed in a YAML file kept outside the repository, and blanks serial numbers. Audit the fixtures before committing: LLDP on a management port carries the neighbor's management address, model, and software version.
- **Synthetic networks:** `fakes.SwitchedNetwork` is a small routed network with a switch. `FakePlatform` backs the server, chat, and web tests. `ScriptedOllama` stands in for the model.
- **Polling:** `FakePlatform`'s counters grow the same amount on every read (`counter_reads` resets them), and the `Poller` takes a clock, so a test sets the time between polls and knows the rates exactly.
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
- **The hosts send traffic,** so the counters have something to count: iperf3 from host1 to host2 and from host3 to host1, 5 to 45 Mbit/s, changing every minute (`lab/traffic.sh`, started by the topology's `exec`).
  - **The hosts' MTU is 1500,** to match SR Linux's IP MTU. At containerlab's default 9500, TCP connects, then stalls on its first full-size segment, which the leaf counts as an input error each time it is resent. `interface_traffic` found it: errors on exactly the two ports the senders plug into, and no traffic.
- **A fresh deploy has empty ARP and MAC tables.** Sweep the host networks from their leaves (`ping_sweep` with `device`) before drawing the map or recording fixtures.
- **Dry-run SR Linux config** before asking for a redeploy. Over JSON-RPC `cli`, send `enter candidate private`, the `set` commands, `commit validate`, and `discard now`. Nothing is committed.
- **SR Linux details:**
  - Module prefixes are stripped from `get` results.
  - One `get` can fetch several narrow paths, which is cheaper than whole subtrees.
  - Management lives in network instance `mgmt`.
  - Asking for routes in an instance a device lacks returns an empty list, not an error.
  - Each ping is a CLI session; about 16 at once is the most a sweep gains from.
- **Junos details** (learned on real QFX5120 and EX4400 switches, Junos 23.4R2):
  - Every operational command has an RPC: `show ... | display xml rpc` names it.
  - The `<command>` RPC ignores pipes without saying so, so the driver filters the text itself. It answers some commands Junos lacks (`show ip route`) with an empty reply, not an error.
  - The LLDP summary names a neighbor's system but not its port; each port's detail (`get-lldp-interface-neighbors`) does. Access points send their MAC as the port ID and name the port in the description.
  - Only the `extensive` level of `get-interface-information` has error counters, and for a whole switch it is most of a megabyte, so counters are read one up port at a time.
  - The main routing instance is `master`, and it lists no interfaces: an interface no other instance lists is in it. NetGPT calls it `default`. `__juniper_private*__` instances and interfaces such as `pfe-`, `bme0`, `jsrv`, and `lsi` are Junos's own. A port nobody configured has a placeholder unit, `.16386`.
  - Routes for an instance are in two tables, `<instance>.inet.0` and `<instance>.inet6.0`. A table that doesn't exist comes back empty.
  - BGP's `Active` state means trying and failing to connect, not up; the tool's description says so. An established session lists each VPN route in `bgp.evpn.0`, where it arrived, and again in every instance table that imports it (`Global.evpn.0`), so only the arrival tables are counted.
  - The built-in `read-only` class can't run `show configuration` (it lacks `view-configuration`). A class with `view` and `view-configuration` can; Junos hides secret statements from it, but not comments, and the Mist agent leaves its device secret in one.
- **The web page reads its assets and inventory at startup.** Restart it after changing `static/` or `lab/inventory.yml`.
- **The gremlin** (`lab/gremlin.py`) is the one thing here that writes to devices. It is a lab tool outside `src/`, and the model never reaches it.
  - Each fault is SR Linux CLI: commands that break, commands that repair, the user's complaint, and the cause.
  - `check` dry-runs every fault in a private candidate. The break must change something, and the break plus the repair must leave an empty `diff`.
  - What it broke is kept in `lab/.gremlin.json` (gitignored) until it is repaired.
  - It breaks only a whole lab: `settle` waits until a set of pings from the leaves all answer. A repaired interface takes a moment to come back, and without the wait the lab shows symptoms of two faults. The pings also refill ARP and MAC tables.
  - Hosts aren't in the inventory, so NetGPT knows them only by address. The game's question gives the addresses (`gremlin.HOSTS`).
- **The game** (`lab/game.py`) runs the model in-process through `Chat`, with no `display` tool, a fresh conversation each round, and the lab always repaired in a `finally`. Rounds go to `lab/.scoreboard.jsonl` (gitignored), marked with the NetGPT commit (plus `+` and a fingerprint of uncommitted changes to `src/`) and the reply limit.
  - **Keywords can't tell blame.** A `loop` answer that names spine1 and says "loop" passed the keyword rubric while blaming leaf1. So a judge model scores each answer, and the rubric is kept beside it.
  - **The judge says what an answer blames before it decides,** through Ollama's `format` schema: the device, any other devices it says need changing (`also_blamed`), and whether it hedges. The code then overrides it: a hedge, or blame that misses the fault's device, is `missed`, and the right device with another beside it is at most `partly`. One field for the blame ran "A and B" (both wrong) together with "A or B" (a hedge).
  - **Write each fault's cause so blame is unambiguous** ("leaf1's route is as it should be"). The judge reads it literally.
  - **After changing the judge,** check it against answers with known verdicts, then `--rejudge`, which scores every round again and prints the ones that changed. A judge change does not replay the rounds.
  - **The game measures the whole stack:** the tools, the chat's loop and reply limit, the model, and the judge. With one model on the board, a better tool and a better accommodation of that model look the same. Play a second model at the same NetGPT version, with the judge fixed (`--judge`), to tell them apart.

## The page

- **The CSP is strict** (`script-src 'self'; style-src 'self'`). No inline scripts or styles. Style through classes in `app.css`, since a stylesheet rule beats an SVG presentation attribute.
- **Device data reaches the DOM only as text:** `textContent` and attributes, never HTML.
- **Narrow screens (under 1000px) show the map or the conversation, not both.** `body.map-full` gives the map the screen. The header button names the screen it leads to, so it has no pressed state there. A live view or answer marks it `fresh` instead of switching screens. A view's chip opens the map.
- **Pins are the viewer's, not the model's.** They are kept per plane in `localStorage`, and every access is wrapped in `try`.
- **Chart colors** are six `--series-n` tokens in `app.css`, one per port in a fixed order, checked for both modes against the page's own surfaces. Three of the light ones are under 3:1 contrast, so every chart has a Data table, and values never wear a series color.
- **Traffic on the map** is a weathermap: each link gets a lane per direction, on the right as it travels. A lane's color is its share of port speed, in five bands from the `--load-n` ramp. That ramp is one hue (teal, since blue means highlighted), checked as an ordinal ramp in both modes. A direction is the sending port's out counter, or else the receiving port's in. A subinterface's traffic is its port's.
  - Rate labels go at whichever end of their lane overlaps least with what is already drawn.
  - Errors get ⚠ and a count, in the warn color. Discards are only told in the inspector.
  - The lab's links run far under 10% of their virtual speeds, so every lane there is the lightest band. To see the others, check against a copy whose ports claim 100M.

## Style

- **Match the code around you:** type hints everywhere, `argparse` for command lines, `pathlib` for files. Docstrings say what a thing is for, and comments say why. Module docstrings open with "The X module ...". No linter is configured; keep lines under about 130 characters.
- **Name tests as sentences** about behavior: `test_hosts_on_one_network_reach_each_other_without_the_router`.
- **Write prose plainly:** README, docstrings, tool descriptions, and commit messages use plain words and concrete detail. Models read the tool descriptions, so say what a tool returns and when to use it.
- **Roadmaps are phases and tracks,** never durations.

## Where things stand

- **Lab track:**
  - Phase 1 is done: a bridging switch, and layer 2 and layer 3 views.
  - Phase 2, a second vendor: Junos, over NETCONF with an SSH key, built against real switches (a QFX5120 and two EX4400s, Junos 23.4R2) rather than a virtual lab. Virtual Junos needs KVM, which this VM lacks, except cRPD, which is only the routing daemon. Cisco IOL is legitimately available through CML-Free.
  - Mixing vendors on one cable will need port names normalized (`Gi1/0/45` and `GigabitEthernet1/0/45`), so both ends match.
- **Gremlin track** (can a model troubleshoot with these tools?):
  - Phase 1 is done: `lab/gremlin.py`, six faults across all four devices, each checked live.
  - Phase 2 is done: `lab/game.py` breaks a fault, asks the model the complaint, has a judge score the answer against the cause, and keeps a scoreboard per model, NetGPT commit, and reply limit.
  - Phase 3 (fix what the scoreboard shows the tools or the chat missing) is paused here. gemma4 26b, 10 replies:
    - Before Phase 3: 1 of 13 found. 11 rounds ran out of replies, and the walk flagged leaf1 for spine1's loop.
    - First pass, 8 of 12: an answer when the replies run out, loops that name every router and where the destination is connected, and SR Linux examples on a parse error. 8 of the 12 answers came on the last reply, so most of the gain is the chat's, not the tools'.
    - Telling the model one call covers every device: 13 of 18, about the same, and the calls didn't change.
  - Three models at f208158, 3 rounds of each fault, 10 replies, judged by qwen3.8 27b (`--judge`). Every earlier round was rejudged by it too, which changed 5 of 74 verdicts.
    - qwen3.8 27b: 18 of 18. It makes about 2 tool calls per reply, and answered on its own in 17 rounds.
    - gemma4 26b: 13 of 18, the same as before `SEVERAL_CALLS`, the null network instance's description, and the `show` examples. It still makes one call per reply, and 14 answers came on the last reply.
    - nemotron 3.5 lightning 30b: 4 of 16, and 4 partly; the keyword rubric says 10. The fastest, at a median of 74 s, but it blames extra devices, hedges, and reads the lab's design as faults: leaf2 routing toward a bridging switch as "a layer 2/3 mismatch", and the leaves' /16 as "too broad".
    - With these tools, the model decides most of the score, and several calls per reply is the clearest difference between them. The six faults no longer test qwen.
  - The misses left are misreadings with the facts in hand: a loop blamed on the leaves' /16 with "connected to leaf2" in the result, spine1's address read as leaf1's own, and host2 "on the wrong port" when its port was in no network instance.
  - Known in the game:
    - A judge call that fails ends the game, and that round is lost. nemotron's 17th was: the judge's reply has no length cap, and it timed out after 900 s.
    - The judge spots hedges inconsistently: "administratively down, or the link is down" passed for qwen and was a hedge for nemotron. `interfaces` reports oper-state but not admin-state, so the tools can't tell a shut port from a pulled cable either.
  - When it resumes:
    - Harder faults, so the game measures qwen again: two at once, an MTU mismatch, a wrong VLAN, something intermittent.
    - Check the tools with no model: break each fault, make fixed tool calls, and check the deciding fact is in the output, unambiguous.
    - Admin-state in `interfaces`.
    - Keep a round whose judging fails, unjudged, for `--rejudge`, and cap the judge's reply.
- **NMS track** (poll statistics and show them on the page, beside the agent):
  - Phase 1, polling: `InterfaceCounters` from the drivers, the `Poller` (every 30 seconds by default, an hour in memory), `interface_traffic` for the model, and steady traffic in the lab.
  - Phase 2, the page: charts in the conversation (`chart`, live, from the model), traffic and errors on the map's links, and a chart of the selected link or device.
  - Phase 3, persistence: storage behind an interface, with TimescaleDB (PostgreSQL) in a compose dev environment under rootless podman. History survives restarts, with rollups and retention, and the tool reads past windows.
  - Phase 4, events: errors climbing, a link saturated or down, shown on the page and told to the agent.
  - Later: protocol state (OSPF, BGP; the lab runs static routes, so a Lab phase adds them first), Neo4j for the network as a graph over time, and gNMI streaming in place of polling.
- **Frontends track:**
  - Phase 1: `netgpt-mcp` over HTTP, behind a token.
  - Phase 2: one login for the page and the endpoint.
  - Phase 3: the map inside MCP clients (MCP Apps).
- **Known gaps:**
  - `topology` and the forwarding walk are IPv4 only. Junos's interfaces and routes include IPv6; SR Linux's don't yet.
  - `bgp_sessions` is Junos only so far, and `ping` isn't on Junos yet.
  - A path and its return path are separate views.
  - A sweep from the NetGPT host can't tell "unreachable" from "empty".
