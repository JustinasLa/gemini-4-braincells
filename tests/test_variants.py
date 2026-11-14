import hashlib
import io
import time
from dataclasses import FrozenInstanceError

import imagehash
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from media.variants import Fingerprint, Relation, fingerprint, relate


def _bytes(image, image_format="PNG", **options):
    stream = io.BytesIO()
    image.save(stream, format=image_format, **options)
    return stream.getvalue()


def _font(size=40):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _picture(seed=7, size=(800, 600)):
    random = np.random.default_rng(seed)
    rows, columns = np.indices((size[1], size[0]))
    texture = random.normal(0, 12, (*rows.shape, 3))
    gradient = np.stack(
        (40 + columns * 140 / size[0], 50 + rows * 130 / size[1], 80 + columns * 80 / size[0]),
        axis=-1,
    )
    image = Image.fromarray(np.clip(gradient + texture, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    for _ in range(28):
        left, top = random.integers(0, size[0] - 90), random.integers(0, int(size[1] * 0.7))
        box = (
            int(left),
            int(top),
            int(left + random.integers(25, 150)),
            int(top + random.integers(25, 100)),
        )
        color = tuple(int(value) for value in random.integers(0, 256, 3))
        if random.random() > 0.5:
            draw.ellipse(box, fill=color)
        else:
            draw.rectangle(box, fill=color)
    draw.rectangle((0, int(size[1] * 0.82), size[0], size[1]), fill="white")
    draw.text((30, int(size[1] * 0.86)), "ORIGINAL CAPTION", font=_font(), fill="black")
    return image


@pytest.fixture(scope="module")
def picture():
    return _picture()


def _assert_note(relation):
    assert relation.note.endswith(".")
    assert "fake" not in relation.note.lower()
    assert relation.distance is None or 0 <= relation.distance <= 64
    assert relation.distance is None or type(relation.distance) is int


def test_identical_bytes_and_identical_pixels(picture):
    data = _bytes(picture)
    assert relate(data, data) == Relation("same", 0, None, None, "Identical image bytes.")
    result = relate(data, _bytes(picture, compress_level=1))
    assert result.kind == "resized"
    assert "re-encoded" in result.note
    _assert_note(result)


def test_each_input_is_decoded_once(picture, monkeypatch):
    base, other = _bytes(picture), _bytes(picture, "JPEG", quality=85)
    original_open = Image.open
    opened = []

    def tracked_open(*args, **kwargs):
        opened.append(1)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(Image, "open", tracked_open)
    assert relate(base, other).kind == "resized"
    assert len(opened) == 2
    opened.clear()
    assert relate(base, base).kind == "same"
    assert len(opened) == 1


@pytest.mark.parametrize("quality", [30, 40, 50, 60, 70, 80, 90, 95])
@pytest.mark.parametrize("scale", [1, 0.5])
def test_jpeg_reencode(picture, quality, scale):
    variant = picture.resize(tuple(round(side * scale) for side in picture.size))
    result = relate(_bytes(picture), _bytes(variant, "JPEG", quality=quality))
    assert result.kind == "resized"
    assert result.edited_region is None
    _assert_note(result)


def test_resize_and_mild_brightness(picture):
    smaller = picture.resize((400, 300), Image.Resampling.LANCZOS)
    result = relate(_bytes(picture), _bytes(smaller, "JPEG", quality=70))
    assert result.kind == "resized"
    assert "800x600 to 400x300" in result.note
    brighter = ImageEnhance.Brightness(ImageEnhance.Contrast(picture).enhance(0.9)).enhance(1.05)
    assert relate(_bytes(picture), _bytes(brighter)).kind == "resized"


@pytest.mark.parametrize("quality", [30, 40, 50, 60, 70, 80, 90, 95])
def test_fine_check_matches_resize_resolution(quality):
    image = _picture(size=(509, 600))
    for top in (20, 120, 220, 320, 420):
        ImageDraw.Draw(image).text(
            (20, top),
            "SMALL OUTLINED MEME CAPTION",
            font=ImageFont.load_default(size=24),
            fill="white",
            stroke_width=1,
            stroke_fill="black",
        )
    smaller = image.resize((254, 300), Image.Resampling.LANCZOS)
    result = relate(_bytes(image), _bytes(smaller, "JPEG", quality=quality))
    assert result.kind == "resized", result
    assert result.edited_region is None


@pytest.mark.parametrize("quality", [50, 85])
@pytest.mark.parametrize("scale", [0.97, 0.99])
@pytest.mark.parametrize("resampling", [Image.Resampling.BICUBIC, Image.Resampling.LANCZOS])
def test_outlined_caption_resize(quality, scale, resampling):
    image = _picture(size=(1200, 1198)).crop((0, 0, 1200, 980)).resize((1200, 1198))
    ImageDraw.Draw(image).text(
        (20, 20),
        "UNCHANGED HIGH CONTRAST CAPTION",
        font=ImageFont.load_default(size=32),
        fill="white",
        stroke_width=2,
        stroke_fill="black",
    )
    smaller = image.resize(tuple(round(side * scale) for side in image.size), resampling)
    result = relate(_bytes(image), _bytes(smaller, "JPEG", quality=quality))
    assert result.kind == "resized", result
    assert result.edited_region is None
    _assert_note(result)


@pytest.mark.parametrize(
    "box",
    [
        (120, 90, 680, 510),
        (32, 24, 592, 444),
        (80, 60, 760, 570),
        (240, 180, 560, 420),
        (120, 60, 680, 570),
        (8, 6, 792, 594),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_crops(picture, box, reverse):
    base, other = _bytes(picture), _bytes(picture.crop(box), "JPEG", quality=75)
    result = relate(other, base) if reverse else relate(base, other)
    assert result.kind == "cropped", result
    expected = tuple(value / picture.size[index % 2] for index, value in enumerate(box))
    if reverse:
        assert result.crop_box is None
    else:
        assert result.crop_box == pytest.approx(expected, abs=0.05)
        left, top, right, bottom = result.crop_box
        assert 0 <= left < right <= 1 and 0 <= top < bottom <= 1
        assert "% of its width" in result.note and "% of its height" in result.note
    assert result.edited_region is None
    assert ("extra area around it" in result.note) is reverse
    _assert_note(result)


@pytest.mark.parametrize("change", ["caption", "patch", "overlay"])
def test_localized_edits(picture, change):
    variant = picture.copy()
    draw = ImageDraw.Draw(variant)
    if change == "caption":
        draw.rectangle((0, 492, 800, 600), fill="white")
        draw.text((30, 516), "A DIFFERENT MESSAGE", font=_font(), fill="black")
    elif change == "patch":
        variant.paste(_picture(123).resize((160, 160)), (40, 40))
    else:
        draw.text(
            (200, 150),
            "EXTRA WORDS",
            font=_font(),
            fill="white",
            stroke_width=2,
            stroke_fill="black",
        )
    result = relate(_bytes(picture), _bytes(variant, "JPEG", quality=80))
    assert result.kind == "edited", result
    assert result.crop_box is None
    left, top, right, bottom = result.edited_region
    assert 0 <= left < right <= 1 and 0 <= top < bottom <= 1
    if change == "caption":
        assert top >= 0.78 and bottom >= 0.90
    elif change == "patch":
        assert left <= 0.10 and top <= 0.10 and right < 0.4 and bottom < 0.45
        assert right >= 0.23 and bottom >= 0.30
    else:
        assert left < 0.4 and top < 0.3 and bottom > 0.3
    _assert_note(result)


def test_different_image(picture):
    result = relate(_bytes(picture), _bytes(_picture(42)))
    assert result.kind == "different", result
    _assert_note(result)


@pytest.mark.parametrize("texts", [("PRICE $10", "PRICE $100"), ("VOTE MAY", "VOTE JUNE")])
@pytest.mark.parametrize("font_size", [24, 40])
def test_small_caption_changes(texts, font_size):
    image = _picture(size=(601, 600))
    first, second = image.copy(), image.copy()
    for target, text in zip((first, second), texts, strict=True):
        ImageDraw.Draw(target).text(
            (20, 20),
            text,
            font=ImageFont.load_default(size=font_size),
            fill="white",
            stroke_width=1,
            stroke_fill="black",
        )
    result = relate(_bytes(first), _bytes(second, "JPEG", quality=85))
    assert result.kind == "edited", result
    left, top, right, bottom = result.edited_region
    changed_rows, changed_columns = np.nonzero(
        np.any(np.asarray(first) != np.asarray(second), axis=2)
    )
    assert left <= changed_columns.mean() / image.width <= right
    assert top <= changed_rows.mean() / image.height <= bottom
    assert right - left < 0.3 and bottom - top < 0.15
    _assert_note(result)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_two_separated_caption_changes(image_format):
    image = _picture(size=(601, 600))
    first, second = image.copy(), image.copy()
    for target, text in zip((first, second), ("PRICE $10", "PRICE $100"), strict=True):
        ImageDraw.Draw(target).text(
            (20, 20),
            text,
            font=ImageFont.load_default(size=24),
            fill="white",
            stroke_width=1,
            stroke_fill="black",
        )
    draw = ImageDraw.Draw(second)
    draw.rectangle((0, 492, 601, 600), fill="white")
    draw.text((40, 520), "A DIFFERENT MESSAGE", font=_font(), fill="black")
    result = relate(_bytes(first), _bytes(second, image_format, quality=85))
    assert result.kind == "edited", result
    left, top, right, bottom = result.edited_region
    assert left <= 133 / 601 and right >= 147 / 601
    assert top <= 0.05 and bottom >= 0.9
    assert "2 areas" in result.note and "top" in result.note and "bottom" in result.note
    _assert_note(result)


def test_small_corner_watermark():
    image = _picture(size=(601, 600))
    variant = image.copy()
    ImageDraw.Draw(variant).text(
        (550, 8),
        "AC",
        font=ImageFont.load_default(size=22),
        fill="white",
        stroke_width=1,
        stroke_fill="black",
    )
    result = relate(_bytes(image), _bytes(variant, "JPEG", quality=85))
    assert result.kind == "edited", result
    left, top, right, bottom = result.edited_region
    assert 0.85 < left < right <= 1 and 0 <= top < bottom < 0.10


@pytest.mark.parametrize("reverse", [False, True])
def test_quarter_crop(picture, reverse):
    box = (280, 120, 480, 270)
    base, other = _bytes(picture), _bytes(picture.crop(box), "JPEG", quality=80)
    result = relate(other, base) if reverse else relate(base, other)
    assert result.kind == "cropped", result
    if reverse:
        assert result.crop_box is None
    else:
        expected = tuple(value / picture.size[index % 2] for index, value in enumerate(box))
        assert result.crop_box == pytest.approx(expected, abs=0.03)


@pytest.mark.parametrize("reverse", [False, True])
def test_quarter_crop_of_unrelated_image(picture, reverse):
    base, other = _bytes(_picture(42)), _bytes(picture.crop((280, 120, 480, 270)))
    result = relate(other, base) if reverse else relate(base, other)
    assert result.kind == "different", result


def test_large_16bit_png(picture):
    gray = picture.resize((1600, 1200)).convert("L")
    image = Image.fromarray(np.asarray(gray, dtype=np.uint16) * 257)
    data = _bytes(image)
    result = fingerprint(data)
    assert (result.width, result.height, result.format) == (1600, 1200, "PNG")
    assert result.phash == fingerprint(_bytes(gray)).phash
    assert relate(data, _bytes(gray)).kind == "resized"


@pytest.mark.parametrize("mode", ["I;16B", "I", "F", "CMYK", "YCbCr", "LAB", "LA", "PA"])
def test_large_non_rgb_modes(picture, mode):
    image = picture.resize((1600, 1200))
    if mode in ("I;16B", "I", "F"):
        values = np.asarray(image.convert("L"), dtype=np.float32)
        values = (values - values.min()) * (255 / (values.max() - values.min()))
        expected = Image.fromarray(values.astype(np.uint8))
        if mode == "I;16B":
            image = Image.fromarray((np.asarray(expected, dtype=np.uint16) * 257).astype(">u2"))
        elif mode == "I":
            image = Image.fromarray((np.asarray(expected, dtype=np.int32) * 1000) - 1234)
        else:
            image = Image.fromarray(values / 512 - 0.25)
        data = _bytes(image, "TIFF")
    else:
        image = image.convert(mode)
        if mode in ("LA", "PA"):
            image.putalpha(Image.linear_gradient("L").resize(image.size))
            expected = Image.alpha_composite(
                Image.new("RGBA", image.size, "white"), image.convert("RGBA")
            )
        else:
            expected = image.convert("RGB")
        data = _bytes(image, "JPEG" if mode == "CMYK" else "PNG" if mode == "LA" else "TIFF")
    result = fingerprint(data)
    assert (result.width, result.height) == (1600, 1200)
    assert relate(data, _bytes(expected)).kind == "resized"


def test_transparent_modes_composite_on_white(picture):
    rgba = picture.convert("RGBA")
    draw = ImageDraw.Draw(rgba)
    draw.rectangle((0, 0, 799, 15), fill=(255, 0, 0, 0))
    draw.rectangle((0, 584, 799, 599), fill=(0, 0, 0, 0))
    white = Image.new("RGBA", rgba.size, "white")
    expected = Image.alpha_composite(white, rgba).convert("RGB")
    assert relate(_bytes(expected), _bytes(rgba)).kind == "resized"
    grayscale_alpha = rgba.convert("LA")
    expected_gray = Image.alpha_composite(white, grayscale_alpha.convert("RGBA")).convert("RGB")
    assert relate(_bytes(expected_gray), _bytes(grayscale_alpha)).kind == "resized"
    palette = rgba.quantize(colors=64)
    expected_palette = Image.alpha_composite(white, palette.convert("RGBA")).convert("RGB")
    assert relate(_bytes(expected_palette), _bytes(palette)).kind == "resized"


@pytest.mark.parametrize("mode,image_format", [("P", "GIF"), ("P", "PNG"), ("1", "PNG")])
def test_large_palette_and_binary_images(picture, mode, image_format):
    image = picture.resize((1600, 1200)).convert(mode)
    data = _bytes(image, image_format)
    result = fingerprint(data)
    assert (result.width, result.height) == (1600, 1200)
    assert relate(_bytes(image.convert("RGB")), data).kind == "resized"


def test_crop_with_uniform_background():
    image = Image.new("RGB", (800, 600), "skyblue")
    draw = ImageDraw.Draw(image)
    draw.ellipse((260, 90, 660, 590), fill="white", outline="black", width=8)
    draw.ellipse((330, 200, 400, 270), fill="black")
    draw.polygon([(410, 280), (540, 330), (410, 350)], fill="orange")
    box = (42, 58, 722, 568)
    result = relate(_bytes(image), _bytes(image.crop(box)))
    assert result.kind == "cropped", result
    expected = tuple(value / image.size[index % 2] for index, value in enumerate(box))
    assert result.crop_box == pytest.approx(expected, abs=0.05)


def test_screenshot_padding(picture):
    screenshot = Image.new("RGB", (880, 740), "white")
    screenshot.paste(picture, (40, 100))
    ImageDraw.Draw(screenshot).text((20, 25), "IMAGE VIEWER", fill="black")
    result = relate(_bytes(picture), _bytes(screenshot, "JPEG", quality=80))
    assert result.kind == "cropped", result
    assert result.crop_box is None
    assert "extra area around it" in result.note


def test_added_caption_bar(picture):
    canvas = Image.new("RGB", (picture.width, picture.height + 100), "white")
    canvas.paste(picture, (0, 100))
    ImageDraw.Draw(canvas).text((20, 20), "A NEW HEADLINE", font=_font(), fill="black")
    result = relate(_bytes(picture), _bytes(canvas, "JPEG", quality=85))
    assert result.kind == "cropped", result
    assert result.crop_box is None
    assert "extra area around it" in result.note
    _assert_note(result)


def test_gif_first_frame(picture):
    gif = _bytes(picture, "GIF", save_all=True, append_images=[_picture(42)], duration=100, loop=0)
    with Image.open(io.BytesIO(gif)) as image:
        first = _bytes(image.convert("RGB"))
    assert relate(first, gif).kind == "resized"
    assert relate(_bytes(picture), gif).kind == "resized"
    assert fingerprint(gif).phash == fingerprint(first).phash
    assert fingerprint(gif).format == "GIF"


@pytest.mark.parametrize("size", [(800, 600), (2048, 1536)])
def test_exif_orientation(picture, size):
    picture = picture.resize(size)
    rotated = picture.transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[274] = 6
    data = _bytes(rotated, "JPEG", quality=90, exif=exif)
    result = fingerprint(data)
    assert (result.width, result.height) == size
    assert result.format == "JPEG"
    assert relate(_bytes(picture), data).kind == "resized"


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_fingerprint_fields(picture, image_format):
    data = _bytes(picture, image_format)
    result = fingerprint(data)
    assert isinstance(result, Fingerprint)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.format == image_format
    assert (result.width, result.height) == picture.size
    for value in (result.phash, result.dhash, result.whash):
        assert len(value) == 16
        assert 0 <= int(value, 16) < 2**64
    with pytest.raises(FrozenInstanceError):
        result.width = 1
    assert not hasattr(result, "__dict__")


def test_hashes_are_default_imagehash_on_small_images(picture):
    small = picture.resize((320, 240))
    result = fingerprint(_bytes(small))
    assert result.phash == str(imagehash.phash(small))
    assert result.dhash == str(imagehash.dhash(small))
    assert result.whash == str(imagehash.whash(small))


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\ntruncated"])
def test_invalid_bytes(picture, data):
    with pytest.raises(ValueError, match="decode"):
        fingerprint(data)
    for base, other in [(data, _bytes(picture)), (_bytes(picture), data), (data, data)]:
        with pytest.raises(ValueError, match="decode"):
            relate(base, other)


@pytest.mark.parametrize("size,color", [((8, 8), "red"), ((800, 600), "white")])
def test_unknown(picture, size, color):
    data = _bytes(Image.new("RGB", size, color))
    assert relate(_bytes(picture), data).kind == "unknown"
    assert relate(data, _bytes(picture)).kind == "unknown"
    assert relate(data, data).kind == "same"


@pytest.mark.parametrize(
    "image_format,progressive,size",
    [
        ("JPEG", False, (2048, 2048)),
        ("JPEG", True, (2048, 2048)),
        ("PNG", False, (2048, 2048)),
        ("JPEG", False, (4000, 500)),
    ],
)
def test_performance(image_format, progressive, size):
    original = _picture(size=size)
    variant = original.copy()
    ImageDraw.Draw(variant).rectangle((100, 100, 350, 350), fill="orange")
    base, other = (
        _bytes(image, image_format, quality=85, progressive=progressive)
        for image in (original, variant)
    )
    _assert_performance(base, other, "edited", f"{size} {image_format} progressive={progressive}")


def _assert_performance(base, other, expected, label):
    relate(base, other)
    durations = []
    for _ in range(5):
        start = time.perf_counter()
        result = relate(base, other)
        durations.append(time.perf_counter() - start)
    assert result.kind == expected, result
    median = float(np.median(durations))
    assert median < 0.300, durations
    print(f"{label}: median of 5 = {median * 1000:.1f} ms")


def test_tiny_vs_huge_performance(picture):
    tiny = _bytes(picture.resize((8, 8)))
    huge = _bytes(picture.resize((4096, 4096)), "JPEG", quality=85)
    _assert_performance(tiny, huge, "unknown", "tiny vs huge")
