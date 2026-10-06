# Local TUI (Ollama-backed) — Best Practices Checklist

Use this as an audit list against an existing build. Organized by category.

## Rendering & Streaming

- [ ] Text streams token-by-token without flicker or full-screen redraws
- [ ] Partial markdown renders correctly mid-stream (a code fence opened but not yet closed doesn't break layout)
- [ ] Syntax highlighting works on streamed code blocks, not just complete ones
- [ ] Long output (big file dumps, long command output) doesn't blow up scrollback or freeze the UI
- [ ] Terminal resize is handled gracefully (re-wraps text, doesn't corrupt layout)
- [ ] Diffs render with clear add/remove coloring, line numbers, and stay readable at narrow widths
- [ ] Tool calls are visually distinct from assistant prose (e.g., a boxed/dimmed "ran: grep foo" block, collapsible if long)
- [ ] Unicode/emoji/wide characters (CJK, etc.) don't break column alignment
- [ ] Works across common terminals (iTerm2, Alacritty, Windows Terminal, tmux) — test in at least 2-3

## Input Handling

- [ ] Multiline input works (shift+enter or a clear alternate binding) without submitting prematurely
- [ ] Command history (up/down arrows) persists across sessions
- [ ] Paste of large blocks doesn't lag or get mis-rendered character-by-character
- [ ] Slash commands with autocomplete/discoverability (`/help`, `/clear`, `/model`, `/compact`)
- [ ] Ctrl+C interrupts a running generation/tool call without killing the whole app
- [ ] Ctrl+D / explicit quit command exits cleanly (no orphaned Ollama requests or zombie subprocesses)
- [ ] Input box shows a clear "busy" state while the model is generating (so users don't type into a black hole)

## Approval / Safety UX

- [ ] File writes and shell commands require explicit approval by default
- [ ] Approval prompt shows the *actual* diff/command, not just a generic "allow tool?" message
- [ ] "Allow once / always for session / deny" options exist — not just yes/no
- [ ] Denied tool calls are fed back to the model as a clear rejection, not a silent hang
- [ ] There's a visible indicator of what permission mode is active (auto-accept edits, ask-always, etc.)

## Status & Observability

- [ ] Current model name + quantization visible
- [ ] Context window usage shown (e.g., "6.2K / 8K tokens") and updates live
- [ ] Tokens/sec or generation speed visible somewhere (local inference speed varies a lot — users want this)
- [ ] Clear indicator when context is about to be compacted/truncated, ideally before it happens
- [ ] Errors (Ollama connection lost, model not found, tool execution failure) show a readable message, not a stack trace dump or silent freeze

## Session Management

- [ ] Conversations persist to disk and can be resumed
- [ ] Session list/picker to resume a past conversation
- [ ] `/clear` or `/reset` genuinely clears context (and confirms it did)
- [ ] Project-level context (e.g. a config/instructions file, git branch) loads automatically per working directory

## Resilience

- [ ] Malformed tool-call JSON from the model triggers a repair/retry loop, not a crash
- [ ] Infinite tool-call loops are capped (max iterations) with a visible message when hit
- [ ] Ollama being down/unreachable at startup gives a clear error, not a silent hang
- [ ] Mid-stream network/connection drop is recoverable (retry or clear failure state) rather than leaving the UI stuck "thinking"
- [ ] Killing the app (Ctrl+C, crash) doesn't leave orphaned background processes from `run_command`

## Performance

- [ ] Rendering doesn't visibly lag behind token generation speed
- [ ] Large file reads/edits don't block the input loop
- [ ] Scrollback of a long session doesn't degrade frame rate over time (watch for unbounded re-render of full history on each update)

## Polish

- [ ] Theming / respects terminal's light-dark setting or offers a toggle
- [ ] Keyboard shortcut reference accessible via `/help` or `?`
- [ ] Sensible default keybindings that don't conflict with terminal/tmux defaults
- [ ] Startup time is fast (lazy-load anything heavy)
- [ ] Config file for defaults (model, permission mode, keybindings) rather than only CLI flags
