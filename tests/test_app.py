import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
import app

class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app.DB = Path(cls.tmp.name)/'test.sqlite3'
        app.initialize()
        cls.server = app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self,path,data=None,auth=None,extra=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        headers={'Content-Type':'application/json'}
        if auth: headers.update({'Cookie':auth[0],'X-CSRF-Token':auth[1]})
        if extra: headers.update(extra)
        conn.request('POST' if data is not None else 'GET','/api/'+path,json.dumps(data) if data is not None else None,headers)
        r=conn.getresponse()
        result=(r.status,json.loads(r.read()),r.getheader('Set-Cookie'))
        conn.close()
        return result

    def login(self,name):
        status,body,cookie=self.request('login',{'login':name,'password':'Gate10Demo1234'})
        self.assertEqual(status,200)
        return cookie.split(';')[0],body['csrf']

    def test_full_workflow(self):
        sales=self.login('sales01'); manager=self.login('manager')
        self.assertEqual(len(self.request('deals',auth=manager)[1]['deals']),24)
        data=dict(customer='検証用顧客',title='登録から反応まで',stage='提案中',action='訪問',due=app.today().isoformat(),amount='',expected='',memo='初回')
        status,body,_=self.request('deals',data,sales);self.assertEqual(status,201)
        id=body['id']; path=f'deals/{id}'
        self.assertEqual(self.request(path,auth=self.login('sales02'))[0],403)
        self.assertEqual(self.request(path+'/read',{'version':1},sales)[0],403)
        self.assertEqual(self.request(path+'/read',{'version':1},manager)[0],200)
        self.assertEqual(self.request(path+'/react',{'version':1,'reaction':'同行しようか'},manager)[0],200)
        self.assertEqual(self.request(path,auth=sales)[1]['feedback']['reaction'],'同行しようか')
        self.assertIsNone(self.request(path,auth=sales)[1]['deal']['amount'])
        data.update(version=1,amount='120000',memo='更新',stage='受注')
        self.assertEqual(self.request(path,data,sales)[0],200)
        current=self.request(path,auth=sales)[1]
        self.assertIsNone(current['feedback'])
        self.assertEqual(len(current['history']),2)
        self.assertTrue(current['deal']['won_at'])
        self.assertEqual(self.request(path,data,sales)[0],409)
        self.assertEqual(self.request(path+'/react',{'version':1,'reaction':'👍'},manager)[0],409)
        self.assertEqual(self.request('deals',data,sales)[0],409)

    def test_security_and_validation(self):
        self.assertEqual(self.request('deals')[0],401)
        auth=self.login('sales03')
        self.assertEqual(self.request('deals',{},auth,{'X-CSRF-Token':'bad'})[0],403)
        self.assertEqual(self.request('deals',{},auth,{'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('deals',{},auth)[0],422)
        self.assertEqual(self.request('deals',{},self.login('manager'))[0],403)
        self.assertEqual(self.request('deals/99999',auth=auth)[0],404)
        for value in ['-1','1.5',True,'1000000000000']:
            with self.assertRaises(app.Problem):
                app.validate(dict(customer='a',title='b',stage='提案中',action='電話',due=app.today().isoformat(),amount=value))

    def test_stagnation_and_deadline(self):
        row={'stage':'提案中','updated':(app.dt.datetime.now(app.JST)-app.dt.timedelta(days=7,seconds=1)).isoformat(),'due':(app.today()-app.dt.timedelta(days=1)).isoformat()}
        self.assertTrue(app.decorate(row)['stale']);self.assertTrue(app.decorate(row)['overdue'])
        row['due']=app.today().isoformat();self.assertFalse(app.decorate(row)['overdue'])
        row['stage']='失注';self.assertFalse(app.decorate(row)['stale'])

    def test_seed_does_not_reset(self):
        with app.connect() as c: count=c.execute('SELECT count(*) FROM deals').fetchone()[0]
        app.initialize()
        with app.connect() as c: self.assertEqual(count,c.execute('SELECT count(*) FROM deals').fetchone()[0])

    def test_login_lock(self):
        for _ in range(5): self.assertEqual(self.request('login',{'login':'nobody','password':'bad'})[0],401)
        self.assertEqual(self.request('login',{'login':'nobody','password':'bad'})[0],429)

if __name__=='__main__': unittest.main()
