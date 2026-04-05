# Memory & Context Rules

- **DO NOT** save information to local `.md` files (MEMORY.md, notes, etc.).
- **USE** LightRAG MCP tools for all long-term memory and project context:
  - `mcp__lightrag__insert_document` — store new information
  - `mcp__lightrag__query_document` — retrieve context before starting tasks
- When asked to "remember" something, call the LightRAG insert tool immediately.
- Before starting any non-trivial task, query LightRAG for related context.
