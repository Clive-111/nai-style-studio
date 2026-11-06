"""Offline tests for historical prompt rerolls, isolated from the live library."""
import json
import threading
import time
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from PIL import Image
from app import Handler
from generation import DEFAULTS, GenerationService, validate, png_bytes, NEGATIVE_PRESETS
from inspector import inspect_png
import test_generation as fixtures
from test_generation import png


class RerollTests(unittest.TestCase):
    setUp = fixtures.OfflineGenerationTests.setUp
    tearDown = fixtures.OfflineGenerationTests.tearDown
    transport = fixtures.OfflineGenerationTests.transport
    wait = fixtures.OfflineGenerationTests.wait

    def source(self):
        d = validate(self.d | {"request": "Original scene", "style_id": 7, "example_id": 9,
            "style_name": "Original style", "recipe": "1.2::artist:Original::", "seed": 123,
            "final_prompt": "  A fox near a window.\r\n\r\n1.2::artist:Original::, masterpiece  ",
            "analysis_image": "a"*64+".png",
            "composition": {"ai_prompt": "Earlier AI text before editing", "notes": ["Original note"],
                            "source": {"request": "Original scene", "recipe": "1.2::artist:Original::"}}})
        image_id = self.g.persist("source-job", png(), d, self.g.request(d, 123))
        return image_id, self.g.image(image_id)["snapshot"]

    def test_exact_prompt_current_settings_batch_and_lineage(self):
        image_id, source = self.source()
        source_record = dict(self.g.image_record(image_id))
        current = self.d | {"request": "Changed input", "recipe": "artist:Different", "final_prompt": "FAKE",
            "model": "nai-diffusion-4-5-full", "negative": "  left negative\r\n,  ", "uc_preset": "heavy",
            "width": 192, "height": 128, "sampler": "k_dpmpp_2m_sde", "steps": 28, "guidance": 5.5,
            "rescale": .1, "uc_strength": .3, "schedule": "exponential", "seed": 0, "count": 4,
            "quality": "standard", "analysis_image": "b"*64+".png"}
        self.g.draft(current)
        draft_before = self.g.draft()
        self.g.settings.save({"assistant": {"clear_key": True, "model": "", "url": ""}})
        result = self.wait(self.g.reroll(image_id, current)["job"]["id"])
        self.assertEqual((result["state"], result["completed"]), ("completed", 4))
        self.assertEqual(result["generation_kind"], "reroll")
        self.assertEqual(result["source_image_id"], image_id)
        self.assertEqual(len(self.calls), 4)
        for i, (url, wire) in enumerate(self.calls):
            self.assertTrue(url.endswith("/ai/generate-image"))
            self.assertEqual(wire["input"], source["sent_prompt"])
            self.assertEqual(wire["model"], current["model"])
            self.assertEqual(wire["action"], "generate")
            p = wire["parameters"]
            self.assertEqual(p["v4_prompt"]["caption"]["base_caption"], source["sent_prompt"])
            self.assertEqual(p["negative_prompt"], NEGATIVE_PRESETS["heavy"]+", "+current["negative"])
            for key, value in {"width":192,"height":128,"sampler":"k_dpmpp_2m_sde","steps":28,
                "scale":5.5,"cfg_rescale":.1,"uncond_scale":.3,"noise_schedule":"exponential","seed":i}.items():
                self.assertEqual(p[key], value)
            self.assertNotIn("image", p)
        entry = self.g.image(result["images"][0]); saved = entry["snapshot"]
        self.assertEqual(saved["recipe"], source["recipe"])
        self.assertEqual(saved["style_id"], source["style_id"])
        self.assertEqual(saved["example_id"], source["example_id"])
        self.assertEqual(saved["style_name"], source["style_name"])
        self.assertEqual(saved["request"], source["request"])
        self.assertEqual(saved["composition"], source["composition"])
        self.assertEqual(saved["negative"], current["negative"])
        self.assertEqual(saved["quality"], "none")
        self.assertEqual(saved["source_image_id"], image_id)
        self.assertEqual(self.g.draft(), draft_before)
        self.assertEqual(dict(self.g.image_record(image_id)), source_record)
        meta = inspect_png((self.g.directory / entry["filename"]).read_bytes())["metadata"]
        raw = json.loads(meta["raw"]["Comment"])["generation_settings"]
        self.assertEqual(meta["prompt"], source["sent_prompt"])
        self.assertEqual(raw["source_image_id"], image_id)
        self.assertEqual(raw["composition"], source["composition"])
        self.assertEqual(raw["seed"], 0)
        self.assertEqual(self.g.collect(entry["id"], {"name":"Rerolled style"})["duplicate"], False)
        restart = GenerationService(self.library, self.transport)
        self.assertEqual(restart.image(entry["id"])["snapshot"]["source_image_id"], image_id)

    def test_random_seed_empty_inputs_and_repeated_reroll(self):
        image_id, source = self.source()
        # Reroll is valid with a blank drafting area and needs no assistant reference asset.
        current = {**DEFAULTS, "width":128, "height":128, "count":2, "seed":""}
        with patch("generation.secrets.randbelow", side_effect=[11, 29]):
            result = self.wait(self.g.reroll(image_id, current)["job"]["id"])
        self.assertEqual([c[1]["parameters"]["seed"] for c in self.calls], [11,29])
        again = self.wait(self.g.reroll(result["images"][0], current | {"count":1,"seed":18})["job"]["id"])
        saved = self.g.image(again["images"][0])["snapshot"]
        self.assertEqual(saved["sent_prompt"], source["sent_prompt"])
        self.assertEqual(saved["source_image_id"], result["images"][0])
        self.assertEqual(saved["recipe"], source["recipe"])

    def test_recorded_prompt_fallbacks_and_missing(self):
        image_id, _ = self.source()
        exact = "  recorded\r\ntext  "
        for raw, metadata in [({"sent_prompt":exact,"final_prompt":"later"}, {"prompt":"other"}),
                              ({"final_prompt":exact}, {}), ({}, {"prompt":exact}), ({}, {"Prompt":exact})]:
            with self.library.connection() as db:
                db.execute("UPDATE generation_images SET snapshot=?,metadata=? WHERE id=?",
                           (json.dumps(raw), json.dumps(metadata), image_id))
            self.assertTrue(self.g.image(image_id)["reroll"]["available"])
            result = self.wait(self.g.reroll(image_id, self.d)["job"]["id"])
            self.assertEqual(result["state"], "completed")
            self.assertEqual(self.calls[-1][1]["input"], exact)
        with self.library.connection() as db:
            db.execute("UPDATE generation_images SET snapshot=?,metadata='{}' WHERE id=?",
                       (json.dumps({"prompt":"old subject", "recipe":"old recipe", "composition":{"ai_prompt":"unconfirmed"}}), image_id))
        entry = self.g.image(image_id)
        self.assertTrue(entry["snapshot"]["final_prompt"])  # legacy reconstruction exists only for viewing
        self.assertFalse(entry["reroll"]["available"])
        self.assertIn("缺少完整发送提示词", entry["reroll"]["reason"])
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, "缺少完整发送提示词"):
            self.g.reroll(image_id, self.d)
        self.assertEqual(len(self.calls), before)

    def test_current_image_modes_and_preflight(self):
        image_id, _ = self.source()
        base = self.g.upload(png("red", (192,128)))["asset"]
        mask = Image.new("L", (128,128)); mask.paste(255, (20,20,60,60))
        mask_token = self.g.upload(png_bytes(mask))["asset"]
        reference = self.g.upload(png("green"))["asset"]
        d = self.d | {"model":"nai-diffusion-4-5-full", "mode":"img2img", "base":base,
                      "strength":.45,"noise":.15,"fit":"crop"}
        self.assertEqual(self.wait(self.g.reroll(image_id,d)["job"]["id"])["state"], "completed")
        wire = self.calls[-1][1]
        self.assertEqual(wire["action"], "img2img")
        self.assertIn("image", wire["parameters"])
        self.assertEqual(wire["parameters"]["strength"], .45)
        self.assertEqual(wire["parameters"]["noise"], .15)
        d.update(mode="infill",mask=mask_token,use_reference=True,reference=reference,reference_type="style")
        self.assertEqual(self.wait(self.g.reroll(image_id,d)["job"]["id"])["state"], "completed")
        wire = self.calls[-1][1]
        self.assertEqual(wire["model"], "nai-diffusion-4-5-full-inpainting")
        self.assertEqual(wire["action"], "infill")
        self.assertIn("mask", wire["parameters"])
        self.assertIn("director_reference_images", wire["parameters"])
        before=len(self.calls)
        for changes in ({"base":""},{"mask":""},{"mask":"a"*64+".png"},{"width":1080}):
            with self.assertRaises(ValueError):self.g.reroll(image_id,d|changes)
        self.assertEqual(len(self.calls),before)

    def test_stop_shared_lock_partial_failure_and_no_retry(self):
        image_id, _ = self.source()
        self.delay=.2
        job=self.g.reroll(image_id,self.d|{"count":4})["job"]["id"]
        deadline=time.monotonic()+2
        while not self.calls and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.g.job(job)["job"]["state"],"running")
        with self.assertRaisesRegex(ValueError,"已有生成任务"):self.g.start(self.d)
        with self.assertRaisesRegex(ValueError,"已有生成任务"):self.g.reroll(image_id,self.d)
        self.g.stop(job)
        result=self.wait(job)
        self.assertEqual((result["state"],result["completed"]),("stopped",1))
        self.assertEqual(len(self.calls),1)
        self.calls.clear();self.fail_at=2;self.delay=0
        result=self.wait(self.g.reroll(image_id,self.d|{"count":4})["job"]["id"])
        self.assertEqual((result["state"],result["completed"]),("failed",1))
        self.assertEqual(len(self.calls),2)
        self.assertIn("NAI 生图失败",result["error"])
        self.calls.clear();self.fail_at=None;self.delay=.2
        job=self.g.start(self.d)["job"]["id"]
        deadline=time.monotonic()+2
        while not self.calls and time.monotonic()<deadline:time.sleep(.01)
        with self.assertRaisesRegex(ValueError,"已有生成任务"):self.g.reroll(image_id,self.d)
        self.g.stop(job);self.wait(job)

    def test_reroll_http_route(self):
        image_id, source=self.source()
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,library=self.library,generator=self.g))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def request(path,data):
            return urlopen(Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'},method='POST'))
        try:
            with request('/api/generation/images/'+image_id+'/reroll',self.d|{'negative':'current'}) as response:
                job=json.load(response)['job']
            self.assertEqual(job['generation_kind'],'reroll')
            self.assertEqual(self.wait(job['id'])['state'],'completed')
            self.assertEqual(len(self.calls),1)
            self.assertEqual(self.calls[0][1]['input'],source['sent_prompt'])
            with self.assertRaises(HTTPError) as error:
                request('/api/generation/images/'+'a'*32+'/reroll',self.d)
            self.assertEqual(error.exception.code,404);error.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
