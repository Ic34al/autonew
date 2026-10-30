import aiohttp
import asyncio
import html
import os
import re

from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageOps


OUTPUT_DIR = Path("thumbs")
OUTPUT_DIR.mkdir(exist_ok=True)

WIDTH = 1600
HEIGHT = 900

BLACK = (5, 5, 8)
DARK = (10, 10, 15)
RED = (235, 0, 48)
WHITE = (255, 255, 255)
MUTED = (190, 190, 200)


def get_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass

    return ImageFont.load_default()


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]*>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def fit_text(draw, text, font, max_width):
    text = str(text)

    while text and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        text = text[:-2].rstrip() + "…"

    return text


def wrap_text(draw, text, font, max_width, max_lines=8):
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

    if len(lines) == max_lines and len(words) > 0:
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

    if not url.startswith(("https://", "http://")):
        return None

    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None

            data = await response.read()

            if not data or len(data) > 15 * 1024 * 1024:
                return None

            image = Image.open(BytesIO(data))
            image.load()

            return image.convert("RGB")

    except Exception as exc:
        print(f"Thumbnail download failed: {exc}")
        return None


def crop_cover(image, size):
    return ImageOps.fit(
        image.convert("RGB"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def rounded_image(canvas, image, box, radius=15):
    x1, y1, x2, y2 = box
    width = x2 - x1
    height = y2 - y1

    fitted = crop_cover(image, (width, height))

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=255,
    )

    canvas.paste(fitted, (x1, y1), mask)


def panel(draw, box, fill=(0, 0, 0, 225), outline=RED, radius=18, width=2):
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width,
    )


def draw_gradient_overlay(canvas):
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Darken the left side for readable title text.
    for x in range(0, 1220):
        alpha = int(225 * (1 - x / 1220) ** 0.72)
        draw.line(
            (x, 0, x, HEIGHT),
            fill=(0, 0, 0, alpha),
        )

    # Darken the bottom edge.
    for y in range(680, HEIGHT):
        alpha = int(185 * ((y - 680) / (HEIGHT - 680)))
        draw.line(
            (0, y, WIDTH, y),
            fill=(0, 0, 0, alpha),
        )

    return Image.alpha_composite(canvas, overlay)


def get_metadata(metadata, anime_title):
    metadata = metadata or {}

    title_data = metadata.get("title") or {}

    if isinstance(title_data, dict):
        title = (
            title_data.get("english")
            or title_data.get("romaji")
            or anime_title
        )
    else:
        title = anime_title

    title = clean_text(title) or clean_text(anime_title) or "Unknown Anime"

    genres = metadata.get("genres") or []
    genres = [
        clean_text(item).upper()
        for item in genres
        if item
    ][:5]

    studio_data = metadata.get("studios") or {}
    studio_nodes = (
        studio_data.get("nodes", [])
        if isinstance(studio_data, dict)
        else []
    )

    studio = "NOT LISTED"

    if studio_nodes and isinstance(studio_nodes[0], dict):
        studio = clean_text(studio_nodes[0].get("name")) or studio

    status_map = {
        "RELEASING": "ONGOING",
        "FINISHED": "FINISHED",
        "NOT_YET_RELEASED": "UPCOMING",
        "CANCELLED": "CANCELLED",
        "HIATUS": "ON HIATUS",
    }

    raw_status = str(metadata.get("status") or "").upper()
    status = status_map.get(raw_status, "NOT LISTED")

    season = clean_text(metadata.get("season") or "").replace("_", " ").upper()

    start_date = metadata.get("startDate") or {}
    year = metadata.get("seasonYear")

    if not year and isinstance(start_date, dict):
        year = start_date.get("year")

    season_value = season or "NOT LISTED"
    year_value = str(year) if year else "NOT LISTED"

    anime_format = clean_text(metadata.get("format") or "TV").upper()
    anime_type = "TV ANIME" if anime_format in ("TV", "TV_SHORT") else anime_format

    synopsis = clean_text(metadata.get("description") or "")

    if not synopsis:
        synopsis = "Synopsis information is not available."

    return {
        "title": title,
        "genres": genres,
        "studio": studio,
        "status": status,
        "season": season_value,
        "year": year_value,
        "type": anime_type,
        "synopsis": synopsis,
    }


def draw_brand(canvas):
    draw = ImageDraw.Draw(canvas)

    # Header logo plate.
    panel(
        draw,
        (100, 22, 570, 100),
        fill=(0, 0, 0, 235),
        outline=(235, 235, 235, 220),
        radius=17,
        width=2,
    )

    # Crown-like brand mark.
    crown = [
        (124, 66),
        (118, 43),
        (137, 54),
        (151, 35),
        (166, 54),
        (185, 43),
        (178, 68),
    ]

    draw.polygon(crown, fill=RED)
    draw.rectangle((125, 69, 179, 77), fill=RED)

    draw.text(
        (195, 32),
        "ANIME",
        font=get_font(43, True),
        fill=WHITE,
    )

    draw.text(
        (350, 32),
        "SOUL",
        font=get_font(43, True),
        fill=RED,
    )

    # Small menu icon.
    for y in (34, 48, 62):
        draw.rounded_rectangle(
            (22, y, 65, y + 6),
            radius=3,
            fill=WHITE,
        )


def draw_sidebar(canvas):
    draw = ImageDraw.Draw(canvas)

    draw.rectangle(
        (0, 0, 82, HEIGHT),
        fill=(0, 0, 0, 220),
    )

    draw.line(
        (82, 0, 82, HEIGHT),
        fill=(255, 255, 255, 55),
        width=2,
    )

    items = [
        ("◆", "HOME"),
        ("⌕", "SEARCH"),
        ("▦", "CATEGORY"),
        ("★", "FAVORITES"),
    ]

    y = 185

    for index, (symbol, label) in enumerate(items):
        active = index == 0
        color = RED if active else WHITE

        draw.text(
            (41, y),
            symbol,
            font=get_font(42, True),
            fill=color,
            anchor="mm",
        )

        draw.text(
            (41, y + 38),
            label,
            font=get_font(12, True),
            fill=color,
            anchor="mm",
        )

        y += 135

    # Social icons are kept simple and readable.
    social_y = 720

    for label in ("TELEGRAM", "DISCORD", "INSTAGRAM", "YOUTUBE"):
        draw.text(
            (41, social_y),
            label,
            font=get_font(10, True),
            fill=WHITE,
            anchor="mm",
        )
        social_y += 38


def draw_genres(draw, genres):
    x = 100
    y = 615

    for genre in genres[:5]:
        font = get_font(15, True)
        genre = fit_text(draw, genre, font, 180)

        text_width = draw.textbbox((0, 0), genre, font=font)[2]
        box_width = text_width + 36

        if x + box_width > 1160:
            break

        draw.rounded_rectangle(
            (x, y, x + box_width, y + 43),
            radius=22,
            fill=RED if x == 100 else (0, 0, 0, 220),
            outline=RED,
            width=2,
        )

        draw.text(
            (x + 18, y + 12),
            genre,
            font=font,
            fill=WHITE,
        )

        x += box_width + 12


def draw_metadata_card(draw, box, label, value, icon):
    x1, y1, x2, y2 = box

    panel(
        draw,
        box,
        fill=(0, 0, 0, 225),
        outline=(RED[0], RED[1], RED[2], 220),
        radius=13,
        width=2,
    )

    draw.text(
        (x1 + 17, y1 + 12),
        icon,
        font=get_font(25, True),
        fill=WHITE,
    )

    draw.text(
        (x1 + 58, y1 + 10),
        label.upper(),
        font=get_font(13, True),
        fill=MUTED,
    )

    value_font = get_font(19, True)
    value = fit_text(draw, value, value_font, x2 - x1 - 75)

    draw.text(
        (x1 + 58, y1 + 34),
        value,
        font=value_font,
        fill=RED,
    )


def create_dashboard(
    background,
    cover,
    metadata,
    anime_title,
    episode,
    output_path,
):
    info = get_metadata(metadata, anime_title)

    # Preserve artwork aspect ratio. Never stretch the original image.
    background = crop_cover(background, (WIDTH, HEIGHT))

    background = ImageEnhance.Color(background).enhance(1.12)
    background = ImageEnhance.Contrast(background).enhance(1.10)
    background = ImageEnhance.Sharpness(background).enhance(1.18)

    canvas = background.convert("RGBA")
    canvas = draw_gradient_overlay(canvas)

    draw = ImageDraw.Draw(canvas)

    # Brand and navigation.
    draw_brand(canvas)
    draw_sidebar(canvas)

    draw = ImageDraw.Draw(canvas)

    # Quote area.
    draw.text(
        (105, 125),
        "“",
        font=get_font(78, True),
        fill=RED,
    )

    draw.text(
        (160, 145),
        "ANIME STORIES",
        font=get_font(20, True),
        fill=WHITE,
    )

    draw.text(
        (160, 174),
        "THAT STAY WITH YOU.",
        font=get_font(20, True),
        fill=RED,
    )

    # Anime title.
    title_font = get_font(67, True)
    title = fit_text(draw, info["title"].upper(), title_font, 980)

    draw.text(
        (105, 285),
        title,
        font=title_font,
        fill=WHITE,
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )

    # Episode / season heading.
    episode_text = clean_text(episode) or "TBA"

    draw.text(
        (110, 380),
        f"SEASON  {info['season']}     |     EPISODE  {episode_text}",
        font=get_font(27, True),
        fill=RED,
    )

    draw.rounded_rectangle(
        (110, 430, 510, 485),
        radius=28,
        fill=(0, 0, 0, 225),
        outline=RED,
        width=2,
    )

    draw.ellipse(
        (128, 439, 168, 479),
        fill=RED,
    )

    draw.polygon(
        [(144, 448), (144, 470), (161, 459)],
        fill=WHITE,
    )

    draw.text(
        (185, 443),
        "EPISODE UPDATE",
        font=get_font(20, True),
        fill=WHITE,
    )

    # Metadata row.
    card_y1 = 510
    card_y2 = 590
    card_width = 205
    gap = 12
    start_x = 100

    cards = [
        ("STUDIO", info["studio"], "✦"),
        ("STATUS", info["status"], "●"),
        ("SEASON", info["season"], "▤"),
        ("YEAR", info["year"], "▦"),
        ("TYPE", info["type"], "▶"),
    ]

    for index, (label, value, icon) in enumerate(cards):
        x1 = start_x + index * (card_width + gap)

        draw_metadata_card(
            draw,
            (x1, card_y1, x1 + card_width, card_y2),
            label,
            value,
            icon,
        )

    # Genre row.
    draw_genres(draw, info["genres"])

    # Right synopsis panel.
    panel(
        draw,
        (1190, 235, 1570, 785),
        fill=(0, 0, 0, 242),
        outline=RED,
        radius=22,
        width=3,
    )

    # Cover artwork.
    if cover is not None:
        rounded_image(
            canvas,
            cover,
            (1210, 255, 1550, 425),
            radius=13,
        )

    draw = ImageDraw.Draw(canvas)

    draw.text(
        (1215, 445),
        "SYNOPSIS",
        font=get_font(26, True),
        fill=WHITE,
    )

    draw.rounded_rectangle(
        (1215, 482, 1545, 488),
        radius=3,
        fill=RED,
    )

    synopsis_font = get_font(19)
    synopsis_lines = wrap_text(
        draw,
        info["synopsis"],
        synopsis_font,
        325,
        max_lines=12,
    )

    y = 510

    for line in synopsis_lines:
        draw.text(
            (1215, y),
            line,
            font=synopsis_font,
            fill=(238, 238, 242),
        )
        y += 27

    # Bottom artwork strip.
    draw.rectangle(
        (90, 675, 1600, 900),
        fill=(0, 0, 0, 190),
    )

    strip_y = 700
    strip_height = 145
    strip_width = 350
    strip_gap = 18

    # Use different crops of the available banner artwork.
    # These are crops, not falsely presented as separate source scenes.
    for index in range(4):
        x = 100 + index * (strip_width + strip_gap)

        crop_width = background.width // 4
        left = index * crop_width
        right = background.width if index == 3 else (index + 1) * crop_width

        crop = background.crop(
            (left, 0, right, background.height)
        )

        rounded_image(
            canvas,
            crop,
            (x, strip_y, x + strip_width, strip_y + strip_height),
            radius=14,
        )

        draw = ImageDraw.Draw(canvas)

        draw.rounded_rectangle(
            (x, strip_y, x + strip_width, strip_y + strip_height),
            radius=14,
            outline=RED,
            width=2,
        )

    # Brand footer.
    draw = ImageDraw.Draw(canvas)

    draw.text(
        (104, 862),
        "ANIME SOUL  •  YOUR ANIME DESTINATION",
        font=get_font(15, True),
        fill=WHITE,
    )

    # Export at full 1600x900 resolution.
    canvas = canvas.convert("RGB")

    canvas.save(
        output_path,
        "JPEG",
        quality=97,
        optimize=True,
        progressive=True,
        subsampling=0,
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

    cover_data = metadata.get("coverImage") or {}

    cover_url = (
        cover_data.get("extraLarge")
        or cover_data.get("large")
        or image_url
    )

    timeout = aiohttp.ClientTimeout(total=35)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            background, cover = await asyncio.gather(
                download_image(session, banner_url),
                download_image(session, cover_url),
            )

        if background is None:
            background = cover

        if background is None:
            print("Thumbnail skipped: no usable anime artwork.")
            return None

        if cover is None:
            cover = background

        output_path = OUTPUT_DIR / os.path.basename(output_name)

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
