#!/usr/bin/env python3
"""A small VT100 terminal text editor, ported from the Kilo C example.

Usage: python kilo_editor.py <file>
Controls: Ctrl-S save, Ctrl-Q quit, Ctrl-F find, arrows move.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import termios
    import tty
except ImportError:  # Windows
    termios = tty = None
    import msvcrt


VERSION = "0.0.1"
TAB_STOP = 8
QUIT_TIMES = 3
ESC = "\x1b"

NORMAL, NONPRINT, COMMENT, MLCOMMENT, KEYWORD1, KEYWORD2, STRING, NUMBER, MATCH = range(9)
COLORS = {COMMENT: 36, MLCOMMENT: 36, KEYWORD1: 33, KEYWORD2: 32,
          STRING: 35, NUMBER: 31, MATCH: 34}

C_EXTENSIONS = (".c", ".h", ".cpp", ".hpp", ".cc")
C_KEYWORDS = """auto break case continue default do else enum extern for goto if register return sizeof static struct switch typedef union volatile while NULL alignas alignof and and_eq asm bitand bitor class compl constexpr const_cast decltype delete dynamic_cast explicit export false friend inline mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected public reinterpret_cast static_assert static_cast template this thread_local throw true try typeid typename virtual xor xor_eq int| long| double| float| char| unsigned| signed| void| short| auto| const| bool|""".split()
SEPARATORS = set(",.()+-/*=~%[];{}<>")


@dataclass
class Row:
    chars: str
    render: str = ""
    hl: list[int] = field(default_factory=list)
    open_comment: bool = False

    def update_render(self) -> None:
        out: list[str] = []
        col = 0
        for ch in self.chars:
            if ch == "\t":
                spaces = TAB_STOP - (col % TAB_STOP)
                out.append(" " * spaces)
                col += spaces
            else:
                out.append(ch)
                col += 1
        self.render = "".join(out)


class Terminal:
    """Raw keyboard input with a single ANSI escape-sequence decoder."""

    def __init__(self) -> None:
        self.original = None
        self.windows = os.name == "nt"

    def enable(self) -> None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Kilo needs to run in an interactive terminal.")
        if not self.windows:
            self.original = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
        atexit.register(self.restore)

    def restore(self) -> None:
        if self.original is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, self.original)
            self.original = None
        self.write(ESC + "[0m" + ESC + "[?25h" + ESC + "[2J" + ESC + "[H")

    @staticmethod
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _read_char(self) -> str:
        if self.windows:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
                        "S": "DELETE", "G": "HOME", "O": "END", "I": "PAGEUP",
                        "Q": "PAGEDOWN"}.get(msvcrt.getwch(), "")
            return ch
        return os.read(sys.stdin.fileno(), 1).decode("utf-8", "surrogateescape")

    def read_key(self) -> str:
        ch = self._read_char()
        if ch != ESC or self.windows:
            return ch
        # Raw POSIX input has no timeout; select allows a lone Escape.
        import select
        if not select.select([sys.stdin], [], [], 0.1)[0]:
            return ESC
        first = self._read_char()
        if not select.select([sys.stdin], [], [], 0.05)[0]:
            return ESC
        second = self._read_char()
        if first == "[":
            if second.isdigit():
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    return ESC
                third = self._read_char()
                if third == "~":
                    return {"1": "HOME", "3": "DELETE", "4": "END", "5": "PAGEUP", "6": "PAGEDOWN", "7": "HOME", "8": "END"}.get(second, ESC)
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}.get(second, ESC)
        if first == "O":
            return {"H": "HOME", "F": "END"}.get(second, ESC)
        return ESC


class Editor:
    def __init__(self, filename: str) -> None:
        self.path = Path(filename)
        self.rows: list[Row] = []
        self.cx = self.cy = self.rowoff = self.coloff = 0
        self.screenrows = self.screencols = 0
        self.dirty = False
        self.status = ""
        self.status_time = 0.0
        self.syntax = self.path.suffix.lower() in C_EXTENSIONS
        self.terminal = Terminal()
        self.quit_times = QUIT_TIMES
        self.refresh_size()
        self.open()

    def refresh_size(self) -> None:
        size = shutil.get_terminal_size((80, 24))
        self.screencols = max(1, size.columns)
        self.screenrows = max(1, size.lines - 2)

    def set_status(self, message: str, *args: object) -> None:
        self.status = message % args if args else message
        self.status_time = time.time()

    def open(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as source:
                self.rows = [Row(line.rstrip("\r\n")) for line in source]
        except OSError as error:
            raise RuntimeError(f"Cannot open {self.path}: {error}") from error
        # Newly loaded rows have no cached render/highlight data yet, so the
        # first pass deliberately visits every row.
        open_comment = False
        for row in self.rows:
            row.update_render()
            if self.syntax:
                self.highlight(row, open_comment)
            else:
                row.hl, row.open_comment = [NORMAL] * len(row.render), False
            open_comment = row.open_comment

    def save(self) -> None:
        try:
            # One full write keeps the save operation simple and avoids partial row writes.
            self.path.write_text("\n".join(row.chars for row in self.rows) + ("\n" if self.rows else ""),
                                 encoding="utf-8", errors="surrogateescape", newline="")
        except OSError as error:
            self.set_status("Can't save: %s", error)
            return
        self.dirty = False
        self.set_status("%d bytes written to disk", self.path.stat().st_size)

    @staticmethod
    def separator(ch: str) -> bool:
        return not ch or ch.isspace() or ch in SEPARATORS

    def update_syntax_from(self, start: int) -> None:
        """Re-render/highlight only affected rows; multiline comment state propagates."""
        previous_open = self.rows[start - 1].open_comment if start else False
        for index in range(start, len(self.rows)):
            row = self.rows[index]
            was_open = row.open_comment
            row.update_render()
            if not self.syntax:
                row.hl, row.open_comment = [NORMAL] * len(row.render), False
            else:
                self.highlight(row, previous_open)
            previous_open = row.open_comment
            if index > start and was_open == row.open_comment:
                break

    def highlight(self, row: Row, in_comment: bool) -> None:
        text = row.render
        hl = [NORMAL] * len(text)
        i, previous_separator, quote = 0, True, ""
        while i < len(text):
            ch = text[i]
            if in_comment:
                hl[i] = MLCOMMENT
                if text.startswith("*/", i):
                    hl[i:i + 2] = [MLCOMMENT, MLCOMMENT]
                    i += 2; in_comment = False; previous_separator = True
                else:
                    i += 1; previous_separator = False
                continue
            if not quote and previous_separator and text.startswith("//", i):
                hl[i:] = [COMMENT] * (len(text) - i); break
            if not quote and text.startswith("/*", i):
                hl[i:i + 2] = [MLCOMMENT, MLCOMMENT]
                i += 2; in_comment = True; previous_separator = False; continue
            if quote:
                hl[i] = STRING
                if ch == "\\" and i + 1 < len(text):
                    hl[i + 1] = STRING; i += 2; continue
                if ch == quote: quote = ""
                i += 1; previous_separator = False; continue
            if ch in "\"'":
                quote = ch; hl[i] = STRING; i += 1; previous_separator = False; continue
            if not ch.isprintable():
                hl[i] = NONPRINT; i += 1; previous_separator = False; continue
            if ch.isdigit() and (previous_separator or (i and hl[i - 1] == NUMBER)) or (ch == "." and i and hl[i - 1] == NUMBER):
                hl[i] = NUMBER; i += 1; previous_separator = False; continue
            if previous_separator:
                match = next(((word, kind) for raw in C_KEYWORDS
                              for word, kind in [(raw.rstrip("|"), KEYWORD2 if raw.endswith("|") else KEYWORD1)]
                              if text.startswith(word, i) and self.separator(text[i + len(word):i + len(word) + 1])), None)
                if match:
                    word, kind = match; hl[i:i + len(word)] = [kind] * len(word)
                    i += len(word); previous_separator = False; continue
            previous_separator = self.separator(ch); i += 1
        row.hl, row.open_comment = hl, in_comment

    def insert_row(self, at: int, chars: str) -> None:
        self.rows.insert(at, Row(chars)); self.update_syntax_from(at); self.dirty = True

    def insert_char(self, char: str) -> None:
        while self.cy >= len(self.rows): self.rows.append(Row(""))
        row = self.rows[self.cy]
        at = min(self.cx, len(row.chars))
        row.chars = row.chars[:at] + char + row.chars[at:]
        self.cx = at + 1; self.dirty = True; self.update_syntax_from(self.cy)

    def insert_newline(self) -> None:
        if self.cy >= len(self.rows):
            self.insert_row(len(self.rows), "")
        else:
            row = self.rows[self.cy]; at = min(self.cx, len(row.chars))
            before, after = row.chars[:at], row.chars[at:]
            row.chars = before; self.rows.insert(self.cy + 1, Row(after)); self.update_syntax_from(self.cy); self.dirty = True
        self.cy += 1; self.cx = 0

    def backspace(self) -> None:
        if self.cy == 0 and self.cx == 0: return
        if self.cx > 0 and self.cy < len(self.rows):
            row = self.rows[self.cy]; at = min(self.cx, len(row.chars))
            row.chars = row.chars[:at - 1] + row.chars[at:]; self.cx = at - 1; self.update_syntax_from(self.cy)
        elif self.cy > 0:
            previous = self.rows[self.cy - 1]; self.cx = len(previous.chars)
            previous.chars += self.rows[self.cy].chars; del self.rows[self.cy]; self.cy -= 1; self.update_syntax_from(self.cy)
        self.dirty = True

    def delete(self) -> None:
        if self.cy >= len(self.rows): return
        row = self.rows[self.cy]
        if self.cx < len(row.chars):
            row.chars = row.chars[:self.cx] + row.chars[self.cx + 1:]; self.update_syntax_from(self.cy); self.dirty = True
        elif self.cy + 1 < len(self.rows):
            row.chars += self.rows[self.cy + 1].chars; del self.rows[self.cy + 1]; self.update_syntax_from(self.cy); self.dirty = True

    def move(self, key: str) -> None:
        if key == "LEFT":
            if self.cx: self.cx -= 1
            elif self.cy: self.cy -= 1; self.cx = len(self.rows[self.cy].chars)
        elif key == "RIGHT" and self.cy < len(self.rows):
            if self.cx < len(self.rows[self.cy].chars): self.cx += 1
            elif self.cy + 1 < len(self.rows): self.cy += 1; self.cx = 0
        elif key == "UP" and self.cy: self.cy -= 1
        elif key == "DOWN" and self.cy < len(self.rows): self.cy += 1
        if self.cy < len(self.rows): self.cx = min(self.cx, len(self.rows[self.cy].chars))

    def scroll(self) -> None:
        if self.cy < self.rowoff: self.rowoff = self.cy
        if self.cy >= self.rowoff + self.screenrows: self.rowoff = self.cy - self.screenrows + 1
        if self.cx < self.coloff: self.coloff = self.cx
        if self.cx >= self.coloff + self.screencols: self.coloff = self.cx - self.screencols + 1

    def draw_row(self, row: Row) -> str:
        # coloff/cx use source columns. Convert source columns to render columns for tabs.
        def rendered_column(source_column: int) -> int:
            col = 0
            for ch in row.chars[:source_column]: col += TAB_STOP - col % TAB_STOP if ch == "\t" else 1
            return col
        start = rendered_column(self.coloff)
        text, highlights = row.render[start:start + self.screencols], row.hl[start:start + self.screencols]
        parts: list[str] = []; color = None
        for ch, kind in zip(text, highlights):
            if kind == NONPRINT:
                parts.append(ESC + "[7m" + (chr(ord("@") + ord(ch)) if ord(ch) <= 26 else "?") + ESC + "[0m")
                continue
            new_color = COLORS.get(kind)
            if new_color != color:
                parts.append(f"{ESC}[{new_color if new_color else 39}m"); color = new_color
            parts.append(ch)
        return "".join(parts) + ESC + "[39m" + ESC + "[0K"

    def refresh(self) -> None:
        self.refresh_size(); self.scroll()
        out = [ESC + "[?25l", ESC + "[H"]
        for screen_y in range(self.screenrows):
            file_y = self.rowoff + screen_y
            if file_y >= len(self.rows):
                if not self.rows and screen_y == self.screenrows // 3:
                    welcome = f"Kilo editor -- version {VERSION}"
                    out.append("~" + welcome.center(max(0, self.screencols - 1)) + ESC + "[0K")
                else: out.append("~" + ESC + "[0K")
            else: out.append(self.draw_row(self.rows[file_y]))
            out.append("\r\n")
        name = self.path.name[:20]
        status = f"{name} - {len(self.rows)} lines {'(modified)' if self.dirty else ''}"
        location = f"{self.cy + 1}/{len(self.rows)}"
        out.append(ESC + "[7m" + (status[:self.screencols - len(location)].ljust(max(0, self.screencols - len(location))) + location)[:self.screencols] + ESC + "[m\r\n")
        message = self.status if time.time() - self.status_time < 5 else ""
        out.append(ESC + "[0K" + message[:self.screencols])
        cursor_x = 1
        if self.cy < len(self.rows):
            for ch in self.rows[self.cy].chars[self.coloff:self.cx]: cursor_x += TAB_STOP - cursor_x % TAB_STOP if ch == "\t" else 1
        out.append(f"{ESC}[{self.cy - self.rowoff + 1};{cursor_x}H{ESC}[?25h")
        self.terminal.write("".join(out))

    def find(self) -> None:
        saved = self.cx, self.cy, self.rowoff, self.coloff
        query, last, direction = "", -1, 1
        while True:
            self.set_status("Search: %s (ESC/arrows/Enter)", query); self.refresh(); key = self.terminal.read_key()
            if key in (ESC, "\r", "\n"):
                if key == ESC: self.cx, self.cy, self.rowoff, self.coloff = saved
                self.set_status(""); return
            if key in ("\x7f", "\b", "DELETE"):
                query = query[:-1]; last = -1
            elif key in ("RIGHT", "DOWN"): direction = 1
            elif key in ("LEFT", "UP"): direction = -1
            elif len(key) == 1 and key.isprintable(): query += key; last = -1
            if not query: continue
            for _ in range(len(self.rows)):
                last = (last + direction) % len(self.rows)
                found = self.rows[last].chars.find(query)
                if found >= 0:
                    self.cy, self.cx, self.rowoff, self.coloff = last, found, last, 0; break

    def process_keypress(self) -> bool:
        key = self.terminal.read_key()
        if key == "\x11":
            if self.dirty and self.quit_times:
                self.set_status("WARNING: unsaved changes. Press Ctrl-Q %d more times to quit.", self.quit_times); self.quit_times -= 1; return True
            return False
        if key == "\x13": self.save()
        elif key == "\x06": self.find()
        elif key in ("\r", "\n"): self.insert_newline()
        elif key in ("\x7f", "\b"): self.backspace()
        elif key == "DELETE": self.delete()
        elif key in ("UP", "DOWN", "LEFT", "RIGHT"): self.move(key)
        elif key == "HOME": self.cx = 0
        elif key == "END" and self.cy < len(self.rows): self.cx = len(self.rows[self.cy].chars)
        elif key in ("PAGEUP", "PAGEDOWN"):
            self.cy = self.rowoff if key == "PAGEUP" else min(len(self.rows), self.rowoff + self.screenrows - 1)
            for _ in range(self.screenrows): self.move("UP" if key == "PAGEUP" else "DOWN")
        elif len(key) == 1 and key >= " ": self.insert_char(key)
        self.quit_times = QUIT_TIMES
        return True

    def run(self) -> None:
        self.terminal.enable()
        self.set_status("HELP: Ctrl-S = save | Ctrl-Q = quit | Ctrl-F = find")
        while True:
            self.refresh()
            if not self.process_keypress(): break


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {Path(sys.argv[0]).name} <filename>", file=sys.stderr); return 1
    try:
        editor = Editor(sys.argv[1])
        if hasattr(signal, "SIGWINCH"): signal.signal(signal.SIGWINCH, lambda *_: editor.refresh())
        editor.run()
    except (OSError, RuntimeError) as error:
        print(f"kilo: {error}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
