# Demo GIF

`demo.tape` is a [VHS](https://github.com/charmbracelet/vhs) script that records
`docs/assets/demo.gif`. It runs in Docker, so you don't need VHS, ttyd or ffmpeg locally.

1. Build the eval corpus into a separate data dir (see the README's Evaluation section):

   ```bash
   export DOCURAG_HOME=~/.docurag-eval
   for lib in fastapi httpx typer; do uv run docurag -c docurag/eval/corpus.yaml update $lib; done
   ```

2. Build the recording image, passing that data dir as the `data` build context:

   ```bash
   docker build -f demo/Dockerfile --build-context data="$DOCURAG_HOME" -t docurag-demo .
   ```

3. Record:

   ```bash
   docker run --rm -v "$PWD/demo:/vhs" docurag-demo demo.tape
   mv demo/demo.gif docs/assets/demo.gif
   ```

4. Clean up: `docker image rm docurag-demo`.
