# P6 TUI candidate evaluation

Scores use the approved weighted criteria (100 total); they evaluate the ability to remain a StackMind presentation client, not visual polish.

| Candidate | Runtime 25 | Separation 20 | Extensibility 15 | Session 10 | Tool/provider 10 | License 10 | UX 5 | Health 5 | Total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| OpenCode | 23 | 18 | 14 | 9 | 10 | 10 | 4 | 5 | **93** |
| Pi | 21 | 19 | 14 | 8 | 9 | 10 | 4 | 4 | **89** |
| Crush | 18 | 16 | 13 | 9 | 9 | 4 | 5 | 5 | **79** |

OpenCode is selected as the reference foundation. Its MIT license and existing terminal-agent integrations make UI/component reuse low-risk, while the StackMind adapter prevents its native process, file, provider, and tool facilities from becoming authority.

Pi is a strong alternative with a composable TypeScript interaction model, but requires a second language/runtime in this Python project. Crush has polished UX and MCP support, but its current FSL-1.1-MIT license restricts competing use until each release converts to MIT, making it unsuitable for direct adoption.

Sources checked 2026-09-07: [OpenCode](https://github.com/anomalyco/opencode), [Pi mono-repo](https://github.com/badlogic/pi-mono), [Crush license](https://github.com/charmbracelet/crush/blob/main/LICENSE.md).
