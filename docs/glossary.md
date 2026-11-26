# 术语表

按拼音 / 字母排序。

| 术语 | 定义 | 别名 | 备注 |
| --- | --- | --- | --- |
| Cloudflare 验证 | Cloudflare 对可疑访问返回的人机验证页，响应头带 `cf-mitigated: challenge`，状态码通常为 403。通过后下发 `cf_clearance` Cookie | 5 秒盾、托管质询（Managed Challenge）、Turnstile | 同域名部分走 `/cdn-cgi/challenge-platform/`；交互式验证框来自 `challenges.cloudflare.com` |
| Cloudflare 机房 | 实际响应请求的 Cloudflare 节点，`cf-ray` 响应头末尾或 `/cdn-cgi/trace` 的 `colo=` 字段给出三字码 | colo、PoP、边缘节点 | 如 NRT（东京）、SIN（新加坡）、LAX（洛杉矶）；同一出口访问不同 Cloudflare 域名可能落到不同机房 |
| 出口 IP | 节点访问目标网站时，目标网站看到的 IP | 落地 IP | Cloudflare 验证频率主要看它的信誉 |
| 纯净度 | 出口 IP 的 fraudScore，数值越低越干净，由本服务逐节点检测 | 纯净度分数、fraudScore | 见 `app/purity.py`；`🎯 纯净优选` 分组阈值为 ≤15 |
| 服务端预选 | 本服务在 VPS（或 Windows 本机版）检测后，按与用户网络无关的属性（连通、纯净度、出口地区）决定分组成员 | — | 见 ADR 0001 |
| 客户端测速 | Clash Verge 在用户本机对 url-test 分组成员测速并自动选最快节点 | url-test 测速 | 反映“用户网络 → 节点”线路；见 ADR 0001 |
| url-test | Clash/mihomo 的自动选择分组类型：定时向测速地址发一个请求，选延迟最低的成员；新延迟比当前低出 `tolerance` 毫秒以上才切换 | 自动测速分组 | 只测一次小请求的往返，不测带宽，也看不出是否会被 Cloudflare 验证 |
| 占位分组 | ini 中只写 `REJECT` 作为成员、转换后由代码按条件填入节点的分组 | 填充分组 | 现有：名字含 `后置分组` 的分组（`fill_backend_groups`）；新增：名字含 `LinuxDo` 的 url-test 分组（ADR 0011） |
