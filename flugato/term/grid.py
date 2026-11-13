"""A screen as a grid of cells (character, colour role, background role, bold), drawn by the
screens and shown by the curses layer. Colours are roles the terminal's theme maps, like a
terminal maps its 16 colours; the cat keeps its own colours (RGB tuples)."""

from . import catframes

ROLES = ("text", "muted", "ok", "warn", "fail", "skip", "accent", "rule", "prompt", "white")


class Grid:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cells = [[[" ", "text", None, False] for _ in range(w)] for _ in range(h)]

    def put(self, x, y, text, fg="text", bg=None, bold=False):
        for i, ch in enumerate(text):
            if not ch.isprintable():
                ch = "?"
            if 0 <= x + i < self.w and 0 <= y < self.h:
                c = self.cells[y][x + i]
                c[0], c[1], c[3] = ch, fg, bold
                if bg is not None:
                    c[2] = bg
        return x + len(text)

    def right(self, x_end, y, text, **kw):
        return self.put(x_end - len(text), y, text, **kw)

    def centre(self, y, text, **kw):
        return self.put(max(0, (self.w - len(text)) // 2), y, text, **kw)

    def fill(self, x, y, w, h, bg):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= xx < self.w and 0 <= yy < self.h:
                    self.cells[yy][xx][2] = bg

    def rule(self, y, x=0, w=None):
        self.put(x, y, "─" * (w if w is not None else self.w - x), fg="rule")

    def cat(self, x, y, rows, pal=catframes.PAL):
        """Two cat cells per character: the upper one as the glyph colour of "▀", the lower
        one as the background; a lone lower cell becomes "▄"."""
        for r in range(0, len(rows), 2):
            top = rows[r]
            bot = rows[r + 1] if r + 1 < len(rows) else "." * len(top)
            yy = y + r // 2
            if not 0 <= yy < self.h:
                continue
            for i, (a, b) in enumerate(zip(top, bot)):
                xx = x + i
                if not 0 <= xx < self.w:
                    continue
                ta, tb = pal.get(a), pal.get(b)
                if ta and tb:
                    self.cells[yy][xx] = ["▀", ta, tb, False]
                elif ta:
                    self.cells[yy][xx] = ["▀", ta, None, False]
                elif tb:
                    self.cells[yy][xx] = ["▄", tb, None, False]

    def line(self, y):
        """The characters of a row, for tests."""
        return "".join(c[0] for c in self.cells[y]) if 0 <= y < self.h else ""

    def text(self):
        return "\n".join(self.line(y).rstrip() for y in range(self.h))
