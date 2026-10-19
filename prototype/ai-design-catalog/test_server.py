import base64
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import server


class CatalogTests(unittest.TestCase):
    def test_catalog_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            server.DB=Path(tmp)/'data'/'catalog.sqlite3'
            server.FILES=Path(tmp)/'uploads'
            server.TOKEN='test-token-for-local-integration-only'
            server.AI_KEY=''
            server.init()
            http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            def request(path,method='GET',data=None,admin=False):
                headers={'Content-Type':'application/json'}
                if admin:headers['Authorization']='Bearer '+server.TOKEN
                req=urllib.request.Request('http://127.0.0.1:'+str(http.server_port)+path,
                    data=json.dumps(data).encode() if data is not None else None,
                    method=method,headers=headers)
                try:
                    with urllib.request.urlopen(req) as r:return r.status,r.read()
                except urllib.error.HTTPError as e:return e.code,e.read()
            try:
                self.assertEqual(request('/api/health')[0],200)
                self.assertEqual(request('/')[0],200)
                self.assertEqual(request('/admin.html')[0],200)
                self.assertEqual(request('/api/admin/designs')[0],401)
                d={k:'' for k in server.FIELDS}
                d.update(id='bed-clip',title='Bed clip',context='Bed',design_class='Hidden',rooms=['Bedroom'],status='draft')
                self.assertEqual(request('/api/admin/designs','POST',d)[0],401)
                self.assertEqual(request('/api/admin/designs','POST',d,True)[0],200)
                self.assertEqual(json.loads(request('/api/designs')[1])['designs'],[])
                d['status']='active'
                self.assertEqual(request('/api/admin/designs','POST',d,True)[0],200)
                self.assertEqual(json.loads(request('/api/designs')[1])['designs'][0]['id'],'bed-clip')
                server.init()
                self.assertEqual(json.loads(request('/api/designs')[1])['designs'][0]['title'],'Bed clip')
                self.assertEqual(request('/api/admin/designs','POST',[],True)[0],400)
                bad=dict(d,model_file_url='javascript:alert(1)')
                self.assertEqual(request('/api/admin/designs','POST',bad,True)[0],400)
                raw=b'solid test\nendsolid test'
                code,body=request('/api/admin/upload','POST',{'extension':'stl','base64':base64.b64encode(raw).decode()},True)
                self.assertEqual(code,201)
                self.assertEqual(request(json.loads(body)['url'])[1],raw)
                self.assertEqual(request('/api/admin/upload','POST',{'extension':'html','base64':'YQ=='},True)[0],400)
                self.assertEqual(request('/uploads/../server.py')[0],404)
                self.assertEqual(request('/data/catalog.sqlite3')[0],404)
                self.assertEqual(request('/api/ai/chat','POST',{'message':'help'})[0],503)
                self.assertEqual(request('/unknown','DELETE',admin=True)[0],404)
                self.assertEqual(request('/api/admin/designs/bed-clip','DELETE',admin=True)[0],200)
                self.assertEqual(json.loads(request('/api/designs')[1])['designs'],[])
            finally:
                http.shutdown()
                http.server_close()
                thread.join()


if __name__=='__main__':unittest.main()
