from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import os

WIDTH = 1600
ROW_HEIGHT = 105
HEADER_HEIGHT = 225
FOOTER_HEIGHT = 110

BG = (12, 12, 22)
PANEL = (25, 23, 42)
PURPLE = (145, 95, 255)
GOLD = (245, 200, 75)
WHITE = (248, 248, 252)
MUTED = (170, 170, 190)


def get_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def create_schedule_image(items, now):
    items = items or []

    height = max(
        850,
        HEADER_HEIGHT + len(items) * ROW_HEIGHT + FOOTER_HEIGHT
    )

    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    title_font = get_font(52, True)
    date_font = get_font(25)
    anime_font = get_font(27, True)
    info_font = get_font(19)
    time_font = get_font(24, True)
    footer_font = get_font(20, True)

    # Header panel
    draw.rounded_rectangle(
        (55, 40, WIDTH - 55, 190),
        radius=25,
        fill=PANEL,
        outline=PURPLE,
        width=3,
    )

    draw.text(
        (90, 65),
        "TODAY'S ANIME SCHEDULE",
        font=title_font,
        fill=WHITE,
    )

    draw.text(
        (95, 135),
        now.strftime("%A  |  %d %B %Y").upper()
        + "  •  INDIA STANDARD TIME",
        font=date_font,
        fill=MUTED,
    )

    if not items:
        draw.text(
            (100, 270),
            "NO ANIME RELEASES FOUND TODAY",
            font=anime_font,
            fill=GOLD,
        )

    for index, item in enumerate(items):
        y = HEADER_HEIGHT + index * ROW_HEIGHT

        draw.rounded_rectangle(
            (65, y, WIDTH - 65, y + 85),
            radius=15,
            fill=PANEL,
        )

        draw.rounded_rectangle(
            (65, y, 77, y + 85),
            radius=5,
            fill=PURPLE,
        )

        anime_title = str(item.get("title", "Unknown Anime"))
        episode = str(item.get("episode", "TBA"))
        release_time = str(item.get("time", "TBA"))
        status = str(item.get("status", "Upcoming")).upper()

        if len(anime_title) > 55:
            anime_title = anime_title[:52] + "..."

        draw.text(
            (105, y + 10),
            anime_title,
            font=anime_font,
            fill=GOLD,
        )

        draw.text(
            (105, y + 51),
            f"EPISODE {episode}  |  {status}",
            font=info_font,
            fill=MUTED,
        )

        draw.text(
            (WIDTH - 350, y + 25),
            f"{release_time} IST",
            font=time_font,
            fill=WHITE,
        )

    footer_y = height - FOOTER_HEIGHT

    draw.line(
        (75, footer_y, WIDTH - 75, footer_y),
        fill=PURPLE,
        width=3,
    )

    draw.text(
        (85, footer_y + 35),
        "ONGOING ANIME TIME",
        font=footer_font,
        fill=GOLD,
    )

    draw.text(
        (WIDTH - 480, footer_y + 35),
        "FOLLOW FOR DAILY UPDATES",
        font=footer_font,
        fill=WHITE,
    )

    output = Path("today_anime_schedule.jpg")

    image.save(
        str(output),
        "JPEG",
        quality=94,
        optimize=True,
    )

    return str(output)
