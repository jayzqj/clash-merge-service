import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import httpx
from app.config import Config, DEFAULTS
from app.connectivity import filter_connected, retain_chain_dependencies, pin_tested_chains
from app.merge import dedup_key
from app.purity import MihomoChecker, diversify_fronts
from app.fetcher import fetch_subscription


def node(name, port=8080):
    return {"name": name, "type": "http", "server": "127.0.0.1", "port": port}


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(data=copy.deepcopy(DEFAULTS), path=Path(self.temp.name)/'config.json')
        (Path(self.temp.name)/'output').mkdir()
        self.cfg.data.update(output_path=str(Path(self.temp.name)/'output'/'out.yaml'), notify_enabled=False)

    def test_missing_or_failed_connection_cannot_reuse_score_to_pass(self):
        nodes = [node('unknown',1), node('failed',2), node('connected-unscored',3)]
        records = {dedup_key(nodes[1]): {'connected':False,'score':0}, dedup_key(nodes[2]):{'connected':True,'score':None}}
        kept, report = filter_connected(nodes,records)
        self.assertEqual(kept,[nodes[2]])
        self.assertEqual(report['connectivity_excluded'],2)

    def test_score_service_failure_does_not_change_connectivity(self):
        p=node('normal')
        fake = Mock()
        fake.proc.poll.return_value=None
        fake.connection_batch.return_value={p['name']:12}
        fake.check_one.return_value={'measured':False,'score':None,'attempts':2,'score_error':'timeout'}
        checker = MihomoChecker(self.cfg)
        with patch('app.purity.MihomoChecker',return_value=fake):
            rec=checker._check_shard([p],0)[dedup_key(p)]
        self.assertTrue(rec['connected'])
        self.assertFalse(rec['measured'])

    def test_disabled_scoring_still_checks_connections(self):
        self.cfg.data['purity_enabled']=False
        checker=MihomoChecker(self.cfg)
        with patch.object(checker,'_info_once') as info:
            self.assertEqual(checker.check_one(node('p'))['attempts'],0)
        info.assert_not_called()
        with patch.object(checker,'_run_stage',return_value={dedup_key(node('p')):{'connected':True,'measured':False,'delay_ms':5}}) as stage:
            result=checker.check_all([node('p')])
        stage.assert_called_once()
        self.assertTrue(result[dedup_key(node('p'))]['connected'])

    def test_backend_retries_another_verified_front_and_pins_it(self):
        a,b,back=node('fast',1),node('backup',2),node('静态 backend',3)
        calls=[]
        def stage(nodes,front=None):
            calls.append(front)
            if front is None:
                return {dedup_key(p):{'connected':True,'measured':False,'delay_ms':i+1} for i,p in enumerate(nodes)}
            return {dedup_key(back):{'connected':front==b,'measured':False,'front_key':dedup_key(front)}}
        checker=MihomoChecker(self.cfg)
        with patch.object(checker,'_run_stage',side_effect=stage):
            records=checker.check_all([a,b,back])
        self.assertEqual(calls,[None,a,b])
        self.assertEqual(records[dedup_key(back)]['front_key'],dedup_key(b))
        proxies=[dict(a,name='renamed A'),dict(b,name='renamed B'),dict(back,name='静态 renamed')]
        pin_tested_chains(proxies,records)
        self.assertEqual(proxies[-1]['dialer-proxy'],'renamed B')
        kept,excluded=retain_chain_dependencies([proxies[0],proxies[-1]],records)
        self.assertEqual(excluded,1)
        self.assertEqual(kept,[proxies[0]])

    def test_no_available_front_excludes_backend(self):
        back=node('静态 backend')
        result=MihomoChecker(self.cfg).check_all([back])
        self.assertFalse(result[dedup_key(back)]['connected'])
        self.assertIn('前置',result[dedup_key(back)]['connectivity_error'])

    def test_front_candidates_span_regions_before_repeating_region(self):
        nodes = [dict(node(name,i),server=f'node{i}.invalid')
                 for i,name in enumerate(['香港 A','香港 B','日本 A','新加坡 A','美国 A'])]
        chosen = diversify_fronts(nodes,{})
        self.assertEqual([p['name'] for p in chosen[:4]],['香港 A','日本 A','新加坡 A','美国 A'])

    def test_score_filter_does_not_skip_backend_connectivity(self):
        self.cfg.data['purity_filter_enabled']=True
        a,back=node('front',1),node('静态 backend',2)
        checker=MihomoChecker(self.cfg)
        calls=[]
        def stage(nodes,front=None):
            calls.append(front)
            return {dedup_key(p):{'connected':True,'measured':False,'delay_ms':1,
                                 'front_key':dedup_key(front) if front else None} for p in nodes}
        with patch.object(checker,'_run_stage',side_effect=stage):
            records=checker.check_all([a,back])
        self.assertEqual(calls,[None,a])
        self.assertTrue(records[dedup_key(back)]['connected'])


FIVE_NODES = 'proxies:\n' + ''.join(
    f' - {{name: fixture{i}, type: http, server: 127.0.0.1, port: {80 + i}}}\n' for i in range(5))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_proxy_error_retries_before_direct(self):
        count=0
        def respond(request):
            nonlocal count
            count+=1
            if count==1: raise httpx.ConnectError('transient',request=request)
            return httpx.Response(200,text=FIVE_NODES)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as primary:
            primary.merge_direct_fallback=True
            primary.merge_route='proxy'
            primary.merge_routes={}
            with patch('app.fetcher.httpx.AsyncClient') as direct:
                self.assertEqual(len(await fetch_subscription('https://example.invalid/sub',primary)),5)
                direct.assert_not_called()
            self.assertEqual(count,2)
    async def test_proxy_failure_falls_back_to_valid_direct_subscription(self):
        response=httpx.Response(200,text=FIVE_NODES)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(502))) as primary:
            primary.merge_direct_fallback=True
            primary.merge_routes={}
            direct=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response))
            with patch('app.fetcher.httpx.AsyncClient',return_value=direct):
                nodes=await fetch_subscription('https://example.invalid/sub',primary)
            self.assertEqual(len(nodes),5)
            self.assertEqual(primary.merge_routes['https://example.invalid/sub'],'direct-fallback')

    async def test_explicitly_disabled_fallback_does_not_use_direct(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(502))) as primary:
            primary.merge_direct_fallback=False
            with patch('app.fetcher.httpx.AsyncClient') as direct:
                with self.assertRaises(Exception):
                    await fetch_subscription('https://example.invalid/sub',primary)
            direct.assert_not_called()
