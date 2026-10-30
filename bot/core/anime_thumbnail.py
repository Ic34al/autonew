import aiohttp
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from io import BytesIO
import os

OUTPUT_DIR = Path("thumbs")
OUTPUT_DIR.mkdir(exist_ok=True)


def get_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]

    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


async def create_anime_thumbnail(
    image_url,
    anime_title,
    episode,
    output_name="anime_thumbnail.jpg",
):

    if not image_url or not image_url.startswith("https://"):
        return None

    timeout = aiohttp.ClientTimeout(total=20)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:

            async with session.get(image_url) as response:

                if response.status != 200:
                    return None

                raw = await response.read()

        image = Image.open(BytesIO(raw)).convert("RGB")

        target_ratio = 16 / 9
        width, height = image.size

        if width / height > target_ratio:

            new_width = int(height * target_ratio)
            left = (width - new_width) // 2

            image = image.crop(
                (left, 0, left + new_width, height)
            )

        else:

            new_height = int(width / target_ratio)
            top = (height - new_height) // 2

            image = image.crop(
                (0, top, width, top + new_height)
            )

        image = image.resize(
            (1280, 720),
            Image.Resampling.LANCZOS,
        )

        image = image.filter(
            ImageFilter.GaussianBlur(0.2)
        )

        overlay = Image.new(
            "RGBA",
            image.size,
            (0, 0, 0, 0),
        )

        draw = ImageDraw.Draw(overlay)

        for y in range(390, 720):

            alpha = int(
                190 * ((y - 390) / 330)
            )

            draw.rectangle(
                (0, y, 1280, y + 1),
                fill=(0, 0, 0, alpha),
            )

        draw.rounded_rectangle(
            (55, 45, 455, 105),
            radius=18,
            fill=(20, 15, 40, 225),
            outline=(150, 100, 255, 255),
            width=2,
        )

        draw.text(
            (78, 62),
            "ONGOING ANIME TIME",
            font=get_font(25, True),
            fill=(255, 215, 110, 255),
        )

        title = str(anime_title or "Anime")

        if len(title) > 52:
            title = title[:49] + "..."

        draw.text(
            (65, 525),
            title,
            font=get_font(44, True),
            fill=(255, 255, 255, 255),
        )

        draw.text(
            (70, 600),
            f"EPISODE {episode}",
            font=get_font(27, True),
            fill=(255, 205, 85, 255),
        )

        result = Image.alpha_composite(
            image.convert("RGBA"),
            overlay,
        ).convert("RGB")

        output_path = OUTPUT_DIR / output_name

        result.save(
            output_path,
            "JPEG",
            quality=94,
            optimize=True,
        )

        return str(output_path)

    except Exception as exc:

        print(
            f"Thumbnail generation failed: {exc}"
        )

        return None
