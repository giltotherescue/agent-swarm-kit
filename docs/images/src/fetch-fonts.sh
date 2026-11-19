#!/bin/sh
# Downloads the two OFL fonts build.py uses into docs/images/src/fonts/ (not committed).
set -e
cd "$(dirname "$0")" && mkdir -p fonts && cd fonts
tmp=$(mktemp -d)
curl -sL -o "$tmp/inter.zip" https://github.com/rsms/inter/releases/download/v4.0/Inter-4.0.zip
curl -sL -o "$tmp/jbm.zip" https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip
unzip -joq "$tmp/inter.zip" extras/ttf/Inter-Bold.ttf extras/ttf/Inter-SemiBold.ttf extras/ttf/Inter-Medium.ttf extras/ttf/Inter-Regular.ttf
unzip -joq "$tmp/jbm.zip" fonts/ttf/JetBrainsMono-Medium.ttf fonts/ttf/JetBrainsMono-Regular.ttf
rm -rf "$tmp"; ls
