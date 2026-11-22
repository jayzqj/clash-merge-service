# clash-merge-service

支持 Linux 服务端与 Windows 11 本机版，共用核心逻辑，分别保存配置和检测结果。
Windows EXE 使用、开机启动及打包见 [WINDOWS.md](WINDOWS.md)；Linux 部署继续使用 [deploy/DEPLOY.md](deploy/DEPLOY.md)。

把多个 Clash 订阅合并成一份订阅：逐节点测纯净度，失败时沿用上次成功分数，可选按阈值过滤，
套用自定义规则 ini 生成最终 YAML，托管在 VPS 上，以固定地址
`https://xiuyuan.9527653.xyz/jay_filtered.yaml` 供 Clash Verge 直接订阅。
常驻后端服务 + 配置页 `https://xiuyuan.9527653.xyz/admin`。

需求依据：`~/workspace/your_files/clash-merge-subscription-requirements/clash-merge-subscription-requirements.md`
（本文档不重复全部需求，只写实现层面的约定；冲突时以需求文档为准。）

## 语言选择：Python 3.11 + FastAPI + Uvicorn + PyYAML + httpx

- VPS 已有 Python 3.11.2，无需另装运行时；部署就是 `venv + pip install -r requirements.txt`。
- 现有每 3 小时纯净度任务的脚本就是 Python，逐节点自查的方法（mihomo
  切换节点 → 经 mixed port 查 `my.123169.xyz/v1/info` → 记录出口 IP / 国家 /
  fraudScore）沿用原接口；连通性与评分已经拆分，见 `app/purity.py`。
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
  purity_history.py      保存上次成功分数、失败时沿用、按当前或历史分数过滤
  converter.py           本地套用 ini（分组/规则/base 模板/exclude_remarks）
  dialer.py              转换后注入 dialer-proxy、填充后置分组
  notify.py              企业微信通知（成功和失败都发，附过滤值、分钟用时及下次计划）
  pipeline.py            流水线 + 调度器 + 运行锁 + 状态
  web.py                 FastAPI：/health、/admin、/api/*
  static/admin.html      单页管理界面
config.example.json      配置模板（占位符，可提交）
config.json              真实配置（gitignored，含订阅 token 与 webhook，勿提交）
deploy/
  clash-merge.service    systemd 单元（开机自启、失败自动拉起）
  nginx-xiuyuan.conf     /admin 反代 + /jay_filtered.yaml 静态文件
  DEPLOY.md              部署步骤（首次部署、git 拉取更新、直接上传文件）
  update.sh              VPS 上的更新脚本：拉取 origin/main，按需重启并检查
```

## 配置

`config.json`（首次部署时从 `config.example.json` 复制后填真实值）：

| 字段 | 说明 |
| --- | --- |
| `subscriptions[]` | `{id, url, enabled, name?}`；新增必须填写 URL，名称可选（最多 100 字符）；列表支持编辑或清空名称，仅修改名称不触发执行或改变计划时间；旧配置无名称时显示“未命名”；地址绝不写死在代码里 |
| `interval_seconds` | 默认 3600；最小值 `min_interval_seconds`（默认 300，一轮纯净度检测要几十分钟），最大 86400（一天） |
| `execution_start` / `execution_end` | 每天允许启动完整检测的时段，按北京时间，默认 `08:00`–`23:59`；后台可填写如 `07:00`–`23:00`。格式为 `HH:MM`，包含结束分钟；开始晚于结束表示跨天，两者不能相同，全天执行使用 `00:00`–`23:59`。自动调度、保存配置触发、手动立即执行都遵守此限制；已启动的一轮允许完成 |
| `notify_enabled` | 企业微信总开关；开 = 成功和失败都发，关 = 一条不发；只节点变化不发 |
| `wechat_webhook` | 沿用之前任务的企业微信 webhook（只放配置文件里） |
| `ini_url` | 自定义规则 ini（subconverter 远程配置） |
| `subconverter_base` / `use_remote_subconverter` / `merged_public_url` | 见下“转换方式” |
| `output_path` | 最终文件路径，VPS 上如 `/var/www/xiuyuan/jay_filtered.yaml` |
| `purity_enabled` | 只控制评分请求；关闭仍必须检测连通性，有历史分数时继续沿用。评分失败不累计连通性失败 |
| `purity_filter_enabled` | 默认关；开启后先要求本轮连通，再用本轮或历史分数比较阈值。没有任何分数的不返回。必须同时启用评分 |
| `purity_threshold` | 后台「纯净度 预留%」，默认 15；输入 0–100 的数字（支持小数），15 表示保留 ≤15%，包含 15%。保存后下轮完整检测生效；过滤关闭时保留阈值但不使用 |
| `purity_workers` | 并行检测 worker 数（1–8，默认 1 = 串行）；每个 worker 独占一个 mihomo 实例（端口 base+i、工作目录 `mihomo/worker-i`），节点轮询分片。4 worker 时 409 节点一轮约 10–20 分钟（串行约 60 分钟）。2GB VPS 建议 ≤4，每个实例多占约 30–50MB。管理页可调 |
| `purity_delay_screen` | 默认开：批量检测连通性；关闭改为逐个检测，失败节点当轮立即排除 |
| `connectivity_url` / `connectivity_timeout_ms` | 独立于评分接口的 HTTP 连通性目标（默认 Cloudflare generate_204）与超时（默认 10000ms，范围 1000–30000），最多两轮，任一轮通过即算连通 |
| `fetch_proxy_url` / `fetch_direct_fallback` | 下载代理；默认开启代理失败后直连重试。只影响订阅和规则下载，不改变节点检测链路；报告记录每个源的实际下载路径 |
| `check_front` / `check_front_enabled` | 可选额外前置候选。后置 TCP 节点始终自动选取本轮已测通的前置，链路失败时最多尝试 8 个不同地区或端点的候选。自定义候选也须检测，通过后按同样规则过滤并输出。最终 dialer-proxy 固定到实测前置节点名 |
| `mihomo_bin` / `mihomo_workdir` / `mihomo_mixed_port` / `mihomo_controller_port` | 检测用 mihomo 的位置与端口（默认 7890 / 9090，避开 sing-box 已占端口，部署时先核对） |

管理员账号密码**不在** `config.json` 里，来自环境变量：

```
ADMIN_USER=jayzqj            # 默认即 jayzqj
ADMIN_PASSWORD=<只写在 VPS 的 /etc/clash-merge/env 里>
# 或 ADMIN_PASSWORD_FILE=/etc/clash-merge/admin_password
```

登录后的管理页和 API 完整显示订阅 URL（含 token，只有管理员登录后能看到）。
webhook 只显示是否已设置；`check_front` 只回显名字、类型、服务器和端口，
密码不回显。日志和企业微信通知里不出现订阅 URL。

## 流水线（硬性顺序）

1. 拉取全部已启用的订阅源；响应必须确实是可解析的 Clash YAML 且节点数 > 0，
   不能只看 HTTP 200（已知故障：某订阅曾返回 HTTP 400 纯文本
   `Subscription source #10 could not be fetched…`）。
   单个源失败（HTTP 错误、空订阅、解析失败）只跳过该源，用其余源继续跑，
   失败的源（地址 + 原因）记进本轮报告的 `sources_failed`；只有全部源都
   失败时整轮才失败。成功通知里显示订阅源成功/总数（如 3/4），并提示有源
   失败、地址见管理页（通知里从不出现订阅地址）；管理页有“末轮失败的订阅源”
   一栏，显示失败地址和原因。
2. 合并 + 去重。去重键 = 除名字外的全部连接字段（server、port、密码/UUID、
   `servername`/`sni`、`ws-opts` 的 path/Host 等身份字段的组合；实现见
   `app/merge.py::dedup_key`）。**不能**只按 server+port 去重，也不能只按
   server+port+密码/UUID 去重：实测共用同一 Cloudflare IP 的 vless 节点
   密码/UUID 相同，只靠 `servername` 与 `ws-opts.path` 区分；反过来，
   共用 server+port 的 hysteria2 节点（洛杉矶 1/2 号、东京 1–3 号）除名字
   外全部字段相同，是同一端点挂了多个名字，按规则应合并为一个。
3. 先做独立的连通性检测。普通节点从运行设备直连检测；后置 TCP 节点先挑选已测通的前置，验证完整链路。默认 HTTP 测试目标为 Cloudflare generate_204，超时 10 秒，最多两轮；失败的节点本轮立即排除，不等待连续三轮，也不允许历史分数把它保留下来。没有候选前置的后置节点不返回。
   评分在已连通节点上单独执行，使用同一个前置和链路。评分接口故障不会跳过连通性测试；最多两次 × 10 秒评分请求，同一分片连续三个已连通节点评分失败后暂缓该分片后续评分，下轮重试。
   本轮未取得评分时沿用最近成功分数；纯净度过滤关闭则保留已连通的无分数节点，开启则还须本轮或历史分数不高于阈值，没有分数的节点不返回。前置被评分过滤移除时，依赖它的后置也移除。评分不影响连通性判定。
   `purity_history.json` 使用连接参数 SHA256 保存评分；`availability.json` 仅用于显示连通失败记录。内核启动失败或中途退出则整轮失败，不把未测节点作为通过发布。
   节点继续按统一名字、国旗和纯净度分数排序。过滤、规则生成与名称匹配沿用相同的标准化逻辑。
4. 套用 ini（见下）。
5. 转换之后为静态 TCP 后置固定 `dialer-proxy`，目标为本轮实际测通的前置节点名，确保使用时链路与检测时一致。UDP 后置保持协议支持的直连检测方式；后置分组填入已通过过滤的后置节点。报告 `tested_chains` 逐项记录后置与前置的对应关系。
   低纯净分组仍使用 ≤15 分且非 http 的已输出节点。
6. 发布前保护仍拒绝订阅来源异常造成的非预期数量骤减。因本轮连通性失败、评分阈值或前置依赖主动排除的节点计入预期缩减；全部被排除时允许发布空节点列表（空组使用 REJECT），避免继续返回已测失败节点。
7. 成功才原子替换输出文件：临时文件写在 `config.json` 同目录（不在 nginx
   静态目录里，候选文件不会被下载到），`mihomo -t` 校验通过后 `os.replace`；
   失败时临时文件被删除。任何一步失败都保留上一次的好文件，并记录失败步骤 + 原因。

运行锁：上一轮没跑完时，调度或手动触发直接跳过并记日志，绝不并发跑两轮。
配置页提供「只保存，下轮生效」和「保存设置并立即执行」两种操作。
只保存会持久化配置，不启动检测，不打断正在运行的一轮，并保留已有的下次执行计划；
如果修改后的执行时段不允许原计划时间，则顺延到允许的时间。新间隔用于后续轮次。
接口使用 `PUT /api/config` 的 `run_now: false` 选择只保存，该字段不写入配置文件。
未传 `run_now` 或传 `true` 保持原行为：在允许时段内立即按新配置跑一轮（已有一轮在跑时不另起，接口返回
`run_started: false`，改动从下一轮生效），并按新间隔重算下次执行时间——
调度器会被立即唤醒，按新的下次时间等待。时段外不启动完整检测，下一次执行
顺延到下一个开始时间；服务重启也会修正旧计划，避免在禁止时段补跑。
已在运行的任务允许完成，结束后再根据当前间隔和时段安排下一轮。
「重新应用规则」仅更新现有文件，不运行完整检测，不受此启动时段限制。订阅源并发拉取。

**重新应用规则**：改了 gist 上的 ini 后，不用等下一轮整点执行，管理页
点「重新应用规则」即可：用最新的 ini 对当前订阅文件重新算分组和规则，
不重新测纯净度（几秒完成）。只对文件里已有的节点生效——被预过滤或连续
失败隐藏的节点不会因此回来，节点名也不改，这些都等下一轮完整执行。
结果显示在「上次执行结果」里（`kind: "reapply"`）。管理页还只读显示
ini 的 `exclude_remarks` 内容和上轮实际过滤掉的节点名。

## 转换方式（实现时做的决定）

需求文档写的是调远程 subconverter
`/sub?target=clash&url=<源>&config=<ini>`。但 `url=` 必须是远程服务能拉到的
公开地址，而合并后的中间文件只存在于 VPS 本地——最终公开地址是本流水线的
**输出**，不能同时当输入，直接照抄会形成循环。

因此 v1 默认**本地套用 ini**（`app/converter.py`）：解析 ini 的
`custom_proxy_group`（分组名正则 / 分组引用 / url-test 参数）、`ruleset`
（生成 rule-providers 与 rules）、`clash_rule_base`（拉取 base_jay.yaml 作模板，
ini 没配 base 时用最小内置模板；配了但拉取失败则本轮失败、保留旧文件）、`exclude_remarks`（按 ini 正则过滤节点）。
分组名在 ini 原文件里有跨行断开的情况（如 🚪 ISP 链式前置分组），解析时已做
逻辑行重接；分组名与正则以 ini 现行内容为准。

严格验证模式强制本地转换，旧远程转换字段仅为兼容保留，不会通过外部中间地址重新带入未测节点。

## 通知

开关打开时，成功和失败都发企业微信；失败说明失败步骤和原因；只节点数量 /
内容变化、不伴随成功或失败时不发。通知不附发送方信息。

成功通知的后置节点仅显示数量，不显示 TCP / UDP 注入明细。
成功和失败通知都显示本轮纯净度过滤开关及配置值、执行间隔、每天执行时段、下次执行时间
（北京时间）和本轮用时。执行间隔、用时均以分钟显示，最多保留两位小数。
下次执行时间与后台调度一致；运行中修改间隔时通知显示更新后的间隔和计划，
过滤值则对应本轮实际使用的配置。

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

不同运行设备的 UDP、路由和运营商限制可能不同；Windows 本机结果以本机网络为准，Linux VPS 结果以服务器网络为准。
缺少 mihomo 内核时本轮检测失败，不发布未验证节点。

## 验收清单（来自需求文档）

- [ ] 最终 YAML 节点总数 = 当前配置中去重、规则预过滤、本轮连通性和可选评分过滤后的总数
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
