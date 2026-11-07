import argparse
import base64
import hashlib
import json
import re
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from http.client import HTTPSConnection, HTTPException
from urllib.parse import parse_qsl, urlencode

from inspector import inspect_png
from library import Library
from recipe_repair import repair_recipe
from generation import GenerationService, MODELS, SAMPLERS, SCHEDULES, DEFAULTS, nearest_size

ROOT = Path(__file__).resolve().parent
MAX_PNG = 64 * 1024 * 1024
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/generate": ("generate.html", "text/html; charset=utf-8"),
          "/static/generate.js": ("generate.js", "text/javascript; charset=utf-8"),
          "/static/generate.css": ("generate.css", "text/css; charset=utf-8"),
          "/static/recipe-repair.js": ("recipe-repair.js", "text/javascript; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/static/style.css": ("style.css", "text/css; charset=utf-8")}


def style_fields(payload):
    values = [payload.get(field, "") for field in ("name", "description", "recipe")]
    if not all(isinstance(v, str) for v in values) or not values[0].strip() or not values[2].strip():
        raise ValueError("请填写画风名称和画风串")
    return values


def inspect(data):
    if len(data) > MAX_PNG:
        raise ValueError("图片超过 64 MB，请选择较小的原图")
    try:
        return inspect_png(data)
    except (OSError, SyntaxError, OverflowError) as exc:
        raise ValueError("图片读取失败，请重新选择原图") from exc


def discord_attachment(address):
    url = urlsplit(address)
    if (url.scheme != "https" or url.netloc not in ("cdn.discordapp.com", "media.discordapp.net")
            or not re.fullmatch(r"/(?:ephemeral-)?attachments/\d+/\d+/[^/\s]+", url.path)):
        raise ValueError("请拖入 Discord 图片附件，或点下载箭头后选择本地原图。")
    signature = urlencode([(k,v) for k,v in parse_qsl(url.query) if k in {"ex","is","hm"}])
    conn = HTTPSConnection("cdn.discordapp.com", timeout=15)
    try:
        conn.request("GET", url.path + ("?" + signature if signature else ""))
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("Discord 附件链接已失效或无法访问，请下载原图再拖入。")
        body = response.read(MAX_PNG + 1)
    except (OSError, HTTPException) as exc:
        raise ValueError("连接 Discord 附件失败，请在 Discord 下载原图后拖入。") from exc
    finally:
        conn.close()
    if len(body) > MAX_PNG:
        raise ValueError("图片超过 64 MB")
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return body, "image/png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return body, "image/webp"
    raise ValueError("该附件的文件格式不在支持范围内，请选择 PNG 或 WebP 原图。")


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, library, generator=None, **kwargs):
        self.library = library
        self.generator = generator
        super().__init__(*args, **kwargs)

    def send_body(self, code, body, kind):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=604800" if kind == "image/png" else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, payload, code=200):
        self.send_body(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def read_body(self, limit):
        length = int(self.headers.get("Content-Length", "-1"))
        if not 0 <= length <= limit:
            raise ValueError("请求过大或长度无效")
        return self.rfile.read(length)

    def handle_request(self):
        try:
            host = self.headers.get("Host", "")
            allowed = {f"{h}:{self.server.server_port}" for h in ("127.0.0.1", "localhost")}
            if host not in allowed or self.headers.get("Origin") not in (None, "http://" + host):
                return self.reply({"error": "仅支持本机同源访问"}, 403)
            path = urlsplit(self.path).path
            sid = re.fullmatch(r"/api/styles/(\d+)", path)
            if self.generator and self.generation_route(path):
                return
            if self.command == "GET":
                if path in ASSETS:
                    filename, kind = ASSETS[path]
                    return self.send_body(200, (ROOT / "static" / filename).read_bytes(), kind)
                if path == "/api/health":
                    return self.reply({"app": "nai-style-library"})
                if path == "/api/styles":
                    with self.library.connection() as db:
                        return self.reply({"styles": self.library.summaries(db)})
                if sid:
                    return self.reply(self.library.detail(int(sid[1])))
                if re.fullmatch(r"/images/[a-f0-9]{64}\.(png|webp)", path):
                    return self.send_body(200, (self.library.images / path[8:]).read_bytes(), "image/" + path.rsplit(".",1)[1])
            if path == "/api/discord-attachment" and self.command == "POST":
                if self.headers.get_content_type() != "application/json":
                    return self.reply({"error":"需要 JSON 数据"}, 415)
                address = json.loads(self.read_body(16384))
                if not isinstance(address,str):
                    raise ValueError("无效的附件地址")
                return self.send_body(200, *discord_attachment(address))
            if path == "/api/inspect" and self.command == "POST":
                data = self.read_body(MAX_PNG)
                return self.reply(inspect(data) | {"hash": hashlib.sha256(data).hexdigest()})
            if path == "/api/recipe/repair" and self.command == "POST":
                if self.headers.get_content_type() != "application/json":
                    return self.reply({"error": "需要 JSON 数据"}, 415)
                payload = json.loads(self.read_body(1_000_000))
                if not isinstance(payload, dict):
                    raise ValueError("无效的画师串内容")
                return self.reply(repair_recipe(payload.get("text")))
            if (path == "/api/save" and self.command == "POST") or (sid and self.command == "PUT"):
                if self.headers.get_content_type() != "application/json":
                    return self.reply({"error": "需要 JSON 数据"}, 415)
                payload = json.loads(self.read_body(MAX_PNG * 4 // 3 + 2_000_000))
                if not isinstance(payload, dict):
                    raise ValueError("无效的保存内容")
                if sid:
                    cover = payload.get("cover_id")
                    fields = style_fields(payload) if cover is None or "recipe" in payload else None
                    self.library.edit(int(sid[1]), fields, cover)
                    return self.reply(self.library.detail(int(sid[1])))
                target = payload.get("target_id")
                if target is not None and type(target) is not int:
                    raise ValueError("无效的收藏编号")
                fields = style_fields(payload) if target is None else None
                data = base64.b64decode(payload.get("image", ""), validate=True)
                meta = inspect(data)["metadata"]
                filename = Path(str(payload.get("filename") or "image.png")).name
                return self.reply(self.library.save(target, fields, data, meta, filename))
            self.reply({"error": "未找到该地址"}, 404)
        except (KeyError, FileNotFoundError):
            self.reply({"error": "找不到这条记录或文件"}, 404)
        except (ValueError, TypeError) as exc:
            self.reply({"error": str(exc)}, 400)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self.reply({"error": "本地服务处理失败"}, 500)

    do_GET = do_POST = do_PUT = handle_request

    def generation_route(self, path):
        g = self.generator
        if path.startswith(("/generation-assets/", "/generations/")) and self.command == "GET":
            if re.fullmatch(r"/generation-assets/[a-f0-9]{64}\.png", path):
                self.send_body(200, g.asset(path.rsplit("/", 1)[1]).read_bytes(), "image/png")
            elif re.fullmatch(r"/generations/[a-f0-9]{32}\.png", path):
                self.send_body(200, (g.directory / path.rsplit("/", 1)[1]).read_bytes(), "image/png")
            else:
                raise KeyError(path)
            return True
        if not path.startswith("/api/generation/"):
            return False
        action = path[len("/api/generation/"):]
        if self.command == "GET":
            if action == "capabilities":
                result = {"models": MODELS, "samplers": SAMPLERS, "schedules": SCHEDULES, "defaults": DEFAULTS}
            elif action == "settings":
                result = g.settings.public(g.settings.read())
            elif action == "draft":
                result = g.draft()
            elif action == "history":
                result = g.history()
            elif action == "job":
                result = g.job()
            elif re.fullmatch(r"jobs/[a-f0-9]{32}", action):
                result = g.job(action.split("/")[1])
            elif re.fullmatch(r"images/[a-f0-9]{32}", action):
                result = g.image(action.split("/")[1])
            else:
                raise KeyError(path)
        elif self.command in ("POST", "PUT"):
            if action == "upload":
                self.reply(g.upload(self.read_body(MAX_PNG)))
                return True
            if self.headers.get_content_type() != "application/json":
                raise ValueError("需要 JSON 数据")
            data = json.loads(self.read_body(2_000_000))
            if not isinstance(data, dict):
                raise ValueError("无效的数据")
            if action == "settings":
                result = g.settings.save(data)
            elif action == "draft":
                result = g.draft(data)
            elif action == "preview":
                result = g.preview(data)
            elif action == "start":
                result = g.start(data)
            elif action == "nearest-size":
                w, h = nearest_size(data["width"], data["height"])
                result = {"width": w, "height": h}
            elif action == "assistant":
                result = g.assistant(data)
            elif action == "models":
                result = g.models()
            elif action == "test":
                result = g.test_provider(data.get("provider"))
            elif re.fullmatch(r"jobs/[a-f0-9]{32}/stop", action):
                result = g.stop(action.split("/")[1])
            elif re.fullmatch(r"images/[a-f0-9]{32}/collect", action):
                result = g.collect(action.split("/")[1], data)
            elif re.fullmatch(r"images/[a-f0-9]{32}/reroll", action):
                result = g.reroll(action.split("/")[1], data)
            else:
                raise KeyError(path)
        else:
            raise ValueError("请求方式不支持")
        self.reply(result)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8724)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    library = Library(args.data)
    try:
        server = ThreadingHTTPServer((args.host, args.port), partial(Handler, library=library))
    except OSError:
        if args.open:
            try:
                with urlopen(url + "api/health", timeout=2) as res:
                    if json.load(res).get("app") == "nai-style-library":
                        webbrowser.open(url)
                        return
            except (OSError, ValueError):
                pass
        raise
    with server:
        repaired = library.repair_saved_recipes()
        if repaired:
            print(json.dumps(repaired, ensure_ascii=False), flush=True)
        server.RequestHandlerClass = partial(Handler, library=library, generator=GenerationService(library))
        print(url, flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()


if __name__ == "__main__":
    main()
