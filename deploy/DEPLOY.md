# 部署步骤（Racknerd VPS, Debian 12, Python 3.11.2）

## 0. 部署前先在 VPS 上核对

```bash
ss -tlnp | grep -E ':(7890|9090|18080)\b'   # 确认 7890/9090/18080 空闲
ls /opt/clash-merge-service                 # 部署目录
which mihomo || ls /opt/clash-merge-service/mihomo   # 检测用 mihomo 二进制 + geodata
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/
```

mihomo 二进制与 geodata（`geosite.dat` / `geoip.dat` / `Country.mmdb` 等）
复用现有纯净度任务那份，放到 `mihomo_workdir`（默认
`/opt/clash-merge-service/mihomo`），并在 `config.json` 里把
`mihomo_bin` 指过去（绝对路径或 PATH 中的名字）。

## 1. 传代码

```bash
# 仓库 clone 到 VPS（私有仓库，需先配好 deploy key / 凭据）
git clone git@github.com:jayzqj/clash-merge-service.git /opt/clash-merge-service
cd /opt/clash-merge-service
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 2. 写配置（只在 VPS 上，不进仓库）

```bash
cp config.example.json config.json
# 编辑 config.json：填三个初始订阅地址、wechat_webhook、output_path
#   /var/www/xiuyuan/jay_filtered.yaml；确认 ini_url 与 subconverter_base。
mkdir -p /etc/clash-merge
cat > /etc/clash-merge/env <<'EOF'
ADMIN_USER=jayzqj
ADMIN_PASSWORD=<只写在这里>
CLASH_MERGE_CONFIG=/opt/clash-merge-service/config.json
PORT=18080
EOF
chmod 600 /etc/clash-merge/env
```

## 3. 注册系统服务

```bash
cp deploy/clash-merge.service /etc/systemd/system/clash-merge.service
systemctl daemon-reload
systemctl enable --now clash-merge.service
systemctl status clash-merge.service
journalctl -u clash-merge.service -f
```

## 4. 修正 xiuyuan 站点的 nginx 转发

按 `deploy/nginx-xiuyuan.conf` 把 `/admin` 反代到 `127.0.0.1:18080`、
`/api/` 反代到后端、`/jay_filtered.yaml` 走静态文件。
**删掉**旧的占位 `proxy_pass http://127.0.0.1:80;`（会自己转发给自己）。
1Panel 里改完后重载 OpenResty，然后验证：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/jay_filtered.yaml
curl -s -u jayzqj:<密码> https://xiuyuan.9527653.xyz/api/status
```

## 5. 首轮验证

1. 在 `https://xiuyuan.9527653.xyz/admin` 点“立即执行一次”，观察日志与耗时。
2. 先确认 hysteria2 节点在 VPS 上能测通（UDP 通），再跑全量。
3. 检查 `jay_filtered.yaml`：节点总数 = 去重后总数；静态 TCP 节点都有
   `dialer-proxy: 🚪 ISP链式前置分组`；hysteria2 前置在前置分组内。
4. 在 Clash Verge 里订阅 `https://xiuyuan.9527653.xyz/jay_filtered.yaml`，
   刷新、解析、链式节点连通。
5. 配置页验收：新增/删除订阅源、改间隔、开关通知（见 README 验收清单）。
