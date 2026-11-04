"""Dynamic hockey-rink lineup rendering for the /team command."""

from __future__ import annotations

import base64
import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from xml.sax.saxutils import escape

from database import Player
from commands.common import resolve_card_image

logger = logging.getLogger(__name__)

IMAGE_WIDTH = 1600
IMAGE_HEIGHT = 1810
OUTPUT_FILENAME = "thsl-lineup.png"


@dataclass(frozen=True)
class SlotLayout:
    position: str
    label: str
    x: int
    y: int
    width: int
    height: int
    accent: str


SLOT_LAYOUTS = (
    SlotLayout("LW", "WING 1 · LW", 90, 920, 440, 360, "#59d4a1"),
    SlotLayout("RW", "WING 2 · RW", 1070, 920, 440, 360, "#59d4a1"),
    SlotLayout("GK", "GOALIE · GK", 580, 920, 440, 360, "#80d9ff"),
    SlotLayout("SUB1", "SUB 1", 325, 1375, 430, 360, "#c7d6e3"),
    SlotLayout("SUB2", "SUB 2", 845, 1375, 430, 360, "#c7d6e3"),
)


class LineupImageError(RuntimeError):
    """Raised when the image renderer cannot create the final PNG."""


@lru_cache(maxsize=64)
def _card_png_data_uri(path_string: str, mtime_ns: int) -> str | None:
    del mtime_ns  # Included in the cache key so replacing an asset refreshes it.
    try:
        converted = subprocess.run(
            [
                "magick",
                path_string,
                "-auto-orient",
                "-strip",
                "-resize",
                "320x420",
                "png:-",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Unable to prepare lineup card image %s: %s", path_string, exc)
        return None
    return "data:image/png;base64," + base64.b64encode(converted.stdout).decode("ascii")


def _player_image_uri(player: Player) -> str | None:
    image_path = resolve_card_image(player)
    if image_path is None:
        return None
    try:
        modified = image_path.stat().st_mtime_ns
    except OSError:
        return None
    return _card_png_data_uri(str(image_path), modified)


def _slot_markup(layout: SlotLayout, player: Player | None) -> str:
    x, y, width, height = layout.x, layout.y, layout.width, layout.height
    image_x = x + 22
    image_y = y + 82
    image_width = width - 44
    image_height = height - 105
    common = (
        f'<g id="slot-{layout.position.lower()}">'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="24" '
        f'fill="#0b1714" stroke="{layout.accent}" stroke-width="3"/>'
        f'<text x="{x + width // 2}" y="{y + 42}" class="slot-title" '
        f'text-anchor="middle">{escape(layout.label)}</text>'
    )

    if player is None:
        radius = min(image_width, image_height) // 5
        center_x = x + width // 2
        center_y = image_y + image_height // 2 - 12
        return (
            common
            + f'<circle cx="{center_x}" cy="{center_y}" r="{radius}" '
            'fill="none" stroke="#edf7f5" stroke-width="6"/>'
            f'<text x="{center_x}" y="{center_y + radius + 35}" '
            'class="empty-label" text-anchor="middle">EMPTY</text>'
            "</g>"
        )

    label = escape(f"{player.name} · {player.overall} OVR")
    markup = (
        common
        + f'<text x="{x + width // 2}" y="{y + 72}" class="player-label" '
        f'text-anchor="middle">{label}</text>'
    )
    image_uri = _player_image_uri(player)
    if image_uri is None:
        radius = min(image_width, image_height) // 5
        center_x = x + width // 2
        center_y = image_y + image_height // 2 - 12
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="{radius}" '
            'fill="none" stroke="#edf7f5" stroke-width="6"/>'
            f'<text x="{center_x}" y="{center_y + radius + 30}" '
            'class="empty-label" text-anchor="middle">IMAGE UNAVAILABLE</text>'
        )
    else:
        markup += (
            f'<image href="{image_uri}" x="{image_x}" y="{image_y}" '
            f'width="{image_width}" height="{image_height}" '
            'preserveAspectRatio="xMidYMid meet"/>'
        )
    return markup + "</g>"


def build_lineup_svg(
    lineup: dict[str, Player],
    formation: str,
    overall_text: str,
) -> str:
    """Build the accessible SVG source used to render a lineup PNG."""
    plain_overall = overall_text.replace("**", "")
    subtitle = escape(f"Formation: {formation}  ·  Starting lineup: {plain_overall}")
    slot_markup = "".join(
        _slot_markup(layout, lineup.get(layout.position)) for layout in SLOT_LAYOUTS
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:xlink="http://www.w3.org/1999/xlink"
        width="{IMAGE_WIDTH}" height="{IMAGE_HEIGHT}"
        viewBox="0 0 {IMAGE_WIDTH} {IMAGE_HEIGHT}">
      <defs>
        <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#101b23"/>
          <stop offset="1" stop-color="#07110f"/>
        </linearGradient>
        <linearGradient id="rink" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#f7fafc"/>
          <stop offset="1" stop-color="#e5edf2"/>
        </linearGradient>
        <clipPath id="ice-boundary">
          <rect x="78" y="165" width="1444" height="630" rx="72"/>
        </clipPath>
        <style>
          text {{ font-family: 'DejaVu Sans', Arial, sans-serif; }}
          .title {{ fill: #f5fbff; font-size: 43px; font-weight: 700; letter-spacing: 1px; }}
          .subtitle {{ fill: #a9bfca; font-size: 21px; }}
          .badge {{ fill: #e5f5ff; font-size: 22px; font-weight: 700; }}
          .slot-title {{ fill: #f1f8fa; font-size: 21px; font-weight: 700; }}
          .player-label {{ fill: #c5d8df; font-size: 16px; font-weight: 600; }}
          .empty-label {{ fill: #91a9a6; font-size: 16px; font-weight: 700; letter-spacing: 1px; }}
          .section {{ fill: #f2f7fa; font-size: 25px; font-weight: 700; letter-spacing: 2px; }}
        </style>
      </defs>
      <rect width="100%" height="100%" fill="url(#background)"/>
      <text x="66" y="69" class="title">THSL STARTING LINEUP</text>
      <text x="68" y="111" class="subtitle">{subtitle}</text>
      <rect x="1280" y="33" width="260" height="54" rx="27"
        fill="#183b4a" stroke="#7ed4f5" stroke-width="2"/>
      <text x="1410" y="68" class="badge" text-anchor="middle">2 Wings + 1 GK</text>

      <rect x="58" y="145" width="1484" height="670" rx="90"
        fill="#f8fbfc" stroke="#eef3f6" stroke-width="8"/>
      <rect x="76" y="163" width="1448" height="634" rx="76"
        fill="url(#rink)" stroke="#c6d4dc" stroke-width="3"/>
      <g clip-path="url(#ice-boundary)">
        <line x1="800" y1="165" x2="800" y2="795"
          stroke="#d52b3e" stroke-width="7"/>
        <line x1="485" y1="165" x2="485" y2="795"
          stroke="#2867a6" stroke-width="8"/>
        <line x1="1115" y1="165" x2="1115" y2="795"
          stroke="#2867a6" stroke-width="8"/>
        <path d="M 152 392 A 115 115 0 0 1 152 622 L 152 392 Z"
          fill="#e83d50" fill-opacity="0.12" stroke="#d52b3e" stroke-width="5"/>
        <path d="M 1448 392 A 115 115 0 0 0 1448 622 L 1448 392 Z"
          fill="#e83d50" fill-opacity="0.12" stroke="#d52b3e" stroke-width="5"/>
        <line x1="152" y1="165" x2="152" y2="795"
          stroke="#d52b3e" stroke-width="6"/>
        <line x1="1448" y1="165" x2="1448" y2="795"
          stroke="#d52b3e" stroke-width="6"/>
        <circle cx="800" cy="480" r="112" fill="none"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="800" cy="480" r="10" fill="#d52b3e"/>
        <circle cx="360" cy="330" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="1240" cy="330" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="360" cy="630" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="1240" cy="630" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="360" cy="330" r="8" fill="#d52b3e"/>
        <circle cx="1240" cy="330" r="8" fill="#d52b3e"/>
        <circle cx="360" cy="630" r="8" fill="#d52b3e"/>
        <circle cx="1240" cy="630" r="8" fill="#d52b3e"/>
        <circle cx="610" cy="330" r="8" fill="#d52b3e"/>
        <circle cx="990" cy="330" r="8" fill="#d52b3e"/>
        <circle cx="610" cy="630" r="8" fill="#d52b3e"/>
        <circle cx="990" cy="630" r="8" fill="#d52b3e"/>
      </g>
      <rect x="84" y="468" width="68" height="75" rx="8"
        fill="none" stroke="#d52b3e" stroke-width="5"/>
      <rect x="1448" y="468" width="68" height="75" rx="8"
        fill="none" stroke="#d52b3e" stroke-width="5"/>

      <text x="800" y="865" class="section" text-anchor="middle">STARTING LINEUP</text>
      <text x="800" y="895" class="subtitle" text-anchor="middle">
        The three starters determine the lineup rating
      </text>

      {slot_markup}

      <text x="800" y="1320" class="section" text-anchor="middle">SUBSTITUTES</text>
      <text x="800" y="1350" class="subtitle" text-anchor="middle">
        Bench cards do not change the starting-lineup rating
      </text>
    </svg>"""


def render_lineup_image(
    lineup: dict[str, Player],
    formation: str,
    overall_text: str,
) -> bytes:
    """Render the lineup SVG to a Discord-ready PNG using ImageMagick."""
    svg = build_lineup_svg(lineup, formation, overall_text)
    try:
        result = subprocess.run(
            ["magick", "svg:-", "-strip", "png:-"],
            input=svg.encode("utf-8"),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.exception("Failed to render THSL lineup image")
        raise LineupImageError(
            "The lineup image could not be rendered. Please try again shortly."
        ) from exc
    if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise LineupImageError("The lineup renderer did not produce a PNG image.")
    return result.stdout
