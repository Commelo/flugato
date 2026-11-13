"""Exports the cat for the terminal app. Run from the repository root:

    python3 tools/export_cat_term.py

Writes flugato/term/catframes.py: every pose the terminal app shows, for every mood, as
run-length rows on the cat's own 28 x 18 canvas, plus the palette. The terminal app ships as
a stdlib-only file, so it cannot draw the cat with Pillow the way flugato.pet does; the cells
are baked here instead. Run it again after any change to pet.py."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flugato import pet  # noqa: E402

OUT = os.path.join(ROOT, "flugato", "term", "catframes.py")

POSES = [
    # awake, on the results screens
    "sit", "sit1", "blink", "blink1", "look_l", "look_r", "look_up", "twitch", "yawn1", "yawn2",
    "stretch", "lick1", "lick2", "tongue", "annoyed", "annoyed1", "hop", "scared",
    # asleep, no link
    "loaf", "loaf_in", "loaf_awake", "loaf_look_l", "loaf_look_r", "loaf_yawn", "loaf_scared",
]
MOODS = ["good", "testing", "warn", "fail", "sleep", "blink_on", "blink_off"]


def rle(row):
    out, i = [], 0
    while i < len(row):
        j = i
        while j < len(row) and row[j] == row[i]:
            j += 1
        n = j - i
        out.append((str(n) if n > 1 else "") + row[i])
        i = j
    return "".join(out)


def main():
    frames, shared = {}, {}
    for pose in POSES:
        for mood in MOODS:
            rows = pet.cells(mood, pose, 0)
            packed = "/".join(rle(r) for r in rows)
            key = shared.setdefault(packed, f"{mood}:{pose}")
            frames[f"{mood}:{pose}"] = packed if key == f"{mood}:{pose}" else "=" + key
    lines = [
        '"""The cat\'s cells for the terminal app, baked from flugato/pet.py by',
        'tools/export_cat_term.py. Do not edit: run the exporter after a change to pet.py."""',
        "",
        f"W, H = {pet.CW}, {pet.CH}",
        "PAL = {",
    ]
    for k, v in pet.PAL.items():
        lines.append(f"    {k!r}: {v!r},")
    lines += ["}", "", "# mood:pose -> run-length rows ('4.3o' = four dots, three o), or '=other' (same cells)", "FRAMES = {"]
    for k, v in frames.items():
        lines.append(f"    {k!r}: {v!r},")
    lines += ["}", "", "", "def rows(mood, pose):",
              '    """The cat as H strings of W palette letters (\'.\' = nothing there)."""',
              "    key = f\"{mood}:{pose}\"",
              "    packed = FRAMES.get(key) or FRAMES[f\"good:{pose}\"]",
              "    while packed.startswith('='):",
              "        packed = FRAMES[packed[1:]]",
              "    out = []",
              "    for row in packed.split('/'):",
              "        cells, n = [], ''",
              "        for ch in row:",
              "            if ch.isdigit():",
              "                n += ch",
              "            else:",
              "                cells.append(ch * (int(n) if n else 1)); n = ''",
              "        out.append(''.join(cells))",
              "    return out",
              ""]
    with open(OUT, "w") as f:
        f.write("\n".join(lines))
    print(OUT, len(frames), "frames", sum(1 for v in frames.values() if not v.startswith("=")), "distinct")


if __name__ == "__main__":
    main()
