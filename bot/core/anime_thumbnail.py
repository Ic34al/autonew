import aiohttp
import asyncio
import html
import os
import re
import textwrap
import uuid

from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

OUTPUT_DIR = Path("thumbs")
OUTPUT_DIR.mkdir(exist_ok=True)

WIDTH = 1600
HEIGHT = 900

BG = (7, 10, 20)
PANEL = (5, 9, 19, 245)
CYAN = (0, 183, 255)
WHITE = (250, 250, 255)
MUTED = (190, 200, 215)


def get_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def fit_text(draw, text, font, max_width):
    text = str(text)

    while text and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        text = text[:-2].rstrip() + "…"

    return text


def wrap_text(draw, text, font, max_width, max_lines=9):
    words = clean_text(text).split()
    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()

        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines and words:
        used = " ".join(lines).split()
        if len(used) < len(words):
            lines[-1] = fit_text(
                draw,
                lines[-1] + "…",
                font,
                max_width,
            )

    return lines


async def download_image(session, url):
    if not isinstance(url, str):
        return None

    if not url.startswith("https://"):
        return None

    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None

            data = await response.read()

            if len(data) > 12 * 1024 * 1024:
                return None

            return Image.open(BytesIO(data)).convert("RGB")

    except Exception as exc:
        print(f"Thumbnail image download failed: {exc}")
        return None


def crop_cover(image, size):
    image = image.copy()
    image.thumbnail(size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", size, BG)

    x = (size[0] - image.width) // 2
    y = (size[1] - image.height) // 2

    canvas.paste(image, (x, y))

    return canvas


def draw_rounded_image(base, image, box, radius=18):
    x1, y1, x2, y2 = box
    size = (x2 - x1, y2 - y1)

    image = crop_cover(image, size)

    mask = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(mask)

    mask_draw.rounded_rectangle(
        (0, 0, size[0], size[1]),
        radius=radius,
        fill=255,
    )

    base.paste(image, (x1, y1), mask)


def create_dashboard(
    background,
    cover,
    metadata,
    anime_title,
    episode,
    output_path,
):
    canvas = background.resize(
        (WIDTH, HEIGHT),
        Image.Resampling.LANCZOS,
    ).convert("RGBA")

    canvas = ImageEnhance.Color(canvas).enhance(1.12)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.08)

    # Slight background blur for premium dashboard depth.
    canvas = canvas.filter(ImageFilter.GaussianBlur(1.2))

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Dark cinematic overlay.
    for x in range(WIDTH):
        alpha = int(150 * (1 - x / WIDTH))
        draw.line(
            (x, 0, x, HEIGHT),
            fill=(0, 0, 0, alpha),
        )

    draw.rectangle(
        (0, 0, WIDTH, HEIGHT),
        fill=(0, 0, 0, 35),
    )

    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    title_font = get_font(66, True)
    section_font = get_font(27, True)
    body_font = get_font(23)
    small_font = get_font(19, True)
    label_font = get_font(19)
    value_font = get_font(24, True)

    title_data = metadata.get("title") or {}

    if isinstance(title_data, dict):
        official_title = (
            title_data.get("english")
            or title_data.get("romaji")
            or anime_title
        )
    else:
        official_title = anime_title

    title = clean_text(official_title) or "Unknown Anime"

    genres = metadata.get("genres") or []
    genres = [clean_text(x).upper() for x in genres[:3] if x]

    studios_data = metadata.get("studios") or {}
    studio_nodes = studios_data.get("nodes", []) if isinstance(studios_data, dict) else []

    studio = (
        studio_nodes[0].get("name")
        if studio_nodes and isinstance(studio_nodes[0], dict)
        else "Not listed"
    )

    status_map = {
        "RELEASING": "ONGOING",
        "FINISHED": "FINISHED",
        "NOT_YET_RELEASED": "UPCOMING",
        "CANCELLED": "CANCELLED",
        "HIATUS": "ON HIATUS",
    }

    raw_status = str(metadata.get("status") or "").upper()
    status = status_map.get(raw_status, "NOT LISTED")

    season = str(metadata.get("season") or "").replace("_", " ").title()
    year = metadata.get("seasonYear") or (
        metadata.get("startDate", {}).get("year")
        if isinstance(metadata.get("startDate"), dict)
        else None
    )

    season_text = (
        f"{season.upper()} {year}"
        if season and year
        else str(year) if year
        else season.upper() if season
        else "NOT LISTED"
    )

    synopsis = clean_text(metadata.get("description") or "")
    if not synopsis:
        synopsis = "Synopsis information is not available."

    # Left navigation.
    draw.rounded_rectangle(
        (25, 215, 155, 705),
        radius=32,
        fill=(0, 0, 0, 245),
        outline=(CYAN[0], CYAN[1], CYAN[2], 160),
        width=2,
    )

    nav_items = [
        ("◆", "HOME"),
        ("⌕", "SEARCH"),
        ("▦", "CATEGORY"),
        ("★", "FAVORITES"),
    ]

    nav_y = 255

    for symbol, label in nav_items:
        draw.text(
            (67, nav_y),
            symbol,
            font=get_font(42, True),
            fill=CYAN if label == "HOME" else WHITE,
            anchor="mm",
        )

        draw.text(
            (90, nav_y + 43),
            label,
            font=get_font(15, True),
            fill=CYAN if label == "HOME" else WHITE,
            anchor="mm",
        )

        nav_y += 112

    # Brand bar.
    draw.rounded_rectangle(
        (28, 32, 605, 132),
        radius=25,
        fill=(0, 0, 0, 245),
        outline=(CYAN[0], CYAN[1], CYAN[2], 180),
        width=2,
    )

    draw.text(
        (65, 55),
        "☰",
        font=get_font(48, True),
        fill=WHITE,
    )

    draw.text(
        (145, 61),
        "ONGOING",
        font=get_font(37, True),
        fill=CYAN,
    )

    draw.text(
        (330, 61),
        "ANIME TIME",
        font=get_font(37, True),
        fill=WHITE,
    )

    draw.text(
        (550, 61),
        "♛",
        font=get_font(34, True),
        fill=(255, 205, 80),
    )

    # Genre pills.
    genre_x = 190

    for genre in genres:
        genre = fit_text(draw, genre, small_font, 160)
        box_width = draw.textbbox(
            (0, 0), genre, font=small_font
        )[2] + 44

        draw.rounded_rectangle(
            (genre_x, 195, genre_x + box_width, 245),
            radius=24,
            fill=WHITE,
        )

        draw.text(
            (genre_x + 22, 208),
            genre,
            font=small_font,
            fill=(10, 12, 20),
        )

        genre_x += box_width + 14

    # Main anime title.
    title = fit_text(draw, title, title_font, 900)

    draw.text(
        (185, 280),
        title,
        font=title_font,
        fill=WHITE,
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )

    # Episode label.
    draw.text(
        (195, 375),
        f"EPISODE {episode}",
        font=section_font,
        fill=CYAN,
    )

    # Studio card.
    draw.rounded_rectangle(
        (180, 450, 930, 555),
        radius=20,
        fill=PANEL,
        outline=(CYAN[0], CYAN[1], CYAN[2], 180),
        width=2,
    )

    draw.text(
        (215, 468),
        "STUDIO",
        font=label_font,
        fill=MUTED,
    )

    draw.text(
        (215, 505),
        fit_text(draw, studio, value_font, 650),
        font=value_font,
        fill=WHITE,
    )

    # Status card.
    draw.rounded_rectangle(
        (180, 575, 535, 680),
        radius=20,
        fill=(255, 255, 255, 245),
    )

    draw.text(
        (215, 590),
        "STATUS",
        font=label_font,
        fill=(40, 40, 50),
    )

    draw.text(
        (215, 625),
        status,
        font=value_font,
        fill=(0, 145, 220),
    )

    # Season/year card.
    draw.rounded_rectangle(
        (555, 575, 930, 680),
        radius=20,
        fill=(255, 255, 255, 245),
    )

    draw.text(
        (590, 590),
        "SEASON / YEAR",
        font=label_font,
        fill=(40, 40, 50),
    )

    draw.text(
        (590, 625),
        fit_text(draw, season_text, value_font, 300),
        font=value_font,
        fill=(0, 100, 190),
    )

    # Right synopsis panel.
    draw.rounded_rectangle(
        (1120, 215, 1570, 780),
        radius=28,
        fill=(0, 0, 0, 247),
        outline=(CYAN[0], CYAN[1], CYAN[2], 180),
        width=3,
    )

    if cover:
        draw_rounded_image(
            canvas,
            cover,
            (1140, 235, 1550, 410),
            radius=20,
        )

    draw = ImageDraw.Draw(canvas)

    draw.text(
        (1145, 430),
        "SYNOPSIS",
        font=section_font,
        fill=CYAN,
    )

    synopsis_lines = wrap_text(
        draw,
        synopsis,
        body_font,
        380,
        max_lines=11,
    )

    y = 475

    for line in synopsis_lines:
        draw.text(
            (1145, y),
            line,
            font=body_font,
            fill=WHITE,
        )
        y += 31

    # Bottom accent strip.
    draw.rectangle(
        (0, 775, WIDTH, HEIGHT),
        fill=(0, 0, 0, 210),
    )

    # Reuse banner artwork as cropped cinematic frames.
    # This is decorative, not a claim that these are different characters.
    strip_y = 790
    strip_height = 88
    strip_width = 285

    for index in range(5):
        x = 28 + index * 310

        crop_w = background.width // 5
        crop = background.crop(
            (
                index * crop_w,
                0,
                min((index + 1) * crop_w, background.width),
                background.height,
            )
        )

        draw_rounded_image(
            canvas,
            crop,
            (x, strip_y, x + strip_width, strip_y + strip_height),
            radius=12,
        )

        draw = ImageDraw.Draw(canvas)

        draw.rounded_rectangle(
            (x, strip_y, x + strip_width, strip_y + strip_height),
            radius=12,
            outline=(CYAN[0], CYAN[1], CYAN[2], 180),
            width=2,
        )

    canvas.convert("RGB").save(
        output_path,
        "JPEG",
        quality=94,
        optimize=True,
    )

    return str(output_path)


async def create_anime_thumbnail(
    image_url,
    anime_title,
    episode,
    output_name="anime_thumbnail.jpg",
    metadata=None,
):
    metadata = metadata or {}

    banner_url = (
        metadata.get("bannerImage")
        or image_url
    )

    cover_url = (
        (metadata.get("coverImage") or {}).get("large")
        or image_url
    )

    timeout = aiohttp.ClientTimeout(total=25)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            background = await download_image(
                session,
                banner_url,
            )

            cover = await download_image(
                session,
                cover_url,
            )

            if background is None:
                background = cover

            if background is None:
                return None

            if cover is None:
                cover = background

        output_path = OUTPUT_DIR / output_name

        return await asyncio.to_thread(
            create_dashboard,
            background,
            cover,
            metadata,
            anime_title,
            episode,
            output_path,
        )

    except Exception as exc:
        print(f"Anime dashboard generation failed: {exc}")
        return None
