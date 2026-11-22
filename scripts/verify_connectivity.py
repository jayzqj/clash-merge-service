"""Real Mihomo integration: chained HTTP proxy, dead node and score outage."""
import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Config, DEFAULTS
from app import pipeline
import yaml

class Fixture(BaseHTTPRequestHandler):
    score_down=False
    connectivity_down=False
    connects=0
    def log_message(self,*_): pass
    def do_HEAD(self):
        time.sleep(0.005)
        self.send_response(503 if self.connectivity_down else 204)
        self.end_headers()
    def do_CONNECT(self):
        type(self).connects+=1
        self.send_response(200)
        self.end_headers()
        self.handle_one_request()
        self.close_connection=True
    def do_GET(self):
        info=urlsplit(self.path).path=='/info'
        body=json.dumps({'ip':'203.0.113.1','fraudScore':5,'countryCode':'JP'}).encode() if info else b''
        self.send_response(503 if info and self.score_down else (200 if info else 204))
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

async def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base='http://127.0.0.1:'+str(server.server_port)
    with tempfile.TemporaryDirectory(prefix='clash-connectivity-') as folder:
        root=Path(folder)
        cfg=Config(data=copy.deepcopy(DEFAULTS),path=root/'config.json')
        binary=ROOT/'vendor/clash-merge-mihomo.exe'
        if len(sys.argv)>1: binary=Path(sys.argv[1])
        cfg.data.update(notify_enabled=False,execution_start='00:00',execution_end='23:59',
                        subscriptions=[{'id':'fixture','url':base}],output_path=str(root/'out.yaml'),
                        mihomo_bin=str(binary),mihomo_workdir=str(root/'mihomo'),purity_workers=1,
                        mihomo_mixed_port=free_port(),mihomo_controller_port=free_port(),
                        connectivity_url=base+'/ping',connectivity_timeout_ms=1000,purity_info_url=base+'/info')
        nodes=[{'name':'front','type':'http','server':'127.0.0.1','port':server.server_port},
               {'name':'静态 backend','type':'http','server':'localhost','port':server.server_port},
               {'name':'dead','type':'http','server':'127.0.0.1','port':free_port()}]
        ini='[custom]\ncustom_proxy_group=Test`select`.*\nruleset=Test,[]FINAL\n'
        async def run():
            with patch.object(pipeline,'fetch_subscription',AsyncMock(return_value=copy.deepcopy(nodes))),patch.object(pipeline,'fetch_text',AsyncMock(return_value=ini)):
                report=await pipeline.run_pipeline(cfg,pipeline.ServiceState())
            assert report['ok'], report
            doc=yaml.safe_load(Path(cfg.output_path).read_text(encoding='utf-8'))
            assert report['connectivity_passed']==2,report
            assert len(doc['proxies'])==2,doc
            front=next(p for p in doc['proxies'] if '静态' not in p['name'])
            back=next(p for p in doc['proxies'] if '静态' in p['name'])
            assert back['dialer-proxy']==front['name']
            return report
        first=await run()
        assert first['purity_measured']==2,first
        assert Fixture.connects>=3,'Nested CONNECT chain was not exercised'
        before=yaml.safe_load(Path(cfg.output_path).read_text(encoding='utf-8'))['proxies']
        with patch.object(pipeline,'fetch_text',AsyncMock(return_value=ini)):
            reapplied=await pipeline.reapply_rules(cfg,pipeline.ServiceState())
        assert reapplied['ok'],reapplied
        after=yaml.safe_load(Path(cfg.output_path).read_text(encoding='utf-8'))['proxies']
        assert before==after,'Reapply changed the verified chain'
        Fixture.score_down=True
        down=await run()
        assert down['purity_measured']==0,down
        assert down['purity_reused']==2,down
        cfg.data['purity_enabled']=False
        disabled=await run()
        assert disabled['connectivity_passed']==2
        Fixture.connectivity_down=True
        with patch.object(pipeline,'fetch_subscription',AsyncMock(return_value=copy.deepcopy(nodes))),patch.object(pipeline,'fetch_text',AsyncMock(return_value=ini)):
            rejected=await pipeline.run_pipeline(cfg,pipeline.ServiceState())
        assert rejected['ok'],rejected
        assert rejected['connectivity_passed']==0,rejected
        assert yaml.safe_load(Path(cfg.output_path).read_text(encoding='utf-8'))['proxies']==[]
        print('PASS: real Mihomo verified front + backend chain, rejected dead node and HTTP 503, pinned output chain; scoring outage and disabled scoring preserve connectivity filtering; all failed publishes empty output',flush=True)
    server.shutdown()

if __name__=='__main__': asyncio.run(main())
