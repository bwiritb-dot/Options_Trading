# Rules

- English only in all instructions, code comments, and responses.
- Query LightRAG (`mcp__lightrag__query_document`) before any non-trivial task for project context.
- Use `mcp__lightrag__insert_document` to store new decisions/findings. Set `file_source`.
- Do NOT save to local .md memory files.
- New task = new session or `/clear`. Don't accumulate unrelated context.
- Batch actions: plan multi-step work upfront, execute in one flow.
- Keep responses concise. No trailing summaries unless asked.

# LightRAG Query Rules

Always set: `only_need_context: true`, provide `hl_keywords` + `ll_keywords`, default `mode: "mix"`.
Use `top_k: 10-15` for specific questions, `20-30` for architecture, `40-60` for broad research.
Modes: `mix` (default) | `local` (entity-specific) | `global` (patterns) | `naive` (exact text).
Do NOT index: `CLAUDE.md`, `MEMORY.md`, `*.md` meta files, `SYSTEM_PROMPT*.md`.
