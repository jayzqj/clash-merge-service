"""Enterprise WeChat notifications.

Rules (per spec):
- Master switch ``notify_enabled`` in config; off means no message
  is ever sent.
- When on: send on BOTH success and failure. Never send for a mere
  node-count/content change with no success/failure status.
- Failure messages name the step and the reason.
- Every message ends with exactly:
  ``发送方：Muse（账号：azqjzm@gmail.com）``
- The webhook URL is never logged.
"""
from __future__ import annotations

import logging

import httpx

from app.fetcher import make_client

log = logging.getLogger(__name__)

FOOTER = "发送方：Muse（账号：azqjzm@gmail.com）"


def _with_footer(content: str) -> str:
    content = content.rstrip()
    if not content.endswith(FOOTER):
        content += f"\n{FOOTER}"
    return content


async def send_wechat(webhook: str, content: str) -> bool:
    if not webhook:
        return False
    try:
        async with make_client(timeout=30) as client:
            resp = await client.post(
                webhook,
                json={"msgtype": "text",
                      "text": {"content": _with_footer(content)}})
        ok = resp.status_code == 200
        if not ok:
            log.warning("notify: wechat HTTP %s", resp.status_code)
        return ok
    except Exception as e:  # noqa: BLE001 - notification must not crash a run
        log.warning("notify: wechat send failed: %s", e)
        return False


def success_text(report: dict) -> str:
    ok = int(report.get('sources_ok', 0) or 0)
    total = int(report.get('sources_total', 0) or 0)
    partial = ""
    if total and ok < total:
        # Counts only — the failed addresses are on the admin page,
        # never in a WeChat message.
        partial = (f"注意：有 {total - ok} 个订阅源失败，已跳过，"
                   "失败地址见管理页\n")
    return (
        "Clash 合并订阅：本次执行成功\n"
        f"订阅源：{ok}/{total} 个成功\n"
        f"{partial}"
        f"节点：合并 {report.get('total_in', 0)} 个，去重 {report.get('dedup_removed', 0)} 个，"
        f"最终 {report.get('proxies_out', 0)} 个\n"
        f"纯净度：已测 {report.get('purity_measured', 0)} 个，未测 {report.get('purity_unmeasured', 0)} 个\n"
        f"后置节点：{report.get('backend_total', 0)} 个，"
        f"其中 TCP 已注入 dialer-proxy {report.get('backend_tcp_injected', 0)} 个，"
        f"UDP 未注入 {report.get('backend_udp_no_dialer', 0)} 个\n"
        f"用时：{report.get('duration_seconds', 0)} 秒\n"
        "最终地址：https://xiuyuan.9527653.xyz/jay_filtered.yaml"
    )


def failure_text(step: str, reason: str) -> str:
    return (
        "Clash 合并订阅：本次执行失败\n"
        f"失败步骤：{step}\n"
        f"原因：{reason}\n"
        "已保留上一次的 jay_filtered.yaml，未用不完整结果覆盖。"
    )
