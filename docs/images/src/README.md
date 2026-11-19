# Diagram sources

Style: dark blueprint plate. Deep ink background, off-white hairline isometric line art, one
signal-orange accent (#FF6B35), Inter for labels and JetBrains Mono for small technical tags.

- `prompts/`: the codex-image prompt for each scene. Append `_style.txt` to each one.
- `scenes/`: the generated scenes (1536x1024, saved as WebP at quality 95). Each one includes its
  own arrows and empty label tags. The build script blanks each tag and sets type in it.
- `logos/`: single-color vector marks (see `../CREDITS.md`).
- `build.py`: writes the four PNGs into `docs/images/`.

Rebuild:

```sh
python3 -m venv /tmp/dvenv && /tmp/dvenv/bin/pip install pillow cairosvg
sh docs/images/src/fetch-fonts.sh
/tmp/dvenv/bin/python docs/images/src/build.py
```

Redraw a scene:

```sh
cat docs/images/src/prompts/who-does-what.txt docs/images/src/prompts/_style.txt | codex-image /tmp/s.png -
```

Save the result as `scenes/<name>.webp`. A new scene puts its tags and sockets in new places,
so re-measure them and update the coordinates in `build.py`.
