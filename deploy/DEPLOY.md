# 部署步骤（Racknerd VPS, Debian 12, Python 3.11.2）

部署目录是 `/opt/clash-merge-service`，它是本仓库 `main` 分支的一个 git 检出。
更新代码有两种方式，见第 6 节：在 VPS 上拉取（常用），或者直接上传文件。

## 0. 部署前先在 VPS 上核对

```bash
ss -tlnp | grep -E ':(789[0-9]|909[0-9]|18080)\b'   # 检测端口和 18080 是否空闲
ls /opt/clash-merge-service                         # 部署目录
which mihomo || ls /opt/clash-merge-service/mihomo  # 检测用 mihomo 二进制 + geodata
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/
```

每个检测 worker 占用两个端口：`mihomo_mixed_port + i` 和
`mihomo_controller_port + i`（默认 7890+i、9090+i）。现在这台 VPS 上 9095、9096
被 docker 占用，所以 `purity_workers` 不要超过 5（线上是 4）。

mihomo 二进制与 geodata（`geosite.dat` / `geoip.dat` / `Country.mmdb` 等）
放到 `mihomo_workdir`（默认 `/opt/clash-merge-service/mihomo`），并在
`config.json` 里把 `mihomo_bin` 指过去（绝对路径或 PATH 中的名字）。
`mihomo/` 被 gitignore，拉取代码不会动它。

## 1. 传代码（首次部署）

仓库是公开的，用 HTTPS 匿名拉取，VPS 上不需要配置 GitHub 密钥：

```bash
git clone https://github.com/jayzqj/clash-merge-service.git /opt/clash-merge-service
cd /opt/clash-merge-service
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 2. 写配置（只在 VPS 上，不进仓库）

```bash
cp config.example.json config.json
# 编辑 config.json：填订阅地址、wechat_webhook、output_path；
# 确认 ini_url 与 subconverter_base。
mkdir -p /etc/clash-merge
cat > /etc/clash-merge/env <<'EOF'
ADMIN_USER=jayzqj
ADMIN_PASSWORD=<只写在这里>
CLASH_MERGE_CONFIG=/opt/clash-merge-service/config.json
PORT=18080
EOF
chmod 600 /etc/clash-merge/env
```

`output_path` 填网站目录里的实际路径。这台 VPS 的站点由 1Panel 的 OpenResty 托管，
路径是
`/opt/1panel/apps/openresty/openresty/www/sites/xiuyuan.9527653.xyz/index/jay_filtered.yaml`。

`config.json`、`state.json`、`availability.json` 都被 gitignore，拉取代码不会动它们。

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
`/api/` 反代到后端、`/jay_filtered.yaml` 走静态文件（`alias` 改成第 2 节的
`output_path`）。**删掉**旧的占位 `proxy_pass http://127.0.0.1:80;`（会自己转发给自己）。
1Panel 里改完后重载 OpenResty，然后验证：

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/jay_filtered.yaml
curl -s -u jayzqj:<密码> https://xiuyuan.9527653.xyz/api/status
```

## 5. 首轮验证

1. 在 `https://xiuyuan.9527653.xyz/admin` 点“立即执行一次”，观察日志与耗时。
2. 先确认 hysteria2 节点在 VPS 上能测通（UDP 通），再跑全量。
3. 检查 `jay_filtered.yaml`：节点总数 = 去重后总数；静态 TCP 节点都有
   `dialer-proxy`，值是 ini 里名字含 `前置` 的那个分组（现在是
   `🚪 链式代理前置分组`）；hysteria2 前置在前置分组内。
4. 在 Clash Verge 里订阅 `https://xiuyuan.9527653.xyz/jay_filtered.yaml`，
   刷新、解析、链式节点连通。
5. 配置页验收：新增/删除订阅源、改间隔、开关通知（见 README 验收清单）。

在 VPS 上用命令触发一轮（修改类接口必须带 `X-Requested-With` 请求头）：

```bash
set -a; . /etc/clash-merge/env; set +a
curl -s -u "${ADMIN_USER:-jayzqj}:$ADMIN_PASSWORD" -X POST \
  -H 'X-Requested-With: XMLHttpRequest' http://127.0.0.1:18080/api/run
```

## 6. 更新代码

### 方式 A：在 VPS 上拉取（常用）

先在开发机提交并 `git push origin main`，然后在 VPS 上：

```bash
bash /opt/clash-merge-service/deploy/update.sh
```

脚本做的事：

1. `git fetch`，把检出移到 `origin/main`。gitignore 的文件（`config.json`、
   `state.json`、`availability.json`、`venv/`、`mihomo/`）不受影响。
2. `requirements.txt` 有变化时执行 `pip install`。
3. Python 代码或依赖有变化时重启服务：有一轮正在执行就先等它结束（最多 30 分钟），
   重启后检查 `/health`。只改了 `app/static/` 时不重启，页面每次请求都从磁盘读取，
   刷新浏览器即可。
4. `deploy/clash-merge.service` 与已安装的 unit 不同时只给出提示，不自动覆盖。

选项：

- `--no-wait`：不等当前这一轮，立刻重启。这一轮作废，已发布的文件保持不变。
- `--force`：VPS 上被跟踪的文件有改动且和 `origin/main` 不一致时，脚本默认停下；
  加这个选项会丢弃这些改动。

### 方式 B：直接上传文件

适合还没提交、想先在 VPS 上试一下的改动。把改过的文件传到同样的路径，然后重启：

```bash
# 在开发机上
scp -r app main.py requirements.txt root@<vps>:/opt/clash-merge-service/
ssh root@<vps> systemctl restart clash-merge.service
ssh root@<vps> curl -s http://127.0.0.1:18080/health
```

- 只传了 `app/static/admin.html` 时不需要重启。
- 仓库有 `.gitattributes`（`* text=auto eol=lf`），工作区文件都是 LF，可以直接上传。
  如果某个文件是在加这条规则之前检出的、还是 CRLF，先重新检出它再上传。
- 上传后 VPS 上 `git status` 会显示这些文件有改动，这是正常的，
  `git diff` 可以看到线上和提交之间的差别。

上传的改动之后提交并推送了，在 VPS 上再跑一次 `deploy/update.sh`：
文件内容和 `origin/main` 一致时，脚本直接把检出对齐到新提交，不需要 `--force`。
想放弃上传的改动、回到已提交的版本，用 `deploy/update.sh --force`。

### 回滚

```bash
cd /opt/clash-merge-service
git log --oneline -5
git reset --hard <要回到的提交>
systemctl restart clash-merge.service
```

之后再执行 `deploy/update.sh` 会重新更新到 `origin/main`。

### 把一个不是 git 检出的部署目录改成 git 检出

目录里已经有代码和运行数据、但没有 `.git` 时（2026-11-14 之前这台 VPS 就是这样）：

```bash
cd /opt/clash-merge-service
git init -b main
git remote add origin https://github.com/jayzqj/clash-merge-service.git
git fetch origin main
git reset --mixed origin/main      # 只移动 HEAD 和索引，不改任何文件
git branch --set-upstream-to=origin/main
git status                         # 列出磁盘上和 origin/main 不一致的文件
```

`git status` 干净说明磁盘上的代码和 `origin/main` 一致；有差别就先看 `git diff`，
确认后用 `deploy/update.sh --force` 对齐。
