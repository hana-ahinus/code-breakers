"""A dependency-free Python implementation of the classic cowsay program.

The module offers :func:`say`, :func:`think`, and :func:`list_cows`; run this
file directly for the command-line interface.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


DEFAULT_EYES = "oo"
DEFAULT_TONGUE = "  "
DEFAULT_WIDTH = 40
COW_DIRECTORY = Path(__file__).with_name("cows")

FACES: dict[str, tuple[str, str]] = {
    "b": ("==", "  "),  # borg
    "d": ("xx", "U "),  # dead
    "g": ("$$", "  "),  # greedy
    "p": ("@@", "  "),  # paranoid
    "s": ("**", "U "),  # stoned
    "t": ("--", "  "),  # tired
    "w": ("OO", "  "),  # wired
    "y": ("..", "  "),  # youthful
}


def list_cows() -> list[str]:
    """Return bundled cow names, without their ``.cow`` suffixes."""
    return sorted(path.stem for path in COW_DIRECTORY.glob("*.cow"))


def _display_width(value: str) -> int:
    """Approximate terminal-cell width without a third-party dependency."""
    width = 0
    for character in value:
        if character in "\n\r" or unicodedata.combining(character):
            continue
        if unicodedata.category(character).startswith("C"):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def _split_long_word(word: str, width: int) -> list[str]:
    """Split a word by display columns, keeping every character intact."""
    if width <= 0:
        return [word]
    pieces: list[str] = []
    current = ""
    current_width = 0
    for character in word:
        character_width = _display_width(character)
        if current and current_width + character_width > width:
            pieces.append(current)
            current = ""
            current_width = 0
        current += character
        current_width += character_width
    if current or not pieces:
        pieces.append(current)
    return pieces


def _wrap_line(line: str, width: int | None) -> list[str]:
    if width is None or width <= 0:
        return [line]
    if not line:
        return [""]

    words = re.findall(r"\S+", line)
    if not words:
        return [""]

    result: list[str] = []
    current = ""
    for word in words:
        if _display_width(word) > width:
            if current:
                result.append(current)
                current = ""
            result.extend(_split_long_word(word, width))
            continue

        candidate = word if not current else f"{current} {word}"
        if current and _display_width(candidate) > width:
            result.append(current)
            current = word
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _message_lines(message: str, width: int | None) -> list[str]:
    lines: list[str] = []
    for line in message.split("\n"):
        lines.extend(_wrap_line(line, width))
    return lines or [""]


def _balloon(message: str, width: int | None, thinking: bool) -> str:
    lines = _message_lines(message, width)
    longest = max(_display_width(line) for line in lines)
    horizontal = " " + "_" * (longest + 2)
    bottom = " " + "-" * (longest + 2)
    if thinking:
        delimiters = (("(", ")"), ("(", ")"), ("(", ")"))
    else:
        delimiters = (("/", "\\"), ("|", "|"), ("\\", "/"))

    rendered = [horizontal]
    for index, line in enumerate(lines):
        if len(lines) == 1:
            left, right = ("(", ")") if thinking else ("<", ">")
        elif index == 0:
            left, right = delimiters[0]
        elif index == len(lines) - 1:
            left, right = delimiters[2]
        else:
            left, right = delimiters[1]
        padding = " " * (longest - _display_width(line))
        rendered.append(f"{left} {line}{padding} {right}")
    rendered.append(bottom)
    return "\n".join(rendered)


def _cow_path(name: str | Path) -> Path:
    requested = Path(name)
    if requested.is_file():
        return requested
    filename = requested.name
    if not filename.endswith(".cow"):
        filename += ".cow"
    bundled = COW_DIRECTORY / filename
    if bundled.is_file():
        return bundled
    raise FileNotFoundError(f"Cow file not found: {name}")


def _cow_template(name: str | Path) -> str:
    source = _cow_path(name).read_text(encoding="utf-8")
    match = re.search(
        r"\$the_cow\s*(?:=\s*)?<<\s*\"?([A-Za-z0-9_]+)\"?\s*;?\s*\r?\n(.*?)\r?\n\1\s*;?\s*$",
        source,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError(f"Invalid cow file: {name}")
    return match.group(2)


def _render_cow(name: str | Path, *, eyes: str, tongue: str, thoughts: str) -> str:
    template = _cow_template(name)
    substitutions = {
        "thoughts": thoughts,
        "eyes": eyes,
        "eye": eyes[:1],
        "tongue": tongue,
    }

    def substitute(match: re.Match[str]) -> str:
        return substitutions.get(match.group(1) or match.group(2), match.group(0))

    rendered = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, template)
    # Cowfiles are Perl double-quoted here-documents.  These are the escapes
    # used by the bundled artwork to preserve literal backslashes, dollars,
    # and at-signs in the rendered figure.
    return rendered.replace("\\\\", "\\").replace("\\$", "$").replace("\\@", "@")


def _normalise_options(
    options: Mapping[str, Any] | str | None, extra: Mapping[str, Any]
) -> dict[str, Any]:
    if options is None:
        resolved: dict[str, Any] = {}
    elif isinstance(options, str):
        resolved = {"text": options}
    elif isinstance(options, Mapping):
        resolved = dict(options)
    else:
        raise TypeError("options must be a mapping, a message string, or None")
    resolved.update(extra)
    return resolved


def _face(options: Mapping[str, Any]) -> tuple[str, str]:
    eyes = str(options.get("e", DEFAULT_EYES))
    tongue = str(options.get("T", DEFAULT_TONGUE))
    # Match the original flags' deterministic precedence when more than one
    # mode is requested.
    for mode, mode_face in FACES.items():
        if options.get(mode):
            eyes, tongue = mode_face
    return eyes, tongue


def _say_or_think(
    options: Mapping[str, Any] | str | None,
    *,
    thinking: bool,
    **extra: Any,
) -> str:
    values = _normalise_options(options, extra)
    message = values.get("text")
    if message is None:
        remaining = values.get("_") or []
        message = " ".join(map(str, remaining))
    message = str(message)

    cow_name: str | Path
    if values.get("r"):
        cow_name = random.choice(list_cows())
    else:
        cow_name = values.get("f") or "default"
    eyes, tongue = _face(values)
    return _balloon(message, None if values.get("n") else values.get("W", DEFAULT_WIDTH), thinking) + "\n" + _render_cow(
        cow_name, eyes=eyes, tongue=tongue, thoughts="o" if thinking else "\\"
    )


def say(options: Mapping[str, Any] | str | None = None, /, **kwargs: Any) -> str:
    """Render a speaking cow.

    ``options`` uses the original CLI names: ``text``, ``e``, ``T``, ``f``,
    ``r``, ``n``, ``W``, and the face-mode flags ``b`` through ``y``.
    """
    return _say_or_think(options, thinking=False, **kwargs)


def think(options: Mapping[str, Any] | str | None = None, /, **kwargs: Any) -> str:
    """Render a thinking cow; accepts the same options as :func:`say`."""
    return _say_or_think(options, thinking=True, **kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        usage="%(prog)s [-e eye_string] [-f cowfile] [-h] [-l] [-n] [-T tongue_string] [-W column] [-bdgpstwy] text",
        description="Configurable talking cow.",
    )
    parser.add_argument("-e", default=DEFAULT_EYES, metavar="EYES", help="cow eye string (default: oo)")
    parser.add_argument("-T", default=DEFAULT_TONGUE, metavar="TONGUE", help="cow tongue string")
    parser.add_argument("-W", default=DEFAULT_WIDTH, type=int, metavar="COLUMN", help="wrap column (default: 40)")
    parser.add_argument("-f", default="default", metavar="COWFILE", help="bundled cow name or cowfile path")
    parser.add_argument("-r", action="store_true", help="choose a random cow")
    parser.add_argument("-l", action="store_true", help="list bundled cow names")
    parser.add_argument("-n", action="store_true", help="do not wrap the message")
    parser.add_argument("--think", action="store_true", help="make the cow think")
    for mode, description in (
        ("b", "borg"), ("d", "dead"), ("g", "greedy"), ("p", "paranoid"),
        ("s", "stoned"), ("t", "tired"), ("w", "wired"), ("y", "youthful"),
    ):
        parser.add_argument(f"-{mode}", action="store_true", help=f"mode: {description}")
    parser.add_argument("text", nargs="*", help="message to display")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(argv)
    values = vars(namespace)
    if values.pop("l"):
        print("  ".join(list_cows()))
        return 0

    text = values.pop("text")
    if text:
        values["text"] = " ".join(text)
    elif not sys.stdin.isatty():
        values["text"] = sys.stdin.read().removesuffix("\n").removesuffix("\r")
    else:
        parser.print_help()
        return 0

    thinking = values.pop("think") or Path(sys.argv[0]).stem.endswith("cowthink")
    try:
        print(think(values) if thinking else say(values))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
