# Diagram sources

Each README topic has two images that share one look (white card, Inter and JetBrains Mono, one
orange accent, the marks in `logos/`):

- `<topic>.png`, the short version: a small illustration strip over four plain-language columns.
- `<topic>-steps.png`, step by step: a light swimlane with one row per part of the system. Orange
  outlines are the only places you act, and dashed cards are the "model gets stuck" loop.

Files:

- `prompts/`: the codex-image prompt for each illustration strip. Append `_style.txt` to each one.
- `strips/`: the generated strips (WebP). Each has four separate objects on white and no text.
  `build.py` cuts the objects out, turns the white into transparency and places them over the columns.
- `logos/`: single-color vector marks (see `../CREDITS.md`).
- `build.py`: all text, cards, lanes and arrows, set as SVG, screenshotted at 2x with `agent-browser`,
  then given transparent rounded corners so the card reads on GitHub light and dark.

Rebuild (needs `agent-browser` on the PATH):

```sh
python3 -m venv /tmp/dvenv && /tmp/dvenv/bin/pip install pillow
sh docs/images/src/fetch-fonts.sh
/tmp/dvenv/bin/python docs/images/src/build.py            # all eight
/tmp/dvenv/bin/python docs/images/src/build.py journey    # one topic: who, journey, decisions, which
```

Redraw a strip (match the style of the existing ones by passing one as a reference):

```sh
cat docs/images/src/prompts/who-does-what.txt docs/images/src/prompts/_style.txt \
  | CODEX_IMAGE_REF=docs/images/src/strips/request-journey.webp codex-image /tmp/s.png -
```

Save it as `strips/<topic>.webp`. The objects are found by the white gaps between them, so the
prompt must keep exactly four objects with clear space in between.

Text sizes are set for GitHub: the short version is shown in the README at 800 px wide, and the
step-by-step version in docs/how-it-works.md at GitHub's full content width (about 840 px), so no text
ends up under about 12 px.
