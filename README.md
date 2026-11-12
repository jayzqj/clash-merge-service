# clash-merge-service

把多个 Clash 订阅合并成一份订阅：逐节点测纯净度（全部保留，不设阈值），
套用自定义规则 ini 生成最终 YAML，托管在 VPS 上，以固定地址
`https://xiuyuan.9527653.xyz/jay_filtered.yaml` 供 Clash Verge 直接订阅。
常驻后端服务 + 配置页 `https://xiuyuan.9527653.xyz/admin`。

需求依据：`~/workspace/your_files/clash-merge-subscription-requirements/clash-merge-subscription-requirements.md`
（本文档不重复全部需求，只写实现层面的约定；冲突时以需求文档为准。）

## 语言选择：Python 3.11 + FastAPI + Uvicorn + PyYAML + httpx

- VPS 已有 Python 3.11.2，无需另装运行时；部署就是 `venv + pip install -r requirements.txt`。
- 现有每 3 小时纯净度任务的脚本就是 Python，逐节点自查的方法（mihomo
  切换节点 → 经 mixed port 查 `my.123169.xyz/v1/info` → 记录出口 IP / 国家 /
  fraudScore，失败重试 3 次）可以直接复用，见 `app/purity.py`。
- 服务本身常驻只占几十 MB，适合 2 GB VPS；mihomo 只在每轮检测时临时启动，
  检测完立即停掉，不常驻。
- FastAPI 自带 `/admin` 静态页 + `/api/*`，HTTP Basic 鉴权实现简单；
  PyYAML / httpx 覆盖解析与拉取，无需重型依赖。

## 结构

```
main.py                  入口 (uvicorn, 127.0.0.1:18080)
app/
  config.py              持久化 config.json、校验、脱敏视图
  fetcher.py             拉取订阅并校验“确实是可解析的 Clash YAML 且 >0 节点”
  merge.py               合并 + 去重（server+port+密码/UUID/路径）+ 按纯净度排序
  purity.py              mihomo 逐节点纯净度检测（复用旧任务方法）
  converter.py           本地套用 ini（分组/规则/base 模板/exclude_remarks）
  dialer.py              转换后注入 dialer-proxy、填充后置分组
  notify.py              企业微信通知（成功和失败都发，末尾带发送方行）
  pipeline.py            流水线 + 调度器 + 运行锁 + 状态
  web.py                 FastAPI：/health、/admin、/api/*
  static/admin.html      单页管理界面
config.example.json      配置模板（占位符，可提交）
config.json              真实配置（gitignored，含订阅 token 与 webhook，勿提交）
deploy/
  clash-merge.service    systemd 单元（开机自启、失败自动拉起）
  nginx-xiuyuan.conf     /admin 反代 + /jay_filtered.yaml 静态文件
  DEPLOY.md              部署步骤
```

## 配置

`config.json`（首次部署时从 `config.example.json` 复制后填真实值）：

| 字段 | 说明 |
| --- | --- |
| `subscriptions[]` | `{id, url, enabled}`；新增只需一个 URL；地址绝不写死在代码里 |
| `interval_seconds` | 默认 3600；最小值 `min_interval_seconds`（默认 300，一轮纯净度检测要几十分钟） |
| `notify_enabled` | 企业微信总开关；开 = 成功和失败都发，关 = 一条不发；只节点变化不发 |
| `wechat_webhook` | 沿用之前任务的企业微信 webhook（只放配置文件里） |
| `ini_url` | 自定义规则 ini（subconverter 远程配置） |
| `subconverter_base` / `use_remote_subconverter` / `merged_public_url` | 见下“转换方式” |
| `output_path` | 最终文件路径，VPS 上如 `/var/www/xiuyuan/jay_filtered.yaml` |
| `purity_enabled` | 纯净度检测开关；关则全部标记未测，仍保留全部节点 |
| `purity_workers` | 并行检测 worker 数（1–16，默认 1 = 串行）；每个 worker 独占一个 mihomo 实例（端口 base+i、工作目录 `mihomo/worker-i`），节点轮询分片。4 worker 时 409 节点一轮约 10–20 分钟（串行约 60 分钟）。2GB VPS 建议 ≤4，每个实例多占约 30–50MB。管理页可调 |
| `check_front` | 静态节点检测专用前置：一个完整的节点定义（订阅里一个 `proxies` 项的形状），只写配置文件，不进管理页，接口只回显名字/类型/服务器，密码永不回显。**必须配**：Bright Data 网关只接受特定出口 IP 的链路——实测 2026-11-12，机场前置（洛杉矶 hysteria2、日本 vless/hysteria2）走静态节点全部被掐断，只有用户 VPS 上的 Racknerd anytls 节点（出口即 VPS IP）能测通，分数与老任务完全一致（静态01 57%、02 28%、03 20%）。不配时回退到从订阅里挑前置，静态节点基本测不到 |
| `mihomo_bin` / `mihomo_workdir` / `mihomo_mixed_port` / `mihomo_controller_port` | 检测用 mihomo 的位置与端口（默认 7890 / 9090，避开 sing-box 已占端口，部署时先核对） |

管理员账号密码**不在** `config.json` 里，来自环境变量：

```
ADMIN_USER=jayzqj            # 默认即 jayzqj
ADMIN_PASSWORD=<只写在 VPS 的 /etc/clash-merge/env 里>
# 或 ADMIN_PASSWORD_FILE=/etc/clash-merge/admin_password
```

API 与页面从不回显真实订阅 URL（只给 scheme+host+path 的脱敏版）与
webhook（只显示是否已设置）。

## 流水线（硬性顺序）

1. 拉取全部已启用的订阅源；响应必须确实是可解析的 Clash YAML 且节点数 > 0，
   不能只看 HTTP 200（已知故障：某订阅曾返回 HTTP 400 纯文本
   `Subscription source #10 could not be fetched…`）。
2. 合并 + 去重。去重键 = 除名字外的全部连接字段（server、port、密码/UUID、
   `servername`/`sni`、`ws-opts` 的 path/Host 等身份字段的组合；实现见
   `app/merge.py::dedup_key`）。**不能**只按 server+port 去重，也不能只按
   server+port+密码/UUID 去重：实测共用同一 Cloudflare IP 的 vless 节点
   密码/UUID 相同，只靠 `servername` 与 `ws-opts.path` 区分；反过来，
   共用 server+port 的 hysteria2 节点（洛杉矶 1/2 号、东京 1–3 号）除名字
   外全部字段相同，是同一端点挂了多个名字，按规则应合并为一个。
3. 纯净度检测：mihomo 逐个切换节点自查，每节点失败重试 3 次；
   记录出口 IP、国家、fraudScore，校验出口 IP 与节点预期 IP 是否一致，
   验证链接记为 `https://ippure.com/?ip=<IP>`；节点按 IP 识别，不按国家标签。
   **不设阈值，全部保留**；只用于排序（已测按分数升序，未测排最后、保持原顺序）。
   并行数见 `purity_workers`（每个 worker 一个 mihomo 实例）。
   静态节点检测时走配置的 `check_front`（见上表），不用它网关会拒绝。
   检测前先用 ini 的 `exclude_remarks` 预过滤（和转换时同一套
   `re.search` 匹配，当前 ini 无 `^` 锚点，两者结果完全一致），
   被排除的节点不参与检测；检测后按可用性记录过滤：连续 3 次
   测不到的节点不输出（仍每轮测试，测到可用时自动恢复；记录存
   在 `availability.json`，管理页可查看并清空）。
   排序后，测到的节点会在名字前加上纯净度分数，例如
   `日本02 三网高速`（fraudScore 5）变成 `5%-日本02 三网高速`；
   没测到的节点保持原名。加前缀不影响分组匹配（ini 的分组正则都是
   搜索式匹配），同名同分时后面一个会再加 ` (n)` 以保证名字唯一。
4. 套用 ini（见下）。
5. 转换**之后**再注入 `dialer-proxy`（转换服务通常会剥掉该字段）：
   名字含 `静态` 的 TCP 后置节点加 `dialer-proxy: 🚪 ISP链式前置分组`
   （一字不差：🚪 + 一个空格 + `ISP链式前置分组`）；
   `静态` 的 hysteria2 / tuic / wireguard 不加；前置与普通节点不加。
   同时把 ini 里 `select []REJECT` 占位的两个后置分组
   （🎯 ISP链式后置分组、🎯 链式代理后置分组）填入后置节点名。
6. 成功才原子替换输出文件（写临时文件再 `os.replace`）；任何一步失败都保留
   上一次的好文件，并记录失败步骤 + 原因。

运行锁：上一轮没跑完时，调度或手动触发直接跳过并记日志，绝不并发跑两轮。
保存任何配置后立即按新配置跑一轮，并按新间隔重算下次执行时间。

## 转换方式（实现时做的决定）

需求文档写的是调远程 subconverter
`/sub?target=clash&url=<源>&config=<ini>`。但 `url=` 必须是远程服务能拉到的
公开地址，而合并后的中间文件只存在于 VPS 本地——最终公开地址是本流水线的
**输出**，不能同时当输入，直接照抄会形成循环。

因此 v1 默认**本地套用 ini**（`app/converter.py`）：解析 ini 的
`custom_proxy_group`（分组名正则 / 分组引用 / url-test 参数）、`ruleset`
（生成 rule-providers 与 rules）、`clash_rule_base`（拉取 base_jay.yaml 作模板，
失败时用最小内置模板）、`exclude_remarks`（按 ini 正则过滤节点）。
分组名在 ini 原文件里有跨行断开的情况（如 🚪 ISP 链式前置分组），解析时已做
逻辑行重接；分组名与正则以 ini 现行内容为准。

远程模式仍保留为可选项：`use_remote_subconverter=true` 且填好
`merged_public_url`（一个能公开访问到的合并中间文件地址）时，流水线改为
调远程 `/sub`。默认关闭。

## 通知

开关打开时，成功和失败都发企业微信；失败说明失败步骤和原因；只节点数量 /
内容变化、不伴随成功或失败时不发。每条通知末尾带一行：

```
发送方：Muse（账号：azqjzm@gmail.com）
```

## 本地运行（开发机）

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.json config.json   # 填真实值；config.json 不提交
ADMIN_USER=jayzqj ADMIN_PASSWORD=<本地测试密码> \
  CLASH_MERGE_CONFIG=$PWD/config.json PORT=18080 \
  venv/bin/python main.py
# curl http://127.0.0.1:18080/health
# 浏览器打开 http://127.0.0.1:18080/admin （会弹 Basic Auth）
```

注意：本开发机 UDP 出站被封，hysteria2 / tuic 在这里测不了、跑不起来，
纯净度检测会把这些节点标为未测（仍保留）。VPS 上 UDP 是通的，
部署后以 VPS 实测为准；不要把开发机的限制带到 VPS 方案里。
本机也没有 mihomo 二进制时，检测步骤会自动跳过（全部未测），合并流程照常走完。

## 验收清单（来自需求文档）

- [ ] 最终 YAML 节点总数 = 当前配置中全部订阅源去重后的总数（无阈值丢弃）
- [ ] 静态节点全部进入静态分组，且其中 TCP 节点都带正确的 `dialer-proxy`
- [ ] hysteria2 前置仍在前置分组内
- [ ] 订阅地址在 Clash Verge 中刷新后可用（能拉取、能解析、链式节点能连通）
- [ ] 配置页新增订阅源后不重启，下一次执行就包含它；删除后下一次不再包含
- [ ] 改执行间隔后下一次计划执行时间随之变化
- [ ] 通知开关关掉后不再发企业微信、打开后恢复发送
- [ ] 任一订阅拉取失败 / 解析 0 节点 / 转换失败时，不覆盖上一次的好文件

## 待 VPS 验证（本机无法验证，见 deploy/DEPLOY.md）

- hysteria2 节点在 VPS 上能测通纯净度
- 首轮完整运行的耗时（一轮约 270 节点串行检测）
- `https://xiuyuan.9527653.xyz/jay_filtered.yaml` 在 Clash Verge 里实际订阅
- nginx 转发修正后 `/admin` 与 `/jay_filtered.yaml` 都正常
