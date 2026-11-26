# 0002 linux.do 分组的分流域名

状态：已接受。规则的实现方式（由代码生成并置顶）Superseded by 0011，改为用户写在 ini 中。

## 背景

Cloudflare 验证涉及两类请求：

- linux.do 自己域名下的验证脚本和结果提交（`/cdn-cgi/challenge-platform/...`）；
- 交互式验证框，来自 `challenges.cloudflare.com`，所有 Cloudflare 站点共用。

验证结果和页面请求的出口 IP 不一致时，可能通过验证后又被要求验证。

本机实测（同一时刻，经 Clash Verge）：

| 地址 | 出口 IP | Cloudflare 机房 |
| --- | --- | --- |
| `https://linux.do/cdn-cgi/trace` | 168.144.255.28（SG） | SIN |
| `https://www.cloudflare.com/cdn-cgi/trace` | 95.135.35.44（US） | LAX |
| `https://cp.cloudflare.com/cdn-cgi/trace` | 95.135.35.44（US） | LAX |

可见当前 linux.do 和 Cloudflare 自有域名已经从不同出口访问，验证框与页面出口不一致的情况确实存在。

## 决策

linux.do 专用分组的规则覆盖：

- `DOMAIN-SUFFIX,linux.do`（包含验证脚本路径和所有子域名）；
- `DOMAIN,challenges.cloudflare.com`。

规则须排在 `GEOSITE` / `GEOIP` / `FINAL` 之前。

## 备选方案

- **只加 linux.do**：其他网站不受影响，但 linux.do 的验证框可能走另一出口，导致反复验证。放弃。
- **全部 Cloudflare 验证相关地址都走新分组**：改变的是所有 Cloudflare 站点的验证行为，超出“只优化 linux.do”的范围。放弃。

## 后果

- 正面：linux.do 页面、验证脚本、验证框使用同一出口。
- 负面：其他 Cloudflare 站点的验证框也走 linux.do 分组；如果这些站点页面走的出口不同，可能多验证一次。
- 跟进：
  - linux.do 的图片、头像如果用了非 `linux.do` 后缀的 CDN 域名，需要补充（待确认）；
  - 上表中本机 linux.do 出口在几分钟内从日本（188.253.118.70）变成新加坡（168.144.255.28），说明 url-test 分组在自动切换出口，这正是重复验证的诱因之一，见 ADR 0001 的跟进项“防抖或固定策略”。
