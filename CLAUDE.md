# NetGPT

NetGPT gives language models read-only access to network devices. It is an MCP
server (`netgpt-mcp`) with typed tools: LLDP, interfaces, ARP, MAC and routing
tables, a merged topology at layer 2 or 3, forwarding paths read from the routing
tables, and pings. `netgpt-chat` drives the same server from a local Ollama model,
in the terminal or as a web page (`--web`) with a map the model draws on.

## Goals

The gremlin's games are how NetGPT finds work to move from the model's reasoning into
deterministic tools. The question behind each change: how do we make the platform easier
for the assistant to use, so it gives better answers?

- **Two directions:** better tools for the assistant, and better visibility for the user. Where they meet, make it easy for the assistant to show the user good visuals.
- **A tool takes over a fact, not a verdict.** Which end of a mismatch is wrong is a convention: partway through rolling out authentication, the end that has it is the new standard. That one end is set unlike every other interface is a fact. The model decides from it, and says what it assumed.
- **Put the fact where the model already looks.** A better tool beside the one it calls goes unused. In 65 WAN rounds, qwen read `ospf_interfaces` in 59 and the OSPF view in 18, and none of its authentication misses opened the view. The transcripts say which tools get called.
- **A fact worth computing is one an engineer would want too.** That keeps the tools from being fitted to the gremlin's faults, and it puts the fact on the page as well: one computation, given to the model as text (`describe`) and to the page as the view. What the page shows also lets the user check the assistant's answer.
- **Each miss has three levers:** the tools, the chat's loop, and the model (fine-tuning, for what can't be made deterministic: reading the complaint, choosing what to look at, the platform's commands). With one model on the scoreboard, a better tool and a better accommodation of that model look the same; a change that helps a second model too is the tool's.

## Commands

```bash
pip install -e '.[dev,store]'           # Python 3.12+, in the pyenv virtualenv "netgpt"; store is psycopg
pytest                                  # the whole suite; quick, needs no devices
NETGPT_TEST_STORE=postgresql://netgpt@127.0.0.1:5432/netgpt_test pytest   # and the TimescaleDB store
podman compose -f dev/compose.yml up -d # TimescaleDB on 127.0.0.1:5432 (start podman.socket first: see the file)
netgpt-mcp --inventory lab/inventory.yml
netgpt-chat --inventory lab/inventory.yml --model gemma4:e4b --ollama http://127.0.0.1:11434
netgpt-chat ... --web 127.0.0.1:8080    # sign-in token from $NETGPT_WEB_TOKEN, or printed
netgpt-chat ... --poll-interval 10      # interface counters every 10 s (default 30; 0 turns polling off)
netgpt-chat ... --store postgresql://netgpt@127.0.0.1:5432/netgpt   # keep them in TimescaleDB, across restarts
python lab/record_fixtures.py           # re-record the lab's responses as test fixtures
python lab/record_junos.py --inventory I --rename R   # record Junos devices' replies as renamed fixtures
python lab/gremlin.py break|reveal|repair|check   # break one thing in the lab on purpose, and put it back
python lab/game.py --model M --ollama URL         # the gremlin's game: break, ask the model why, keep score
python lab/game.py --lab wan --model M ...        # the same with OSPF faults, in the WAN lab
python lab/game.py --lab wan --anywhere ...       # each fault at a random one of its places
```

## Layout

- `src/netgpt/primitives.py`: the typed records every tool returns, by layer. Each names the device that reported it.
- `src/netgpt/platforms/`: one driver per platform (`srlinux.py` speaks SR Linux JSON-RPC, `junos.py` NETCONF over SSH). `base.py` is the interface, and `connect` opens an inventory device with its driver.
- `src/netgpt/guard.py`: the read-only CLI policy for `show`.
- `src/netgpt/inventory.py`: devices, credentials (a password from an env var or `chmod 600` file, or a `chmod 600` SSH key), each device's `known_hosts`, and the networks `ping_sweep` may cover.
- `src/netgpt/topology.py`: merges the tables into one graph: `build_topology` (layer 2), `build_l3` (layer 3).
- `src/netgpt/forwarding.py`: walks routing tables hop by hop (`trace_path`). It sends nothing.
- `src/netgpt/statistics.py`: the `Poller`, which reads every device's interface counters on an interval, and the rates worked out from them.
- `src/netgpt/store.py`: where the poller's samples are kept, with each interval's rate and their rollups. `Store` is the interface (async, so a database can stand behind it). `MemoryStore` keeps the last hour in the process; `TimescaleStore` keeps a week of samples and rates in TimescaleDB, across restarts, with 5-minute rollups for 90 days and hourly ones for two years.
- `dev/compose.yml`: TimescaleDB for development, under rootless podman.
- `src/netgpt/views.py`: turns the model's choices into a `View` of the graph. `describe` is the text the model reads back.
- `src/netgpt/server.py`: the MCP tools. `display` exists only when `build_server(display=True)`.
- `src/netgpt/chat.py`, `web.py`: the Ollama tool loop, and the web page's server.
- `src/netgpt/static/`: the page. `map.js` is the map on vendored d3-force, with each link's traffic, and `chart.js` draws charts in the conversation and the map's inspector.
- `lab/`: the containerlab lab (SR Linux spine, two leaves, a bridging switch, Linux hosts) and its inventory.
- `lab/wan/`: the WAN lab: three sites (HQ, Ops, a branch) on 25G, 10G, and 1G links, routed by OSPF, with its own inventory.
- `.github/workflows/tests.yml`: the whole suite on every push to main and every pull request, against a fresh TimescaleDB.
- The 2023 v0.3 app is at the tag `v0.3.6g`, kept for reference only. Never deploy it: its API accepts any bearer token.

## Rules that hold everywhere

- **Read-only.** Every tool is annotated read-only and nothing writes to a device. `show` passes the guard first. Where a command is built for the device (ping), it is built from validated parts, never from model text.
- **Inventory only.** Tools reach only inventory devices, and sweeps only the inventory's `sweep_networks`.
- **Credentials never reach the model.** They are not in tool arguments, results, errors, or logs. The store's password is in `~/.pgpass`, where libpq finds it, and a `--store` URL with a password in it is refused.
- **Devices supply facts; the model supplies choices.** The model picks what to focus on, highlight, and note. Every name it uses is checked against the graph, and a refusal lists the names that exist.
- **Evidence for everything.** Every node and link carries the lines of device output it came from.
- **One device's failure is not the answer's.** Multi-device tools return a `Collection` with per-device errors. A query a platform hasn't implemented raises `UnsupportedError`. The topology treats MAC tables and interfaces as optional.

## How it works, briefly

- **Layer 2:** LLDP gives cables. A port hearing several LLDP neighbors is a shared segment. For each ARP entry, `locate` follows the address from the port it was heard on through switches' MAC tables to the port it is plugged into. A forgotten MAC leaves the host behind the switch with no port.
- **Layer 3:** routers, subnets (from routed interfaces), and the hosts on them. Switches drop out. A /30 or /31 between two routers becomes a link with `subnet` set.
- **OSPF view** (`build_ospf`, layer `ospf`): OSPF interfaces are grouped by the network their addresses are on. Two routers on one network are a link whether or not they became neighbors, with each end's area, cost, and adjacency state, and `problems` naming what keeps them apart (settings that differ, area type and authentication among them, a passive end, how far each side got). For a setting an interface has of its own (not its area or area type), the problems say how the other OSPF interfaces that aren't passive have it, so the end set unlike the rest is named, and which end to change stays the model's call (`ospf_disagreements`). `ospf_interfaces` returns the same lines, after each network, as `mismatches`: models read it far more often than the view. A router on the network that doesn't run OSPF there is named too. A network with one OSPF router on it is a subnet node in its area. Router IDs map neighbors to devices; one outside the inventory is a `neighbor` node. `describe` gives the model every link's problems.
- **Forwarding walk:** longest match, recursive next-hop resolution, then the cable. `beyond` crosses shared segments and bridged ports, and each switch crossed becomes a hop. Hosts on the same network reach each other without the router.
- **Statistics:** while a client is connected, the server's lifespan runs the `Poller`: every device's interface counters every 30 seconds, the second poll 5 seconds after the first, and the samples in a `Store` (by default, the last hour in memory). As each sample arrives, the store works out the rate over the interval since that port's previous one (`store.rate`), the one place that knows about resets, and keeps the rate beside the counters. A rate is the difference of two readings over the time between them. A counter that goes backwards is a reset, and readings more than three polls apart are a gap, so neither interval is measured. A window longer than 400 polls is read from rollups, in the shortest step from 5 minutes up that keeps it to 400 points (`step_for`). Each step keeps its average and its busiest interval, so a peak survives the averaging. `interface_traffic` exists only when the server polls; the game's server doesn't. `chart` needs polling and the page: its structured content is a `Chart`, which the page draws in the conversation and asks `/api/chart` for again every poll. The map asks `/api/traffic` every poll for each port's latest rates and its errors over the last 5 minutes.
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
- **WAN fixtures** (`tests/fixtures/srlinux/wan/`) are the WAN lab's tables, OSPF included, and `fakes.recorded_wan()` parses them through the real driver. The OSPF view's failures (areas, MTUs, a passive or one-way end, and how the other interfaces are set) are tested on synthetic routers in `test_ospf.py`.
- **Synthetic networks:** `fakes.SwitchedNetwork` is a small routed network with a switch. `FakePlatform` backs the server, chat, and web tests. `ScriptedOllama` stands in for the model.
- **Stores:** `tests/test_store.py` holds what every store must do, and runs it against each kind, rollups included: `MemoryStore` rolls up in Python and TimescaleDB in SQL, to the same answers. The TimescaleDB store is tested only when `NETGPT_TEST_STORE` names a database the tests may write to; each test makes a schema of its own and drops it. Its rollups are read in real time, so a test sees rows TimescaleDB hasn't stored in them yet.
  - **The tests' samples are stamped from 2100** (`START`), where TimescaleDB's jobs never reach. Stamped from 1970, a fresh database's retention job dropped them mid-test, every time. The dev container's `netgpt_test` runs no job scheduler, so it never showed; CI's fresh database does.
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
- **Deploying needs root.** Ask the user to run `sudo containerlab deploy -t lab/netgpt.clab.yml` (`--reconfigure` after changing the topology). The WAN lab is `lab/wan/wan.clab.yml`, beside it on 172.20.21.0/24.
- **The hosts send traffic,** so the counters have something to count: iperf3 from host1 to host2 and from host3 to host1, 5 to 45 Mbit/s, changing every minute (`lab/traffic.sh`, started by the topology's `exec`).
  - **The hosts' MTU is 1500,** to match SR Linux's IP MTU. At containerlab's default 9500, TCP connects, then stalls on its first full-size segment, which the leaf counts as an input error each time it is resent. `interface_traffic` found it: errors on exactly the two ports the senders plug into, and no traffic.
- **A fresh deploy has empty ARP and MAC tables.** Sweep the host networks from their leaves (`ping_sweep` with `device`) before drawing the map or recording fixtures.
- **Dry-run SR Linux config** before asking for a redeploy. Over JSON-RPC `cli`, send `enter candidate private`, the `set` commands, `commit validate`, and `discard now`. Nothing is committed.
- **SR Linux details:**
  - Module prefixes are stripped from `get` results.
  - One `get` can fetch several narrow paths, which is cheaper than whole subtrees.
  - Management lives in network instance `mgmt`.
  - Asking for routes in an instance a device lacks returns an empty list, not an error.
  - OSPF: a neighbor's `up-time` is in hundredths of a second, and 0.0.0.0 names no router (the DR of a point-to-point link). A wildcard `get` on a device without OSPF returns `{}`.
  - An OSPF area's type is in its `stub` and `nssa` containers, and an interface's authentication in its keychain, from the same one `get`. Only each keychain key's `algorithm` is read: a wider path would fetch the keys. With nothing configured, each of those paths answers `{}`.
  - Each ping is a CLI session; about 16 at once is the most a sweep gains from.
  - The lab's nodes are 7220 IXR-D2L, whose 25G ports accept `port-speed` 1G, 10G, or 25G. Ports come in groups that share a speed mode: a port left at 25G goes down, "unsupported-speed", when another in its group is set to 10G or 1G, and `commit validate` doesn't catch it. Setting 25G explicitly fails validation even alone, so leave it unset.
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
  - It knows two labs (`gremlin.LABS`): the fabric, with static-route faults, and the WAN, with OSPF faults (a cost, an area, an MTU, a passive interface, a hello interval, a static route that beats OSPF, an area range, a stub area, and authentication). Fault names don't repeat across labs, so `gremlin.FAULTS` finds any of them; `--lab` picks the lab.
  - **Variants:** each WAN fault has a usual place (`WAN_FAULTS`) and variants (`Lab.variants`, 72 in all): the faults that kill an adjacency at either end of any of the five (`WAN_ADJACENCIES`), and other costs, static routes, and area ranges. Each carries the complaint its symptoms would bring. A fault always in one place can be remembered rather than found, so `--anywhere` (gremlin and game) breaks a random variant. A variant is named `fault@place` (`passive@hq-dc:ethernet-1/1.0`); the state file and each round keep those names, and the scoreboard keeps rounds played anywhere apart from rounds in the usual places.
  - **Every variant was broken live** (all 72): each showed its symptoms, and every claim its complaint makes held (the routes it moves, the pings it stops and the ones it doesn't). `takes` is the slowest variant's time, with some to spare: a passive end or a wrong area drops the adjacency at once at its own end, but the far end holds it full until its dead interval runs out (up to 48 seconds), so before then the OSPF view shows one side full. The lab was whole within 45 seconds of every repair.
  - Now and then SR Linux refuses a JSON-RPC request with "Invalid session id" before running it; `cli` tries once more. One ended a run of the checks after 59 faults.
  - Each fault is SR Linux CLI: commands that break, commands that repair, the user's complaint, and the cause. `takes` is how long its symptoms take to show: a hello mismatch drops the adjacency only when the dead interval runs out.
  - `check` dry-runs every fault in a private candidate. The break must change something, and the break plus the repair must leave an empty `diff`.
  - What it broke is kept in the lab's `.gremlin.json` (`lab/`, `lab/wan/`; gitignored) until it is repaired.
  - It breaks only a whole lab: `settle` waits until a set of pings all answer, and in the WAN, until every router has its full OSPF neighbors, since OSPF routes around a missing adjacency and every ping still answers. A repaired interface takes a moment to come back, and without the wait the lab shows symptoms of two faults. The pings also refill ARP and MAC tables.
  - Hosts aren't in the inventory, so NetGPT knows them only by address. The game's question gives the addresses (the lab's `hosts`).
  - The WAN's pings get three tries each: its datapath drops the odd packet.
- **The game** (`lab/game.py`) runs the model in-process through `Chat`, with no `display` tool, a fresh conversation each round, and the lab always repaired in a `finally`. Rounds go to `lab/.scoreboard.jsonl` (gitignored), marked with the NetGPT commit (plus `+` and a fingerprint of uncommitted changes to `src/`) and the reply limit. Each answered round's whole conversation, thinking and tool results included, goes to `lab/.transcripts/` (gitignored) in Ollama's message format with the tools as offered, and the round's `transcript` names it. Rounds before it kept only the calls and the answer.
  - **Models write Unicode hyphens** ("hq‑core" with U+2011), so the rubric reads them as plain ones. Two of qwen's right answers had failed it on that alone.
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
- **The OSPF view** labels each end of a link with its cost and drops the network from the middle. Area 0 keeps the plain wire, and each other area takes one of three `--area-n` tokens in turn, on its links and stub networks, below the highlight in the stylesheet. They weren't checked as carefully as the chart colors. A link with problems is dashed in `--error`. A link keeps its element from view to view, and layer 3 and OSPF name a link between routers alike, so `relabel` refreshes its labels on every view.
- **The map orders a row of devices by the inventory.** The WAN inventory lists the branch between the two sites, so it sits between their cores.
- **Long charts:** a chart read from rollups has `step` set. Its caption names the averages ("30-minute averages"), the hover matches points within half a step, and errors count in whole numbers. Time ticks fall on the viewer's clock (hours and midnights where they are), and past a day they carry the weekday or the date.
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
- **OSPF track** (branch `feature/ospf`):
  - Phase 1 is done: the WAN lab. Every adjacency is Full, and the branch's traffic to Ops goes through HQ (cost 22) while its own 1G link to Ops sits idle.
  - Phase 2 is done: `ospf_neighbors` and `ospf_interfaces`, read from SR Linux in one `get` of narrow paths (the link-state database stays on the device), with fixtures recorded from the WAN lab (`tests/fixtures/srlinux/wan/`). Routes already carried `ospfv2`, metric, and preference.
  - The WAN lab's adjacencies reset now and then: hq-core's three went down and back at different times in its first 10 minutes, then held. hq-core carries every cross-site flow and counts the most input errors, so lost bursts of hellos are the guess.
  - Phase 3 is done: the OSPF view, for `topology` and `display` (layer `ospf`), and on the map. Checked live (no problems on the WAN lab, and the branch-to-Ops path drawn along its adjacencies through HQ) and in headless Firefox with an area mismatch built from the fixtures.
  - Later in the view: sites as groups on the map (an inventory `site`), and adjacency states on a network with several OSPF routers (drawn as a subnet node now, without them).
  - Phase 4: Junos OSPF.
  - Phase 5: WAN faults for the gremlin: a cost that moves traffic, an area mismatch, an MTU mismatch stuck in ExStart, a passive interface, mismatched hello timers, and a static blackhole that beats OSPF. Two pairs share a complaint. Each passes `check`, and each was broken live. The MTU change brings an adjacency that is already full down on its own.
  - **Harder faults:** `summary` (hq-core keeps HQ's datacenter out of area 0 with an `area-range` set not to advertise: every adjacency stays full, and only the routes tell), and `stub` and `auth` (ops-dc's area stub at its end only, or MD5 on ops-core's end: the adjacency is gone, and no setting the typed tools reported differed, so the cause was in `show`). Each was checked live. `ospf_interfaces` reports area types and authentication now, so the OSPF view names both, and they are no longer hidden.
  - **Pairs** (`--pairs`, `break --pair`): two faults at once, on different devices, whose complaints don't contradict each other. Anywhere, the first is a variant that changes the paths between sites (`effect` "wan") and the second one that cuts a datacenter off ("dc"), which leave each other's symptoms alone. Static routes and area ranges don't pair, so `hello+stub` replaced `static+stub`. The question numbers both complaints. The judge takes each break on its own, told about the other (`JUDGE_PAIR_PROMPT`), so blaming the other break's device isn't extra blame; a pair is found when both are, and partly when either is. The rubric needs both. A pair's scoreboard column is `cost+auth`.
  - First WAN game (ee0ad09, qwen3.8 27b, 3 rounds of each, 10 replies): 18 of 18, none on the last reply, a median of 154 s and 4 replies. It used an OSPF tool in 17 rounds (`ospf_neighbors` 18 calls, `ospf_interfaces` 14, `topology` as ospf 6), and `bgp_sessions` 4 times, which SR Linux doesn't support. The OSPF faults don't test qwen either.
  - Harder game (bc5166d, qwen3.8 27b, 3 rounds of each, 10 replies): 16 of 21, 3 partly. Alone, `summary` and `stub` 3 of 3 and `auth` 2 of 3; in pairs, `mtu+hello` and `area+passive` 3 of 3, `cost+auth` and `static+stub` 1 of 3.
    - **The guesses were SR Linux's OSPF commands:** almost all of its failed `show` commands were attempts at OSPF's, in SR OS's spelling (`show router ospf`), Cisco's (`show ospf`), or nearly right (`protocol` for `protocols`). The parse-error hint had no OSPF example; it has since 12e0b4c.
    - **A missing adjacency with no setting that differs reads as authentication.** In two `static+stub` rounds it never read the configuration, and blamed or hedged on authentication where the stub flag was. Its one `auth` miss hedged among authentication, timers, and stub for the same reason.
    - **Which end of a mismatch is wrong is a convention.** Both `cost+auth` rounds not found named ops-core's keychain on the right interface, then offered to add it to ops-dc instead. The judge counts that as blaming ops-dc. The evidence for ops-core's end is that no other link authenticates.
  - **Transcript run** (d83ef0d on the scoreboard, the store branch before a rebase, with 1e176a3's tools; `qwen3.8:27b` Q4_K_M on the P40s, `--anywhere`, 5 cycles of the 9 faults and the 4 pairs, 10 replies, judged by itself): 58 of 65 found, 3 partly, 4 missed; a median of 310 s and 5 replies, one on the last reply. Every fault 5 of 5 but `hello` 4, `cost+auth` 3, and `auth` 1. Every round's transcript was kept; for fine-tuning, the rounds both the judge and the rubric call found (57), since the judge let one hedge through.
    - **qwen never once took authentication off a core router.** With the keychain on ops-dc or the branch, it removed it there (4 of 4). With it on a core, it added a keychain at the other end, or offered both ends (0 of 6). The deciding fact, that nothing else authenticates, was in `ospf_interfaces`, which it read; it didn't use it. So `ospf_interfaces` and the OSPF view now say how the other interfaces are set.
    - It still opens most rounds with SR OS's `show router ...`, and recovers from the hint each time.
  - **The WAN lab's traffic is 1 to 9 Mbit/s a flow.** At the fabric's 5 to 45, hq-core, which forwards every cross-site flow, heard only about 60% of the hellos sent to it, while its neighbors heard about 97% of its own. Its adjacencies flapped whenever four in a row were lost. The other routers heard 89 to 98%, in step with how much each forwards.
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
  - Phase 3, persistence (3a and 3b merged from `feature/persist-store`; 3c on `feature/rollups`): TimescaleDB (PostgreSQL) in a compose dev environment under rootless podman. Postgres holds the counters, and later the protocol state, events, and table snapshots, in one place. Neo4j isn't planned: the graphs are built from the tables by tested Python, so keeping the tables over time and running the same builders on old ones answers "what did the network look like then" without writing the topology a second time in Cypher.
    - 3a is done: the `Store` interface, with the in-memory store behind it, and tests every store must pass (`tests/test_store.py`, one fixture per kind of store).
    - 3b is done: `TimescaleStore`, `dev/compose.yml`, and `--store` for `netgpt-mcp` and `netgpt-chat`.
      - Raw counters go in a hypertable, as `numeric(20)`, since an unsigned 64-bit counter outgrows `bigint`. Each poll goes in with `COPY`, and `interface_ports` lists the ports, so listing them doesn't scan a week of samples. A retention policy drops samples older than the store's window (7 days).
      - The password is in `~/.pgpass` for NetGPT and in `~/.config/netgpt/timescale.env` for the container, both `chmod 600`. `dev/.env` (gitignored) can put the data in a directory of its own instead of a named volume.
      - A store that can't keep a poll is reported for each device it polled, and polling goes on. `open` connects once without the pool first, since a pool that can't connect only says it timed out.
      - The pool checks each connection as it hands it out, so after the database restarts, a dropped connection is replaced instead of failing a poll. `open` makes the tables under an advisory lock: two stores opening at once raced on `CREATE TABLE IF NOT EXISTS` without it. Both are tested.
      - **Two pollers on one store** (a page and `netgpt-mcp`, say): since 3c, each works out its intervals from its own previous reading, so rates are right, and so are averages over a window (both measure the same seconds). But errors, discards, and flaps are counted twice, and a chart read poll by poll has both pollers' points. Run one poller per store until samples carry which poller took them.
      - Checked live: a poller ran against the WAN lab and stopped, and a new one, before it had polled, read the old one's rates and drew a chart from them.
      - The container is started by hand. A restart policy needs `podman-restart.service`, which would restart every rootless container on the machine, not just this one.
    - 3c is done: rollups, and `interface_traffic` and `chart` reading long windows from them.
      - `Store.add` works out each port's rate since its previous sample and hands both to the store (`_keep`). `TimescaleStore` keeps the rates in a hypertable of their own, `interface_rates`. A store that has just opened finds each port's latest sample in the database (`_recall`), so after a short restart the first interval spans the gap. After a gap of more than three polls (`LONGEST_GAP`), it isn't measured.
      - The rollups are continuous aggregates read in real time (`materialized_only = false`): `interface_rates_5m` from the rates, and `interface_rates_1h` from the 5-minute one. Each step keeps the seconds measured, the bits, the busiest interval, and the counts, so rolling rollups up again is exact. A 15-minute, 6-hour, or 2-day step is rolled up at read time from the longest rollup it is a whole number of.
      - Kept: samples and rates 7 days, 5-minute rollups 90 days, hourly 2 years (`ROLLUPS`).
      - A refresh recomputes its rollup from the source, and one that reached past the source's retention would empty steps whose intervals are gone. So the refreshes reach back half the raw window, and a third of the 5-minute rollups' 90 days. A TimescaleDB store keeps at least an hour.
      - `step_for`: poll by poll up to 400 points (3 hours 20 minutes at 30 s), then the shortest of 5, 15, and 30 minutes, 1, 3, 6, and 12 hours, and 1 and 2 days that keeps to 400. A window read in steps starts at the first step that begins inside it, up to one step late.
      - `interface_traffic` has the window's average now. The latest rate is still the last interval between polls, and the peak still the busiest single interval, however long the window. `chart` says its step (`Chart.step`), and counts errors per step. The tool descriptions give the reach (2 years with TimescaleDB, an hour in memory).
      - Checked live: a poller on the WAN lab wrote to a scratch schema. TimescaleDB's rollups at 5, 15, and 60 minutes matched `roll_up` of the same intervals on every port, to float noise. `interface_traffic` for a day, read from rollups, matched 15 minutes read poll by poll on all 24 ports. A second poller's first interval started from the first one's last sample. Charts of a synthetic week were checked in headless Firefox, in both modes.
      - **Averages hide bursts on a chart.** A 10-minute burst to 50% is a small bump in a week's 30-minute averages, though `interface_traffic`'s peak keeps it. Charts don't draw peaks yet.
      - History starts when the store first runs with 3c: samples kept before it have no rates or rollups.
    - 3d: snapshots of the tables the tools read (OSPF neighbors, LLDP, routes), kept when they change, so "what changed since" is answerable, OSPF state over time with it, and Phase 4's events have something to come from.
  - Phase 4, events: errors climbing, a link saturated or down, shown on the page and told to the agent.
  - Later: BGP state, and gNMI streaming in place of polling.
- **Frontends track:**
  - Phase 1: `netgpt-mcp` over HTTP, behind a token.
  - Phase 2: one login for the page and the endpoint.
  - Phase 3: the map inside MCP clients (MCP Apps).
- **Contract track** (NetGPT as a project across repositories: this one holds the engine, its faces, and the labs, and owns the schemas the others read):
  - Repositories split along a contract that holds still, and the parts that change together stay together.
    - **xspace** (a WebXR walk through the network: React, three.js, react-three-fiber) is a repository of its own, and stays one: another stack, another runtime, and it has never changed alongside NetGPT. Its protobuf stubs are mostly empty, so there is no contract between them yet.
    - **The page stays here.** Of the 9 commits that changed `static/` from the rewrite to v0.4.0, 7 changed the tools or the engine too: one computation, given to the model as text and to the page as the view. It moves out only when it needs a build step, something other than NetGPT's server has to serve it, or it talks to more than one backend.
    - **Fine-tuning** (the model lever) gets a repository when it starts: another toolchain, and it reads the game's transcripts as data, not code. The game stays here, since each round is marked with NetGPT's commit.
  - Phase 1: the contract. JSON Schema for every record NetGPT sends outside itself (views, charts, traffic, and the primitives the tools return), written from the pydantic models, checked in, versioned with the package, and kept equal to the code by a test.
  - Phase 2: a read API: views and traffic without a conversation, behind the page's token. The page's map reads it too.
  - Phase 3: xspace draws the lab from that API, with types generated from the schema in place of its protobuf stubs.
- **Known gaps:**
  - `topology` and the forwarding walk are IPv4 only. Junos's interfaces and routes include IPv6; SR Linux's don't yet.
  - `bgp_sessions` is Junos only so far, the OSPF tools SR Linux only, and `ping` isn't on Junos yet.
  - A path and its return path are separate views.
  - A sweep from the NetGPT host can't tell "unreachable" from "empty".
