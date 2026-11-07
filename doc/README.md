# Myco documentation

Guides and agent skills that go beyond the [main README](../README.md).

## Skills

Instructions written for a coding agent (Claude Code or any agent that follows a
`SKILL.md`). They run on a developer machine; their output is reviewed like any other code.

| Skill | Use it to |
|---|---|
| [new-connector](skills/new-connector/SKILL.md) | Generate a connector pack from an OpenAPI spec, API docs or an upstream MCP server, and update it when the vendor API changes. |

To use a skill with Claude Code, ask it to follow the file ("follow
doc/skills/new-connector/SKILL.md to add a connector for …"), or link the skill folder into
`.claude/skills/` so it is picked up automatically:

```bash
mkdir -p .claude/skills && ln -s ../../doc/skills/new-connector .claude/skills/new-connector
```
