# DocuRAG demo. Record it with the commands in demo/README.md.
Output demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set PlaybackSpeed 1.0

Hide
Type "export PS1='$ ' && clear" Enter
Sleep 1s
Show

Type "# Three docs sites are crawled and indexed locally." Enter
Sleep 500ms
Type "docurag libraries" Enter
Sleep 3s

Type "clear" Enter
Type "# Ask the way a developer would, across every library. Found by meaning." Enter
Sleep 500ms
Type `docurag search "my frontend on another port can't call the API, browser blocks it" -k 3` Enter
Sleep 7s

Type "clear" Enter
Type "# Exact API names are found by keywords. Every hit cites its section." Enter
Sleep 500ms
Type 'docurag search "jsonable_encoder" --lib fastapi -k 2' Enter
Sleep 6s

Type "clear" Enter
Type "# Assistants get the same search over MCP:" Enter
Type "#   claude mcp add docurag -- docurag serve --mcp" Enter
Sleep 4s
