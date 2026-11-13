# Limitations

- The Docker image is about 1 GB, mostly onnxruntime and numpy. With Chromium for
  `render_js` it is about 3 GB.
- Crawls started through the API are not resumed after a server restart; the next
  `POST /crawl` for that library resumes the interrupted job.
- The reranker is slow on weak CPUs (about 3 s per query on the benchmark machine); it stays
  off by default.
- Pages crawled before section anchors existed get them only when their content changes
  (a `304 Not Modified` carries no HTML). `docurag remove` + `add` refreshes them.
- Rendering in Chromium is slower than plain HTTP, and the browser's own sub-requests
  (scripts, XHR) are not counted against the per-domain rate limit.
- Notices a site repeats on every page inside the main content are kept. FastAPI and Typer
  open most pages with an "AI coding agents" box, so it starts many snippets. It does not
  change the eval scores, but it costs tokens in results.
- Code blocks have a language tag only when the site's HTML provides one.
  FastAPI's HTML does not, so its code fences are untagged.
- Version detection looks at the first three directory levels only, so `/api/v1/`
  reference paths deeper in a site aren't mistaken for docs versions.
- The `sqlite` vector store searches by brute force. It is fast up to a few hundred thousand
  chunks; beyond that, use Qdrant.
