# Image credits

The eight README diagrams (`who-does-what`, `request-journey`, `decisions-and-goals` and
`which-tool-when`, each as `<name>.png` and `<name>-steps.png`) are built by
[`src/build.py`](src/build.py).

## Illustration
The objects in the illustration strips (`src/strips/`) were generated with Codex image generation
(`codex-image`) from the prompts in `src/prompts/`. They contain no words and no logos. All text,
cards, lanes and arrows are drawn by the build script with real type, together with the vector marks
below.

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
