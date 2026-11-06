"""Dynamic hockey-rink lineup rendering for the /team command."""

from __future__ import annotations

import base64
import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from xml.sax.saxutils import escape

from database import FORMATION_STARTERS, Player
from commands.common import resolve_card_image

logger = logging.getLogger(__name__)

IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 1980
OUTPUT_FILENAME = "thsl-lineup.png"

RINK_LEFT = 145
RINK_TOP = 125
RINK_RIGHT = 1055
RINK_BOTTOM = 1545
STARTER_CARD_WIDTH = 190
STARTER_CARD_HEIGHT = 260
STARTER_SLOT_HEIGHT = 320


@dataclass(frozen=True)
class SlotLayout:
    position: str
    label: str
    x: int
    y: int
    width: int
    height: int
    accent: str


FORMATION_SLOT_LAYOUTS = {
    "Balanced": {
        "WING1": SlotLayout(
            "WING1", "LEFT WING", 165, 270, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
        "CM1": SlotLayout(
            "CM1", "CENTER", 505, 650, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
        "GK": SlotLayout(
            "GK", "GOALIE", 505, 1065, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
    },
    "Offensive": {
        "WING1": SlotLayout(
            "WING1", "LEFT WING", 165, 270, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
        "WING2": SlotLayout(
            "WING2", "RIGHT WING", 845, 270, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
        "GK": SlotLayout(
            "GK", "GOALIE", 505, 1065, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
    },
    "Defensive": {
        "CM1": SlotLayout(
            "CM1", "CENTER 1", 375, 650, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
        "CM2": SlotLayout(
            "CM2", "CENTER 2", 635, 650, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
        "GK": SlotLayout(
            "GK", "GOALIE", 505, 1065, STARTER_CARD_WIDTH,
            STARTER_SLOT_HEIGHT, "#b23a48",
        ),
    },
}

SUBSTITUTE_SLOT_LAYOUTS = (
    SlotLayout("SUB1", "SUBSTITUTE 1", 230, 1650, 320, 300, "#607d8b"),
    SlotLayout("SUB2", "SUBSTITUTE 2", 650, 1650, 320, 300, "#607d8b"),
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


def _short_label(value: str, limit: int = 19) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _starter_slot_markup(layout: SlotLayout, player: Player | None) -> str:
    x, y, width, height = layout.x, layout.y, layout.width, layout.height
    center_x = x + width // 2
    image_y = y
    image_height = STARTER_CARD_HEIGHT
    markup = (
        f'<g id="slot-{layout.position.lower()}">'
        f'<text x="{center_x}" y="{y - 16}" class="position-label" '
        f'text-anchor="middle">{escape(layout.label)}</text>'
    )
    if player is None:
        center_y = y + image_height // 2
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="58" '
            'fill="#fafdff" fill-opacity="0.76" '
            f'stroke="{layout.accent}" stroke-width="5" stroke-dasharray="12 10"/>'
            f'<circle cx="{center_x}" cy="{center_y}" r="8" '
            f'fill="{layout.accent}" fill-opacity="0.7"/>'
            f'<text x="{center_x}" y="{y + 294}" class="empty-label" '
            'text-anchor="middle">OPEN POSITION</text>'
        )
        return markup + "</g>"

    image_uri = _player_image_uri(player)
    if image_uri is None:
        center_y = image_y + image_height // 2
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="58" '
            'fill="#fafdff" fill-opacity="0.82" '
            f'stroke="{layout.accent}" stroke-width="5"/>'
            f'<text x="{center_x}" y="{center_y + 6}" class="empty-label" '
            'text-anchor="middle">ART UNAVAILABLE</text>'
        )
    else:
        markup += (
            f'<image href="{image_uri}" x="{x}" y="{image_y}" '
            f'width="{width}" height="{image_height}" '
            'preserveAspectRatio="xMidYMid meet"/>'
        )

    markup += (
        f'<rect x="{x - 8}" y="{y + image_height + 5}" '
        f'width="{width + 16}" height="52" rx="12" '
        'fill="#102b3a" fill-opacity="0.92" '
        'stroke="#ffffff" stroke-opacity="0.55" stroke-width="2"/>'
        f'<text x="{center_x}" y="{y + image_height + 26}" '
        'class="starter-name" text-anchor="middle">'
        f'{escape(_short_label(player.name))}</text>'
        f'<text x="{center_x}" y="{y + image_height + 46}" '
        'class="starter-meta" text-anchor="middle">'
        f'{player.overall} OVR · {escape(player.rarity.upper())}</text>'
    )
    return markup + "</g>"


def _substitute_slot_markup(layout: SlotLayout, player: Player | None) -> str:
    x, y, width, height = layout.x, layout.y, layout.width, layout.height
    center_x = x + width // 2
    markup = (
        f'<g id="slot-{layout.position.lower()}">'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="24" '
        'fill="#102b3a" fill-opacity="0.96" stroke="#b9d4df" stroke-width="3"/>'
        f'<text x="{center_x}" y="{y + 30}" class="sub-position" '
        f'text-anchor="middle">{escape(layout.label)}</text>'
    )
    if player is None:
        center_y = y + 154
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="54" fill="none" '
            'stroke="#b8d3df" stroke-width="5" stroke-dasharray="12 10"/>'
            f'<text x="{center_x}" y="{center_y + 88}" class="empty-label" '
            'text-anchor="middle">EMPTY</text>'
        )
        return markup + "</g>"

    image_uri = _player_image_uri(player)
    image_width, image_height = 145, 200
    image_x, image_y = x + (width - image_width) // 2, y + 42
    if image_uri is None:
        center_y = image_y + image_height // 2
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="48" fill="none" '
            'stroke="#b8d3df" stroke-width="5"/>'
            f'<text x="{center_x}" y="{center_y + 6}" class="empty-label" '
            'text-anchor="middle">NO ART</text>'
        )
    else:
        markup += (
            f'<image href="{image_uri}" x="{image_x}" y="{image_y}" '
            f'width="{image_width}" height="{image_height}" '
            'preserveAspectRatio="xMidYMid meet"/>'
        )
    markup += (
        f'<text x="{center_x}" y="{y + 270}" class="sub-name" '
        f'text-anchor="middle">{escape(_short_label(player.name, 24))}</text>'
        f'<text x="{center_x}" y="{y + 292}" class="sub-meta" '
        'text-anchor="middle">'
        f'{player.overall} OVR · {escape(player.rarity.upper())}</text>'
    )
    return markup + "</g>"


def build_lineup_svg(
    lineup: dict[str, Player],
    formation: str,
    overall_text: str,
    value_text: str = "0 coins",
) -> str:
    """Build the accessible SVG source used to render a lineup PNG."""
    if formation not in FORMATION_STARTERS:
        raise ValueError(f"Unsupported formation: {formation}")
    plain_overall = overall_text.replace("**", "")
    subtitle = escape(
        f"Formation: {formation}  ·  Starting lineup: {plain_overall}  ·  "
        f"Team value: {value_text}"
    )
    starter_markup = "".join(
        _starter_slot_markup(FORMATION_SLOT_LAYOUTS[formation][position], lineup.get(position))
        for position in FORMATION_STARTERS[formation]
    )
    substitute_markup = "".join(
        _substitute_slot_markup(layout, lineup.get(layout.position))
        for layout in SUBSTITUTE_SLOT_LAYOUTS
    )
    slot_markup = starter_markup + substitute_markup
    formation_badge = escape(f"{formation.upper()} · 3 STARTERS")
    return f"""<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:xlink="http://www.w3.org/1999/xlink"
        width="{IMAGE_WIDTH}" height="{IMAGE_HEIGHT}"
        viewBox="0 0 {IMAGE_WIDTH} {IMAGE_HEIGHT}">
      <defs>
        <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#0b1c28"/>
          <stop offset="1" stop-color="#102b39"/>
        </linearGradient>
        <linearGradient id="ice" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#ffffff"/>
          <stop offset="0.52" stop-color="#f7fbfd"/>
          <stop offset="1" stop-color="#edf5f9"/>
        </linearGradient>
        <pattern id="ice-grain" width="34" height="34" patternUnits="userSpaceOnUse">
          <path d="M0 33.5H34" stroke="#c9dce6" stroke-opacity="0.16" stroke-width="1"/>
          <circle cx="8" cy="10" r="1" fill="#a8c4d2" fill-opacity="0.14"/>
          <circle cx="26" cy="25" r="1" fill="#a8c4d2" fill-opacity="0.1"/>
        </pattern>
        <clipPath id="ice-boundary">
          <rect x="{RINK_LEFT}" y="{RINK_TOP}" width="{RINK_RIGHT - RINK_LEFT}"
            height="{RINK_BOTTOM - RINK_TOP}" rx="160"/>
        </clipPath>
        <style>
          text {{ font-family: 'DejaVu Sans', Arial, sans-serif; }}
          .title {{ fill: #f5fbff; font-size: 39px; font-weight: 700; letter-spacing: 1.5px; }}
          .subtitle {{ fill: #bdd0d9; font-size: 18px; }}
          .badge {{ fill: #e5f5ff; font-size: 20px; font-weight: 700; }}
          .position-label {{ fill: #234251; font-size: 17px; font-weight: 800; letter-spacing: 1.4px; }}
          .starter-name {{ fill: #ffffff; font-size: 16px; font-weight: 700; }}
          .starter-meta {{ fill: #d1e2e9; font-size: 15px; font-weight: 700; }}
          .sub-position {{ fill: #eaf6fb; font-size: 19px; font-weight: 800; letter-spacing: 1.2px; }}
          .sub-name {{ fill: #ffffff; font-size: 18px; font-weight: 700; }}
          .sub-meta {{ fill: #c5d9e1; font-size: 16px; font-weight: 600; }}
          .empty-label {{ fill: #597681; font-size: 15px; font-weight: 800; letter-spacing: 1px; }}
          .section {{ fill: #f2f7fa; font-size: 25px; font-weight: 800; letter-spacing: 2px; }}
          .ice-line {{ fill: none; stroke: #d52b3e; stroke-width: 5; }}
        </style>
      </defs>
      <rect width="100%" height="100%" fill="url(#background)"/>
      <text x="600" y="57" class="title" text-anchor="middle">THSL STARTING LINEUP</text>
      <text x="600" y="91" class="subtitle" text-anchor="middle">{subtitle}</text>
      <rect x="420" y="103" width="360" height="40" rx="20"
        fill="#173b4b" stroke="#75c9e8" stroke-width="2"/>
      <text x="600" y="130" class="badge" text-anchor="middle">{formation_badge}</text>

      <rect x="130" y="110" width="940" height="1460" rx="180"
        fill="#eef4f7" stroke="#d8e5eb" stroke-width="7"/>
      <rect x="{RINK_LEFT}" y="{RINK_TOP}" width="{RINK_RIGHT - RINK_LEFT}"
        height="{RINK_BOTTOM - RINK_TOP}" rx="160"
        fill="url(#ice)" stroke="#367fa8" stroke-width="4"/>
      <g clip-path="url(#ice-boundary)">
        <rect x="{RINK_LEFT}" y="{RINK_TOP}" width="{RINK_RIGHT - RINK_LEFT}"
          height="{RINK_BOTTOM - RINK_TOP}" fill="url(#ice-grain)"/>

        <line x1="155" y1="610" x2="1045" y2="610"
          stroke="#2867a6" stroke-width="8"/>
        <line x1="155" y1="1020" x2="1045" y2="1020"
          stroke="#2867a6" stroke-width="8"/>
        <line x1="155" y1="815" x2="1045" y2="815"
          stroke="#d52b3e" stroke-width="7"/>
        <line x1="155" y1="230" x2="1045" y2="230"
          stroke="#d52b3e" stroke-width="6"/>
        <line x1="155" y1="1475" x2="1045" y2="1475"
          stroke="#d52b3e" stroke-width="6"/>

        <circle cx="600" cy="815" r="115" fill="#ffffff" fill-opacity="0.18"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="600" cy="815" r="10" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>

        <circle cx="410" cy="520" r="58" fill="none"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="790" cy="520" r="58" fill="none"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="410" cy="1220" r="58" fill="none"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="790" cy="1220" r="58" fill="none"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="410" cy="520" r="9" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>
        <circle cx="790" cy="520" r="9" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>
        <circle cx="410" cy="1220" r="9" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>
        <circle cx="790" cy="1220" r="9" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>

        <circle cx="470" cy="610" r="8" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>
        <circle cx="730" cy="610" r="8" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>
        <circle cx="470" cy="1030" r="8" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>
        <circle cx="730" cy="1030" r="8" fill="#d52b3e" stroke="#ffffff" stroke-width="3"/>

        <path d="M 510 230 A 90 90 0 0 1 690 230"
          fill="#74bce3" fill-opacity="0.12" stroke="#d52b3e" stroke-width="5"/>
        <path d="M 510 1475 A 90 90 0 0 0 690 1475"
          fill="#74bce3" fill-opacity="0.12" stroke="#d52b3e" stroke-width="5"/>
      </g>

      <rect x="555" y="135" width="90" height="95" rx="10"
        fill="#ffffff" fill-opacity="0.28" stroke="#d52b3e" stroke-width="5"/>
      <line x1="555" y1="160" x2="645" y2="160" stroke="#d52b3e" stroke-width="4"/>
      <rect x="555" y="1475" width="90" height="58" rx="8"
        fill="#ffffff" fill-opacity="0.3" stroke="#d52b3e" stroke-width="5"/>
      <line x1="555" y1="1490" x2="645" y2="1490" stroke="#d52b3e" stroke-width="4"/>

      {slot_markup}

      <text x="600" y="1600" class="section" text-anchor="middle">SUBSTITUTES</text>
      <text x="600" y="1632" class="subtitle" text-anchor="middle">
        Two bench positions · available in every formation
      </text>
    </svg>"""


def render_lineup_image(
    lineup: dict[str, Player],
    formation: str,
    overall_text: str,
    value_text: str = "0 coins",
) -> bytes:
    """Render the lineup SVG to a Discord-ready PNG using ImageMagick."""
    svg = build_lineup_svg(lineup, formation, overall_text, value_text)
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
