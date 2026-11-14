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


@pytest.mark.parametrize("quality", [30, 70, 95])
def test_jpeg_reencode(picture, quality):
    result = relate(_bytes(picture), _bytes(picture, "JPEG", quality=quality))
    assert result.kind == "resized"
    _assert_note(result)


def test_resize_and_mild_brightness(picture):
    smaller = picture.resize((400, 300), Image.Resampling.LANCZOS)
    result = relate(_bytes(picture), _bytes(smaller, "JPEG", quality=70))
    assert result.kind == "resized"
    assert "800x600 to 400x300" in result.note
    brighter = ImageEnhance.Brightness(ImageEnhance.Contrast(picture).enhance(0.9)).enhance(1.05)
    assert relate(_bytes(picture), _bytes(brighter)).kind == "resized"


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
    assert result.crop_box == pytest.approx(expected, abs=0.05)
    assert result.edited_region is None
    assert ("base picture is a crop" in result.note) is reverse
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
    assert result.crop_box == pytest.approx((40 / 880, 100 / 740, 840 / 880, 700 / 740), abs=0.05)
    assert "base picture is a crop" in result.note


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


@pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
def test_2048_performance(image_format):
    original = _picture(size=(2048, 2048))
    variant = original.copy()
    ImageDraw.Draw(variant).rectangle((100, 100, 350, 350), fill="orange")
    base, other = _bytes(original, image_format), _bytes(variant, image_format)
    relate(base, other)
    durations = []
    for _ in range(3):
        start = time.perf_counter()
        result = relate(base, other)
        durations.append(time.perf_counter() - start)
    assert result.kind == "edited", result
    assert min(durations) < 0.300, durations
    print(f"2048 {image_format}: {min(durations) * 1000:.1f} ms")
