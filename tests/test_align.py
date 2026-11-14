"""media.align: the same photograph is found through captions, poster frames, crops and
thumbnails; an unrelated picture is not."""

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from media.align import Matcher, Placement, photo_crop


def photo(seed: int, size=(640, 427)) -> Image.Image:
    rng = np.random.default_rng(seed)
    image = Image.fromarray(rng.integers(0, 255, (size[1] // 8, size[0] // 8, 3), dtype=np.uint8))
    image = image.resize(size, Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(1.2))
    draw = ImageDraw.Draw(image)
    for _ in range(40):  # corners and edges for keypoints
        x, y = rng.integers(0, size[0]), rng.integers(0, size[1])
        w, h = rng.integers(10, 80), rng.integers(10, 80)
        draw.rectangle((x, y, x + w, y + h), fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    return image


def jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def captioned(image: Image.Image) -> Image.Image:
    image = image.copy()
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image.width, 60), fill="white")
    draw.text((20, 20), "WHEN THE BILL PASSES", fill="black")
    draw.rectangle((0, image.height - 60, image.width, image.height), fill="black")
    return image


def poster(image: Image.Image) -> Image.Image:
    frame = Image.new("RGB", (image.width + 160, image.height + 220), "black")
    frame.paste(image, (80, 60))
    ImageDraw.Draw(frame).text((frame.width // 2 - 40, image.height + 100), "BOREDOM", fill="white")
    return frame


@pytest.fixture(scope="module")
def base():
    return photo(1)


def test_same_photo_through_meme_edits(base):
    matcher = Matcher(jpeg(base))
    cases = {
        "recompressed": jpeg(base),
        "captioned": jpeg(captioned(base)),
        "poster": jpeg(poster(base)),
        "cropped": jpeg(base.crop((80, 40, 600, 400))),
        "thumbnail": jpeg(base.resize((120, 80))),
    }
    for name, data in cases.items():
        found = matcher.locate(data)
        assert found is not None and found.same_photo, name
    framed = matcher.locate(cases["poster"])
    # the photo sits inside the frame: it covers part of the poster, not all of it
    assert 0.4 < framed.coverage < 0.85
    left, top, right, bottom = framed.box
    assert 0.05 < left < 0.25 and 0.03 < top < 0.15 and right < 0.95


def test_unrelated_photo_is_not_the_same(base):
    matcher = Matcher(jpeg(base))
    for seed in (2, 3, 4):
        found = matcher.locate(jpeg(photo(seed)))
        assert found is None or not found.same_photo


def test_photo_crop_cuts_away_the_frame(base):
    matcher = Matcher(jpeg(base))
    framed = jpeg(poster(base))
    crop = photo_crop(framed, matcher.locate(framed))
    with Image.open(io.BytesIO(crop)) as image:
        ratio = image.width / image.height
    assert abs(ratio - base.width / base.height) < 0.08


def test_tiny_or_broken_images_raise_or_miss(base):
    matcher = Matcher(jpeg(base))
    with pytest.raises(ValueError):
        matcher.locate(jpeg(Image.new("RGB", (8, 8))))
    with pytest.raises(Exception):  # noqa: B017 - PIL's own error type
        matcher.locate(b"not an image")
    assert matcher.locate(jpeg(Image.new("RGB", (300, 200), "gray"))) is None
    assert Placement(40, 0.9, 1.0, 0.0, (0, 0, 1, 1), 1.0).same_photo
    assert not Placement(12, 0.9, 1.0, 0.0, (0, 0, 1, 1), 1.0).same_photo
