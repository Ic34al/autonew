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
    """
    Fill the landscape canvas without cutting off the original artwork.
    Uses a blurred background to fill empty space when the source is portrait.
    """
    image = image.convert("RGB")
    target_w, target_h = size

    # Landscape sources are cropped very lightly to fill the canvas.
    if image.width / image.height >= target_w / target_h:
        return ImageOps.fit(
            image,
            size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

    # Portrait artwork: preserve the complete original image.
    background = ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).filter(
        __import__("PIL.ImageFilter", fromlist=["GaussianBlur"]).GaussianBlur(24)
    )

    foreground = ImageOps.contain(
        image,
        size,
        method=Image.Resampling.LANCZOS,
    )

    x = (target_w - foreground.width) // 2
    y = (target_h - foreground.height) // 2

    background.paste(foreground, (x, y))
    return background

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
    """
    Split-screen series card inspired by the user's Manga Cruise reference:
    full-height artwork on the left and a clean, bright information panel
    on the right. Keep the original art intact and avoid the previous busy UI.
    """
    info = get_metadata(metadata, anime_title)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (246, 246, 249))

    # Left: use the actual cover artwork as a tall hero image.
    hero = cover or background
    if hero is not None:
        hero = ImageOps.fit(
            hero.convert("RGB"),
            (735, HEIGHT),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        hero = ImageEnhance.Color(hero).enhance(1.06)
        hero = ImageEnhance.Sharpness(hero).enhance(1.12)
        canvas.paste(hero, (0, 0))

    # Right panel: faint, enlarged artwork watermark beneath a pale veil.
    if background is not None:
        watermark = ImageOps.fit(
            background.convert("RGB"),
            (865, HEIGHT),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.35),
        ).filter(__import__("PIL.ImageFilter", fromlist=["GaussianBlur"]).GaussianBlur(8))
        watermark = ImageEnhance.Brightness(watermark).enhance(1.45)
        canvas.paste(watermark, (735, 0))
    veil = Image.new("RGBA", (865, HEIGHT), (250, 250, 253, 225))
    canvas.paste(veil, (735, 0), veil)

    draw = ImageDraw.Draw(canvas)

    # Header and wordmark.
    draw.text((800, 48), "ANIME SOUL", font=get_font(34, True), fill=(20, 20, 25))
    draw.text((1035, 61), "/ STREAM", font=get_font(18, True), fill=(180, 35, 55))
    draw.text((1230, 65), "HOME     ANIME     SCHEDULE     LIBRARY",
              font=get_font(15, True), fill=(55, 55, 65))
    for offset in (0, 13, 26):
        draw.rounded_rectangle((1540, 48 + offset, 1580, 53 + offset),
                               radius=2, fill=(25, 25, 30))

    # Small series eyebrow.
    season_label = info["season"] if info["season"] != "NOT LISTED" else "ANIME SERIES"
    year_label = info["year"] if info["year"] != "NOT LISTED" else ""
    draw.text((800, 165), f"{season_label}  {year_label}".strip().upper(),
              font=get_font(18, True), fill=(165, 55, 60))

    # Large title, wrapped cleanly to the right panel.
    title_font = get_font(57, True)
    title_lines = wrap_text(draw, info["title"], title_font, 740, max_lines=3)
    title_y = 215
    for line in title_lines:
        draw.text((800, title_y), line, font=title_font, fill=(20, 20, 25))
        title_y += 70
    rule_y = title_y + 6
    draw.line((800, rule_y, 1535, rule_y), fill=(135, 135, 145), width=2)

    # Synopsis block, preserving the synopsis itself as the main copy.
    synopsis_y = rule_y + 40
    synopsis_font = get_font(23)
    synopsis_lines = wrap_text(draw, info["synopsis"], synopsis_font, 735, max_lines=7)
    for line in synopsis_lines:
        draw.text((800, synopsis_y), line, font=synopsis_font, fill=(65, 65, 75))
        synopsis_y += 36

    # Read action and bookmark, styled like the supplied reference.
    button_y = min(max(synopsis_y + 42, 610), 720)
    draw.rounded_rectangle((800, button_y, 1040, button_y + 66),
                           radius=33, fill=(15, 15, 20))
    draw.text((920, button_y + 33), "WATCH NOW", font=get_font(20, True),
              fill=WHITE, anchor="mm")
    draw.ellipse((1060, button_y, 1126, button_y + 66), fill=WHITE,
                 outline=(210, 210, 215), width=2)
    draw.text((1093, button_y + 31), "☆", font=get_font(34, True),
              fill=(25, 25, 30), anchor="mm")

    # Compact factual metadata row along the bottom of the light panel.
    meta_y = 785
    draw.line((800, meta_y - 24, 1535, meta_y - 24), fill=(175, 175, 185), width=2)
    studio = info["studio"]
    status = info["status"]
    genre_text = ", ".join(info["genres"][:2]) or "NOT LISTED"
    meta_items = [
        ("STUDIO", studio),
        ("GENRES", genre_text),
        ("STATUS", status),
        ("TYPE", info["type"]),
    ]
    col_width = 184
    for index, (label, value) in enumerate(meta_items):
        x = 800 + index * col_width
        draw.text((x, meta_y), label, font=get_font(13, True), fill=(115, 115, 125))
        draw.text((x, meta_y + 28), fit_text(draw, value, get_font(15, True), 170),
                  font=get_font(15, True), fill=(35, 35, 45))

    # Bottom navigation capsule, echoing the reference without copying its brand.
    nav_box = (1010, 845, 1510, 895)
    draw.rounded_rectangle(nav_box, radius=25, fill=(8, 8, 12))
    nav_labels = ["⌂", "⌕", "+", "♟", "↗"]
    for index, label in enumerate(nav_labels):
        cx = 1060 + index * 105
        if index == 2:
            draw.ellipse((cx - 19, 853, cx + 19, 891), fill=(215, 125, 20))
        draw.text((cx, 872), label, font=get_font(25, True), fill=WHITE, anchor="mm")

    # Left-side title treatment kept subtle so the artwork remains dominant.
    left_overlay = Image.new("RGBA", (735, 190), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(left_overlay)
    for y in range(190):
        alpha = int(150 * (y / 189))
        overlay_draw.line((0, y, 735, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, HEIGHT - 190), left_overlay)
    draw = ImageDraw.Draw(canvas)
    draw.text((48, HEIGHT - 115), fit_text(draw, info["title"], get_font(31, True), 640),
              font=get_font(31, True), fill=WHITE)
    draw.text((50, HEIGHT - 64), f"{season_label}  •  {info['type']}",
              font=get_font(17, True), fill=(235, 205, 130))

    canvas.save(
        output_path,
        "JPEG",
        quality=96,
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
