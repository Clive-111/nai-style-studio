"""Local NAI job queue, image history, request validation and prompt assistant."""
import base64
import copy
import hashlib
import io
import json
import math
import re
import secrets
import socket
import threading
import time
import uuid
import zipfile
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import urlsplit

from PIL import Image, ImageOps, ImageFilter, PngImagePlugin
from inspector import inspect_png
from provider_settings import ProviderSettings, validate_url
from prompt_composer import COMPOSE_INSTRUCTION, recipe_units, parse_composition
from recipe_repair import repair_recipe, require_clean


def repair_input(data, field, strict=True):
    result = require_clean(data[field], '画师串' if field == 'recipe' else '正向提示词') if strict else repair_recipe(data[field])
    data[field] = result['text']
    if result['count']:
        data.setdefault('prompt_repairs', []).append({'field': field, 'count': result['count'], 'changes': result['changes']})
    return result

SAMPLERS = {"k_euler_ancestral": "Euler Ancestral", "k_euler": "Euler",
            "k_dpmpp_2s_ancestral": "DPM++ 2S Ancestral", "k_dpmpp_2m_sde": "DPM++ 2M SDE",
            "k_dpmpp_2m": "DPM++ 2M", "k_dpmpp_sde": "DPM++ SDE"}
MODELS = [{"id": f"nai-diffusion-{version}-{kind}",
           "name": f"NAI Diffusion V{version.replace('-', '.')} {kind.title()}",
           "reference": version == "4-5", "schedule": version == "4-5",
           "inpaint": f"nai-diffusion-{'4-5' if version == '5' and kind == 'curated' else version}-{kind}-inpainting"}
          for version in ("5", "4-5") for kind in ("full", "curated")]
MODEL_MAP = {m["id"]: m for m in MODELS}
SCHEDULES = ["karras", "exponential", "polyexponential"]
MAX_PIXELS = 3_145_728
MAX_RESPONSE = 80 * 1024 * 1024
QUALITY = {"none": "", "standard": "very aesthetic, masterpiece, no text"}
NEGATIVE_PRESETS = {"none": "", "heavy": "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page"}
DEFAULTS = {"model": MODELS[0]["id"], "prompt": "", "recipe": "", "negative": "",
            "prompt_version": 3, "final_prompt": "", "analysis_image": "", "composition": None,
            "prompt_origin": "manual", "final_source": None,
            "request": "", "style_id": None, "style_name": "", "example_id": None,
            "width": 1024, "height": 1024, "sampler": "k_euler_ancestral", "steps": 23,
            "guidance": 7, "rescale": 0, "uc_strength": 0, "schedule": "karras",
            "seed": "", "count": 1, "mode": "generate", "base": "", "mask": "",
            "fit": "pad", "strength": 0.6, "noise": 0, "quality": "none", "uc_preset": "none",
            "reference": "", "reference_type": "character", "reference_strength": 0.6,
            "reference_fidelity": 1, "use_reference": False}


def number(data, key, low, high, integer=False):
    try:
        v = float(data[key])
    except (ValueError, TypeError, KeyError):
        raise ValueError(f"{key} 需要填写数字") from None
    if not math.isfinite(v) or not low <= v <= high or (integer and v != int(v)):
        raise ValueError(f"{key} 需在 {low}～{high} 之间" + ("，且为整数" if integer else ""))
    return int(v) if integer else v


def nearest_size(w, h):
    w, h = max(64, min(4096, float(w))), max(64, min(4096, float(h)))
    # Search all legal sizes; relative distance preserves the user's aspect as far as possible.
    return min(((a, b) for a in range(64, 4097, 64) for b in range(64, 4097, 64)
                if a*b <= MAX_PIXELS), key=lambda s: math.log(s[0]/w)**2 + math.log(s[1]/h)**2)


def join_prompts(*items):
    return ", ".join(s.strip().strip(",").strip() for s in items if s and s.strip().strip(",").strip())


def quality_suffix(data):
    quality = QUALITY.get(data.get("quality", "none"), "")
    if quality and data.get("model") == "nai-diffusion-4-5-curated":
        quality += ", -0.8::feet::, rating:general"
    return quality


def upgrade_prompt(raw, history=False):
    """Adapt stored legacy drafts/history without rewriting their database records."""
    result = {**DEFAULTS, **raw}
    if raw.get("prompt_version") not in (2, 3):
        recorded = raw.get("final_prompt")
        result["final_prompt"] = recorded if isinstance(recorded, str) else join_prompts(
            raw.get("prompt", ""), raw.get("recipe", ""), quality_suffix(raw))
        result["prompt_origin"] = "history" if history else "legacy"
    result["prompt_version"] = 3
    if history and raw.get("prompt_version") != 3 and isinstance(raw.get("final_negative"), str):
        result["negative"] = raw["final_negative"]
        result["uc_preset"] = "none"
    if result.get("final_prompt") and result.get("final_source") is None:
        result["final_source"] = (result.get("composition") or {}).get("source") or {
            k: result[k] for k in ("request", "recipe", "analysis_image", "model", "quality")}
    return result


def recorded_prompt(snapshot, metadata):
    """Read a persisted complete prompt before any legacy draft reconstruction."""
    for value in (snapshot.get("sent_prompt"), snapshot.get("final_prompt"),
                  metadata.get("prompt"), metadata.get("Prompt")):
        if isinstance(value, str) and value.strip():
            return value
    return None


def validate(raw, require_materials=True):
    if not isinstance(raw, dict):
        raise ValueError("无效的生图设置")
    if raw.get("prompt_version") != 3:
        raise ValueError("生图流程已更新，请刷新页面后点击「生成图片」")
    d = copy.deepcopy({k: raw.get(k, v) for k, v in DEFAULTS.items()})
    for key in ("prompt", "final_prompt", "recipe", "negative", "request", "style_name"):
        if not isinstance(d[key], str) or len(d[key]) > 60000:
            raise ValueError(f"{key} 内容无效或过长")
    if d["model"] not in MODEL_MAP:
        raise ValueError("请选择支持的 NAI 模型")
    for key, options in (("sampler", SAMPLERS), ("schedule", SCHEDULES), ("mode", ("generate", "img2img", "infill")),
                         ("fit", ("pad", "crop")), ("quality", QUALITY), ("uc_preset", NEGATIVE_PRESETS),
                         ("reference_type", ("character", "style", "character&style"))):
        if d[key] not in options:
            raise ValueError(f"不支持的 {key}")
    for k, a, b, integer in (("width", 64, 4096, True), ("height", 64, 4096, True),
            ("steps", 1, 50, True), ("guidance", 0, 10, False), ("rescale", 0, 1, False),
            ("uc_strength", 0, 1.5, False), ("count", 1, 4, True), ("strength", 0, 1, False),
            ("noise", 0, 1, False), ("reference_strength", 0, 1, False), ("reference_fidelity", 0, 1, False)):
        d[k] = number(d, k, a, b, integer)
    w, h = d["width"], d["height"]
    if w % 64 or h % 64 or w*h > MAX_PIXELS:
        near = nearest_size(w, h)
        raise ValueError(f"尺寸需为 64 的倍数，总像素至多 3,145,728。最近可用：{near[0]}×{near[1]}")
    if d["seed"] in ("", None):
        d["seed"] = None
    else:
        d["seed"] = number(d, "seed", 0, 4294967295, True)
    if not MODEL_MAP[d["model"]]["schedule"]:
        d["schedule"] = "karras"
    if not isinstance(d["analysis_image"], str):
        raise ValueError("无效的 AI 参考图")
    if require_materials and not any(d[k].strip() for k in ("request", "recipe", "analysis_image")):
        raise ValueError("请填写中文需求、选择画师串或添加 AI 参考图")
    if len(d["request"]) + len(d["recipe"]) > 65000:
        raise ValueError("整理素材内容过长")
    if d["composition"] is not None and (not isinstance(d["composition"], dict)
                                           or len(json.dumps(d["composition"])) > 700000):
        raise ValueError("整理记录过大或无效")
    if d["mode"] != "generate" and not d["base"]:
        raise ValueError("请先添加底图")
    if d["mode"] == "infill" and not d["mask"]:
        raise ValueError("请先绘制并应用重绘遮罩")
    if d["use_reference"]:
        if not MODEL_MAP[d["model"]]["reference"]:
            raise ValueError("精确参考目前支持 V4.5，请切换模型后使用")
        if not d["reference"]:
            raise ValueError("请添加人物／画风参考图")
    for k in ("style_id", "example_id"):
        if d[k] is not None and (type(d[k]) is not int or d[k] < 1):
            raise ValueError("无效的收藏或例图编号")
    return d


def png_bytes(im, metadata=None):
    info = PngImagePlugin.PngInfo()
    for k, v in (metadata or {}).items():
        info.add_text(k, v)
    out = io.BytesIO()
    im.save(out, "PNG", pnginfo=info)
    return out.getvalue()


def read_image(data):
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > 24_000_000 or im.width < 1 or im.height < 1:
            raise ValueError("图片尺寸超过 2400 万像素")
        im.load()
        return ImageOps.exif_transpose(im).convert("RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片读取失败，请选择 PNG、WebP 或 JPEG") from exc


def fitted(im, size, fit="pad"):
    im = im.convert("RGB")
    if fit == "crop":
        return ImageOps.fit(im, size, Image.Resampling.LANCZOS)
    resized = ImageOps.contain(im, size, Image.Resampling.LANCZOS)
    result = Image.new("RGB", size, (0, 0, 0))
    result.paste(resized, ((size[0]-resized.width)//2, (size[1]-resized.height)//2))
    return result


def endpoint(base, path):
    base = validate_url(base)
    if base.endswith(path):
        return base
    if re.search(r"/ai/generate-image(?:-stream)?$", base):
        return base if "generate-image" in path else base[:base.rfind("/ai/")] + path
    if path in ("/models", "/chat/completions"):
        for suffix in ("/chat/completions", "/models"):
            if base.endswith(suffix):
                base = base[:-len(suffix)]
        if not urlsplit(base).path.strip("/"):
            base += "/v1"
    return base + path


def remote(address, key, payload=None, timeout=150):
    p = urlsplit(validate_url(address))
    conn = (HTTPSConnection if p.scheme == "https" else HTTPConnection)(p.hostname, p.port, timeout=timeout)
    headers = {"Accept": "application/json, application/zip, image/png, text/event-stream",
               "User-Agent": "LocalStyleLibrary/1.0", "Authorization": "Bearer " + key}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request("POST" if payload is not None else "GET", p.path or "/", body, headers)
        r = conn.getresponse()
        data = r.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("接口响应超过 80 MB，请减少图片尺寸")
        if r.status < 200 or r.status >= 300:
            message = data.decode("utf-8", "replace")
            try:
                parsed = json.loads(message)
                message = parsed.get("message") or parsed.get("error") or parsed.get("detail") or message
                if isinstance(message, dict):
                    message = message.get("message") or json.dumps(message, ensure_ascii=False)
            except ValueError:
                message = re.sub(r"<[^>]+>", " ", message)
            message = str(message).replace(key, "[已隐藏]") if key else str(message)
            raise ValueError(f"接口返回 HTTP {r.status}：{message[:700]}")
        return data, r.getheader("Content-Type", "")
    except (socket.timeout, TimeoutError):
        raise ValueError("请求超时，出图结果尚未确认。请先检查中转记录，再决定是否手动重试；本次未自动重试。") from None
    except (OSError, HTTPException) as exc:
        raise ValueError("连接中断或接口不可达，结果尚未确认。请检查接口地址和网络，再手动重试。") from exc
    finally:
        conn.close()


def decode_output(data, kind=""):
    if data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [f for f in archive.infolist() if f.filename.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))]
            if not files or sum(f.file_size for f in files) > MAX_RESPONSE:
                raise ValueError("图片压缩包为空或超过大小限制")
            return [archive.read(f) for f in files]
    if data.startswith((b"\x89PNG", b"RIFF", b"\xff\xd8")):
        return [data]
    try:
        if "event-stream" in kind:
            values = [json.loads(s[5:].strip()) for s in data.decode().splitlines()
                      if s.startswith("data:") and s[5:].strip() not in ("", "[DONE]")]
            obj = next((x for x in reversed(values) if x.get("image") or x.get("output") or x.get("data")), {})
        else:
            obj = json.loads(data)
        if isinstance(obj, dict) and obj.get("error"):
            raise ValueError(str(obj["error"])[:600])
        value = obj if isinstance(obj, list) else obj.get("images", obj.get("data", obj.get("image", obj.get("output"))))
        if not isinstance(value, list):
            value = [value]
        result = []
        for v in value:
            if isinstance(v, dict):
                v = v.get("b64_json") or v.get("image") or v.get("base64")
            if not isinstance(v, str):
                raise ValueError("响应没有图片数据")
            if v.startswith("data:"):
                v = v.split(",", 1)[1]
            result.append(base64.b64decode(v, validate=True))
        if not result:
            raise ValueError("响应没有图片数据")
        return result
    except (ValueError, TypeError, AttributeError, zipfile.BadZipFile) as exc:
        raise ValueError("中转返回的图片格式无法读取（支持 PNG、WebP、ZIP、Base64 JSON 和 SSE）。" + str(exc)[:200]) from exc


class GenerationService:
    def __init__(self, library, transport=remote):
        self.library = library
        self.settings = ProviderSettings(library.directory)
        self.directory = library.directory / "generations"
        self.directory.mkdir(exist_ok=True)
        self.assets = self.directory / "assets"
        self.assets.mkdir(exist_ok=True)
        self.transport = transport
        self.lock = threading.RLock()
        self.active = None
        with library.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS generation_jobs(
                    id TEXT PRIMARY KEY, created REAL, state TEXT, total INTEGER,
                    completed INTEGER DEFAULT 0, error TEXT DEFAULT '', cancel INTEGER DEFAULT 0,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_images(
                    id TEXT PRIMARY KEY, job_id TEXT, created REAL, filename TEXT,
                    snapshot TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_draft(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            """)
            db.execute("UPDATE generation_jobs SET state='interrupted', error='本地服务已重启，尚未确认的请求请查看中转记录；未自动重试。' WHERE state IN ('queued','composing','running','stopping')")

    def asset(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}\.png", token):
            raise ValueError("无效的图片编号")
        path = self.assets / token
        if not path.is_file():
            raise ValueError("底图或遮罩文件已丢失，请重新添加")
        return path

    def upload(self, data):
        normalized = png_bytes(read_image(data))
        token = hashlib.sha256(normalized).hexdigest() + ".png"
        path = self.assets / token
        if not path.exists():
            path.write_bytes(normalized)
        with Image.open(io.BytesIO(normalized)) as im:
            return {"asset": token, "url": "/generation-assets/" + token, "width": im.width, "height": im.height}

    def request(self, d, seed, include_images=True):
        model = MODEL_MAP[d["model"]]
        prompt = require_clean(d["final_prompt"], '正向提示词')['text']
        negative = (NEGATIVE_PRESETS[d["uc_preset"]] + (", " if d["negative"] else "")
                    if d["uc_preset"] != "none" else "") + d["negative"]
        p = {"params_version": 4, "width": d["width"], "height": d["height"],
             "steps": d["steps"], "scale": d["guidance"], "sampler": d["sampler"],
             "seed": seed, "n_samples": 1, "cfg_rescale": d["rescale"],
             "uncond_scale": max(1e-5, d["uc_strength"]), "noise_schedule": d["schedule"],
             "negative_prompt": negative, "qualityToggle": False, "qualityPresetId": "none", "ucPreset": 3,
             "ucPresetId": "none", "dynamic_thresholding": False, "legacy": False,
             "legacy_v3_extend": False, "deliberate_euler_ancestral_bug": False, "prefer_brownian": True,
             "image_format": "png", "characterPrompts": [],
             "v4_prompt": {"caption": {"base_caption": prompt, "char_captions": []}, "use_coords": False, "use_order": True},
             "v4_negative_prompt": {"caption": {"base_caption": negative, "char_captions": []}, "legacy_uc": False}}
        if d["model"].startswith("nai-diffusion-5"):
            p["straight_alpha"] = True
        if d["mode"] != "generate":
            if include_images:
                im = fitted(read_image(self.asset(d["base"]).read_bytes()), (d["width"], d["height"]), d["fit"])
                p["image"] = base64.b64encode(png_bytes(im)).decode()
            else:
                p["image"] = "[底图：按目标尺寸" + ("等比补边]" if d["fit"] == "pad" else "居中裁剪]")
            p.update(strength=d["strength"], noise=d["noise"], extra_noise_seed=seed)
        if d["mode"] == "infill":
            if include_images:
                mask = read_image(self.asset(d["mask"]).read_bytes()).convert("L")
                if mask.size != (d["width"], d["height"]):
                    raise ValueError("遮罩尺寸与目标尺寸不符，请重新绘制并应用遮罩")
                if not mask.getbbox():
                    raise ValueError("遮罩为空，请涂抹需要重绘的区域")
                # NAI's inpainting mask is expressed in latent (1/8) coordinates.
                mask = mask.resize((d["width"]//8, d["height"]//8), Image.Resampling.BOX).point(lambda x: 255 if x > 0 else 0)
                p["mask"] = base64.b64encode(png_bytes(mask)).decode()
            else:
                p["mask"] = "[白色重绘 / 黑色保留，按 1/8 尺寸发送]"
            p.update(add_original_image=True, inpaintImg2ImgStrength=d["strength"])
        if d["use_reference"]:
            if include_images:
                ref = read_image(self.asset(d["reference"]).read_bytes())
                size = min(((1024, 1536), (1472, 1472), (1536, 1024)), key=lambda s: abs(math.log((s[0]/s[1])/(ref.width/ref.height))))
                encoded = base64.b64encode(png_bytes(fitted(ref, size))).decode()
            else:
                encoded = "[人物／画风参考图]"
            p.update(director_reference_images=[encoded],
                     director_reference_descriptions=[{"caption": {"base_caption": d["reference_type"], "char_captions": []}, "legacy_uc": False}],
                     director_reference_strength_values=[d["reference_strength"]],
                     director_reference_secondary_strength_values=[1-d["reference_fidelity"]],
                     director_reference_information_extracted=[1])
        return {"input": prompt, "model": model["inpaint"] if d["mode"] == "infill" else d["model"], "action": d["mode"], "parameters": p}

    def preview(self, raw):
        d = validate(raw)
        # Source/parameter preview is local and never calls the assistant.
        wire = self.request(d | {"final_prompt": ""}, d["seed"] if d["seed"] is not None else "随机（每张独立）", False)
        wire.pop("input")
        wire["parameters"].pop("v4_prompt")
        return {"materials": {k: d[k] for k in ("request", "recipe", "analysis_image", "quality")},
                "settings": wire, "note": "点击生成后自动整理，实际发送文本保存在当次记录中。"}

    def start(self, raw):
        d = validate(raw)
        repair_input(d, 'recipe')
        # Old outputs are archival data, never inputs to a fresh composition.
        d.update(final_prompt="", composition=None, final_source=None, prompt_origin="ai",
                 generation_kind="compose")
        return self.enqueue(d)

    def reroll(self, image_id, raw):
        entry = self.image_record(image_id)
        source, metadata = json.loads(entry["snapshot"]), json.loads(entry["metadata"])
        prompt = recorded_prompt(source, metadata)
        if prompt is None:
            raise ValueError("这张历史图缺少完整发送提示词，暂时无法重 Roll")
        d = validate(raw, require_materials=False)
        # Provenance follows the historical positive prompt. All generation controls,
        # negative text, base/mask/reference assets come from the current settings.
        for key in ("request", "recipe", "style_id", "style_name", "example_id",
                    "analysis_image", "composition", "final_source"):
            d[key] = copy.deepcopy(source.get(key, DEFAULTS[key]))
        d.update(final_prompt=prompt, prompt_origin="reroll", generation_kind="reroll",
                 source_image_id=image_id, quality="none")
        repair_input(d, 'recipe', strict=False)
        repair_input(d, 'final_prompt')
        return self.enqueue(d)

    def enqueue(self, d):
        composing = d["generation_kind"] == "compose"
        config = self.settings.read()
        if not config["nai"]["url"] or not config["nai"]["key"]:
            raise ValueError("请先在接口设置中填写 NAI 地址和 Token")
        if composing and not all(config["assistant"].get(k) for k in ("url", "key", "model")):
            raise ValueError("请先配置提示词助手的接口地址、API Key 和模型名")
        if composing and d["analysis_image"]:
            read_image(self.asset(d["analysis_image"]).read_bytes())
        # Validate assets and mask before committing a paid request to the queue.
        self.request(d, 0)
        with self.lock:
            if self.active:
                raise ValueError("已有生成任务正在进行，请等待完成或停止后续图片")
            job = uuid.uuid4().hex
            with self.library.connection() as db:
                db.execute("INSERT INTO generation_jobs(id,created,state,total,payload) VALUES(?,?,?,?,?)",
                           (job, time.time(), "queued", d["count"], json.dumps(d)))
            self.active = job
            threading.Thread(target=self.run, args=(job, d, config), daemon=True).start()
        return self.job(job)

    def run(self, job, d, config):
        composing = d["generation_kind"] == "compose"
        stage = "AI 整理" if composing else "NAI 生图"
        try:
            if composing:
                with self.lock:
                    with self.library.connection() as db:
                        if db.execute("SELECT cancel FROM generation_jobs WHERE id=?", (job,)).fetchone()[0]:
                            db.execute("UPDATE generation_jobs SET state='stopped' WHERE id=?", (job,))
                            return
                        db.execute("UPDATE generation_jobs SET state='composing' WHERE id=?", (job,))
                composed = self.assistant({"action": "compose", **{k: d[k] for k in
                    ("request", "recipe", "analysis_image", "model", "quality")}}, config["assistant"])
                d.update(final_prompt=composed["final_prompt"], composition=composed["composition"],
                         final_source=composed["composition"]["source"])
                repair_input(d, 'final_prompt')
                with self.library.connection() as db:
                    db.execute("UPDATE generation_jobs SET payload=? WHERE id=?", (json.dumps(d), job))
            stage = "NAI 生图"
            for i in range(d["count"]):
                with self.lock:
                    with self.library.connection() as db:
                        if db.execute("SELECT cancel FROM generation_jobs WHERE id=?", (job,)).fetchone()[0]:
                            db.execute("UPDATE generation_jobs SET state='stopped' WHERE id=?", (job,))
                            break
                        db.execute("UPDATE generation_jobs SET state='running' WHERE id=?", (job,))
                seed = secrets.randbelow(2**32) if d["seed"] is None else (d["seed"]+i) % 2**32
                snapshot = copy.deepcopy(d)
                snapshot["seed"] = seed
                request = self.request(snapshot, seed)
                address = endpoint(config["nai"]["url"], "/ai/generate-image")
                if address.endswith("-stream"):
                    request["parameters"]["stream"] = "sse"
                body, kind = self.transport(address, config["nai"]["key"], request)
                images = decode_output(body, kind)
                # n_samples is exactly one. Unexpected containers are surfaced, not silently multiplied.
                if len(images) != 1:
                    raise ValueError(f"请求 1 张，中转返回 {len(images)} 张，已停止后续请求；请检查中转接口格式")
                self.persist(job, images[0], snapshot, request)
                with self.library.connection() as db:
                    db.execute("UPDATE generation_jobs SET completed=completed+1 WHERE id=?", (job,))
            else:
                with self.library.connection() as db:
                    db.execute("UPDATE generation_jobs SET state=CASE WHEN cancel=1 THEN 'stopped' ELSE 'completed' END WHERE id=?", (job,))
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else "本地处理生成结果失败，已停止后续请求"
            for provider in config.values():
                if provider.get("key"):
                    message = message.replace(provider["key"], "[已隐藏]")
            with self.library.connection() as db:
                cancelled = db.execute("SELECT cancel FROM generation_jobs WHERE id=?", (job,)).fetchone()[0]
                state = "stopped" if cancelled and stage == "AI 整理" else "failed"
                db.execute("UPDATE generation_jobs SET state=?,error=? WHERE id=?", (state, (stage+"失败："+message)[:1200], job))
        finally:
            with self.lock:
                self.active = None

    def persist(self, job, data, snapshot, request):
        im = read_image(data)
        try:
            returned = inspect_png(data)["metadata"]
        except (ValueError, OSError):
            returned = {}
        # A provider's explicit seed has precedence over the requested seed.
        if type(returned.get("seed")) is int:
            snapshot["seed"] = returned["seed"]
        snapshot["width"], snapshot["height"] = im.size
        p = copy.deepcopy(request["parameters"])
        for key in ("image", "mask", "director_reference_images"):
            p.pop(key, None)
        p.update(prompt=request["input"], seed=snapshot["seed"], width=im.width, height=im.height,
                 model=request["model"], style_recipe=snapshot["recipe"], uncond_scale=snapshot["uc_strength"],
                 effective_uncond_scale=request["parameters"]["uncond_scale"],
                 request_type=request["action"], generation_settings=snapshot)
        snapshot["actual_model"] = request["model"]
        snapshot["final_prompt"] = request["input"]
        snapshot["sent_prompt"] = request["input"]
        snapshot["final_negative"] = p["negative_prompt"]
        result = png_bytes(im, {"Software": "NovelAI", "Source": request["model"],
                               "Description": request["input"], "Comment": json.dumps(p, ensure_ascii=False)})
        meta = inspect_png(result)["metadata"]
        image_id = uuid.uuid4().hex
        filename = image_id + ".png"
        (self.directory / filename).write_bytes(result)
        with self.library.connection() as db:
            db.execute("INSERT INTO generation_images VALUES(?,?,?,?,?,?)",
                       (image_id, job, time.time(), filename, json.dumps(snapshot), json.dumps(meta)))
        return image_id

    def job(self, job=None):
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM generation_jobs " + ("WHERE id=?" if job else "ORDER BY created DESC LIMIT 1"), (job,) if job else ()).fetchone()
            if not row:
                return {"job": None}
            result = {k: row[k] for k in row.keys() if k != "payload"}
            saved = json.loads(row["payload"])
            result["generation_kind"] = saved.get("generation_kind", "compose")
            result["source_image_id"] = saved.get("source_image_id")
            result["record"] = {k: saved.get(k) for k in ("request", "recipe", "negative", "analysis_image", "composition", "final_prompt", "prompt_repairs")}
            result["images"] = [r[0] for r in db.execute("SELECT id FROM generation_images WHERE job_id=? ORDER BY created", (row["id"],))]
            return {"job": result}

    def stop(self, job):
        with self.lock:
            with self.library.connection() as db:
                db.execute("UPDATE generation_jobs SET cancel=1,state='stopping' WHERE id=? AND state IN ('running','queued','composing')", (job,))
        return self.job(job)

    def history(self):
        with self.library.connection() as db:
            rows = db.execute("SELECT * FROM generation_images ORDER BY created DESC").fetchall()
            return {"images": [{"id": r["id"], "job_id": r["job_id"], "created": r["created"],
                                "url": "/generations/"+r["filename"],
                                "style_name": json.loads(r["snapshot"]).get("style_name", ""),
                                "model": json.loads(r["snapshot"])["model"]} for r in rows]}

    def image_record(self, image_id):
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM generation_images WHERE id=?", (image_id,)).fetchone()
        if not row:
            raise KeyError(image_id)
        return row

    def image(self, image_id):
        row = self.image_record(image_id)
        source, metadata = json.loads(row["snapshot"]), json.loads(row["metadata"])
        available = recorded_prompt(source, metadata) is not None
        return {**{k: row[k] for k in ("id", "created", "filename", "job_id")}, "url": "/generations/"+row["filename"],
                "snapshot": upgrade_prompt(source, history=True), "metadata": metadata,
                "reroll": {"available": available, "reason": "" if available else "这张历史图缺少完整发送提示词，暂时无法重 Roll"}}

    def collect(self, image_id, payload):
        entry = self.image(image_id)
        target = payload.get("target_id")
        if target is not None and type(target) is not int:
            raise ValueError("无效的收藏编号")
        fields = None
        if target is None:
            name = str(payload.get("name") or entry["snapshot"].get("style_name") or "新画风").strip()
            recipe = entry["snapshot"]["recipe"]
            if not recipe.strip():
                raise ValueError("本次没有画师串，请先在收藏台解析并补充配方")
            fields = [name, str(payload.get("description", "")), recipe]
        return self.library.save(target, fields, (self.directory / entry["filename"]).read_bytes(), entry["metadata"], entry["filename"])

    def draft(self, data=None):
        with self.library.connection() as db:
            if data is not None:
                # Drafts can be incomplete; validation happens at preview/submission.
                upgraded = upgrade_prompt(data)
                value = {k: upgraded.get(k, v) for k, v in DEFAULTS.items()}
                value['recipe'] = repair_recipe(value['recipe'])['text']
                db.execute("INSERT OR REPLACE INTO generation_draft VALUES(1,?)", (json.dumps(value),))
                return {"saved": True}
            row = db.execute("SELECT payload FROM generation_draft WHERE id=1").fetchone()
            return {"draft": upgrade_prompt(json.loads(row[0])) if row else copy.deepcopy(DEFAULTS)}

    def assistant(self, payload, config=None):
        config = config or self.settings.read()["assistant"]
        if not config["url"] or not config["key"] or not config["model"]:
            raise ValueError("请先配置提示词助手的接口地址、API Key 和模型名")
        action = payload.get("action", "compose")
        if action not in ("compose", "test"):
            raise ValueError("写词入口已更新，请刷新页面后点击「生成图片」")
        source = None
        units = []
        if action == "test":
            instruction, text = "Reply with OK only.", "Connection test"
        else:
            source = {k: payload.get(k, DEFAULTS[k]) for k in ("request", "recipe", "analysis_image", "model", "quality")}
            if any(not isinstance(v, str) for v in source.values()) or len(source["request"])+len(source["recipe"]) > 65000:
                raise ValueError("整理素材内容无效或过长")
            if source["model"] not in MODEL_MAP or source["quality"] not in QUALITY:
                raise ValueError("请选择有效的模型与质量设置")
            repair_input(source, 'recipe')
            if not any(source[k].strip() for k in ("request", "recipe", "analysis_image")):
                raise ValueError("请填写画面需求、选择画师串或添加 AI 参考图")
            units = recipe_units(source["recipe"])
            instruction = COMPOSE_INSTRUCTION
            text = json.dumps({"user_request": source["request"], "style_recipe": source["recipe"],
                               "model": source["model"], "quality_suffix": quality_suffix(source),
                               "recipe_units": [{k: u[k] for k in ("id", "text", "parts")} for u in units]}, ensure_ascii=False)
        if len(text) > 260000:
            raise ValueError("助手输入过长")
        content = text
        if source and source["analysis_image"]:
            data = self.asset(source["analysis_image"]).read_bytes()
            im = read_image(data)
            im.thumbnail((1536, 1536))
            content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes(im)).decode()}}]
        body, _ = self.transport(endpoint(config["url"], "/chat/completions"), config["key"],
                                 {"model": config["model"], "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": content}], "stream": False}, 90)
        try:
            answer = json.loads(body)["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise ValueError("助手没有返回可用文字，请检查模型是否支持聊天／视觉请求") from None
        if action == "test":
            return {"text": answer.strip()}
        result = parse_composition(answer, units, quality_suffix(source))
        return {**result, "composition": {"ai_prompt": result["final_prompt"], "notes": result["notes"],
                "artist_blocks": result["artist_blocks"], "source": source, "model": config["model"], "created": time.time()}}

    def test_provider(self, kind):
        if kind == "assistant":
            return self.assistant({"action": "test"})
        config = self.settings.read()["nai"]
        if not config["url"] or not config["key"]:
            raise ValueError("请先保存 NAI 地址与 Token")
        body, _ = self.transport(endpoint(config["url"], "/user/subscription"), config["key"], None, 20)
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or not any(k in value for k in ("tier", "active", "trainingStepsLeft", "usage")):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("接口已响应，订阅检查返回格式不兼容；请向中转确认支持的接口") from None
        return {"text": "连接成功，Token 可访问账户信息。图像功能以实际生成响应为准。"}

    def models(self):
        config = self.settings.read()["assistant"]
        if not config["url"] or not config["key"]:
            raise ValueError("请先保存助手地址和 API Key")
        body, _ = self.transport(endpoint(config["url"], "/models"), config["key"], None, 20)
        try:
            return {"models": sorted(str(m["id"]) for m in json.loads(body)["data"] if "id" in m)}
        except (ValueError, TypeError, KeyError):
            raise ValueError("该中转没有返回兼容的模型列表，请手动填写模型名") from None
