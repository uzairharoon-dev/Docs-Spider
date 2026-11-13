# CLI

| Command | Description |
| --- | --- |
| `docurag add URL --name NAME` | Add a library, crawl it and index it. Re-running resumes an unfinished crawl. |
| `docurag update NAME [--full]` | Re-crawl and re-index incrementally. `--full` ignores sitemap `<lastmod>`. |
| `docurag index [NAME] [--force]` | Chunk and embed new or changed pages (all libraries by default). |
| `docurag search QUERY --lib NAME` | Search. Options: `--mode`, `--rerank`, `--version`, `-k`, `--json`, `--full`. |
| `docurag eval` | Run the retrieval eval: Recall@k, MRR and latency per search mode. |
| `docurag serve [--host] [--port]` | REST API with MCP mounted at `/mcp` (default `127.0.0.1:8000`). |
| `docurag serve --mcp` | MCP server over stdio, for Claude Code, Cursor and other MCP clients. |
| `docurag libraries` | List libraries with page, indexed-page and chunk counts. |
| `docurag jobs [NAME]` | Show recent crawl jobs and their stats. |
| `docurag page URL [--raw]` | Print the stored Markdown for a page. |
| `docurag remove NAME` | Delete a library and all its pages. |
| `docurag version` | Print the version. |

Global options: `--config/-c PATH` and `--verbose/-v`.

## Options for `add`

| Option | Default | Description |
| --- | --- | --- |
| `--include, -i` | — | Keep only paths that match. Repeatable. |
| `--exclude, -x` | — | Skip paths that match. Repeatable. |
| `--max-pages` | unlimited | Stop after this many fetches. |
| `--max-depth` | 20 | Maximum link depth from the start page. |
| `--rate-limit` | 2 | Requests per second per domain. |
| `--version` | `latest` | Label every page with this version (turns off detection). |
| `--version-pattern` | — | Regex with one group that reads the version from the URL path. |
| `--render-js` | `off` | `auto` renders JS-shell pages in Chromium, `on` renders all. |
| `--no-sitemap` | off | Don't auto-discover sitemaps. |
| `--fresh` | off | Start a new job instead of resuming. |
| `--no-index` | off | Crawl only; run `docurag index` later. |

Patterns in `--include` / `--exclude` are substrings, or globs when they contain `*`, `?` or `[`.

## Search modes

| Mode | Needs a model | Good at |
| --- | --- | --- |
| `hybrid` (default) | yes | Everything: fuses the two below |
| `vector` | yes | Paraphrased questions |
| `bm25` | no | Exact API names such as `jsonable_encoder` |

`--rerank` re-orders the top 20 hybrid results with a cross-encoder. It helps a little at
rank 1 and costs seconds per query on a slow CPU; see [evaluation](evaluation.md).
