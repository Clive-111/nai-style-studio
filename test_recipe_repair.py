"""Artist syntax, persistence, and generation boundary regression tests."""
import io
import json
import sqlite3
import threading
import unittest
from functools import partial
from contextlib import closing
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from PIL import Image, PngImagePlugin
from app import Handler
from generation import DEFAULTS, validate
from inspector import _units, inspect_png
from recipe_repair import repair_recipe, require_clean
import test_generation as fixtures

PAINTER = '\U0001f9d1\u200d\U0001f3a8'
BROKEN = '0.45:' + PAINTER + 'Example_Name::'
FIXED = '0.45::artist:Example_Name::'


class RepairRulesTests(unittest.TestCase):
    def test_people_skin_tones_colons_and_idempotence(self):
        for person in ['\U0001f9d1', '\U0001f468', '\U0001f469']:
            for skin in ['', *map(chr, range(0x1f3fb, 0x1f400))]:
                for colon in [':', '::']:
                    text = '-.5' + colon + person + skin + '\u200d\U0001f3a8' + 'Example_Name::'
                    with self.subTest(person=person, skin=skin, colon=colon):
                        result = repair_recipe(text)
                        self.assertEqual(result['text'], '-.5::artist:Example_Name::')
                        self.assertEqual(result['count'], 1)
                        self.assertFalse(result['warnings'])
                        self.assertEqual(repair_recipe(result['text'])['count'], 0)

    def test_existing_prefix_bare_and_mixed_group(self):
        cases = {
            PAINTER+'Foo': 'artist:Foo',
            ':'+PAINTER+'Foo': 'artist:Foo',
            'artist:'+PAINTER+'Foo': 'artist:Foo',
            'artist:'+PAINTER+'artist:Foo': 'artist:Foo',
            '0.5::Artist: '+PAINTER+'Foo::': '0.5::Artist: Foo::',
            '0.5:'+PAINTER+'artist:Foo::': '0.5::artist:Foo::',
            '2::soft_light, '+PAINTER+'Foo, old_city::': '2::soft_light, artist:Foo, old_city::',
            '0.25:'+PAINTER+'Foo, ::': '0.25::artist:Foo, ::',
        }
        for before, after in cases.items():
            with self.subTest(text=before):
                self.assertEqual(repair_recipe(before)['text'], after)
                self.assertFalse(repair_recipe(after)['changes'])
        self.assertEqual(list(_units(repair_recipe('2::soft_light, '+PAINTER+'Foo, old_city::')['text'])),
                         ['2::soft_light, artist:Foo, old_city::'])

    def test_normal_text_unchanged_and_unknown_location(self):
        text='  1.25::artist:Foo_Bar, artist:中文画师::, [[soft light]], ^_^\r\n{contrast}, ♥, ©  '
        self.assertEqual(repair_recipe(text)['text'], text)
        self.assertFalse(repair_recipe(text)['warnings'])
        for emoji in ['\U0001f680', '\U0001f3a8', '\u2764\ufe0f', '1\ufe0f\u20e3']:
            result=repair_recipe('normal\n'+emoji)
            self.assertEqual(result['warnings'][0]['line'], 2)
            self.assertEqual(result['warnings'][0]['column'], 1)
            with self.assertRaisesRegex(ValueError, '第 2 行第 1 列'):
                require_clean('normal\n'+emoji)
        self.assertTrue(repair_recipe('A '+PAINTER+' paints a picture')['warnings'])
        self.assertTrue(repair_recipe('0.5:::'+PAINTER+'Foo::')['warnings'])

    def test_png_repairs_before_extraction_keeps_original(self):
        raw=BROKEN+', standing'
        for explicit in [False, True]:
            fields=PngImagePlugin.PngInfo()
            params={'prompt':raw,'negative_prompt':'unchanged negative'}
            if explicit: params['style_recipe']=BROKEN
            fields.add_text('Comment',json.dumps(params))
            data=io.BytesIO();Image.new('RGB',(64,64)).save(data,format='PNG',pnginfo=fields)
            result=inspect_png(data.getvalue())
            self.assertIn(FIXED,result['recipe'])
            self.assertNotIn(PAINTER,result['recipe'])
            self.assertEqual(result['metadata']['prompt'],raw)
            self.assertEqual(result['metadata']['negative'],'unchanged negative')
            self.assertTrue(any('已修复 1' in x for x in result['warnings']))


class EmojiFlowTests(unittest.TestCase):
    setUp=fixtures.OfflineGenerationTests.setUp
    tearDown=fixtures.OfflineGenerationTests.tearDown
    transport=fixtures.OfflineGenerationTests.transport
    wait=fixtures.OfflineGenerationTests.wait

    def test_collection_draft_and_backup_migration(self):
        with self.library.connection() as db:
            sid=db.execute('INSERT INTO styles(name,description,recipe) VALUES(?,?,?)',('Synthetic style','',BROKEN)).lastrowid
            self.library.add_example(db,sid,'synthetic.png',None,'archive',{'prompt':BROKEN})
            draft=json.dumps({**DEFAULTS,'recipe':BROKEN,'negative':'keep negative'})
            db.execute('INSERT OR REPLACE INTO generation_draft VALUES(1,?)',(draft,))
        result=self.library.repair_saved_recipes()
        self.assertEqual((result['repaired_styles'], result['repaired_draft'], result['repairs']),(1,True,2))
        with closing(sqlite3.connect(result['backup'])) as backup:
            self.assertEqual(backup.execute('SELECT recipe FROM styles WHERE id=?',(sid,)).fetchone()[0],BROKEN)
            self.assertEqual(backup.execute('SELECT payload FROM generation_draft').fetchone()[0],draft)
        self.assertEqual(self.library.detail(sid)['recipe'],FIXED)
        self.assertEqual(self.library.detail(sid)['examples'][0]['metadata']['prompt'],BROKEN)
        self.assertEqual(self.g.draft()['draft']['recipe'],FIXED)
        self.assertEqual(self.g.draft()['draft']['negative'],'keep negative')
        self.assertIsNone(self.library.repair_saved_recipes())
        self.library.edit(sid,['Synthetic style','',BROKEN])
        self.assertEqual(self.library.detail(sid)['recipe'],FIXED)
        self.g.draft(self.d|{'recipe':BROKEN})
        self.assertEqual(self.g.draft()['draft']['recipe'],FIXED)
        saved=self.library.save(None,['Another','',BROKEN],fixtures.png(),{'prompt':BROKEN},'synthetic.png')
        self.assertEqual(self.library.detail(saved['id'])['recipe'],FIXED)

    def test_generate_repairs_once_for_batch_and_preserves_negative(self):
        negative='keep '+PAINTER+' negative'
        d=self.d|{'recipe':BROKEN,'negative':negative,'count':4,'request':'A cat'}
        self.g.draft(d);before=self.g.draft()
        job=self.wait(self.g.start(d)['job']['id'])
        self.assertEqual((job['state'],job['completed']),('completed',4))
        self.assertEqual(len(self.calls),5)
        material=json.loads(self.calls[0][1]['messages'][1]['content'])
        self.assertEqual(material['style_recipe'],FIXED)
        for _,request in self.calls[1:]:
            self.assertIn(FIXED,request['input'])
            self.assertNotIn(PAINTER,request['input'])
            self.assertEqual(request['parameters']['negative_prompt'],negative)
        entry=self.g.image(job['images'][0])
        self.assertEqual(entry['snapshot']['prompt_repairs'][0]['count'],1)
        self.assertEqual(job['record']['prompt_repairs'][0]['field'],'recipe')
        self.assertEqual(self.g.draft(),before)
        parsed=inspect_png((self.g.directory/entry['filename']).read_bytes())
        raw=json.loads(parsed['metadata']['raw']['Comment'])['generation_settings']
        self.assertEqual(raw['prompt_repairs'][0]['count'],1)

    def test_unknown_recipe_and_ai_result_never_reach_nai(self):
        with self.assertRaisesRegex(ValueError,'第 1 行'):
            self.g.start(self.d|{'recipe':'artist:Example, \U0001f680'})
        self.assertEqual(self.calls,[])
        self.composition_reply['description']='A cat \U0001f680'
        job=self.wait(self.g.start(self.d)['job']['id'])
        self.assertEqual(job['state'],'failed')
        self.assertEqual(len(self.calls),1)
        self.assertIn('正向提示词',job['error'])

    def old_history(self, prompt):
        d=validate(self.d|{'recipe':FIXED,'final_prompt':FIXED})
        image_id=self.g.persist('old-job',fixtures.png(),d,self.g.request(d,0))
        source=self.g.image(image_id)['snapshot']
        source.update(recipe=BROKEN, sent_prompt=prompt, final_prompt=prompt)
        with self.library.connection() as db:
            db.execute('UPDATE generation_images SET snapshot=? WHERE id=?',(json.dumps(source),image_id))
        return image_id

    def test_reroll_corrects_positive_without_assistant_or_history_rewrite(self):
        prompt='A quiet lake.\r\n'+BROKEN
        image_id=self.old_history(prompt)
        before=dict(self.g.image_record(image_id))
        image_before=(self.g.directory/before['filename']).read_bytes()
        self.g.settings.save({'assistant':{'clear_key':True}})
        job=self.wait(self.g.reroll(image_id,self.d|{'negative':'left negative','steps':28})['job']['id'])
        self.assertEqual(job['state'],'completed')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][1]['input'],'A quiet lake.\r\n'+FIXED)
        self.assertEqual(self.calls[0][1]['parameters']['steps'],28)
        self.assertEqual(self.calls[0][1]['parameters']['negative_prompt'],'left negative')
        self.assertEqual(dict(self.g.image_record(image_id)),before)
        self.assertEqual((self.g.directory/before['filename']).read_bytes(),image_before)
        new=self.g.image(job['images'][0])['snapshot']
        self.assertEqual(new['source_image_id'],image_id)
        self.assertEqual(new['prompt_repairs'][-1]['field'],'final_prompt')
        unknown=self.old_history('A lake \U0001f680')
        with self.assertRaises(ValueError):self.g.reroll(unknown,self.d)
        self.assertEqual(len(self.calls),1)

    def test_repair_endpoint_validation(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,library=self.library,generator=self.g))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)
            def call(payload):
                return urlopen(Request(base+'/api/recipe/repair',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'}))
            with call({'text':BROKEN}) as response:self.assertEqual(json.load(response)['text'],FIXED)
            with self.assertRaises(HTTPError) as error:call({'text':None})
            self.assertEqual(error.exception.code,400)
            error.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
