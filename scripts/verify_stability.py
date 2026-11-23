"""Real core integration: independent HTTP samples with deterministic failures."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Config, DEFAULTS
from app.purity import MihomoChecker
from verify_connectivity import free_port


class Intermittent(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_CONNECT(self):
        self.send_response(200); self.end_headers()
        self.handle_one_request(); self.close_connection=True
    def do_HEAD(self):
        server=self.server
        with server.sample_lock:
            index=server.requests
            server.requests+=1
        code=204 if server.pattern[index % len(server.pattern)] else 503
        self.send_response(code); self.end_headers()


def main():
    binary=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'vendor/clash-merge-mihomo.exe'
    for rounds in (3,5):
        for batch in (True,False):
            servers=[]
            with tempfile.TemporaryDirectory(prefix='clash-stability-') as folder:
                nodes=[]
                for name,pattern in [('stable',[True]*rounds),('majority',[True]*(rounds//2+1)+[False]*(rounds//2)),
                                      ('flaky',[True]+[False]*(rounds-1))]:
                    server=ThreadingHTTPServer(('127.0.0.1',0),Intermittent)
                    server.pattern=pattern;server.requests=0;server.sample_lock=threading.Lock()
                    threading.Thread(target=server.serve_forever,daemon=True).start()
                    servers.append(server)
                    nodes.append(dict(name=name,type='http',server='127.0.0.1',port=server.server_port))
                cfg=Config(copy.deepcopy(DEFAULTS),Path(folder)/'config.json')
                cfg.data.update(mihomo_bin=str(binary),mihomo_workdir=folder,purity_enabled=False,
                    mihomo_mixed_port=free_port(),mihomo_controller_port=free_port(),
                    connectivity_rounds=rounds,purity_delay_screen=batch,connectivity_url='http://fixture.invalid/ping',
                    connectivity_timeout_ms=1000)
                checker=MihomoChecker(cfg)
                try:
                    assert checker.start(nodes),'core failed to start'
                    passed=checker.connection_batch(nodes)
                    assert set(passed)=={'stable','majority'},(passed,checker.connection_samples)
                    assert all(s.requests==rounds for s in servers),[s.requests for s in servers]
                    assert checker.connection_samples['flaky']['connectivity_successes']==1
                    print(f'PASS: {rounds} independent HTTP samples, batch={batch}, majority retained and flaky rejected',flush=True)
                finally:
                    checker.stop()
                    for server in servers:server.shutdown();server.server_close()


if __name__=='__main__':main()
