"""Dynamic hockey-rink lineup rendering for the /team command."""

from __future__ import annotations

import base64
import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from xml.sax.saxutils import escape

from database import FORMATION_STARTERS, Player
from commands.common import player_card_label, resolve_card_image

logger = logging.getLogger(__name__)

IMAGE_WIDTH = 1600
IMAGE_HEIGHT = 1640
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


STARTER_SLOT_LAYOUTS = {
    "WING1": SlotLayout("WING1", "WING 1", 172, 835, 298, 170, "#b23a48"),
    "WING2": SlotLayout("WING2", "WING 2", 500, 835, 285, 170, "#b23a48"),
    "CM1": SlotLayout("CM1", "CM 1", 500, 835, 285, 170, "#b23a48"),
    "CM2": SlotLayout("CM2", "CM 2", 500, 835, 285, 170, "#b23a48"),
    "GK": SlotLayout("GK", "GOALIE", 815, 835, 285, 170, "#2867a6"),
}

SUBSTITUTE_SLOT_LAYOUTS = (
    SlotLayout("SUB1", "SUBSTITUTE 1", 300, 1215, 430, 350, "#607d8b"),
    SlotLayout("SUB2", "SUBSTITUTE 2", 870, 1215, 430, 350, "#607d8b"),
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


def _starter_slot_markup(layout: SlotLayout, player: Player | None) -> str:
    x, y, width, height = layout.x, layout.y, layout.width, layout.height
    markup = (
        f'<g id="slot-{layout.position.lower()}">'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="13" '
        'fill="#f9fcfd" fill-opacity="0.97" '
        f'stroke="{layout.accent}" stroke-width="3"/>'
        f'<text x="{x + width // 2}" y="{y + 24}" class="slot-title" '
        f'text-anchor="middle">{escape(layout.label)}</text>'
    )
    if player is None:
        center_x = x + width // 2
        center_y = y + 92
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="38" fill="none" '
            'stroke="#8b9ba3" stroke-width="4"/>'
            f'<text x="{center_x}" y="{y + 151}" class="empty-label" '
            'text-anchor="middle">EMPTY</text>'
        )
        return markup + "</g>"

    image_uri = _player_image_uri(player)
    image_x, image_y = x + 12, y + 38
    image_width, image_height = 82, 118
    if image_uri is None:
        markup += (
            f'<circle cx="{image_x + image_width // 2}" '
            f'cy="{image_y + image_height // 2}" r="32" fill="none" '
            'stroke="#8b9ba3" stroke-width="4"/>'
            f'<text x="{image_x + image_width // 2}" y="{image_y + image_height + 15}" '
            'class="empty-label" text-anchor="middle">NO ART</text>'
        )
    else:
        markup += (
            f'<image href="{image_uri}" x="{image_x}" y="{image_y}" '
            f'width="{image_width}" height="{image_height}" '
            'preserveAspectRatio="xMidYMid meet"/>'
        )

    display_name = player.name if len(player.name) <= 17 else player.name[:16] + "…"
    markup += (
        f'<text x="{x + 108}" y="{y + 86}" class="starter-name">'
        f'{escape(display_name)}</text>'
        f'<text x="{x + 108}" y="{y + 117}" class="starter-meta">'
        f'{player.overall} OVR · {escape(player.rarity.upper())}</text>'
    )
    return markup + "</g>"


def _substitute_slot_markup(layout: SlotLayout, player: Player | None) -> str:
    x, y, width, height = layout.x, layout.y, layout.width, layout.height
    markup = (
        f'<g id="slot-{layout.position.lower()}">'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="22" '
        'fill="#f9fcfd" stroke="#c7d6e3" stroke-width="3"/>'
        f'<text x="{x + width // 2}" y="{y + 42}" class="slot-title-dark" '
        f'text-anchor="middle">{escape(layout.label)}</text>'
    )
    if player is None:
        center_x, center_y = x + width // 2, y + 190
        markup += (
            f'<circle cx="{center_x}" cy="{center_y}" r="68" fill="none" '
            'stroke="#8b9ba3" stroke-width="6"/>'
            f'<text x="{center_x}" y="{center_y + 105}" class="empty-label" '
            'text-anchor="middle">EMPTY</text>'
        )
        return markup + "</g>"

    image_uri = _player_image_uri(player)
    image_x, image_y = x + 24, y + 72
    image_width, image_height = 160, 252
    if image_uri is None:
        markup += (
            f'<circle cx="{image_x + image_width // 2}" '
            f'cy="{image_y + image_height // 2}" r="56" fill="none" '
            'stroke="#8b9ba3" stroke-width="5"/>'
            f'<text x="{image_x + image_width // 2}" y="{image_y + image_height + 23}" '
            'class="empty-label" text-anchor="middle">IMAGE UNAVAILABLE</text>'
        )
    else:
        markup += (
            f'<image href="{image_uri}" x="{image_x}" y="{image_y}" '
            f'width="{image_width}" height="{image_height}" '
            'preserveAspectRatio="xMidYMid meet"/>'
        )
    markup += (
        f'<text x="{x + 210}" y="{y + 150}" class="sub-name">'
        f'{escape(player.name[:24])}</text>'
        f'<text x="{x + 210}" y="{y + 190}" class="sub-meta">'
        f'{player.overall} OVR · {escape(player.rarity.upper())}</text>'
        f'<text x="{x + 210}" y="{y + 230}" class="sub-meta">'
        f'{escape(player.position)}</text>'
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
        _starter_slot_markup(STARTER_SLOT_LAYOUTS[position], lineup.get(position))
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
          <stop offset="0" stop-color="#101b23"/>
          <stop offset="1" stop-color="#07110f"/>
        </linearGradient>
        <linearGradient id="rink" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#f7fafc"/>
          <stop offset="1" stop-color="#e5edf2"/>
        </linearGradient>
        <clipPath id="ice-boundary">
          <rect x="78" y="165" width="1444" height="860" rx="72"/>
        </clipPath>
        <style>
          text {{ font-family: 'DejaVu Sans', Arial, sans-serif; }}
          .title {{ fill: #f5fbff; font-size: 43px; font-weight: 700; letter-spacing: 1px; }}
          .subtitle {{ fill: #a9bfca; font-size: 18px; }}
          .badge {{ fill: #e5f5ff; font-size: 22px; font-weight: 700; }}
          .slot-title {{ fill: #243e4a; font-size: 15px; font-weight: 700; letter-spacing: 1px; }}
          .slot-title-dark {{ fill: #243e4a; font-size: 22px; font-weight: 700; letter-spacing: 1px; }}
          .starter-name {{ fill: #18303b; font-size: 18px; font-weight: 700; }}
          .starter-meta {{ fill: #46606b; font-size: 16px; font-weight: 600; }}
          .sub-name {{ fill: #18303b; font-size: 23px; font-weight: 700; }}
          .sub-meta {{ fill: #46606b; font-size: 19px; font-weight: 600; }}
          .empty-label {{ fill: #91a9a6; font-size: 16px; font-weight: 700; letter-spacing: 1px; }}
          .section {{ fill: #f2f7fa; font-size: 25px; font-weight: 700; letter-spacing: 2px; }}
        </style>
      </defs>
      <rect width="100%" height="100%" fill="url(#background)"/>
      <text x="66" y="69" class="title">THSL STARTING LINEUP</text>
      <text x="68" y="111" class="subtitle">{subtitle}</text>
      <rect x="1100" y="33" width="440" height="54" rx="27"
        fill="#183b4a" stroke="#7ed4f5" stroke-width="2"/>
      <text x="1320" y="68" class="badge" text-anchor="middle">{formation_badge}</text>

      <rect x="58" y="145" width="1484" height="900" rx="90"
        fill="#f8fbfc" stroke="#eef3f6" stroke-width="8"/>
      <rect x="76" y="163" width="1448" height="864" rx="76"
        fill="url(#rink)" stroke="#c6d4dc" stroke-width="3"/>
      <g clip-path="url(#ice-boundary)">
        <line x1="800" y1="165" x2="800" y2="1025"
          stroke="#d52b3e" stroke-width="7"/>
        <line x1="485" y1="165" x2="485" y2="1025"
          stroke="#2867a6" stroke-width="8"/>
        <line x1="1115" y1="165" x2="1115" y2="1025"
          stroke="#2867a6" stroke-width="8"/>
        <path d="M 152 420 A 115 115 0 0 1 152 650 L 152 420 Z"
          fill="#e83d50" fill-opacity="0.12" stroke="#d52b3e" stroke-width="5"/>
        <path d="M 1448 420 A 115 115 0 0 0 1448 650 L 1448 420 Z"
          fill="#e83d50" fill-opacity="0.12" stroke="#d52b3e" stroke-width="5"/>
        <line x1="152" y1="165" x2="152" y2="1025"
          stroke="#d52b3e" stroke-width="6"/>
        <line x1="1448" y1="165" x2="1448" y2="1025"
          stroke="#d52b3e" stroke-width="6"/>
        <circle cx="800" cy="575" r="112" fill="none"
          stroke="#d52b3e" stroke-width="5"/>
        <circle cx="800" cy="575" r="10" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="360" cy="380" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="1240" cy="380" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="360" cy="740" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="1240" cy="740" r="68" fill="none"
          stroke="#d52b3e" stroke-width="4"/>
        <circle cx="360" cy="380" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="1240" cy="380" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="360" cy="740" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="1240" cy="740" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="610" cy="380" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="990" cy="380" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="610" cy="740" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
        <circle cx="990" cy="740" r="8" fill="#d52b3e" stroke="#fff" stroke-width="3"/>
      </g>
      <rect x="84" y="522" width="68" height="75" rx="8"
        fill="#fff" stroke="#d52b3e" stroke-width="5"/>
      <rect x="1448" y="522" width="68" height="75" rx="8"
        fill="#fff" stroke="#d52b3e" stroke-width="5"/>

      <text x="800" y="817" class="section" text-anchor="middle">STARTING LINEUP · THREE POSITIONS</text>

      {slot_markup}

      <text x="800" y="1100" class="section" text-anchor="middle">SUBSTITUTES</text>
      <text x="800" y="1142" class="subtitle" text-anchor="middle">
        Two bench positions · value included in team worth
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
