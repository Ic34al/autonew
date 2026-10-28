from pathlib import Path

REQUIRED_QUALITIES = ("480", "720", "1080")


async def upload_all_and_publish(
    encoded_files,
    uploader,
    publish_post,
    build_button,
):
    """
    Upload 480p, 720p, and 1080p files before
    publishing the final post.

    encoded_files must contain paths for all
    three prepared video files.
    """
    # Validate all files before starting uploads.
    for quality in REQUIRED_QUALITIES:
        file_path = encoded_files.get(quality)
        if not file_path:
            raise ValueError(
                f"Missing required {quality}p video."
            )
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Cannot find {quality}p file: {path}"
            )
        if path.stat().st_size == 0:
            raise ValueError(
                f"The {quality}p file is empty: {path}"
            )

    uploaded = {}
    # Upload each prepared file.
    for quality in REQUIRED_QUALITIES:
        message = await uploader(
            encoded_files[quality],
            quality,
        )
        if message is None or not getattr(message, "id", None):
            raise RuntimeError(
                f"{quality}p upload failed. "
                "Final post was not published."
            )
        uploaded[quality] = message

    # Create quality buttons after every upload succeeds.
    buttons = [
        build_button(quality, uploaded[quality])
        for quality in REQUIRED_QUALITIES
    ]
    # Publish only after all three uploads succeed.
    return await publish_post(buttons)
