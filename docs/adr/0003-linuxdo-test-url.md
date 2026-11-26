# 0003 linux.do 分组的测速地址

状态：已接受

## 背景

url-test 只测一次小请求的往返时间（mihomo 使用 HEAD 请求）。用户担心：测 linux.do 能不能反映实际体验，会不会出现“linux.do 测得快、但 Cloudflare 验证接口慢”。

分析：

- linux.do 页面、验证脚本（`/cdn-cgi/challenge-platform/`）都由同一出口落到的同一个 Cloudflare 机房响应，线路延迟基本相同；
- `challenges.cloudflare.com` 已按 ADR 0002 与 linux.do 走同一分组；
- url-test 测不到的问题另有归属：是否被验证靠纯净度预选（ADR 0001）；图片带宽 url-test 测不到；源站本身慢与节点无关，换节点也解决不了。

本机实测（同一时刻，HEAD 请求 4 次）：

| 地址 | 状态 | 机房 | 耗时 ms |
| --- | --- | --- | --- |
| `https://linux.do/cdn-cgi/trace` | 403 | SIN | 412, 226, 251, 216 |
| `https://linux.do/` | 403 | SIN | 227, 321, 227, 216 |
| `https://cp.cloudflare.com/generate_204` | 204 | LAX | 1176, 941, 1059, 1103 |

`cp.cloudflare.com` 落到了另一个出口和机房，不能代表访问 linux.do 的线路。

## 决策

测速地址使用 `https://linux.do/cdn-cgi/trace`，不设置 `expected-status`。

## 备选方案

- **`https://linux.do/`**：没被验证时会到达源站，测得的时间混入源站处理耗时；被验证时由机房直接返回验证页。不同节点的数值不可比。放弃。
- **沿用 `https://cp.cloudflare.com/generate_204`**：与现有地区分组一致，但实测与 linux.do 落到不同机房，测的不是同一条线路。放弃。

## 后果

- 正面：只测“用户网络 → 节点 → linux.do 所在机房”这条线路，结果稳定、不经过源站。
- 负面：HEAD 请求返回 403，不能用 `expected-status` 区分“被验证”和“未被验证”；这部分完全依赖纯净度预选。
- 跟进：如果以后 linux.do 关闭或限制 `/cdn-cgi/trace`，需要换地址（任何返回状态都算通，只有超时才算失败）。
