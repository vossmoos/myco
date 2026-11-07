"""etchmem connector: company memory, permissioned by WHO WROTE IT.

Every signal in etchmem has one writer: the agent that deposited it through Myco, an
import job, or a source system. Labels are ``<source>:<writer>:<operation>``:

    memory:mossy:read        read what the agent mossy wrote
    memory:hubspot:read      read what came from HubSpot
    memory:mossy:create      write as mossy (an agent can only ever write as itself)
    memory:*:read            read everything in memory

A reading call is labelled with every writer the caller may read (Myco fills that in and
hides it), and etchmem returns only beliefs ALL of whose writers are among them, so
knowledge built from a source the caller may not read never reaches it. ``remember`` is
labelled with the calling agent and stamps it as the writer. Scopes are not permissions:
they are a free topic filter (a skill's focus), on reads and writes alike.

Settings (``MYCO_SOURCE__<NAME>__<SETTING>``):

    BASE_URL       etchmem server root, e.g. http://etchmem:8000 (the /v1 API is used)
    API_KEY        etchmem's ETCHMEM_API_KEY, if it requires one
    WRITERS        every writer whose memory may be read through Myco, comma-separated,
                   e.g. mossy,support_bot,hubspot,zendesk. Each becomes a label entity, so
                   it must be a lowercase name; map a writer spelled differently in etchmem
                   with name=writer, e.g. support_bot=support-bot. Memory from a writer not
                   listed here is never shown through Myco.
    DEFAULT_SCOPE  the scope ``remember`` uses when the agent names none (default general)
    TIMEOUT        seconds, default 30

``forget`` erases across all writers and scopes and has its own label,
``<source>:subject:delete``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from myco_mcp.connector import Connector, PackError, Setting, agent_label_value, current_call, tool
from myco_mcp.http import HttpClient, UpstreamError
from myco_mcp.labels import NAME_RE

Query = Annotated[str, Field(min_length=1, max_length=2000, description="What to look for, in plain words")]
Entity = Annotated[str, Field(min_length=1, max_length=300, description="Entity id or name, e.g. Acme or MSM-0808")]
EtchId = Annotated[str, Field(min_length=3, max_length=500, description="Belief id: <entity_id>::<property>")]
TopK = Annotated[int, Field(ge=1, le=20, description="Maximum number of results")]
Limit = Annotated[int, Field(ge=1, le=200, description="Page size")]
Offset = Annotated[int, Field(ge=0, description="Skip this many results")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
AsOf = Annotated[str, Field(description="ISO-8601 time: the beliefs as they stood then")]
AsOfBasis = Annotated[
    Literal["event", "ingest"],
    Field(
        description="How `as_of` reads the past. 'event' (default): what was TRUE then, by the "
        "dates of the facts ('who managed Acme on 1 November'). 'ingest': what memory had "
        "RECORDED by then ('what did we know on 1 November'). They differ when records arrive late."
    ),
]
Text = Annotated[str, Field(min_length=1, max_length=20000, description="What happened, in plain text")]
RecordId = Annotated[str, Field(min_length=1, max_length=300)]
SignalId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

SCOPES_HELP = "Topics to read, e.g. the scope of your current skill; omit for all."
WRITERS_HELP = "Set by Myco: the writers whose memory the caller may read."
Writers = Annotated[list[str] | None, Field(description=WRITERS_HELP)]
Scopes = Annotated[list[str] | None, Field(description=SCOPES_HELP)]


Quotes = Annotated[
    Literal["ref", "full"],
    Field(
        description="'full' (default): each belief's quote names who said it, the source record "
        "and when, with an excerpt of the original text. 'ref': the excerpt only for beliefs "
        "without a wording, for shorter replies when reading many beliefs."
    ),
]


class EtchmemConnector(Connector):
    type = "etchmem"
    settings = {
        "base_url": Setting(help="etchmem server root, e.g. http://etchmem:8000"),
        "api_key": Setting(required=False, secret=True, help="etchmem's ETCHMEM_API_KEY"),
        "writers": Setting(help="comma-separated writers, e.g. mossy,hubspot or support_bot=support-bot"),
        "default_scope": Setting(required=False, default="general"),
        "timeout": Setting(required=False, default="30"),
    }

    def __init__(self, source: str, config: dict[str, str], *, transport: Any = None) -> None:
        super().__init__(source, config)
        self.writers = _parse_writers(source, config["writers"])
        self.default_scope = config.get("default_scope") or "general"
        headers = {"Authorization": f"Bearer {config['api_key']}"} if config.get("api_key") else {}
        self.http = HttpClient(
            config["base_url"].rstrip("/") + "/v1",
            headers=headers,
            timeout=float(config.get("timeout", 30)),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def label_values(self, argument: str) -> tuple[str, ...]:
        if argument == "writers":
            return tuple(self.writers)
        return super().label_values(argument)

    # ------------------------------------------------------------------ reads
    @tool
    async def recall(
        self,
        query: Query,
        scope: Scopes = None,
        top_k: TopK = 5,
        as_of: AsOf | None = None,
        as_of_basis: AsOfBasis = "event",
        include_signals: Annotated[
            bool, Field(description="Also return fresh raw signals not consolidated yet")
        ] = False,
        quotes: Quotes = "full",
        writers: Writers = None,
    ) -> list[dict[str, Any]]:
        """Recall consolidated beliefs by meaning: each with its value, confidence, status
        (settled or contested), scope, its wording (`details`) and a `quote`: who said it,
        in which source record, when, and an excerpt of the original text. `as_of` gives the
        beliefs as of that time (see `as_of_basis`)."""
        body = {
            "query": query,
            "scope": scope or None,
            "writers": self._real(writers),
            "top_k": top_k,
            "as_of": as_of,
            "as_of_basis": as_of_basis,
            "include_signals": include_signals,
            "quotes": quotes,
        }
        reply = await self.http.post("/recall", json_body=body)
        return list(reply.get("results") or [])

    @tool
    async def know(
        self,
        entity: Entity,
        scope: Scopes = None,
        as_of: AsOf | None = None,
        as_of_basis: AsOfBasis = "event",
        entity_type: Annotated[str | None, Field(description="e.g. company, person, product")] = None,
        quotes: Quotes = "full",
        writers: Writers = None,
    ) -> dict[str, Any]:
        """Everything believed about one subject — exhaustive, not ranked, each belief with
        its wording and a `quote` (who said it, which source record, when, an excerpt). Use it for "what
        do we know about X"; `recall` ranks by wording and can miss facts."""
        params = {
            "scope": scope or None,
            "writers": self._real(writers),
            "as_of": as_of,
            "as_of_basis": as_of_basis,
            "entity_type": entity_type,
            "quotes": quotes,
        }
        try:
            return await self.http.get(f"/entity/{_path(entity)}/etches", params=params)
        except UpstreamError as exc:
            if exc.status == 404:
                return {"entity": None, "etches": [], "count": 0, "note": f"nothing known about {entity!r}"}
            raise

    @tool
    async def list_beliefs(
        self,
        scope: Scopes = None,
        property: Annotated[list[str] | None, Field(description="Only these properties")] = None,
        entity_type: Annotated[list[str] | None, Field(description="Only subjects of these types")] = None,
        status: Literal["settled", "contested"] | None = None,
        min_confidence: Confidence | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
        writers: Writers = None,
    ) -> dict[str, Any]:
        """Every belief matching the filters, newest first — exhaustive, not ranked.
        `total` counts all matches; page with `offset`."""
        params = {
            "scope": scope or None,
            "writers": self._real(writers),
            "property": property or None,
            "entity_type": entity_type or None,
            "status": status,
            "min_confidence": min_confidence,
            "limit": limit,
            "offset": offset,
        }
        return await self.http.get("/etches", params=params)

    @tool
    async def history(self, etch_id: EtchId, writers: Writers = None) -> dict[str, Any]:
        """How one belief changed: every version with its value, confidence and time."""
        dossier = await self._dossier(etch_id, writers)
        etch = dossier.get("etch") or {}
        return {"etch_id": etch_id, "scope": etch.get("scope"), "versions": dossier.get("versions") or []}

    @tool
    async def dossier(self, etch_id: EtchId, writers: Writers = None) -> dict[str, Any]:
        """Why the memory believes something: the belief, its versions, the claims behind
        it and the raw signals they came from."""
        return await self._dossier(etch_id, writers)

    # ------------------------------------------------------------------ writes
    @tool
    async def remember(
        self,
        data: Text,
        scope: Annotated[
            str | None, Field(max_length=100, description="The topic it belongs to, e.g. your skill's scope")
        ] = None,
        occurred_at: Annotated[
            str | None, Field(description="When it happened: ISO-8601 date or time; default now")
        ] = None,
        context: Annotated[str | None, Field(max_length=300, description="Thread or conversation id")] = None,
        source_record_id: Annotated[
            RecordId | None, Field(description="The source record, e.g. zendesk:ticket:123")
        ] = None,
        urgent: Annotated[bool, Field(description="Consolidate on the next tick instead of batching")] = False,
        writer: Annotated[str | None, Field(description="Set by Myco: the calling agent")] = None,
    ) -> dict[str, Any]:
        """Deposit something worth remembering: a decision, a fact learned, an outcome.
        It is consolidated into beliefs in the background."""
        call = current_call()
        if call is None:
            raise ValueError("remember needs a calling agent")
        agent = call.agent
        if writer != agent_label_value(agent):  # Myco fills it; anything else is a bug, not a request
            raise ValueError("an agent can only write as itself")
        body = {
            "data": data,
            "source": self.writers.get(writer, agent),
            "scope": scope or self.default_scope,
            "extract_mode": "immediate" if urgent else "deferred",
            "occurred_at": occurred_at,
            "context": context,
            "source_record_id": source_record_id,
            "actor": {"agent": agent, "user": call.user, "via": f"myco:{self.source}"},
        }
        reply = await self.http.post("/remember", json_body=body, expect=(200, 201, 202))
        return {k: reply.get(k) for k in ("id", "stored", "status", "message")}

    # ------------------------------------------------------------------ witness (not a tool)
    async def witness_deposit(
        self,
        *,
        writer: str,
        user: str,
        tool: str,
        text: str,
        scope: str,
        record_id: str | None,
        occurred_at: Any = None,
    ) -> dict[str, Any]:
        """Deposit what a tool of another source showed an agent (Myco's WITNESS setting).

        Not an agent tool: Myco calls it after a witnessed call. The writer is the agent
        that was shown the reply, so it lands in that agent's memory."""
        body = {
            "data": text,
            "source": self.writers.get(agent_label_value(writer), writer),
            "scope": scope or self.default_scope,
            "extract_mode": "deferred",
            "occurred_at": occurred_at if isinstance(occurred_at, (str, int, float)) else None,
            "source_record_id": record_id,
            "actor": {"agent": writer, "user": user, "via": f"witness:{tool}"},
        }
        reply = await self.http.post("/remember", json_body=body, expect=(200, 201, 202))
        return {k: reply.get(k) for k in ("id", "stored", "status")}

    @tool
    async def forget(
        self,
        entity: Annotated[Entity | None, Field(description="A subject to erase in every scope")] = None,
        source_record_ids: Annotated[
            list[RecordId] | None, Field(description="Erase what came from these source records")
        ] = None,
        signal_ids: Annotated[list[SignalId] | None, Field(description="Erase these signals")] = None,
        reason: Annotated[
            str | None, Field(max_length=300, description="e.g. 'GDPR Art. 17 request #123'; no personal data")
        ] = None,
    ) -> dict[str, Any]:
        """Erase personal data from memory in every scope: the subject's beliefs, the
        claims about it and every raw signal that names it. Irreversible. Returns the
        erasure report."""
        if not (entity or source_record_ids or signal_ids):
            raise ValueError("give entity, source_record_ids or signal_ids")
        call = current_call()
        body = {
            "entity": entity,
            "source_record_ids": source_record_ids or [],
            "signal_ids": signal_ids or [],
            "reason": reason or (f"requested via myco by {call.agent}" if call else None),
        }
        return await self.http.post("/forget", json_body=body)

    # ------------------------------------------------------------------ helpers
    def _real(self, writers: list[str] | None) -> list[str]:
        """Writer labels -> etchmem source names. Myco always fills them in."""
        if not writers:
            raise ValueError("no memory you may read")
        return [self.writers[w] for w in writers if w in self.writers]

    async def _dossier(self, etch_id: str, writers: list[str] | None) -> dict[str, Any]:
        try:
            return await self.http.get(f"/etch/{_path(etch_id)}/dossier", params={"writers": self._real(writers)})
        except UpstreamError as exc:
            if exc.status == 404:
                # The same answer as "does not exist": the caller learns nothing it may not read.
                raise ValueError(f"no belief {etch_id!r}") from None
            raise


def _parse_writers(source: str, raw: str) -> dict[str, str]:
    writers: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, real = item.partition("=")
        name, real = name.strip(), (real.strip() if sep else name.strip())
        if not NAME_RE.match(name):
            raise PackError(
                f"source {source!r}: writer {name!r} cannot be a label entity; use a lowercase name "
                f"and map it, e.g. {agent_label_value(name)}={name}"
            )
        if not real:
            raise PackError(f"source {source!r}: writer {name!r} maps to an empty name")
        writers[name] = real
    if not writers:
        raise PackError(f"source {source!r}: WRITERS lists no writer")
    return writers


def _path(segment: str) -> str:
    from urllib.parse import quote

    return quote(segment, safe=":")
