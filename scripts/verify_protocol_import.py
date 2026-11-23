"""All bundled-core protocols: lossless import and real core validation."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import yaml
from app.node_links import PROTOCOLS, parse_node_source


def main():
    binary=Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'vendor/clash-merge-mihomo.exe'
    uuid='00000000-0000-4000-8000-000000000001'
    key=base64.b64encode(bytes(range(1,33))).decode()
    extras={
        'ss':dict(cipher='aes-128-gcm',password='fixture'),
        'ssr':dict(cipher='aes-128-cfb',password='fixture',obfs='plain',protocol='origin'),
        'vmess':dict(uuid=uuid,alterId=0,cipher='auto'), 'vless':dict(uuid=uuid),
        'trojan':dict(password='fixture',sni='example.com'), 'snell':dict(psk='fixture',version=3),
        'hysteria':{'auth-str':'fixture','up':'10','down':'50'}, 'hysteria2':dict(password='fixture'),
        'wireguard':{'private-key':key,'public-key':key,'ip':'10.0.0.2'},
        'tuic':dict(uuid=uuid,password='fixture'), 'ssh':dict(username='fixture',password='fixture'),
        'mieru':dict(username='fixture',password='fixture',transport='TCP'), 'anytls':dict(password='fixture'),
    }
    nodes=[]
    for protocol in sorted(PROTOCOLS):
        node=dict(name='fixture-'+protocol,type=protocol,server='192.0.2.1',port=443,**extras.get(protocol,{}))
        if protocol in ('direct','dns','reject'):node.pop('server');node.pop('port')
        if protocol == 'hysteria2':
            node = parse_node_source(r'hysteria2://fixture@192.0.2.1:443?security=tls&alpn=h3&insecure=0&allowInsecure=0&sni=www\.example.com&pinSHA256='+'ab'*32+'#fixture-hysteria2')[0]
            assert node['fingerprint'] == 'ab'*32 and node['skip-cert-verify'] is False
        elif protocol == 'tuic':
            node = parse_node_source('tuic://'+uuid+r':fixture@192.0.2.1:443?congestion_control=bbr&udp_relay_mode=native&alpn=h3&sni=www\.example.com&insecure=1&allowInsecure=1&allow_insecure=1#fixture-tuic')[0]
            assert node['sni'] == 'www.example.com' and node['skip-cert-verify'] is True
        elif protocol == 'anytls':
            node = parse_node_source(r'anytls\://fixture@192.0.2.1:443?&sni=www\.example.com&insecure=1&allowInsecure=1#fixture-anytls')[0]
            assert node['sni'] == 'www.example.com' and node['skip-cert-verify'] is True
        nodes.append(node)
    encoded=yaml.safe_dump({'proxies':nodes})
    assert parse_node_source(encoded)==nodes
    assert parse_node_source(json.dumps(nodes))==nodes
    with tempfile.TemporaryDirectory(prefix='clash-protocol-') as folder:
        path=Path(folder)/'config.yaml'
        path.write_text(yaml.safe_dump({'proxies':nodes,'rules':['MATCH,DIRECT']}),encoding='utf-8')
        result=subprocess.run([str(binary),'-t','-f',str(path),'-d',folder],capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr
    print('PASS: all '+str(len(nodes))+' bundled-core protocol types imported losslessly and validated by Mihomo',flush=True)


if __name__=='__main__':main()
