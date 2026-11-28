from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment
from rich.theme import Theme


THEME = Theme({
    "markdown.h1": "bold bright_cyan",
    "markdown.h2": "bold bright_cyan",
    "markdown.h3": "bold",
    "markdown.item.bullet": "#78c9d5",
    "markdown.item.number": "#78c9d5",
    "markdown.table.border": "default",
    "markdown.table.header": "bold #8fe3ed",
    "markdown.code": "#8fe3ed",
    "markdown.link": "#83ccff underline",
})
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def link_target(link: str | None) -> str | None:
    if not link or not link.isprintable():
        return None
    if link.startswith(("https://", "http://")):
        return link
    if link.startswith("/") and not link.startswith("//"):
        link = unquote(link)
        path, _, line = link.rpartition(":")
        local = Path(path if line.isdecimal() else link)
        if local.is_absolute():
            return local.as_uri()
    return None


def markdown_fragments(answer: str, width: int, *, hyperlinks: bool) -> list[tuple[str, str]]:
    source = CONTROLS.sub("", answer.replace("\r\n", "\n"))
    console = Console(width=max(1, min(width, 96)), theme=THEME)
    fragments = []
    for segment in Segment.simplify(console.render(Markdown(source))):
        if segment.control:
            continue
        style, rendered = segment.style, ""
        link = None
        if style:
            attrs = [attr for attr in ("bold", "dim", "italic", "underline", "strike", "reverse") if getattr(style, attr)]
            colors = ((style.color, ""), (style.bgcolor, "bg:"))
            for color, prefix in colors:
                if color and not color.is_default:
                    attrs.append(prefix + color.get_truecolor().hex)
            if style.link:
                attrs.extend(("#83ccff", "underline"))
            rendered = " ".join(attrs)
            link = link_target(style.link) if hyperlinks else None
        if link:
            fragments.append(("[ZeroWidthEscape]", f"\x1b]8;;{link}\x1b\\"))
        fragments.append((rendered, CONTROLS.sub("", segment.text)))
        if link:
            fragments.append(("[ZeroWidthEscape]", "\x1b]8;;\x1b\\"))
    return fragments
