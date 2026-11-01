"""
The api module is the engine's face for programs: GET routes under /api/v1/ that
answer with the contract's records (netgpt.contract), for a frontend in another
repository, or for the page's own map.

Each route reads through the engine, and devices' answers are shared: one is
reused for up to max_age seconds, requests that arrive together wait for one read,
and the Age header says how old the oldest answer behind a response is. A request
with Cache-Control: no-cache reads every device again. The MCP tools always read
live.

Every request needs the token: as a bearer token (Authorization: Bearer ...) from a
program, or the page's cookie from the browser. There is no CORS: a frontend's
browser app reaches the API through its own backend, which holds the token. A
request the engine won't answer gets a Refusal that says why, in the tools' words:
400 for the request, 401 without the token, 404 for a route that isn't one (traffic
and chart are routes only while the engine polls), and 503 when the store fails. v1
is the API's own version: new records and fields stay in it, and renaming or
removing one would make a v2.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from netgpt.engine import Engine, RequestError, SharedReads, Table
from netgpt.forwarding import TraceError
from netgpt.guard import GuardError
from netgpt.inventory import InventoryError
from netgpt.primitives import Collection, DeviceInfo
from netgpt.statistics import MAX_CHART_PORTS, ChartError, Metric, Traffic
from netgpt.store import StoreError
from netgpt.topology import Layer, PlaneChoice
from netgpt.views import ViewError

# The page's sign-in cookie, which the API accepts as well as a bearer token.
COOKIE = "netgpt_token"
# How long a device's answer is shared when the engine doesn't poll; otherwise, one poll interval.
DEFAULT_MAX_AGE = 30.0

HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}

# What the engine refuses, by the status that says so. Anything else is a bug, and fails loudly.
REFUSED = (RequestError, InventoryError, GuardError, TraceError, ViewError, ChartError)
UNAVAILABLE = (StoreError,)


class Refusal(BaseModel):
    """What the API answers instead of a record: why it refused, in the tools' words, and what it would answer."""

    error: str


class Query(BaseModel):
    """A route's query parameters. A parameter the route doesn't take is refused, so a misspelled one isn't ignored."""

    model_config = ConfigDict(extra="forbid")


class TopologyQuery(Query):
    plane: PlaneChoice = "data"
    layer: Layer = "l2"
    network_instance: str = "default"


class PathQuery(Query):
    source: str
    destination: str
    network_instance: str = "default"


class ViewQuery(TopologyQuery):
    title: str = "The network"
    focus: list[str] = Field(default_factory=list)
    highlight: list[str] = Field(default_factory=list)
    source: str | None = None
    destination: str | None = None

    @model_validator(mode="after")
    def both_ends(self) -> ViewQuery:
        if (self.source is None) != (self.destination is None):
            raise ValueError("A path needs both a source and a destination.")
        return self


class TableQuery(Query):
    device: list[str] = Field(default_factory=list)


class RoutesQuery(TableQuery):
    network_instance: str = "default"


class TrafficQuery(Query):
    device: list[str] = Field(default_factory=list)
    minutes: float = Field(5.0, gt=0)


class ChartQuery(Query):
    port: list[str] = Field(min_length=1, max_length=MAX_CHART_PORTS)
    metric: Metric = "traffic"
    minutes: float = Field(15.0, gt=0)
    title: str | None = None


# The tables, by the name of the tool that reads each.
TABLES: dict[str, Table] = {
    "lldp_neighbors": "lldp_neighbors", "interfaces": "interfaces", "mac_table": "mac_table", "arp_table": "arp_table",
    "routes": "routes", "bgp_sessions": "bgp_sessions", "ospf_neighbors": "ospf_neighbors", "ospf_interfaces": "ospf_interfaces",
}

Handler = Callable[[Request, Engine], Awaitable[BaseModel | dict[str, Any]]]


def parse(model: type[Query], request: Request) -> Any:
    """A route's query parameters, as its model takes them: a list parameter is given once for each value."""
    lists = {name for name, field in model.model_fields.items() if get_origin(field.annotation) is list}
    params = request.query_params
    given = {key: params.getlist(key) if key in lists else params[key] for key in params}
    try:
        return model.model_validate(given)
    except ValidationError as error:
        takes = ", ".join(model.model_fields) or "no parameters"
        problems = []
        for problem in error.errors():
            where = ".".join(str(part) for part in problem["loc"])
            if problem["type"] == "extra_forbidden":
                problems.append(f"{where} isn't a parameter here; the route takes {takes}.")
            else:
                problems.append(f"{where}: {problem['msg']}." if where else f"{problem['msg'].removeprefix('Value error, ')}")
        raise RequestError(" ".join(problems)) from None


def holds_token(request: Request, expected: bytes) -> bool:
    """Whether the request carries the token: as a bearer token, or as the page's cookie."""
    scheme, _, offered = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        return secrets.compare_digest(offered.strip().encode(), expected)
    return secrets.compare_digest(request.cookies.get(COOKIE, "").encode(), expected)


def refusal(message: str, status: int) -> JSONResponse:
    return JSONResponse(Refusal(error=message).model_dump(mode="json"), status_code=status, headers=HEADERS)


def build_api(engine: Engine, token: str, shared: SharedReads | None = None) -> Starlette:
    """
    Build the API on an engine, for requests holding the token. It shares devices'
    answers through shared: by default, for one poll interval. Mount it at /api/v1,
    or serve it alone.
    """
    if shared is None:
        shared = SharedReads(engine.statistics.interval if engine.statistics else DEFAULT_MAX_AGE)
    # Imported here, since the contract names this module's Refusal.
    from netgpt.contract import schema as written

    expected = token.encode()
    schema = written()

    def route(path: str, handler: Handler) -> Route:
        async def answer(request: Request) -> Response:
            if not holds_token(request, expected):
                return refusal("Send the token: Authorization: Bearer <token>.", 401)
            live = "no-cache" in request.headers.get("cache-control", "").lower()
            asking = engine if live else engine.reading(shared)
            try:
                found = await handler(request, asking)
            except REFUSED as error:
                return refusal(str(error), 400)
            except UNAVAILABLE as error:
                return refusal(str(error), 503)
            body = found.model_dump(mode="json") if isinstance(found, BaseModel) else found
            age = 0 if asking.oldest is None else max(0, math.floor(shared.clock() - asking.oldest))
            return JSONResponse(body, headers=HEADERS | {"Age": str(age)})

        return Route(path, answer)

    async def devices(request: Request, engine: Engine) -> BaseModel:
        parse(Query, request)
        return Collection[DeviceInfo](items=engine.devices())

    async def topology(request: Request, engine: Engine) -> BaseModel:
        asked = parse(TopologyQuery, request)
        return await engine.topology(asked.plane, asked.layer, asked.network_instance)

    async def path(request: Request, engine: Engine) -> BaseModel:
        asked = parse(PathQuery, request)
        return await engine.forwarding_path(asked.source, asked.destination, asked.network_instance)

    async def view(request: Request, engine: Engine) -> BaseModel:
        asked = parse(ViewQuery, request)
        ends = (asked.source, asked.destination) if asked.source and asked.destination else None
        return await engine.view(
            asked.title, asked.focus, asked.highlight, None, asked.plane, asked.layer, asked.network_instance, ends
        )

    async def table(request: Request, engine: Engine) -> BaseModel:
        name = request.path_params["table"]
        if name not in TABLES:
            raise RequestError(f"There's no table called {name!r}. The tables are {', '.join(TABLES)}.")
        if name == "routes":
            asked = parse(RoutesQuery, request)
            return await engine.routes(asked.device or None, asked.network_instance)
        asked = parse(TableQuery, request)
        return await getattr(engine, name)(asked.device or None)

    async def traffic(request: Request, engine: Engine) -> BaseModel:
        asked = parse(TrafficQuery, request)
        found = await engine.traffic(asked.device or None, asked.minutes)
        return Traffic(interval=engine.poller().interval, minutes=asked.minutes, ports=found.items, errors=found.errors)

    async def chart(request: Request, engine: Engine) -> BaseModel:
        asked = parse(ChartQuery, request)
        return await engine.chart(asked.title or ", ".join(asked.port), asked.port, asked.metric, asked.minutes)

    async def contract_schema(request: Request, engine: Engine) -> dict[str, Any]:
        parse(Query, request)
        return schema

    routes = [route("/devices", devices), route("/topology", topology), route("/path", path), route("/view", view)]
    routes.append(route("/tables/{table}", table))
    # Traffic and charts are the poller's, so an engine that doesn't poll has none, as its MCP server has no tools for them.
    if engine.statistics is not None:
        routes += [route("/traffic", traffic), route("/chart", chart)]
    routes.append(route("/schema", contract_schema))
    paths = ", ".join(route.path.lstrip("/") for route in routes)

    async def not_a_route(request: Request, error: Exception) -> Response:
        if isinstance(error, HTTPException) and error.status_code == 405:
            return refusal(f"{request.method} isn't served here: every route answers GET.", 405)
        return refusal(f"There's no route {request.url.path}. The routes are {paths}.", 404)

    return Starlette(routes=routes, exception_handlers={404: not_a_route, 405: not_a_route})
