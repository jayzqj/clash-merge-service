# 0011 linux.do 分组和规则写在 ini 里，代码只填充内层成员

状态：已接受。取代 ADR 0007 中“两个分组都由代码生成”、ADR 0010 中“由代码决定插入位置”，以及 ADR 0002 中“规则由代码生成并置顶”的实现方式。这些 ADR 的其余内容（分组结构、名称、分流域名的取舍）仍有效。

## 背景

讨论 ADR 0002 的规则位置时发现：用户的 `proxy_ai.list` 含 `DOMAIN-SUFFIX,cloudflare.com`，`challenges.cloudflare.com` 当前走 `🤖 国外AI`。规则顺序直接影响 AI 网站，由代码写死规则位置，用户就没法自己权衡。

用户提出：分组和规则由自己写在 ini（gist）里，服务只负责往分组里放节点。

现有代码已支持这种分工：

- `fill_backend_groups`（`app/dialer.py`）按名字找到 ini 中以 `REJECT` 占位的“后置分组”，转换后填入节点；
- ini 的 `ruleset=分组,[]DOMAIN-SUFFIX,xxx` 内联规则已被 `app/converter.py` 支持，规则顺序即 ini 中的书写顺序。

## 决策

- ini 定义两个分组和分流规则，名称、位置、外层成员、测速地址和参数、规则顺序都由 ini 决定；
- 代码在转换后（完整执行和重新应用规则都做）找到内层分组，用 ADR 0006 的条件计算成员并替换 `REJECT` 占位；没有达标节点时保留 `REJECT`（ADR 0009）；
- 内层分组的识别方式：名字包含 `LinuxDo` 且类型为 `url-test`；外层 select 不改动。

推荐的 ini 写法：

```ini
; 规则：放在所有 ruleset 的最前面，排在 AI 规则之前
ruleset=🐧 LinuxDo,[]DOMAIN-SUFFIX,linux.do
ruleset=🐧 LinuxDo,[]DOMAIN,challenges.cloudflare.com

; 分组：放在 🤖 国外AI 之后（代码会把 🎯 纯净优选 插在 🤖 国外AI 之后，所以最终顺序是 国外AI、纯净优选、LinuxDo）
custom_proxy_group=🐧 LinuxDo`select`[]🐧 LinuxDo 自动`[]🚀 手动选择`[]DIRECT
custom_proxy_group=🐧 LinuxDo 自动`url-test`REJECT`https://linux.do/cdn-cgi/trace`300,,200
```

## 备选方案

- **代码生成分组和规则（原 ADR 0007 / 0010 / 0002 的实现）**：用户不必改 ini，但规则顺序、名称、位置都写死在代码里，与 AI 规则的冲突只能由代码决定。放弃。

## 后果

- 正面：结构和规则顺序由用户控制，可以自己决定 `challenges.cloudflare.com` 是否跟 linux.do 走；代码改动只有“按名字填成员”一处，与 `fill_backend_groups` 的模式一致。
- 负面：ini 中改名后，如果新名字不含 `LinuxDo` 或类型不是 url-test，代码将找不到内层分组，成员保持 `REJECT`；需要在运行报告中记录“未找到 linux.do 分组”。
- 跟进：
  - 超时处理见 ADR 0012（ini 写 `300,,200`，使用 mihomo 默认 5000ms）；
  - `challenges.cloudflare.com` 那一行是否保留由用户在 ini 中决定（影响见 ADR 0002 的后果部分）。
