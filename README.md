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
| `purity_enabled` | 纯净度检测开关；关则本轮全部记为未测，有历史分数的继续显示旧分数，不新增连续失败次数 |
| `purity_filter_enabled` | 纯净度过滤开关，默认关；开启后按本轮分数比较，检测失败或跳过时沿用最近一次成功分数，只返回分数 ≤ `purity_threshold` 的节点。没有任何分数的节点不返回，连续失败三轮仍隐藏。必须同时开启纯净度检测。后台打开开关后才显示阈值输入框 |
| `purity_threshold` | 后台「纯净度 预留%」，默认 15；输入 0–100 的数字（支持小数），15 表示保留 ≤15%，包含 15%。保存后下轮完整检测生效；过滤关闭时保留阈值但不使用 |
| `purity_workers` | 并行检测 worker 数（1–8，默认 1 = 串行）；每个 worker 独占一个 mihomo 实例（端口 base+i、工作目录 `mihomo/worker-i`），节点轮询分片。4 worker 时 409 节点一轮约 10–20 分钟（串行约 60 分钟）。2GB VPS 建议 ≤4，每个实例多占约 30–50MB。管理页可调 |
| `purity_delay_screen` / `purity_delay_url` | 延迟筛选（默认开）：每个 worker 逐个检测前，先对自己那批节点做一次并发延迟测试（mihomo 的 group delay，目标 `purity_delay_url`，超时 10 秒，做 2 轮，任一轮通过即算通过）。通过的节点照常检测（3 次 × 30 秒）；两轮都不通过的节点只做 1 次 15 秒的检测，测到照常记分，测不到才记失败。延迟测试不通过不直接判失败——实测 2026-11-14，5 秒超时下 22 个不通过的节点里有 13 个其实能测到。筛选本身出错或通过率低于 20% 时本批忽略筛选。报告里 `purity_delay_screen_failed` / `purity_delay_screen_rescued` 是不通过的节点数和其中仍测到的节点数。管理页可开关 |
| `check_front` / `check_front_enabled` | **可选**的静态节点检测前置。默认（`check_front_enabled: false`）静态节点从 VPS **直连**检测，不走任何前置——实测 2026-11-14 Bright Data 网关接受 VPS 出口 IP，直连 171 个静态节点测到 170 个。`check_front` 是一个完整的节点定义（订阅里一个 `proxies` 项的形状），可在后台输入 JSON 替换，留空保持、勾选清除后保存可移除；接口只回显名字/类型/服务器，密码永不回显；只有它已配置**且** `check_front_enabled` 为 true 时，静态 TCP 节点检测才走它（开关可在管理页切换）。不再从订阅里自动挑机场前置（实测 2026-11-12 机场前置走静态节点全部被网关掐断）。只影响检测；输出文件里静态节点仍按 ini 的前置分组注入 `dialer-proxy` |
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
3. 纯净度检测：mihomo 逐个切换节点自查，每节点失败重试 3 次；
   记录出口 IP、国家、fraudScore，校验出口 IP 与节点预期 IP 是否一致，
   验证链接记为 `https://ippure.com/?ip=<IP>`；节点按 IP 识别，不按国家标签。
   默认关闭纯净度过滤，按分数升序排列；本轮未取得分数时沿用上次成功分数，
   没有任何历史分数的节点排最后、保持原顺序。历史分数不代表本轮检测成功，
   仍正常累计连续失败次数。开启过滤后按本轮或沿用的历史分数比较，仅返回分数
   不高于预留值的节点；没有任何分数的节点不返回，连续失败三轮仍隐藏。
   历史记录存于配置文件同目录的 `purity_history.json`，连接参数的 SHA-256 作为键，
   不存节点凭据；重命名不丢历史，连接参数改变则不沿用。首次升级从当前发布文件
   的分数前缀恢复已有分数，之后每次成功测量更新记录，失败保留最近一次成功值。
   开启过滤时使用本地转换，保证转换读取本轮过滤后的节点，不从远程中间地址带回旧节点。
   并行数见 `purity_workers`（每个 worker 一个 mihomo 实例）。
   静态节点默认从 VPS 直连检测；可选走 `check_front`（见上表）。检测用的
   mihomo 关闭 IPv6（Bright Data 网关只通 IPv4）。每轮开始前先从 VPS 直连请求一次
   info 接口，请求失败就跳过本轮检测，所有节点记为未测，不计失败。检测中途 mihomo
   退出（例如 OOM）时，该分片剩余节点同样记为未测。前置节点在检测配置里内部改名为
   `__check_front__`，不会和订阅节点重名。
   检测前先用 ini 的 `exclude_remarks` 预过滤。预过滤、转换、重新应用规则
   三处都对同一个名字做 `re.search`：统一后的节点名，不带分数前缀
   `5% ` 和去重后缀 ` (2)`，所以三处结果一致。
   被排除的节点不参与检测；检测后按可用性记录过滤：连续 3 次
   测不到的节点不输出（仍每轮测试，测到可用时自动恢复；记录存
   在 `availability.json`，管理页可查看并清空）。若某一轮尝试检测的节点
   （≥10 个）里超过 60% 测不到，视为检测环境故障（VPS 出口、信息接口或
   mihomo 出问题），这一轮不累计失败次数，报告里 `availability_anomaly_skipped: true`。
   排序前先统一节点名（见 `app/names.py`）：每个节点都带上国旗，
   形状是 `{分数}% {国旗} {国家} {其余}`。普通节点是在国家词前插入
   国旗（`台湾 A01…` → `🇹🇼 台湾 A01…`），已带国旗的不动；静态节点
   重排为 `🇺🇸 美国 静态31 168.158.231.49`（国家代码变中文名，
   `「城市」` 去掉，国旗和 IP 保留，`静态` 保留）。名字里没有国家
   的，用检测到的出口国代码补国旗。只增删国旗/国家词/城市标注，
   其余词原样保留，所以 ini 的分组正则（包括前置分组的 `$` 锚点）
   匹配结果不变。
   排序后，测到的节点会在名字前加上纯净度分数，例如
   `日本02 三网高速`（fraudScore 5）变成 `5% 🇹🇼 日本02 三网高速`；
   没测到但有历史分数的节点沿用旧分数前缀；没有历史分数的保持（已统一的）原名。同名同分时后面一个会再加
   ` (n)` 以保证名字唯一。
4. 套用 ini（见下）。
5. 转换**之后**再注入 `dialer-proxy`（转换服务通常会剥掉该字段）：
   名字含 `静态` 的 TCP 后置节点加 `dialer-proxy`，值取 ini 里那个
   名字含 `前置` 的分组（现在是 `🚪 链式代理前置分组`；以前叫
   `🚪 ISP链式前置分组`，改名后程序自动跟上，不用改代码）；
   `静态` 的 hysteria2 / tuic / wireguard 不加；前置与普通节点不加。
   同时把 ini 里 `select []REJECT` 占位的后置分组（名字含 `后置分组`）
   填入后置节点名。
   再新建低纯净分组 **🎯 纯净优选**（url-test）：测到分数 ≤15 且
   不是 http 类型的节点放进去，分组插在 🤖 国外AI 的后面，并把它
   加进 🚀 手动选择和 🤖 国外AI 两个分组里。
6. 发布前保护（`guard` 步骤）：新文件 0 个节点，或节点数不到当前已发布文件的
   30%（旧文件 ≥20 个节点时），拒绝发布，保留旧文件并按失败通知。
   开启纯净度过滤时，主动过滤和连续失败隐藏的节点数计入缩减保护的比较基数；全部节点因过滤或隐藏
   不合格时允许发布空节点列表（空分组用 REJECT），不会继续返回不符合阈值的旧节点。
   来源异常造成的非预期数量骤减仍受保护。报告包含 `purity_reused`（输出中沿用旧分数数目）、
   `purity_filter_enabled`、`purity_threshold`、`purity_filter_kept` 和 `purity_filter_excluded`。
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

远程模式仍保留为可选项：`use_remote_subconverter=true` 且填好
`merged_public_url`（一个能公开访问到的合并中间文件地址）时，流水线改为
调远程 `/sub`。默认关闭。

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
