# Use it from your assistant

DocuRAG is an [MCP](https://modelcontextprotocol.io) server with three tools:

| Tool | What it returns |
| --- | --- |
| `search_docs(query, library?, version?, k?)` | The most relevant sections as Markdown, each with a `Source: url#section` citation |
| `list_libraries()` | Indexed libraries with versions, page and chunk counts |
| `get_page(url, max_chars?)` | A whole page as Markdown |

The server also sends instructions telling the assistant to check `list_libraries`, search
before answering, and cite its sources.

DocuRAG does not call an LLM itself. Your assistant's model decides when to search and
writes the answer; DocuRAG only retrieves.

## Claude Code

Local, over stdio (Claude Code starts the server):

```bash
claude mcp add docurag -- uv run --directory /path/to/Docs-Spider docurag serve --mcp
```

Against a running server or the Docker container, over HTTP:

```bash
claude mcp add --transport http docurag http://localhost:8000/mcp
# with DOCURAG_API_KEY set: add  --header "Authorization: Bearer $DOCURAG_API_KEY"
```

## Cursor and other MCP clients

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "docurag": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/Docs-Spider", "docurag", "serve", "--mcp"]
    }
  }
}
```

or `{"mcpServers": {"docurag": {"url": "http://localhost:8000/mcp"}}}` for the HTTP server.

## Versions

`search_docs` takes an optional `version`. `latest` (the default) resolves to the newest
release indexed for that library, so an assistant asking about FastAPI gets the current
docs unless it asks for `0.110` explicitly.
