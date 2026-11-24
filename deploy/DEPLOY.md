# 部署步骤（Racknerd VPS, Debian 12, Python 3.11.2）

部署目录是 `/opt/clash-merge-service`，它是本仓库 `main` 分支的一个 git 检出。
更新代码有两种方式，见第 6 节：在 VPS 上拉取（常用），或者直接上传文件。

服务以专用用户 `clash-merge` 运行，不是 root。目录分工：

| 路径 | 属主 | 服务进程权限 | 内容 |
|---|---|---|---|
| `/opt/clash-merge-service` | root | 只读 | 代码、venv |
| `/var/lib/clash-merge/` | clash-merge | 可写（唯一可写的位置） | `config.json`、`state.json`、`availability.json`、`purity_history.json`、`subscription.token` |
| `/var/lib/clash-merge/output/` | clash-merge | 可写 | 发布的 `jay_filtered.yaml` |
| `/var/lib/clash-merge/mihomo/` | clash-merge | 可写 | geodata、各检测 worker 的目录 |
| `/usr/local/bin/mihomo` | root | 只读 | 检测内核 |
| `/etc/clash-merge/env` | root，0600 | 读不到 | systemd 以 root 读取后注入环境变量 |

订阅由服务本身提供，地址是
`https://xiuyuan.9527653.xyz/subscription/<token>/jay_filtered.yaml`。
token 存在 `/var/lib/clash-merge/subscription.token`，首次启动时自动生成。
网站根目录里不放任何订阅文件。

**从旧部署（root 运行、订阅文件放在 1Panel 站点目录）升级，直接看第 7 节。**

## 0. 部署前先在 VPS 上核对

```bash
ss -tlnp | grep -E ':(789[0-9]|909[0-9]|18080)\b'   # 检测端口和 18080 是否空闲
ls /opt/clash-merge-service                         # 部署目录
ls -l /usr/local/bin/mihomo                         # 检测用 mihomo 二进制
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/
```

每个检测 worker 占用两个端口：`mihomo_mixed_port + i` 和
`mihomo_controller_port + i`（默认 7890+i、9090+i）。现在这台 VPS 上 9095、9096
被 docker 占用，所以 `purity_workers` 不要超过 5（线上是 4）。

mihomo 二进制放到 `/usr/local/bin/mihomo`（属主 root，权限 0755），**不要**放在
服务可写的目录里。geodata（`geosite.dat` / `geoip.dat` / `Country.mmdb` 等）放到
`mihomo_workdir`，也就是 `/var/lib/clash-merge/mihomo`。

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
useradd --system --home-dir /var/lib/clash-merge --no-create-home \
        --shell /usr/sbin/nologin clash-merge
install -d -o clash-merge -g clash-merge -m 0750 \
        /var/lib/clash-merge /var/lib/clash-merge/output /var/lib/clash-merge/mihomo
install -o clash-merge -g clash-merge -m 0600 \
        config.example.json /var/lib/clash-merge/config.json
# 编辑 /var/lib/clash-merge/config.json：填订阅地址、wechat_webhook；
# 确认 ini_url 与 subconverter_base。mihomo_bin / mihomo_workdir / output_path
# 保持示例里的值。geodata 拷到 /var/lib/clash-merge/mihomo/ 之后：
chown -R clash-merge:clash-merge /var/lib/clash-merge
mkdir -p /etc/clash-merge
cat > /etc/clash-merge/env <<'EOF'
ADMIN_USER=jayzqj
ADMIN_PASSWORD=<只写在这里>
CLASH_MERGE_CONFIG=/var/lib/clash-merge/config.json
CLASH_MERGE_PUBLIC_BASE=https://xiuyuan.9527653.xyz
PORT=18080
EOF
chmod 600 /etc/clash-merge/env
```

`output_path` 只能是 `/var/lib/clash-merge/output/` 下的 `.yaml` 文件，
不能指向网站目录或其他任何位置。服务启动时和每轮写文件前都会检查：不合法时这一轮在
写文件这一步失败，上一次的结果保留。管理页上这一项是只读的，只能在 `config.json` 里改。
不写这一项时，默认是 `/var/lib/clash-merge/output/jay_filtered.yaml`。

`CLASH_MERGE_PUBLIC_BASE` 只接受 `https://域名`，不能带路径。没设置时订阅照常可用，
只是管理页和企业微信通知里会显示“未配置”，因为拼不出完整地址。

管理员密码建议直接写在 env 文件里。如果改用 `ADMIN_PASSWORD_FILE`，这个文件必须能被
`clash-merge` 用户读到，而且不能放在 `/root` 或 `/home` 下（unit 开了 `ProtectHome`），
否则 `/admin` 会返回 503。

## 3. 注册系统服务

```bash
install -m 0644 deploy/clash-merge.service /etc/systemd/system/clash-merge.service
systemctl daemon-reload
systemctl enable --now clash-merge.service
systemctl status clash-merge.service
journalctl -u clash-merge.service -f
```

unit 用了 `ProtectSystem=strict` 加 `ReadWritePaths=/var/lib/clash-merge`：
除了这个目录，整个系统对服务进程都是只读的。这是内核层面的限制，和代码怎么写无关。
代码目录也是只读的，所以 Python 不会生成 `__pycache__`，启动稍慢一点，不影响运行。

查看沙箱评分：

```bash
systemd-analyze security clash-merge.service | tail -n 1
```

## 4. 修正 xiuyuan 站点的 nginx 转发

按 `deploy/nginx-xiuyuan.conf` 配置：

- `/admin`、`/api/`、`/subscription/` 都反代到 `127.0.0.1:18080`；
- `/subscription/` 关闭 access_log，因为路径里有 token；
- 加上 `location = /jay_filtered.yaml { return 404; }`，旧地址停用；
- **删掉**旧的占位 `proxy_pass http://127.0.0.1:80;`（会自己转发给自己），
  以及旧的 `alias .../jay_filtered.yaml`。

在 1Panel 里改完后重载 OpenResty，然后验证：

```bash
TOKEN=$(cat /var/lib/clash-merge/subscription.token)
curl -s -o /dev/null -w '%{http_code}\n' "https://xiuyuan.9527653.xyz/subscription/$TOKEN/jay_filtered.yaml"  # 200（首轮成功前是 404）
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/subscription/wrong/jay_filtered.yaml    # 404
curl -s -o /dev/null -w '%{http_code}\n' https://xiuyuan.9527653.xyz/jay_filtered.yaml                     # 404
curl -s -u jayzqj:<密码> https://xiuyuan.9527653.xyz/api/status
```

订阅地址里的 token 是唯一的访问凭据。它会显示在管理页顶部，也会写在企业微信的成功通知里，
不要发到公开的地方。要更换 token：停止服务，删掉 `subscription.token`，再启动服务，
然后把所有客户端改成新地址。

## 5. 首轮验证

1. 在 `https://xiuyuan.9527653.xyz/admin` 点“立即执行一次”，观察日志与耗时。
2. 先确认 hysteria2 节点在 VPS 上能测通（UDP 通），再跑全量。
3. 检查 `jay_filtered.yaml`：节点总数 = 去重后总数；静态 TCP 节点都有
   `dialer-proxy`，值是 ini 里名字含 `前置` 的那个分组（现在是
   `🚪 链式代理前置分组`）；hysteria2 前置在前置分组内。
4. 在 Clash Verge 里订阅管理页顶部显示的地址
   （`https://xiuyuan.9527653.xyz/subscription/<token>/jay_filtered.yaml`），
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

1. `git fetch`，把检出移到 `origin/main`。gitignore 的文件（如 `venv/`）不受影响。
   运行数据都在 `/var/lib/clash-merge`，不在代码目录里。
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

## 7. 从旧部署迁移（root 运行 → `clash-merge` 用户 + 带 token 的地址）

适用于：服务以 root 运行，`config.json` 在 `/opt/clash-merge-service`，
订阅文件在 1Panel 站点目录。**迁移完成后旧地址立即停用**，迁移前准备好更新所有客户端。

新代码不能直接在旧部署上运行：旧的 `output_path` 指向站点目录，新代码会判定它不合法，
每轮都会在写文件这一步失败。所以这一次**不要**用 `deploy/update.sh`（它会用旧的 unit
直接重启服务），按下面的顺序来：

```bash
cd /opt/clash-merge-service
git rev-parse HEAD > /root/clash-merge-old-commit   # 回滚用
git fetch origin main
git reset --hard origin/main      # 只换代码，不重启；正在运行的进程不受影响
bash deploy/migrate-to-nonroot.sh
```

迁移脚本先打印计划并要求确认，然后：

1. 如果有一轮正在执行，先等它结束（最多 30 分钟，`--no-wait` 跳过），再停止服务；
2. 把 env、旧 unit 备份到 `/root/clash-merge-migrate-<时间>/`；
3. 创建 `clash-merge` 用户和 `/var/lib/clash-merge/{output,mihomo}`；
4. **复制**（不是移动）`config.json`、各状态文件和 mihomo 工作目录到 `/var/lib/clash-merge`，
   mihomo 二进制装到 `/usr/local/bin/mihomo`；
5. 把当前发布的 `jay_filtered.yaml` 复制到新位置，新地址马上能用，
   缩水保护也能继续和上一次的结果比较；
6. 改写新 `config.json` 里的 `mihomo_bin`、`mihomo_workdir`、`output_path`；
7. 改写 env 里的 `CLASH_MERGE_CONFIG`，补上 `CLASH_MERGE_PUBLIC_BASE`；
8. 安装新 unit 并启动，检查 `/health`，再用 token 在本机拉一次订阅，确认返回 200；
9. 全部成功后，把站点目录里的旧 `jay_filtered.yaml` 移进备份目录，旧地址从此 404；
10. 打印新订阅地址。

任何一步出错脚本就停下。第 9 步之前失败的话，旧文件还在原处。

脚本跑完之后：

1. 按第 4 节修改 nginx：加 `location /subscription/`，把旧的
   `location = /jay_filtered.yaml` 改成 `return 404`，然后重载 OpenResty。
2. 在管理页点“立即执行一次”，用 `journalctl -u clash-merge.service -f` 看日志，
   确认 mihomo 在新沙箱里能正常启动和检测（见下面的“需要实测”）。
3. 把所有 Clash Verge 客户端换成新地址。
4. 跑几轮都正常后，删除旧的副本：`/opt/clash-merge-service/` 下的
   `config.json`、`state.json`、`availability.json`、`purity_history.json`、`mihomo/`，
   以及备份目录。

### 需要实测

- mihomo 在 `RestrictAddressFamilies`、`PrivateDevices` 等限制下能否正常运行。
  如果日志里出现 `permission denied`、`address family not supported` 之类的错误，
  先在 unit 里注释掉对应的那一行，`systemctl daemon-reload` 后重启，确认是哪一项引起的。
- `systemd-analyze security clash-merge.service` 的评分。

### 回滚

旧的数据文件只是被复制，原件还在，所以回滚只需要恢复 env、unit、代码和旧的订阅文件：

```bash
systemctl stop clash-merge.service
B=/root/clash-merge-migrate-<时间>
cp -a "$B/env" /etc/clash-merge/env
cp -a "$B/clash-merge.service" /etc/systemd/system/clash-merge.service
[ -f "$B/old-output.yaml" ] && mv "$B/old-output.yaml" "$(cat "$B/old-output-path")"
cd /opt/clash-merge-service && git reset --hard "$(cat /root/clash-merge-old-commit)"
systemctl daemon-reload && systemctl start clash-merge.service
```

nginx 也改回旧的 `location = /jay_filtered.yaml { alias ...; }`。
注意：迁移之后各轮写入的状态（可用性记录、纯净度历史）只在 `/var/lib/clash-merge` 里，
回滚后旧部署看不到这些更新。
