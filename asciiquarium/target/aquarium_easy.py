"""Simple ASCII aquarium for Windows.  No pip installs required.

Run: python aquarium_easy.py
Keys: q quit, p pause, r reset
"""

import os
import random
import shutil
import sys
import time

try:
    import msvcrt  # Windows keyboard input, included with Python
except ImportError:
    msvcrt = None


FISH_RIGHT = [r"><(((('>", r"><>" , r"><((('>", r">==('>"]
FISH_LEFT = [r"<')))))><", r"<><", r"<')))><", r"<')==<"]
COLORS = (31, 32, 33, 35, 36)


def terminal_size():
    size = shutil.get_terminal_size((80, 24))
    return max(40, size.columns), max(15, size.lines)


def new_fish(width, height):
    moving_right = random.choice((True, False))
    art = random.choice(FISH_RIGHT if moving_right else FISH_LEFT)
    return {
        "art": art,
        "x": -len(art) if moving_right else width,
        "y": random.randrange(7, height - 2),
        "speed": random.uniform(8, 20) * (1 if moving_right else -1),
        "color": random.choice(COLORS),
    }


def reset():
    width, height = terminal_size()
    count = max(4, width // 12)
    fish = [new_fish(width, height) for _ in range(count)]
    seaweed = [(random.randrange(1, width - 2), random.randrange(3, 7)) for _ in range(width // 14)]
    return fish, seaweed


def put(canvas, x, y, text, color=""):
    if 0 <= y < len(canvas):
        for offset, char in enumerate(text):
            if 0 <= x + offset < len(canvas[0]) and char != " ":
                canvas[y][x + offset] = f"\x1b[{color}m{char}" if color else char


def draw(fish, seaweed, paused):
    width, height = terminal_size()
    canvas = [[" " for _ in range(width)] for _ in range(height)]
    put(canvas, 1, 0, "Easy Python Aquarium   [p] pause   [r] reset   [q] quit", "97")
    for y, line in enumerate(("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~", "^^^  ^^^   ^^^  ^^^^   ^^^"), 4):
        put(canvas, 0, y, (line * (width // len(line) + 1))[:width], "36")
    for x, size in seaweed:
        for row in range(size):
            put(canvas, x + (row % 2), height - 2 - row, "|", "32")
            put(canvas, x + 1 - (row % 2), height - 2 - row, "/", "32")
    castle = ("   |~|", "  _|_|_", " |     |", "_|_____|_")
    for row, line in enumerate(castle):
        put(canvas, width - 12, height - 2 - len(castle) + row, line, "33")
    for item in fish:
        put(canvas, int(item["x"]), item["y"], item["art"], str(item["color"]))
    status = "PAUSED" if paused else ""
    put(canvas, width - len(status) - 2, 0, status, "91")
    sys.stdout.write("\x1b[H" + "\n".join("".join(row) + "\x1b[0m" for row in canvas))
    sys.stdout.flush()


def key_pressed():
    if msvcrt and msvcrt.kbhit():
        return msvcrt.getwch().lower()
    return ""


def main():
    fish, seaweed = reset()
    paused = False
    last = time.monotonic()
    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
    try:
        while True:
            now = time.monotonic()
            elapsed, last = min(now - last, 0.1), now
            key = key_pressed()
            if key == "q":
                break
            if key == "p":
                paused = not paused
            if key == "r":
                fish, seaweed = reset()
            if not paused:
                width, height = terminal_size()
                for item in fish:
                    item["x"] += item["speed"] * elapsed
                    if item["x"] > width or item["x"] + len(item["art"]) < 0:
                        item.update(new_fish(width, height))
            draw(fish, seaweed, paused)
            time.sleep(0.04)
    finally:
        sys.stdout.write("\x1b[0m\x1b[?25h\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
