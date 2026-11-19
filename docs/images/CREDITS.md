# Image credits

The four README diagrams (`who-does-what.png`, `request-journey.png`,
`decisions-and-goals.png`, `which-tool-when.png`) are built by
[`src/build.py`](src/build.py).

## Illustration
All illustration in `src/scenes/` was generated with Codex image generation (`codex-image`) from
the prompts in `src/prompts/`. That includes the arrows, connectors and empty label tags. The scenes
contain no words and no logos. The build script only fills the empty tags and sockets with real
typography and the vector marks below.

## Logos
Logos are used only to label which part of the system is which. They are rendered in a single
color, at a small size, and are not modified.

| Mark | Used for | Source |
| --- | --- | --- |
| Claude | the model the worker runs | Simple Icons 16.38.0, `claude.svg`: https://cdn.jsdelivr.net/npm/simple-icons@16.38.0/icons/claude.svg (project: https://simpleicons.org) |
| OpenAI | Codex, the other model the worker runs | Simple Icons 16.38.0, `openai.svg`: https://cdn.jsdelivr.net/npm/simple-icons@16.38.0/icons/openai.svg |
| Grok | Grok Bots (the bots you talk to) | LobeHub Icons, `grok.svg`: https://unpkg.com/@lobehub/icons-static-svg@latest/icons/grok.svg (project: https://github.com/lobehub/lobe-icons). Simple Icons has no Grok/xAI mark, and xAI's brand page (https://x.ai/legal/brand-guidelines) was not reachable from the build machine. No separate "Grok Bot" mark was found, so the Grok mark stands in for it. |

All marks are trademarks of their owners (Anthropic, OpenAI, xAI).

## Fonts
- Inter 4.0 (SIL OFL 1.1): https://github.com/rsms/inter
- JetBrains Mono 2.304 (SIL OFL 1.1): https://github.com/JetBrains/JetBrainsMono

The fonts are downloaded by `src/fetch-fonts.sh` and are not committed.
