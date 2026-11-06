# Myco

**One MCP endpoint that gives each AI agent a curated set of SaaS tools, with every permission kept as YAML in Git.**

Myco gives AI agents cloud-style IAM for the SaaS apps your cloud IAM can't reach. Each agent gets one endpoint, one key and a reviewed tool set, and sees only the tools and data its roles allow. Myco holds the vendor credentials; agents never do.

- **Curated tools, not a proxy.** Each source ships a small, reviewed tool set with constrained arguments. There is no "call any endpoint" tool.
- **One label per tool:** `source:entity:operation`, for example `zendesk:ticket:read`.
- **Roles per source**, such as `zendesk:reader`, combined per agent. Explicit deny always wins.
- **Users:** effective access is the agent's roles ∩ the user's roles. Without a user token, an agent runs as its named service user, or as the built-in `anonymous` user, which has no access by default.
- **Data scoping:** replies are filtered by row (`company_id == {user.org}`) and by field (taints such as `pii`, plus redaction rules).
- **Audit log:** every decision is a JSON line naming the agent, user, tool, label and the rule that applied.
- **Configuration is only environment variables plus a folder of YAML.** Myco has no UI, no CLI and no LLM; every decision is deterministic.

Built on [FastMCP](https://github.com/jlowin/fastmcp). License: MIT.

```
Agent ──(Myco key, optional user SSO token)──▶ Myco /mcp ──(vendor credentials)──▶ your SaaS apps
                                               1. identify agent + user
                                               2. map tool → label
                                               3. check roles, taints, denies
                                               4. scope rows, redact fields, audit
```

---

## Contents

- [Quickstart](#quickstart)
- [Connect an agent](#connect-an-agent)
- [Concepts](#concepts)
- [Example: create an agent with permissions](#example-create-an-agent-with-permissions)
- [Example: create a connector](#example-create-a-connector)
- [Example packs](#example-packs)
- [Configuration reference](#configuration-reference)
- [Deploy](#deploy) (Docker, Helm, Scaleway Kapsule)
- [Security notes](#security-notes)
- [Development](#development)

---

## Quickstart

```bash
git clone https://github.com/vossmoos/myco.git && cd myco
cp .env.example .env
```

Edit `.env`. Keep the sources you use and delete the blocks you don't: an empty required setting stops Myco at startup. Then set an agent key:

```dotenv
MYCO_SOURCE__JIRA__SITE=acme.atlassian.net
MYCO_SOURCE__JIRA__EMAIL=bot@acme.com
MYCO_SOURCE__JIRA__API_TOKEN=ATATT...
MYCO_AGENT__DEFAULT__KEY=<at least 16 random characters>
```

Generate a key with `python3 -c "import secrets; print('myco_' + secrets.token_urlsafe(32))"`.

Run it with Docker:

```bash
docker build -t myco .
docker run --rm -p 8080:8080 --env-file .env -v "$PWD/config:/config:ro" myco
```

Or with Python 3.12+:

```bash
pip install -e .
set -a; source .env; set +a
python -m myco_mcp
```

Check it:

```bash
curl localhost:8080/health
# {"status":"ok","version":"0.1.0","config":"3bcd9bd3777e"}
```

The example [`config/`](config) folder ships a **`default` agent** with every role of every example pack. Its key is `MYCO_AGENT__DEFAULT__KEY`; [`deploy.sh`](deploy.sh) generates one into `.env` when it is missing. Unattended, it runs as the service user `default-service`. The `anonymous` user has no access. Narrow both before real use.

## Connect an agent

The endpoint is `http://<host>:8080/mcp` (MCP streamable HTTP). The agent authenticates with its Myco key:

```
Authorization: Bearer <MYCO_AGENT__<NAME>__KEY>
X-Myco-User-Token: <the person's SSO token>        # optional, see "Users"
```

Any MCP client works. A typical `mcp.json`:

```json
{
  "mcpServers": {
    "myco": {
      "type": "http",
      "url": "https://myco.example.com/mcp",
      "headers": { "Authorization": "Bearer myco_..." }
    }
  }
}
```

From Python with the FastMCP client:

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main():
    transport = StreamableHttpTransport(
        "http://127.0.0.1:8080/mcp",
        headers={"Authorization": "Bearer myco_..."},
    )
    async with Client(transport) as myco:
        for tool in await myco.list_tools():
            print(tool.name)  # only the tools this key may use
        result = await myco.call_tool("jira_get_issue", {"issue_key": "OPS-42"})
        print(result.data)


asyncio.run(main())
```

## Concepts

### Sources and packs

A **source** is one connected app account, configured with environment variables:

```dotenv
MYCO_SOURCE__<NAME>__<SETTING>=value
```

The lowercased `NAME` becomes the source name, which is the first part of every tool, label and role it publishes. `TYPE` picks the **pack** and defaults to the name, so you can connect the same app twice:

```dotenv
MYCO_SOURCE__ZENDESK__DOMAIN=acme.zendesk.com      # source "zendesk", type zendesk
MYCO_SOURCE__ZENDESK__API_KEY=...
MYCO_SOURCE__SUPPORT_EU__TYPE=zendesk                 # source "support_eu", type zendesk
MYCO_SOURCE__SUPPORT_EU__DOMAIN=acme-eu.zendesk.com
MYCO_SOURCE__SUPPORT_EU__API_KEY=...
```

A **pack** is a connector (`<type>.py`) plus its label file (`<type>.yaml`). It defines the curated tools and the roles once; Myco adds the source name when it loads it:

```yaml
# src/myco_mcp/packs/zendesk.yaml (excerpt)
tools:
  get_ticket:
    action: ticket:read
    taint_fields: { pii: [requester.email, requester.phone] }
  create_ticket: ticket:create
  get_contact: { action: contact:read, taints: [pii] }
roles:
  reader: [ticket:read, ticket:list, contact:read]
  contributor: [ticket:read, ticket:list, contact:read, ticket:write]
```

The source `zendesk` therefore publishes the tool `zendesk_get_ticket` with label `zendesk:ticket:read`, and the roles `zendesk:reader` and `zendesk:contributor`. Myco refuses to start if a connector method has no label or a label has no method.

### Labels

`source:entity:operation`. Operations are a fixed set: `read`, `list`, `create`, `update`, `delete`, `send`, `export`, `admin`. In patterns (roles, deny, scope, redact):

| Pattern | Matches |
|---|---|
| `zendesk:ticket:read` | exactly that |
| `*:ticket:read` | ticket reads in every source (Zendesk, a second Zendesk…) |
| `*:*:delete` | every delete |
| `jira:issue:write` | `write` = `create` + `update` |
| `support:*:*` | every source in the `support` domain (see `domains.yaml`) |

Tool annotations come from the label: `read`/`list`/`export` are marked read-only; `delete`/`send`/`admin` are marked destructive.

### The config folder

```
config/
  agents/<agent>.yaml   one file per agent
  users.yaml            SSO users, service users and the built-in anonymous user
  domains.yaml          optional groups of sources
```

Keep it in Git and route reviews with CODEOWNERS: each agent file can belong to its team, and `users.yaml` to security. Myco validates the **whole folder** on every load. A broken folder is refused and the last good one keeps running (`config.rejected` in the audit log). Changes are picked up every `MYCO_RELOAD_SECONDS` (10s by default) without a restart, and connected clients receive `notifications/tools/list_changed`.

### Agents

```yaml
# config/agents/support-bot.yaml
agent: support-bot              # must match the file name
owner: anna@acme.com
roles: [zendesk:reader, jira:reader]
deny: ["*:*:delete", "*:*:send"]
tolerations: []                 # e.g. [pii]
scope:                          # keep only rows whose fields match (applies to replies)
  - match: "zendesk:ticket:*"
    where: { company_id: "{user.org}" }
redact:                         # replace fields with "[redacted]"
  - match: "zendesk:*:*"
    fields: [custom_fields]
```

The key comes from `MYCO_AGENT__SUPPORT_BOT__KEY` (hyphens become underscores). A key without an agent file connects but sees no tools; an agent file without a key cannot connect.

### Users, service users and `anonymous`

Every call has a user, and effective access is **agent roles ∩ user roles**. The user is one of the following:

1. **A person.** The agent forwards that person's SSO token in `X-Myco-User-Token`. Myco validates it against `MYCO_OIDC__ISSUER` / `MYCO_OIDC__AUDIENCE` and matches the claims against `match` entries. Every matching entry adds its roles.
2. **A service user.** An agent that calls without a token runs as the `users.yaml` entry that lists it in `service_for`. This is how unattended agents get access.
3. **`anonymous`.** Everything else. It has **no roles** in the example config, so unattended calls from agents without a service user are denied.

```yaml
# config/users.yaml
users:
  anonymous:
    roles: []                                   # keep it closed

  default-service:                              # unattended identity of the default agent
    service_for: [default]
    roles: [zendesk:responder, jira:contributor, wrike:contributor]

  vp:       { match: { groups: vp },        roles: [jira:contributor] }
  employee: { match: { groups: employees }, roles: [jira:reader, wrike:reader] }
```

- Service users live in `users.yaml`, not in the agent file. An agent team cannot grant its own agent unattended access; whoever owns `users.yaml` (security, via CODEOWNERS) decides.
- Service users are never matched by tokens. A signed-in person always gets their own roles, even when the agent has a service user.
- An agent can have at most one service user. One service user can serve several agents.
- A signed-in person who matches no entry gets no roles.
- A forwarded token that fails validation is refused. It is never silently downgraded to a service user or `anonymous`.
- Any entry may also carry `deny`, `scope` and `redact`.

In the audit log, the user shows as the token's e-mail or subject, `service:<entry>`, or `anonymous`.

### Taints, scope and redaction

- **Tool taints** (`taints: [pii]` in the pack) hide a tool unless the agent lists a matching toleration.
- **Field taints** (`taint_fields`) redact those reply fields unless the agent tolerates the taint.
- **`scope`** filters list replies by row; a single record outside the scope is refused. Placeholders: `{user.<claim>}` and `{agent.name}`. A missing claim means "out of scope".
- **`redact`** replaces extra fields with `[redacted]` for that agent or user.

Tool descriptions tell the agent its scope and redacted fields.

### Domains

```yaml
# config/domains.yaml
support: [zendesk, support_eu]
work: [jira, wrike]
```

A domain works in the source position of any pattern (`support:*:read`). Names must be unique across sources and domains.

### Audit log

Every call writes one JSON line to stdout, and also to `MYCO_AUDIT_FILE` if set:

```json
{"ts":"2026-10-11T16:23:22.264+00:00","audit":"tool.call","decision":"deny","reason":"no role of agent support-bot grants jira:issue:create","agent":"support-bot","user":"anonymous","tool":"jira_create_issue","label":"jira:issue:create","rule":"agent:no-role","args":["summary"],"config":"3bcd9bd3777e"}
```

Arguments are logged by name only, never by value. Rules you will see: `allow:<agent role>&<user>/<user role>`, `deny:agent:<pattern>`, `deny:user:<entry>:<pattern>`, `agent:no-role`, `user:<entry>:no-role`, `taint:<taint>`, `scope:outside`, `agent:no-file`, `user:token-rejected`.

## Example: create an agent with permissions

Goal: an agent that triages Jira issues. It may read and comment, but must never create issues, delete anything or see e-mail addresses.

**1. Write the agent file** `config/agents/jira-triage.yaml`:

```yaml
agent: jira-triage
owner: platform@acme.com
description: Reads issues and adds triage comments.
roles: [jira:contributor]
deny:
  - "jira:issue:create"     # contributor could create; this agent may not
  - "*:*:delete"
# no tolerations: assignee/reporter e-mails are redacted
```

**2. Give it a key** in `.env` (or in the cluster Secret):

```dotenv
MYCO_AGENT__JIRA_TRIAGE__KEY=myco_9mV...   # python3 -c "import secrets; print('myco_' + secrets.token_urlsafe(32))"
```

**3. Let it run unattended.** Effective access is agent ∩ user, and `anonymous` has no roles. So give the agent a service user in `config/users.yaml`; this is the file security reviews:

```yaml
users:
  jira-triage-service:
    service_for: [jira-triage]
    roles: [jira:contributor]
```

**4. Restart** (needed only for the new key; YAML changes alone hot-reload) and check what the key sees:

```text
jira_get_issue  jira_search_issues  jira_list_comments  jira_list_transitions
jira_add_comment  jira_transition_issue  jira_assign_issue  jira_log_work
```

`jira_create_issue` is absent. A direct call is refused anyway and audited as `deny:agent:jira:issue:create`.

## Example: create a connector

A connector is two files with the same name. This one ([`examples/connectors/acme_crm.py`](examples/connectors/acme_crm.py)) talks to a small CRM.

**`acme_crm.py`** contains the code. Tools are `async` methods marked `@tool`. Typed parameters become the tool's JSON schema, and the docstring becomes its description:

```python
from typing import Any, Literal

from myco_mcp.connector import Connector, Setting, tool
from myco_mcp.http import HttpClient

Stage = Literal["lead", "qualified", "proposal", "won", "lost"]


class AcmeCrm(Connector):
    type = "acme_crm"  # MYCO_SOURCE__<NAME>__TYPE=acme_crm
    settings = {
        "base_url": Setting(help="CRM API root"),
        "api_key": Setting(secret=True),
    }

    def __init__(self, source: str, config: dict[str, str]) -> None:
        super().__init__(source, config)
        self.http = HttpClient(config["base_url"], headers={"X-Api-Key": config["api_key"]})

    async def aclose(self) -> None:
        await self.http.aclose()

    @tool
    async def list_deals(self, stage: Stage | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List deals, optionally only one pipeline stage."""
        return (await self.http.get("/deals", params={"stage": stage, "limit": limit}))[:limit]

    @tool
    async def get_deal(self, deal_id: int) -> dict[str, Any]:
        """Get one deal, including its value and margin."""
        return await self.http.get(f"/deals/{deal_id}")

    @tool
    async def move_deal(self, deal_id: int, stage: Stage) -> dict[str, Any]:
        """Move a deal to another pipeline stage."""
        return await self.http.put(f"/deals/{deal_id}", json_body={"stage": stage})
```

**`acme_crm.yaml`** is the pack: exactly one label per tool, plus the roles:

```yaml
pack: acme_crm
version: "1"
tools:
  list_deals: { action: deal:list, taint_fields: { finance: [margin] } }
  get_deal:   { action: deal:read, taint_fields: { finance: [margin] } }
  move_deal: deal:update
roles:
  reader: [deal:read, deal:list]
  sales:  [deal:read, deal:list, deal:update]
```

**Load and configure it:**

```dotenv
MYCO_PLUGINS=acme_crm                 # module to import (must be on PYTHONPATH)
MYCO_SOURCE__CRM__TYPE=acme_crm
MYCO_SOURCE__CRM__BASE_URL=https://crm.acme.com/api
MYCO_SOURCE__CRM__API_KEY=...
```

```bash
docker run --rm -p 8080:8080 --env-file .env \
  -v "$PWD/config:/config:ro" \
  -v "$PWD/examples/connectors:/plugins:ro" -e PYTHONPATH=/plugins \
  myco
```

The source `crm` now publishes `crm_list_deals` (`crm:deal:list`), `crm_get_deal` and `crm_move_deal`, and the roles `crm:reader` and `crm:sales`. Agents without a `finance` toleration get `"margin": "[redacted]"`.

Rules for a good pack:

- **Curated means small.** Expose what agents need, not the whole API.
- **Safe shapes.** Use required fields, `Literal` enums and bounded numbers. Never take a raw URL, method or query passthrough, so every tool maps to exactly one label.
- **Errors:** raise `ValueError` for bad input. `HttpClient` raises `UpstreamError` with a readable message. Both reach the agent as tool errors.
- **Return plain JSON** (dicts and lists). List tools return a list of row dicts, so `scope` can filter them.
- **A mislabelled tool is a security bug.** Review pack changes like code that grants access, and add tests ([`tests/test_connectors.py`](tests/test_connectors.py) uses `respx`).

To ship a pack with Myco itself, put the two files in `src/myco_mcp/packs/` and add the module to `BUILTIN_MODULES` in [`catalog.py`](src/myco_mcp/catalog.py).

## Example packs

### Zendesk (`TYPE=zendesk`)

Settings: `DOMAIN` (e.g. `acme.zendesk.com`), `API_KEY` (the plain API key; Myco encodes it for Basic auth), `TIMEOUT` (optional, default 30 s).

| Tool | Label | Notes |
|---|---|---|
| `list_tickets` | `ticket:list` | filter by requester e-mail, company, `updated_since` |
| `search_tickets` | `ticket:list` | status, priority, tag, created-after (query is built by Myco) |
| `get_ticket` | `ticket:read` | requester name/e-mail/phone and cc are `pii` fields |
| `list_conversations` | `conversation:list` | e-mail fields are `pii` |
| `get_contact` | `contact:read` | tool tainted `pii` |
| `get_company` | `company:read` | |
| `get_article` | `article:read` | optional language, e.g. `de` |
| `create_ticket` | `ticket:create` | |
| `update_ticket` | `ticket:update` | status, priority, type, group, assignee, add tags |
| `add_note` | `note:create` | private note |
| `reply_to_ticket` | `reply:send` | e-mails the customer |

Roles: `reader` (all reads) · `contributor` (+ create/update tickets, notes) · `responder` (+ public replies).

### Jira Cloud (`TYPE=jira`)

Settings: `SITE` (e.g. `acme.atlassian.net`), `EMAIL`, `API_TOKEN`, `PROJECTS` (optional, comma-separated; confines every tool, including JQL search, to those projects), `TIMEOUT`.

| Tool | Label | Notes |
|---|---|---|
| `get_issue` | `issue:read` | description as plain text; people e-mails are `pii` |
| `search_issues` | `issue:list` | JQL, confined to `PROJECTS` |
| `list_comments` | `comment:list` | |
| `list_transitions` | `transition:list` | |
| `create_issue` | `issue:create` | assignee is best effort; ambiguous names create unassigned issues with a warning |
| `add_comment` | `comment:create` | |
| `transition_issue` | `issue:update` | by transition name, target status or id |
| `assign_issue` | `issue:update` | must resolve to exactly one user |
| `log_work` | `worklog:create` | e.g. `1h 30m` |

Roles: `reader` · `contributor`.

### Wrike (`TYPE=wrike`)

Settings: `TOKEN` (permanent access token), `BASE_URL` (default `https://www.wrike.com/api/v4`; EU: `https://app-eu.wrike.com/api/v4`), `TIMEOUT`.

| Tool | Label | Notes |
|---|---|---|
| `get_task` | `task:read` | API id (`MAAAAAEL…`), not the permalink number |
| `list_tasks` | `task:list` | folder (with sub-folders), title, status, `updated_since` |
| `get_task_comments` | `comment:list` | |
| `list_folders` | `folder:list` | |
| `create_task` | `task:create` | |
| `update_task` | `task:update` | |
| `add_comment` | `comment:create` | |

Roles: `reader` · `contributor`. Wrike answers a read the token may not see with an empty result, so Myco reports "not visible" rather than "does not exist".

## Configuration reference

| Variable | Default | |
|---|---|---|
| `MYCO_SOURCE__<NAME>__<SETTING>` | | Source settings; `TYPE` picks the pack |
| `MYCO_AGENT__<NAME>__KEY` | | Agent key, at least 16 characters, unique per agent |
| `MYCO_OIDC__ISSUER`, `MYCO_OIDC__AUDIENCE` | | Enable forwarded user tokens |
| `MYCO_OIDC__JWKS_URL` | from discovery | Override the JWKS location |
| `MYCO_OIDC__ALGORITHMS` | `RS256,ES256,PS256` | |
| `MYCO_USER_TOKEN_HEADER` | `X-Myco-User-Token` | |
| `MYCO_CONFIG_DIR` | `config` (`/config` in the image) | Permissions folder |
| `MYCO_RELOAD_SECONDS` | `10` | `0` disables hot reload |
| `MYCO_HOST`, `MYCO_PORT` | `0.0.0.0`, `8080` | |
| `MYCO_MCP_PATH` | `/mcp` | |
| `MYCO_AUDIT_FILE` | | Extra JSON-lines audit file |
| `MYCO_LOG_LEVEL` | `INFO` | |
| `MYCO_PLUGINS` | | Extra connector modules, comma-separated |
| `MYCO_STATELESS_HTTP` | `false` | `true` for several replicas without sticky sessions |

## Deploy

Every tenant gets its own Myco instance, its own `.env` and its own config folder, all from the same image.

### Docker

See [Quickstart](#quickstart). The image runs as a non-root user (uid 10001), listens on 8080 and has a `/health` check. It contains the example config at `/config`; mount yours over it.

### Helm

The chart in [`helm/myco`](helm/myco) runs Myco with a read-only root filesystem. Inputs:

- `existingSecret`: a Secret with the `MYCO_*` variables (sources, agent keys, OIDC).
- `config.source`:
  - `configMap`: files from `config.files`. `deploy.sh` fills it from `./config`.
  - `gitSync`: a [git-sync](https://github.com/kubernetes/git-sync) sidecar keeps a checkout of your permissions repo current (`gitSync.repo`, `ref`, `path`, `existingSecret` holding `GITSYNC_USERNAME`/`GITSYNC_PASSWORD`).
  - `image`: the example config inside the image.
- `ingress`: publishes only `/mcp` and `/health`, with TLS via cert-manager.

```bash
kubectl create namespace myco
kubectl -n myco create secret generic myco-env --from-env-file=.env.myco   # only MYCO_* lines
helm upgrade --install myco helm/myco -n myco \
  --set existingSecret=myco-env \
  --set config.source=gitSync --set gitSync.repo=https://github.com/acme/myco-config.git
```

Config changes don't restart the pod; Myco hot-reloads the folder. Sessions are in memory, so keep `replicaCount: 1`, or set `env.MYCO_STATELESS_HTTP: "true"` before scaling out.

### Scaleway Kubernetes Kapsule with `deploy.sh`

[`deploy.sh`](deploy.sh) builds the image, pushes it to Scaleway Container Registry and installs the chart. It is the same command for the first install and for updates.

**One-time prerequisites**

1. A Kapsule cluster, and the [`scw` CLI](https://github.com/scaleway/scaleway-cli) configured (`scw init`). You also need `docker`, `kubectl`, `helm` and `python3`.
2. A private registry namespace:
   ```bash
   scw registry namespace create name=myco region=fr-par is-public=false
   ```
3. To publish Myco on a hostname: an ingress controller and cert-manager with a `letsencrypt-prod` ClusterIssuer in the cluster, and a DNS record pointing at the ingress load balancer. (ingress-nginx is no longer maintained upstream; any controller works through `DEPLOY_INGRESS_CLASS`.)

**Deploy**

```bash
cp .env.example .env
# fill in: sources, SCW_SECRET_KEY, SCW_REGISTRY_NAMESPACE, SCW_K8S_CLUSTER_ID, DEPLOY_HOST
./deploy.sh
```

The script:

1. Reads `.env`. Variables already exported in your shell take precedence.
2. Generates `MYCO_AGENT__DEFAULT__KEY` into `.env` if it is empty. This is the default all-access agent.
3. Installs the Kapsule kubeconfig for `SCW_K8S_CLUSTER_ID`. Without it, the script uses the current kubectl context and warns.
4. Builds `linux/amd64` and pushes `rg.<region>.scw.cloud/<namespace>/myco:<git-sha>` and `:latest`.
5. Creates the namespace, the `scaleway-registry` pull secret, and the `<release>-env` Secret from every `MYCO_*` line of `.env`.
6. Ships `./config` as a ConfigMap (or wires git-sync when `DEPLOY_GIT_REPO` is set), then runs `helm upgrade --install --wait`.

Useful variants:

```bash
SKIP_BUILD=1 ./deploy.sh                 # config/secret change only, keep the running image
DEPLOY_DRY_RUN=1 SKIP_BUILD=1 ./deploy.sh # print the manifests, touch nothing
IMAGE_PLATFORM=linux/arm64 ./deploy.sh   # ARM node pools
./deploy.sh --set resources.limits.memory=1Gi   # extra helm args
```

Rotating a key means changing it in `.env` and running `SKIP_BUILD=1 ./deploy.sh`; the pod restarts because the Secret checksum changed.

## Security notes

- **Only Myco holds vendor credentials.** Give each instance its own, least-privilege vendor accounts. A shared full-access vendor key would undo tenant separation.
- **Secrets live in the environment.** Anything that can inspect the process can read them. Inject them from a secret manager or a Kubernetes Secret, never from the config repo.
- **Explicit deny wins.** Combine broad roles with narrow `deny` rules for guardrails such as `*:*:delete` and `*:*:send`.
- **Free text is not redacted.** Field redaction covers structured fields (e-mails, phones). Ticket bodies, descriptions and comments can still contain personal data. Grant those tools deliberately.
- **Review pack changes.** A wrong label grants the wrong access.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # unit tests, connector tests (respx) and an end-to-end MCP test
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Layout:

```
src/myco_mcp/
  server.py      FastMCP app, per-call gate, tools/list filtering, hot reload
  policy.py      decisions (roles ∩ user, deny, taints) and reply shaping
  config.py      config folder loading and validation
  catalog.py     sources → tools, labels and roles
  connector.py   Connector base class, @tool, pack format
  identity.py    agent keys, OIDC user tokens
  audit.py       JSON-lines audit log
  packs/         example connector packs (Zendesk, Jira, Wrike)
config/          example permissions (default agent with full access)
examples/        example plugin connector
helm/myco/       Helm chart
deploy.sh        Scaleway Kapsule deploy
```

## License

[MIT](LICENSE)
