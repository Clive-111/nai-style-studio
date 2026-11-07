"""Read local NovelAI PNGs and prepare a conservative, editable style recipe."""
import gzip
import io
import json
import re
import zlib
from itertools import islice

from PIL import Image, UnidentifiedImageError
from recipe_repair import repair_recipe


MAX_PIXELS = 24_000_000
MAX_STEALTH_BYTES = 4 * 1024 * 1024


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _packed_lsb(pixels, width, height, channels):
    """NovelAI stores bytes MSB-first, reading pixels down each column."""
    value = count = 0
    for x in range(width):
        for y in range(height):
            pixel = pixels[x, y]
            for channel in channels:
                value = (value << 1) | (pixel[channel] & 1)
                count += 1
                if count == 8:
                    yield value
                    value = count = 0


def _stealth(image):
    # Stop after the prefix for ordinary PNGs. Decode just the recorded length.
    mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
    pixels_image = image if image.mode == mode else image.convert(mode)
    pixels = pixels_image.load()
    carriers = [("rgb", (0, 1, 2))]
    if mode == "RGBA":
        carriers.insert(0, ("png", (3,)))
    for carrier, channels in carriers:
        stream = _packed_lsb(pixels, *image.size, channels)
        header = bytes(islice(stream, 15))
        prefix = f"stealth_{carrier}".encode()
        if header not in (prefix + b"comp", prefix + b"info"):
            continue
        length = bytes(islice(stream, 4))
        bit_count = int.from_bytes(length, "big")
        if len(length) != 4 or bit_count % 8 or bit_count > MAX_STEALTH_BYTES * 8:
            continue
        data = bytes(islice(stream, bit_count // 8))
        if len(data) * 8 != bit_count:
            continue
        try:
            if header.endswith(b"comp"):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    data = compressed.read(MAX_STEALTH_BYTES + 1)
            if len(data) <= MAX_STEALTH_BYTES:
                record = _object(data.decode("utf-8"))
                if record:
                    return record
        except (OSError, EOFError, UnicodeDecodeError, zlib.error):
            continue
    return {}


SUBJECT = re.compile(
    r"(?:[0-9]*(?:girls?|boys?|others?)|solo(?: focus)?|duo|couple|woman|man|"
    r"spider[- ]gwen|gwen stacy|standing|sitting|lying|walking|running|kneeling|"
    r"looking at viewer|looking away|from (?:above|below|behind|side)|cowboy shot|"
    r"upper body|full body|close[- ]up|portrait|hands? on hips?|arms? crossed|"
    r"indoors|outdoors|city|cityscape|street|skyline|beach|forest|bedroom|"
    r"cyberpunk city|narrow waist|wide hips|"
    r"(?:short|long|blonde?|black|brown|red|pink|white|blue|green|silver) hair|"
    r"(?:blue|green|brown|red|pink|yellow|purple|black) eyes|"
    r"(?:official costume|original outfit)|(?:character|copyright):.+)"
)
WEIGHT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)::")
TOKEN = re.compile(WEIGHT.pattern + r"|::|[(){}\[\],\n]")
PAIRS = {"(": ")", "[": "]", "{": "}"}


def _is_subject(text):
    label = text.strip(" \t\r\n{}[]").lower().replace("_", " ")
    return bool(SUBJECT.fullmatch(label.strip()))


def _units(prompt):
    """Yield complete top-level tokens, including weighted/bracketed groups."""
    stack = []
    start = 0
    for match in TOKEN.finditer(prompt):
        mark = match[0]
        if WEIGHT.fullmatch(mark):
            stack.append("::")
        elif mark in PAIRS:
            stack.append(PAIRS[mark])
        elif stack and stack[-1] == mark:
            stack.pop()
        elif not stack and mark in (",", "\n"):
            yield prompt[start:match.start()]
            start = match.end()
    yield prompt[start:]


def _extract(prompt):
    kept = []
    warnings = []
    # Some generators leave the final weighted subject group open. Split only
    # when every tag in that suffix is an identifiable subject.
    tail = re.search(r"(?:^|,)\s*([+-]?\d+(?:\.\d+)?::)([^:]+?)\s*\Z", prompt)
    if tail:
        tags = [t for t in tail[2].split(",") if t.strip()]
        if tags and all(_is_subject(t) for t in tags):
            prompt = prompt[:tail.start()]
    for unit in _units(prompt):
        unit = unit.strip()
        if not unit:
            continue
        inner = WEIGHT.sub("", unit).replace("::", "")
        members = [t for t in inner.split(",") if t.strip()]
        subject_count = sum(_is_subject(t) for t in members)
        if members and subject_count == len(members):
            continue
        if subject_count:
            warnings.append("含题材词的分组已整组保留，请核对「" + unit + "」。")
        kept.append(unit)
    return ", ".join(kept) + ("," if kept else ""), warnings


TRAITS = [
    (r"muted.colors?|desaturated|pale.tone", "低饱和色调"),
    (r"pastel", "粉彩配色"),
    (r"3d\b|3d.render", "立体渲染"),
    (r"photorealistic|photo\s*\(medium\)|realistic.skin", "写实质感"),
    (r"anime.style|anime.coloring", "动漫笔触"),
    (r"painting\s*\(medium\)|painterly", "绘画质感"),
    (r"soft.light|soft.shadow|gentle.shading", "柔和光影"),
    (r"chiaroscuro|high.contrast", "明暗对照"),
    (r"cinematic.light", "电影感布光"),
    (r"depth.of.field|blurred.background|bokeh", "浅景深"),
    (r"watercolou?r", "水彩晕染"),
    (r"monochrome|grayscale", "黑白层次"),
    (r"cel.shad", "赛璐璐上色"),
    (r"line.?art", "线稿风格"),
]


def _caption(v4):
    return _object(_object(v4).get("caption"))


def _characters(caption):
    entries = caption.get("char_captions")
    if not isinstance(entries, list):
        return ""
    return "\n".join(str(entry.get("char_caption", "")) for entry in entries
                     if isinstance(entry, dict))


def _first(*values):
    return next((v for v in values if v is not None and v != ""), "")


def _webp_fields(image):
    exif = image.getexif()
    tags = dict(exif)
    if 34665 in exif:
        tags.update(exif.get_ifd(34665))
    fields = {}
    for number, name in ((270, "Description"), (305, "Software"), (272, "Source"),
                         (37500, "Comment"), (37510, "Comment")):
        value = tags.get(number)
        if isinstance(value, bytes):
            if value.startswith(b"UNICODE\0"):
                body = value[8:]
                codec = "utf-16" if body.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-16-le" if body[1:2] == b"\0" else "utf-16-be")
                value = body.decode(codec, "replace")
            else:
                value = value.removeprefix(b"ASCII\0\0\0").decode("utf-8", "replace")
        if isinstance(value, str) and value.strip("\0 "):
            value = value.strip("\0 ")
            record = _object(value)
            if name == "Comment" and ("Comment" in record or "Description" in record):
                fields.update(record)
            elif name != "Comment" or record or name not in fields:
                fields[name] = value
    return fields


def inspect_png(data: bytes) -> dict:
    try:
        with Image.open(io.BytesIO(data), formats=["PNG", "WEBP"]) as image:
            image_format = image.format.lower()
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ValueError("图片超过 2400 万像素，请选择较小的原图。")
            image.load()
            hidden = _stealth(image)
            fields = {**hidden, **(image.text if image_format == "png" else _webp_fields(image))}
            # Direct chunks take precedence; a partial Comment still benefits
            # from other fields available in the hidden generation record.
            params = {**_object(hidden.get("Comment")), **_object(fields.get("Comment"))}
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValueError("请选择完整的 PNG 或 WebP 原图；从 Discord 可点右上角下载箭头获取附件。") from exc

    normalized = {re.sub(r"(?<=[a-z])(?=[A-Z])", "_", str(k)).lower().replace(" ", "_"): v
                  for k, v in {**fields, **params}.items()}
    positive = _caption(normalized.get("v4_prompt"))
    negative = _caption(normalized.get("v4_negative_prompt"))
    prompt = _first(params.get("prompt"), fields.get("Prompt"), normalized.get("prompt"),
                    positive.get("base_caption"), normalized.get("description"))
    prompt = prompt if isinstance(prompt, str) else ""
    explicit = next((normalized[k] for k in ("style_recipe", "style_prompt", "artist_prompt",
                    "artist_string", "artist_chain", "artist_tags", "画师串", "画风串", "style")
                    if isinstance(normalized.get(k), str) and normalized[k].strip()), "")
    source = normalized.get("source", "")
    model = _first(normalized.get("model"), normalized.get("model_name"), source)
    if not normalized.get("model") and (normalized.get("model_hash") == "0ADF9AB7"
                                          or source == "NovelAI Diffusion V5 0ADF9AB7"):
        model = "NAI Diffusion V5 Full"
    request_type = normalized.get("request_type", "")
    metadata = {
        "prompt": prompt,
        "negative": _first(normalized.get("uc"), negative.get("base_caption"),
                           normalized.get("negative_prompt"), normalized.get("undesired_content")),
        "character_prompt": _characters(positive),
        "character_negative": _characters(negative),
        "model": model, "source": source,
        "request_type": "Text to Image" if request_type == "PromptGenerateRequest" else request_type,
        "seed": normalized.get("seed"), "steps": normalized.get("steps"),
        "width": width, "height": height, "image_format": image_format, "sampler": normalized.get("sampler"),
        "schedule": normalized.get("noise_schedule"),
        "guidance": _first(normalized.get("scale"), normalized.get("cfg_scale")),
        "rescale": normalized.get("cfg_rescale"),
        "uc_strength": normalized.get("uncond_scale"),
        "raw": fields,
    }
    warnings = []
    repaired = repair_recipe(explicit or prompt)
    if repaired['count']:
        warnings.append(f"已修复 {repaired['count']} 处画师 emoji 格式，原始记录已保留。")
    warnings.extend(item['message'] for item in repaired['warnings'])
    if explicit:
        recipe, mode = repaired['text'], "explicit"
    elif prompt:
        recipe, extraction_warnings = _extract(repaired['text'])
        warnings.extend(extraction_warnings)
        mode = "suggested"
    else:
        recipe, mode = "", "manual"
    traits = [label for pattern, label in TRAITS if re.search(pattern, recipe, flags=re.I)]
    return {
        "recipe": recipe, "mode": mode, "warnings": warnings, "metadata": metadata,
        "name": " · ".join(traits[:2]) or "未命名画风",
        "description": "、".join(traits[:4]),
    }
