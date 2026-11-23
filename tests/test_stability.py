import asyncio
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app.config import Config, DEFAULTS
from app.fetcher import fetch_subscription, parse_subscription_body
from app.node_links import parse_node_source, validate_source
from app.purity import MihomoChecker
from app.output_names import append_ipv4_names
from app.progress import RunProgress
from app.merge import dedup_key
from app.node_links import PROTOCOLS
from app.converter import _matching_nodes


def node(name='normal', port=80, protocol='http'):
    return dict(name=name, type=protocol, server='192.0.2.1', port=port)


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(copy.deepcopy(DEFAULTS), Path(self.temp.name)/'config.json')
        self.cfg.data.update(mihomo_workdir=self.temp.name, notify_enabled=False)

    def test_strict_majority_three_and_five_samples_no_early_pass(self):
        for count, responses, kept in [(3,[10,None,None],False),(3,[10,None,30],True),
                                        (5,[10,20,None,None,None],False),(5,[10,None,20,None,30],True)]:
            for individual in (False,True):
                self.cfg.data['connectivity_rounds']=count
                checker=MihomoChecker(self.cfg)
                with patch('app.purity.STOPPING.wait',return_value=False), \
                     patch.object(checker,'_batch_once',side_effect=[{'normal':d} if d else {} for d in responses]) as batch, \
                     patch.object(checker,'_probe_once',side_effect=responses) as probe:
                    passed=checker.connection_batch([node()],individual=individual)
                self.assertEqual('normal' in passed,kept)
                self.assertEqual((probe if individual else batch).call_count,count)
                self.assertEqual(checker.connection_samples['normal']['connectivity_successes'],sum(d is not None for d in responses))
                self.assertEqual(checker.connection_samples['normal']['connectivity_attempts'],count)

    def test_failed_batch_vote_is_not_retried_or_counted_twice(self):
        checker=MihomoChecker(self.cfg)
        with patch('app.purity.STOPPING.wait',return_value=False), \
             patch.object(checker,'_batch_once',side_effect=httpx.ConnectError('fixture')), \
             patch.object(checker,'_probe_once',side_effect=[20,None]) as probe:
            self.assertEqual(checker.connection_batch([node()]),{})
        self.assertEqual(probe.call_count,2)
        self.assertEqual(checker.connection_samples['normal']['connectivity_samples_ms'],[None,20,None])

    def test_validation_disallows_even_rounds_and_invalid_protocol_list(self):
        for value in (1,2,4,True,'3'):
            self.cfg.data['connectivity_rounds']=value
            self.assertTrue(any('次数' in e for e in self.cfg.validate()))
        self.cfg.data['connectivity_rounds']=5
        for value in ('vless',['BAD'],[42]):
            self.cfg.data['excluded_protocols']=value
            self.assertTrue(any('协议' in e for e in self.cfg.validate()))

    def test_live_elapsed_and_estimate_handles_new_round_and_progress(self):
        with patch('app.progress.time.monotonic',return_value=100):
            progress=RunProgress()
            progress.update(total=40)
        with patch('app.progress.time.monotonic',return_value=110):
            self.assertEqual(progress.snapshot()['elapsed_seconds'],10)
            self.assertIsNone(progress.snapshot()['estimated_total_seconds'])
            progress.update(units=10)
            self.assertEqual(progress.snapshot()['estimated_total_seconds'],50)
            progress.update(add_total=40)
            self.assertEqual(progress.snapshot()['estimated_total_seconds'],90)


class LinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_mixed_links_with_tls_aliases_and_markdown_escapes(self):
        vmess_tls = {'add':'edge.example.invalid','port':'443','id':'fixture-uuid','aid':'0','net':'ws',
                     'host':'ws.example.invalid','path':'/vm','tls':'tls','sni':'ws.example.invalid','ps':'vmess tls'}
        vmess_plain = {**vmess_tls, 'add':'192.0.2.1','port':'2082','tls':'','ps':'vmess plain'}
        encode = lambda data: base64.b64encode(json.dumps(data).encode()).decode()
        links = [r'vless\://fixture@192.0.2.1:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=example.com&fp=chrome&pbk=fixture&sid=abcd&type=tcp&headerType=none#vless',
                 r'vmess\://'+encode(vmess_tls), r'vmess\://'+encode(vmess_plain),
                 r'hysteria2://fixture@192.0.2.1:443?security=tls&alpn=h3&insecure=0&allowInsecure=0&sni=www\.example.com&pinSHA256='+'ab'*32+'#hy2',
                 r'tuic://fixture:password@192.0.2.1:443?congestion_control=bbr&udp_relay_mode=native&alpn=h3&sni=www\.example.com&insecure=1&allowInsecure=1&allow_insecure=1#tuic',
                 r'anytls\://fixture@192.0.2.1:443?&sni=www\.example.com&allowInsecure=1&insecure=1#anytls']
        for prefix in (' ', '&#x20; ', '\ufeff\u00a0'):
            nodes = parse_node_source(prefix+'|'.join(links))
            self.assertEqual([p['type'] for p in nodes],['vless','vmess','vmess','hysteria2','tuic','anytls'])
            self.assertEqual(nodes[3]['fingerprint'], 'ab'*32)
            self.assertFalse(nodes[3]['skip-cert-verify'])
            for p in nodes[3:]: self.assertEqual(p['sni'], 'www.example.com')
            for p in nodes[4:]: self.assertTrue(p['skip-cert-verify'])
            self.assertEqual(nodes[4]['udp-relay-mode'],'native')
            self.assertTrue(nodes[1]['tls'])
            self.assertFalse(nodes[2]['tls'])

    async def test_tls_aliases_conflicts_and_pin_validation(self):
        for key in ('insecure','allowInsecure','allow_insecure','skip-cert-verify'):
            for value, expected in [('0',False),('FALSE',False),('1',True),('true',True)]:
                p=parse_node_source(f'tuic://fixture:password@192.0.2.1:443?{key}={value}')[0]
                self.assertIs(p['skip-cert-verify'],expected)
        for query in ('insecure=0&allow_insecure=1','insecure=maybe','security=reality','pinSHA256=private-invalid-value'):
            with self.assertRaises(ValueError) as caught:
                parse_node_source('hysteria2://private-password@192.0.2.1:443?'+query)
            self.assertNotIn('private',str(caught.exception))
            self.assertIn('第 1 个',str(caught.exception))
        pin=':'.join(['AB']*32)
        p=parse_node_source('hy2://fixture@192.0.2.1:443?pinSHA256='+pin)[0]
        self.assertEqual(p['fingerprint'],'ab'*32)

    async def test_hostname_unescaping_does_not_change_secrets_or_paths(self):
        p=parse_node_source(r'trojan://pass\.word@edge\.example.com:443?sni=www\.example.com&type=ws&path=/keep%5C.path')[0]
        self.assertEqual(p['password'],r'pass\.word')
        self.assertEqual(p['server'],'edge.example.com')
        self.assertEqual(p['sni'],'www.example.com')
        self.assertEqual(p['ws-opts']['path'],r'/keep\.path')

    async def test_all_core_protocols_have_lossless_native_import(self):
        for protocol in PROTOCOLS:
            original = dict(node(protocol=protocol), **{'custom-future-field':{'nested':['retained']}})
            if protocol in ('direct','reject','dns'):
                original.pop('server'); original.pop('port')
            for value in (original, [original], {'proxies':[original]}):
                self.assertEqual(parse_node_source(json.dumps(value)), [original])
        peers = {'name':'wg','type':'wireguard','private-key':'fixture','peers':[{'server':'192.0.2.1','port':443}]}
        self.assertEqual(parse_node_source(json.dumps(peers)),[peers])
        mieru={'name':'mieru','type':'mieru','server':'192.0.2.1','port-range':'1000-1005'}
        self.assertEqual(parse_node_source(json.dumps(mieru)),[mieru])

    async def test_additional_share_protocols(self):
        links = [
            'trojan://password@192.0.2.1:443?sni=example.com#trojan',
            'anytls://password@192.0.2.1:443?sni=example.com#anytls',
            'hy2://password@192.0.2.1:443?obfs=salamander&obfs-password=secret#hy2',
            'hysteria://192.0.2.1:443?auth=secret&upmbps=10&downmbps=50#hy',
            'tuic://fixture-uuid:password@192.0.2.1:443?congestion_control=bbr#tuic',
            'snell://psk@192.0.2.1:443?version=3#snell',
            'ssh://user:password@192.0.2.1:22#ssh',
            'mieru://user:password@192.0.2.1:443?transport=TCP#mieru',
            'wg://private@192.0.2.1:443?publickey=public&address=10.0.0.2/32&reserved=1,2,3#wg',
            'socks5://user:password@192.0.2.1:1080#socks',
            'https-proxy://user:password@192.0.2.1:443#https',
            'ss://'+base64.b64encode(b'aes-128-gcm:password').decode()+'@192.0.2.1:443#ss',
            'ssr://'+base64.urlsafe_b64encode(b'192.0.2.1:443:origin:aes-128-cfb:plain:cGFzc3dvcmQ/?remarks=dGVzdA').decode(),
        ]
        expected=['trojan','anytls','hysteria2','hysteria','tuic','snell','ssh','mieru','wireguard','socks5','http','ss','ssr']
        self.assertEqual([p['type'] for p in parse_node_source('|'.join(links))],expected)

    async def test_unknown_uri_parameters_are_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            parse_node_source('vless://fixture@192.0.2.1:443?unsupported=required')

    async def test_ip_suffix_preserves_anchored_group_matching(self):
        p=node('Japan A01 192.0.2.1')
        self.assertEqual(_matching_nodes([p], ['A01$']),[p['name']])

    def links(self):
        vless = r'vless\://fixture-uuid@192.0.2.8:443?flow=xtls-rprx-vision&security=reality&sni=apple.com&fp=chrome&pbk=fixture-public-key&sid=abcd&type=tcp#美国 racknerd'
        vmess = {'add':'edge.example.invalid','port':'443','id':'fixture-uuid','aid':'0','net':'ws',
                 'host':'ws.example.invalid','path':'/fixture-vm','tls':'tls','sni':'ws.example.invalid','ps':'美国 vmess'}
        encoded=base64.b64encode(json.dumps(vmess).encode()).decode()
        return vless+'|vmess://'+encoded

    async def test_pipe_links_import_locally_and_keep_transport_fields(self):
        value=validate_source(self.links())
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:self.fail('Local links performed a network fetch'))) as client:
            client.merge_routes={}
            nodes=await fetch_subscription(value,client)
        self.assertEqual(client.merge_routes[value],'local')
        self.assertEqual([p['type'] for p in nodes],['vless','vmess'])
        self.assertEqual(nodes[0]['flow'],'xtls-rprx-vision')
        self.assertEqual(nodes[0]['reality-opts'],{'public-key':'fixture-public-key','short-id':'abcd'})
        self.assertEqual(nodes[1]['ws-opts'],{'path':'/fixture-vm','headers':{'Host':'ws.example.invalid'}})
        self.assertTrue(nodes[1]['tls'])

    async def test_subscription_body_plain_and_base64_links_and_yaml(self):
        text=self.links().replace('|','\n')
        for body in (text.encode(),base64.b64encode(text.encode())):
            self.assertEqual(len(parse_subscription_body(body)),2)
        yaml=b'proxies:\n - {name: test, type: http, server: 192.0.2.1, port: 80}'
        self.assertEqual(parse_subscription_body(yaml),parse_subscription_body(base64.b64encode(yaml)))

    async def test_invalid_link_reports_position_without_credentials(self):
        with self.assertRaisesRegex(ValueError,'第 2 个') as caught:
            parse_node_source(self.links().split('|')[0]+'|vmess://private-broken-payload')
        self.assertNotIn('private',str(caught.exception))

    async def test_ipv4_suffix_updates_groups_chain_and_keeps_ipv6_out(self):
        a=node('日本 A')
        b=node('静态 B',81)
        c=dict(node('IPv6',82),server='2001:db8::1')
        b['dialer-proxy']=a['name']
        doc={'proxies':[a,b,c],'proxy-groups':[{'name':'All','proxies':[a['name'],b['name'],c['name']]}]}
        records={dedup_key(a):{'measured':True,'exit_ip':'203.0.113.8'}}
        await append_ipv4_names(doc,records)
        self.assertTrue(a['name'].endswith('203.0.113.8'))
        self.assertTrue(b['name'].endswith('192.0.2.1'))
        self.assertEqual(c['name'],'IPv6')
        self.assertEqual(b['dialer-proxy'],a['name'])
        self.assertEqual(doc['proxy-groups'][0]['proxies'],[p['name'] for p in doc['proxies']])
        before=copy.deepcopy(doc)
        await append_ipv4_names(doc,records)
        self.assertEqual(doc,before)

    async def test_domain_resolution_timeout_is_optional_and_no_ipv6_suffix(self):
        p=dict(node(),server='example.invalid')
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',AsyncMock(side_effect=OSError('dns'))):
            stats=await append_ipv4_names({'proxies':[p]}, {})
        self.assertEqual(p['name'],'normal')
        self.assertEqual(stats['ipv4_unavailable'],1)
