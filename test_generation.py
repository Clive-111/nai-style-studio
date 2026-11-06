"""Offline integration checks with synthetic fixtures and mock providers."""
import base64
import copy
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image
from app import Handler
from generation import GenerationService, DEFAULTS, validate, png_bytes, decode_output, endpoint, upgrade_prompt
from prompt_composer import recipe_units, parse_composition
from inspector import inspect_png
from library import Library


def png(color="navy", size=(128, 128)):
    return png_bytes(Image.new("RGB", size, color))


class OfflineGenerationTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent / "data" / "test-runs"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.library = Library(self.temp.name)
        self.calls = []
        self.fail_at = None
        self.delay = 0
        self.g = GenerationService(self.library, self.transport)
        self.g.settings.save({"nai": {"url": "https://mock.example", "key": "TEST_SECRET_123"},
                              "assistant": {"url": "https://mock.example/v1", "key": "ASSISTANT_SECRET", "model": "test-model"}})
        self.d = {**DEFAULTS, "final_prompt": "A cat near a window, painted in muted colors.\n\nartist:test", "recipe": "3::muted_color, soft_colors::, artist:test",
                  "negative": "bad quality, text", "width": 128, "height": 128}
        self.composition_reply = {"description": "A cat sits by an autumn window, softly lit in muted colors.",
                                  "artist_selection": [], "notes": ["沿用当前需求，清理旧题材。"]}

    def tearDown(self):
        deadline = time.monotonic()+5
        while self.g.active and time.monotonic()<deadline:
            time.sleep(.02)
        self.temp.cleanup()

    def transport(self, url, key, payload=None, timeout=150):
        self.calls.append((url, copy.deepcopy(payload)))
        if self.delay:
            time.sleep(self.delay)
        if self.fail_at == len(self.calls):
            raise ValueError("接口返回 HTTP 400：unsupported reference")
        if url.endswith("/chat/completions"):
            is_test = payload["messages"][0]["content"] == "Reply with OK only."
            answer = "OK" if is_test else (self.composition_reply if isinstance(self.composition_reply, str) else json.dumps(self.composition_reply))
            return json.dumps({"choices": [{"message": {"content": answer}}]}).encode(), "application/json"
        if url.endswith("/models"):
            return b'{"data":[{"id":"test-model"}]}', "application/json"
        if url.endswith("/subscription"):
            return b'{"tier":3,"active":true}', "application/json"
        data = png(size=(payload["parameters"]["width"], payload["parameters"]["height"]))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("image_0.png", data)
        return out.getvalue(), "application/zip"

    def wait(self, job):
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            info = self.g.job(job)["job"]
            if info["state"] not in ("queued", "composing", "running", "stopping") and not self.g.active:
                return info
            time.sleep(.025)
        self.fail("job timed out")

    def test_config_encryption_masking_and_preserve(self):
        text = self.g.settings.path.read_text()
        self.assertNotIn("TEST_SECRET", text)
        self.assertNotIn("mock.example", text)
        masked = self.g.settings.save({"nai": {"key": ""}})
        self.assertNotIn("key", masked["nai"])
        self.assertTrue(masked["nai"]["has_key"])
        self.assertEqual(self.g.settings.read()["nai"]["key"], "TEST_SECRET_123")
        self.g.settings.save({"nai": {"clear_key": True}})
        self.assertFalse(self.g.settings.read()["nai"]["key"])

    def test_validation_and_wire(self):
        preview = self.g.preview(self.d)
        self.assertFalse(self.calls)
        self.assertNotIn("input", preview["settings"])
        self.assertNotIn("v4_prompt", preview["settings"]["parameters"])
        self.assertEqual(preview["materials"]["recipe"], self.d["recipe"])
        p = self.g.request(validate(self.d), 123)
        self.assertEqual(p["input"], self.d["final_prompt"])
        self.assertEqual(p["parameters"]["negative_prompt"], self.d["negative"])
        self.assertEqual(p["parameters"]["v4_prompt"]["caption"]["base_caption"], p["input"])
        self.assertEqual(p["parameters"]["params_version"], 4)
        for patch in ({"width": 1080}, {"width": 4096, "height": 4096}, {"count": 5}, {"guidance": float("nan")},
                      {"use_reference": True}, {"mode": "infill"}, {"seed": -1}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate(self.d | patch)
        self.assertEqual(validate(self.d | {"schedule": "exponential"})["schedule"], "karras")

    def test_batch_metadata_history_and_collect_restart(self):
        self.d.update(count=4, seed=0)
        job = self.g.start(self.d)["job"]["id"]
        result = self.wait(job)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["completed"], 4)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(sum(c[0].endswith("/chat/completions") for c in self.calls), 1)
        self.assertEqual(len({c[1]["input"] for c in self.calls if c[0].endswith("/ai/generate-image")}), 1)
        self.assertEqual([c[1]["parameters"]["seed"] for c in self.calls if c[0].endswith("/ai/generate-image")], [0, 1, 2, 3])
        entry = self.g.image(result["images"][0])
        data = (self.g.directory / entry["filename"]).read_bytes()
        parsed = inspect_png(data)
        self.assertEqual(parsed["recipe"], self.d["recipe"])
        self.assertEqual(parsed["metadata"]["negative"], self.d["negative"])
        self.assertEqual(parsed["metadata"]["seed"], 0)
        saved = self.g.collect(entry["id"], {"name": "Mock Style"})
        detail = self.library.detail(saved["id"])
        self.assertEqual(detail["recipe"], self.d["recipe"])
        self.assertEqual(detail["examples"][0]["metadata"]["guidance"], 7)
        self.assertTrue(self.g.collect(entry["id"], {"target_id": saved["id"]})["duplicate"])
        self.g.draft(self.d)
        restart = GenerationService(self.library, self.transport)
        self.assertEqual(len(restart.history()["images"]), 4)
        self.assertEqual(restart.draft()["draft"]["recipe"], self.d["recipe"])

    def test_partial_failure_no_retry(self):
        self.fail_at = 3
        result = self.wait(self.g.start(self.d | {"count": 4})["job"]["id"])
        self.assertEqual((result["state"], result["completed"]), ("failed", 1))
        self.assertEqual(len(self.calls), 3)
        self.assertIn("unsupported reference", result["error"])
        self.assertEqual(len(self.g.history()["images"]), 1)

    def test_stop_and_duplicate_submission(self):
        self.delay = .3
        job = self.g.start(self.d | {"count": 4})["job"]["id"]
        deadline=time.monotonic()+2
        while len(self.calls)<2 and time.monotonic()<deadline: time.sleep(.01)
        with self.assertRaisesRegex(ValueError, "已有生成任务"):
            self.g.start(self.d)
        self.g.stop(job)
        result = self.wait(job)
        self.assertEqual((result["state"], result["completed"]), ("stopped", 1))
        self.assertEqual(len(self.calls), 2)

    def test_img2img_mask_and_reference(self):
        base = self.g.upload(png("red", (128, 64)))["asset"]
        mask_im=Image.new("L", (128, 128), 0)
        mask_im.paste(255, (32, 32, 64, 64))
        mask = self.g.upload(png_bytes(mask_im))["asset"]
        ref = self.g.upload(png("green", (64, 128)))["asset"]
        d=validate(self.d | {"base":base,"mask":mask,"mode":"infill","model":"nai-diffusion-4-5-full",
                          "reference":ref,"use_reference":True,"reference_type":"style","strength":.7})
        req=self.g.request(d,123)
        self.assertEqual(req["model"], "nai-diffusion-4-5-full-inpainting")
        self.assertEqual(req["action"], "infill")
        p=req["parameters"]
        with Image.open(io.BytesIO(base64.b64decode(p["image"]))) as im:
            self.assertEqual(im.size, (128,128));self.assertEqual(im.getpixel((0,0)), (0,0,0))
            self.assertEqual(im.getpixel((64,64)), (255,0,0))
        with Image.open(io.BytesIO(base64.b64decode(p["mask"]))) as im:
            self.assertEqual(im.size,(16,16));self.assertEqual(im.getpixel((5,5)),255);self.assertEqual(im.getpixel((0,0)),0)
        self.assertEqual(p["director_reference_descriptions"][0]["caption"]["base_caption"],"style")
        with Image.open(io.BytesIO(base64.b64decode(p["director_reference_images"][0]))) as im:
            self.assertEqual(im.size,(1024,1536))
        self.assertEqual(self.wait(self.g.start(d)["job"]["id"])["state"],"completed")
        d.update(mode="img2img",use_reference=False,fit="crop")
        self.assertEqual(self.wait(self.g.start(d)["job"]["id"])["state"],"completed")

    def test_assistant_and_containers(self):
        self.assertEqual(self.g.assistant({"action":"test"})["text"], "OK")
        for action in ("write", "vision", "clean"):
            with self.assertRaisesRegex(ValueError, "入口已更新"):
                self.g.assistant({"action": action})
        image=self.g.upload(png())["asset"]
        self.g.assistant({"action":"compose","analysis_image":image})
        self.assertIsInstance(self.calls[-1][1]["messages"][-1]["content"],list)
        self.assertFalse(self.g.history()["images"])
        self.assertEqual(self.g.models()["models"],["test-model"])
        self.assertIn("连接成功",self.g.test_provider("nai")["text"])
        data=png();encoded=base64.b64encode(data).decode()
        self.assertEqual(decode_output(data),[data])
        self.assertEqual(decode_output(json.dumps({"data":[{"b64_json":encoded}]}).encode()),[data])
        sse=('data: '+json.dumps({"image":encoded})+'\n\ndata: [DONE]\n').encode()
        self.assertEqual(decode_output(sse,"text/event-stream"),[data])
        self.assertEqual(endpoint('https://example.com/ai/generate-image','/user/subscription'),'https://example.com/user/subscription')

    def test_compose_mixed_artists_quality_and_reference_separation(self):
        image=self.g.upload(png())["asset"]
        recipe="1.25::artist:Foo_Bar, old_city, artist:Baz::, 0.8::unknown_artist::, low_saturation"
        self.composition_reply["artist_selection"]=[{"unit":0,"parts":[0,2]},{"unit":1,"parts":[0]}]
        source={"action":"compose","request":"画窗边的猫","recipe":recipe,"analysis_image":image,"quality":"standard"}
        result=self.g.assistant(source)
        final=result["final_prompt"]
        self.assertIn("1.25::artist:Foo_Bar, artist:Baz::",final)
        self.assertIn("0.8::unknown_artist::",final)
        self.assertNotIn("old_city",final)
        self.assertEqual(final.count("masterpiece"),1)
        self.assertTrue(result["notes"])
        self.assertEqual(result["composition"]["source"]["recipe"],recipe)
        messages=self.calls[-1][1]["messages"]
        self.assertIn("user's current request > the current analysis image",messages[0]["content"])
        self.assertIsInstance(messages[-1]["content"],list)
        self.assertTrue(messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        d=self.d|{"recipe":recipe,"final_prompt":final,"analysis_image":image,"quality":"standard","composition":result["composition"]}
        wire=self.g.request(validate(d), 123)
        self.assertEqual(wire["input"],final)
        self.assertEqual(wire["action"],"generate")
        self.assertNotIn("image",wire["parameters"])
        self.assertNotIn("director_reference_images",wire["parameters"])
        self.assertEqual(wire["parameters"]["negative_prompt"],d["negative"])
        wire=self.g.request(validate(d|{"mode":"img2img","base":image}), 123)
        self.assertIn("image",wire["parameters"])

    def test_oneshot_exact_snapshot_draft_history_and_png(self):
        negative="  lowres,\r\ntext,  "
        d=self.d|{"request":"窗边的猫", "final_prompt":"OLD AI RESULT", "negative":negative,
                  "composition":{"ai_prompt":"OLD AI RESULT"},"quality":"standard"}
        self.g.draft(d)
        before=self.g.draft()["draft"]
        self.delay=.1
        job_id=self.g.start(d)["job"]["id"]
        d["recipe"]="artist:Changed_After_Click"
        job=self.wait(job_id)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(self.calls[0][0].endswith("/chat/completions"))
        self.assertTrue(self.calls[1][0].endswith("/ai/generate-image"))
        image=self.g.image(job["images"][0]);snapshot=image["snapshot"]
        final=snapshot["composition"]["ai_prompt"]
        self.assertEqual(snapshot["final_prompt"],final)
        self.assertEqual(snapshot["sent_prompt"],final)
        self.assertEqual(self.calls[1][1]["input"],final)
        self.assertEqual(self.calls[1][1]["parameters"]["v4_prompt"]["caption"]["base_caption"],final)
        self.assertNotIn("OLD AI RESULT",json.dumps(self.calls))
        self.assertNotIn("Changed_After_Click",final)
        self.assertEqual(final.count("artist:test"),1)
        self.assertEqual(final.count("masterpiece"),1)
        self.assertEqual(snapshot["composition"]["source"]["recipe"],self.d["recipe"])
        self.assertEqual(self.g.draft()["draft"],before)
        meta=inspect_png((self.g.directory/image["filename"]).read_bytes())["metadata"]
        self.assertEqual(meta["prompt"],final)
        self.assertEqual(meta["negative"],negative)
        raw=json.loads(meta["raw"]["Comment"])["generation_settings"]
        self.assertEqual(raw["composition"],snapshot["composition"])
        self.assertEqual(raw["sent_prompt"],final)
        restarted=GenerationService(self.library,self.transport)
        self.assertEqual(restarted.draft()["draft"],before)
        self.assertEqual(restarted.image(image["id"])["snapshot"]["composition"],snapshot["composition"])
        # Reuse restores inputs, and each new click composes afresh.
        self.composition_reply["description"]="A new composition of a cat by the window."
        second=self.wait(self.g.start(snapshot)["job"]["id"])
        again=self.g.image(second["images"][0])["snapshot"]
        self.assertNotEqual(again["sent_prompt"],final)
        self.assertEqual(again["request"],"窗边的猫")

    def test_legacy_upgrade_and_source_validation(self):
        old={"prompt":"old subject","recipe":"artist:Example","quality":"standard"}
        draft=upgrade_prompt(old)
        self.assertEqual(draft["prompt_origin"],"legacy")
        self.assertEqual(draft["final_prompt"],"old subject, artist:Example, very aesthetic, masterpiece, no text")
        self.assertEqual(upgrade_prompt(draft),draft)
        exact="  recorded prompt\nwith spaces  "
        old_history=upgrade_prompt(old|{"final_prompt":exact,"final_negative":"negative, ","uc_preset":"heavy"},True)
        self.assertEqual(old_history["final_prompt"],exact)
        self.assertEqual(old_history["negative"],"negative, ")
        self.assertEqual(old_history["uc_preset"],"none")
        self.assertEqual(old_history["prompt_origin"],"history")
        self.assertEqual(validate(self.d|{"final_prompt":""})["final_prompt"],"")
        with self.assertRaisesRegex(ValueError,"中文需求"):
            validate(self.d|{"recipe":"","request":"","analysis_image":""})
        with self.assertRaisesRegex(ValueError,"刷新页面"):
            validate(self.d|{"prompt_version":2})
        v2=upgrade_prompt(self.d|{"prompt_version":2,"final_prompt":exact})
        self.assertEqual(v2["final_prompt"],exact)
        self.assertEqual(v2["prompt_version"],3)
        legacy_request=dict(self.d);legacy_request.pop("prompt_version")
        with self.assertRaisesRegex(ValueError,"刷新页面"):
            validate(legacy_request)

    def test_assistant_failure_and_invalid_source_indices(self):
        self.g.draft(self.d)
        before=self.g.draft()["draft"]
        self.composition_reply="invalid response"
        with self.assertRaisesRegex(ValueError,"输入素材已保留"):
            self.g.assistant({"action":"compose","request":"cat"})
        self.assertEqual(self.g.draft()["draft"],before)
        self.assertFalse(self.g.history()["images"])
        units=recipe_units("1.2::artist:Example::")
        with self.assertRaises(ValueError):
            parse_composition(json.dumps({"description":"scene","artist_selection":[{"unit":9,"parts":[0]}],"notes":[]}),units)
        # Source exactness is enforced locally even when an explicit artist is missed by the model.
        result=parse_composition(json.dumps({"description":"scene","artist_selection":[],"notes":[]}),units)
        self.assertEqual(result["artist_blocks"],["1.2::artist:Example::"])

    def test_composing_stop_and_failure_never_call_nai(self):
        self.delay=.3
        job=self.g.start(self.d)["job"]["id"]
        deadline=time.monotonic()+2
        while not self.calls and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.g.job(job)["job"]["state"],"composing")
        with self.assertRaisesRegex(ValueError,"已有生成任务"):
            self.g.start(self.d)
        self.g.stop(job)
        result=self.wait(job)
        self.assertEqual((result["state"],result["completed"]),("stopped",0))
        self.assertEqual(len(self.calls),1)
        self.assertTrue(result["record"]["composition"])
        self.calls.clear();self.delay=0;self.composition_reply="invalid"
        result=self.wait(self.g.start(self.d)["job"]["id"])
        self.assertEqual((result["state"],result["completed"]),("failed",0))
        self.assertIn("AI 整理失败",result["error"])
        self.assertEqual(len(self.calls),1)
        self.assertFalse(self.g.history()["images"])

    def test_preflight_and_restart_composing(self):
        for patch in ({"analysis_image":"a"*64+".png"},{"mode":"img2img","base":"a"*64+".png"}):
            with self.assertRaises(ValueError):self.g.start(self.d|patch)
        self.assertFalse(self.calls)
        self.g.settings.save({"assistant":{"clear_key":True}})
        with self.assertRaisesRegex(ValueError,"配置提示词助手"):self.g.start(self.d)
        self.assertFalse(self.calls)
        with self.library.connection() as db:
            db.execute("INSERT INTO generation_jobs(id,created,state,total,payload) VALUES(?,?,?,?,?)",
                       ("a"*32,time.time(),"composing",1,json.dumps(self.d)))
        restarted=GenerationService(self.library,self.transport)
        self.assertEqual(restarted.job("a"*32)["job"]["state"],"interrupted")

    def test_timeout_stop_and_secret_redaction(self):
        def fail(url, key, payload=None, timeout=150):
            self.calls.append(url)
            time.sleep(.15)
            raise ValueError("request timeout ASSISTANT_SECRET TEST_SECRET_123")
        self.g.transport=fail
        result=self.wait(self.g.start(self.d)["job"]["id"])
        self.assertEqual(result["state"],"failed")
        self.assertEqual(len(self.calls),1)
        self.assertIn("AI 整理失败",result["error"])
        self.assertNotIn("SECRET",result["error"])
        self.calls.clear()
        job=self.g.start(self.d)["job"]["id"]
        deadline=time.monotonic()+2
        while not self.calls and time.monotonic()<deadline:time.sleep(.01)
        self.g.stop(job)
        self.assertEqual(self.wait(job)["state"],"stopped")
        self.assertEqual(len(self.calls),1)

    def test_negative_preset_history_keeps_original_input(self):
        d=self.d|{"uc_preset":"heavy","negative":"  original negative  "}
        result=self.wait(self.g.start(d)["job"]["id"])
        snapshot=self.g.image(result["images"][0])["snapshot"]
        self.assertEqual(snapshot["negative"],d["negative"])
        self.assertEqual(snapshot["uc_preset"],"heavy")
        self.assertTrue(snapshot["final_negative"].endswith(d["negative"]))
        again=self.wait(self.g.start(snapshot)["job"]["id"])
        self.assertEqual(self.g.image(again["images"][0])["snapshot"]["final_negative"],snapshot["final_negative"])

    def test_analysis_only_does_not_set_nai_base(self):
        image=self.g.upload(png())["asset"]
        d=self.d|{"analysis_image":image,"negative":"  untouched negative  "}
        result=self.wait(self.g.start(d)["job"]["id"])
        self.assertEqual(result["state"],"completed")
        content=self.calls[0][1]["messages"][-1]["content"]
        self.assertIsInstance(content,list)
        self.assertNotIn("negative",json.loads(content[0]["text"]))
        wire=self.calls[1][1]
        self.assertEqual(wire["action"],"generate")
        self.assertNotIn("image",wire["parameters"])
        self.assertEqual(wire["parameters"]["negative_prompt"],d["negative"])
        self.assertNotIn("image",wire)
        self.calls.clear()
        self.wait(self.g.start(d|{"mode":"img2img","base":image})["job"]["id"])
        self.assertEqual(self.calls[1][1]["action"],"img2img")
        self.assertIn("image",self.calls[1][1]["parameters"])

    def test_http_routes_security(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,library=self.library,generator=self.g))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(base+'/generate') as r:self.assertIn('NAI 生图',r.read().decode())
            with urlopen(base+'/api/generation/capabilities') as r:self.assertEqual(len(json.load(r)['models']),4)
            with urlopen(base+'/api/generation/settings') as r:self.assertNotIn('TEST_SECRET',r.read().decode())
            request=Request(base+'/api/generation/start',data=json.dumps(self.d).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urlopen(request) as r:job=json.load(r)['job']['id']
            self.assertEqual(self.wait(job)['state'],'completed')
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base+'/api/generation/start',data=b'{}',headers={'Origin':'https://evil.example','Content-Type':'application/json'}))
            self.assertEqual(error.exception.code,403)
            error.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
