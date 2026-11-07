---
name: new-connector
description: Generate a Myco connector pack (<type>.py + <type>.yaml + tests) from a REST API's OpenAPI spec, docs or an upstream MCP server's tool list, or update an existing pack when the vendor API changed. Use when adding a new source to Myco, or when a pack's recorded fixtures no longer match what the API returns.
---

# Generate or update a Myco connector

A connector is the **only** thing that decides what an agent can do with a vendor system,
so treat its output as code that grants access: small, typed, labelled, tested and reviewed.
You (the coding agent) generate it once on a developer machine; a person reviews the pull
request; Myco then runs it unchanged. When the vendor API changes, run this skill again in
**update mode**.

Read first, in this repository:

- `README.md` → *Concepts* (sources, packs, labels, taints) and *Example: create a connector*.
- `src/myco_mcp/connector.py` — the pack format and what Myco validates at startup.
- One existing pack as the model: `src/myco_mcp/packs/zendesk.py` + `zendesk.yaml`
  (REST, field taints), or `packs/etchmem.py` + `.yaml` (labels filled in by Myco).
- `tests/test_connectors.py` — how connectors are tested with `respx`.

## Inputs to ask for (only what is missing)

1. **The source:** an OpenAPI/Swagger file or URL, the API docs, or an MCP server's tool
   list (`tools/list` output). For an existing pack: its name.
2. **Use cases:** what agents should do with it, in a few sentences (e.g. "support agents
   read tickets and add internal notes; nobody deletes"). This decides which tools exist.
   Without it, generate **read-only** tools only.
3. **Sample responses:** two or three real replies per tool you will build, from a sandbox
   account. If none are available, derive them from the spec's examples and say so in the
   pull request.
4. **Where it goes:** a plugin (`examples/connectors/` or the customer's own plugin repo,
   loaded with `MYCO_PLUGINS`) or a built-in pack (`src/myco_mcp/packs/` +
   `BUILTIN_MODULES` in `catalog.py`). Default: plugin.

Never ask for, store or print real credentials. Settings are read from
`MYCO_SOURCE__<NAME>__<SETTING>` at runtime; tests use fake values.

## Steps — new connector

### 1. Choose the tools (curation)

From the use cases, pick the **smallest** set of operations. For each, write one line:
`tool_name — entity:operation — why an agent needs it`. Typical first pack: 4–10 tools.

- Operations are a fixed set: `read`, `list`, `create`, `update`, `delete`, `send`,
  `export`, `admin`. `send` is anything that reaches a person outside the company (e-mail,
  public reply, message). `admin` is settings, users, permissions.
- One tool = one vendor action = one label. Never a generic `call_api`, raw path, HTTP
  method, query-language or URL passthrough: those cannot be labelled.
- Leave out `delete`, `send` and `admin` unless the use cases name them.
- Prefer one `get_<entity>` and one `list_<entity>`/`search_<entity>` with typed filters
  over many narrow variants.

### 2. Write `<type>.py`

Follow `zendesk.py`:

- `class <Name>Connector(Connector)` with `type = "<type>"` (lowercase, `[a-z][a-z0-9_]*`)
  and `settings = {...}` using `Setting(secret=True)` for keys and tokens. Document every
  setting in the module docstring.
- One `HttpClient` created in `__init__` (accept `transport=None` for tests), closed in
  `aclose()`. Auth headers are built here, never passed by the agent.
- Each tool is an `async` method marked `@tool`, with a one-sentence docstring the agent
  will read (what it returns; side effects stated plainly, e.g. "This e-mails the customer").
- **Safe shapes:** typed parameters only. `Literal[...]` for enums, `Annotated[int,
  Field(ge=1, le=100)]` for limits, `pattern=` for ids and codes, required fields where the
  API requires them. Translate vendor codes to names (`STATUS_IDS`/`STATUS_NAMES`).
- **Validate free text that ends up in a query** (tags, search terms) with a strict regex;
  raise `ValueError` with a readable message on bad input.
- **Return plain JSON with an explicit allow-list of keys** (`_ticket_summary`-style
  helpers). Never return the raw vendor payload: unknown fields would reach agents
  unreviewed, and a vendor adding a field must not silently widen what agents see.
  List tools return a list of row dicts so `scope` can filter rows.
- Bound everything: page counts, result counts, timeouts.

**Upstream MCP server instead of REST:** same rules. Each curated `@tool` calls exactly one
upstream tool through `fastmcp.Client` (one client per connector, opened lazily, closed in
`aclose()`), with typed parameters and an allow-listed reply. Do not re-publish the
upstream tool list wholesale.

### 3. Write `<type>.yaml` (the pack)

```yaml
pack: <type>
version: "1"
description: One line on what the source holds.
tools:
  list_things: thing:list
  get_thing:
    action: thing:read
    taint_fields:
      pii: [owner.email, owner.phone]
  get_person: { action: person:read, taints: [pii] }
  update_thing: thing:update
roles:
  reader: [thing:read, thing:list, person:read]
  contributor: [thing:read, thing:list, person:read, thing:update]
```

- Exactly one entry per `@tool` method, and nothing else; Myco refuses to start otherwise.
- **Taints:** mark every reply field holding personal data (names of private persons,
  e-mails, phones, addresses) under `taint_fields: { pii: [...] }`, prices/margins/salaries
  under `finance`. A tool whose whole purpose is personal data gets `taints: [pii]`.
  When unsure, taint: an agent can be given a toleration, an unredacted leak cannot be undone.
- **Record ids:** give every tool that returns ONE record, and every write, a `record:`
  entry naming the record and its change time, e.g.
  `get_ticket: { action: ticket:read, record: { id: "ticket:{id}", time: updated_at } }`.
  It lets an installation witness those replies into memory (`WITNESS` setting). Search
  and list tools get none.
- Templated `{argument}:operation` labels only when one argument selects a partition that
  deserves its own permission (see the etchmem pack). Separate accounts or credentials
  are better served by separate source instances of the same pack.
- **Roles:** at least `reader` (read/list only). Add write roles only for the use cases.
  Never a role that includes `delete`, `send` or `admin` by accident through a wildcard.

### 4. Record fixtures and write tests

- Put sanitized sample replies in `tests/fixtures/<type>/<tool>.json`. **Replace every real
  name, e-mail, phone, address and id with synthetic values** before saving.
- `tests/test_<type>.py`, with `respx`, at minimum:
  - the pack loads and matches the connector (`<Connector>.load_pack()`);
  - auth headers are built from settings;
  - each tool calls the expected method/path/params and returns only the allow-listed keys
    (assert an unexpected vendor field in the fixture is **not** in the output);
  - input validation rejects bad enums, ids and free text;
  - an upstream error surfaces as a readable `UpstreamError`;
  - the **fixture shape test**: the allow-listed keys the code reads still exist in each
    fixture (this is what update mode compares against later).
- For a built-in pack, add the type to `test_example_packs_match_their_connectors` in
  `tests/test_packs.py`.

### 5. Document and verify

- Add a section under *Example packs* in `README.md` (built-in) or a README next to the
  plugin: settings, tools with labels, roles, taints.
- Run `.venv/bin/pytest` and `.venv/bin/ruff check . && .venv/bin/ruff format --check .`.
- Start Myco with fake settings (`MYCO_SOURCE__<NAME>__TYPE=<type>`, …) and confirm it boots:
  startup validation is the final check that pack and code agree.

### 6. Pull request

Write the description for the reviewer, who is approving **access**, not just code:

- a table: tool → label → what it can change → who gets it (roles);
- every taint and why; every field deliberately **not** returned;
- what was left out of the API and why;
- whether fixtures came from a real sandbox or from the spec.

## Steps — update mode (the vendor API changed)

Run when a fixture-shape test fails in CI, the vendor announces a change, or Myco reports
an upstream error pattern after a vendor release.

1. Get fresh sample replies for the pack's tools (sandbox), sanitize them as in step 4.
2. **Diff** them against `tests/fixtures/<type>/`: fields added, removed, renamed, changed
   type; endpoints moved or deprecated (compare the new spec with the old one if available).
3. Fix what broke: paths, parameters, renamed fields in the allow-lists, new enum values.
   Keep tool names and labels stable: agents' configs and roles refer to them.
4. **New fields are not exposed automatically.** List them in the pull request; add one to
   an allow-list only if a use case needs it, and taint it if it holds personal or financial
   data.
5. **Never widen access silently.** Any new tool, new write/`send`/`delete`/`admin`
   operation, new field returned, removed taint or changed role gets its own line in the PR
   under a heading **Access changes**, so the reviewer sees it. If there are none, say
   "Access changes: none".
6. Update the fixtures, bump `version` in the pack, run the tests and the startup check.

## Never

- Generate a passthrough tool (raw URL, method, query, GraphQL, SQL, upstream tool name).
- Return a vendor payload without an allow-list.
- Put real credentials, personal data or customer content in code, fixtures, tests or the PR.
- Label a tool with a weaker operation than what it does (an update labelled `read`, an
  e-mail-sending action labelled `create`). A mislabelled tool is a security bug.
- Remove a taint or add a field to a reply without listing it under **Access changes**.
