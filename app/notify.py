"""Enterprise WeChat notifications.

Rules (per spec):
- Master switch ``notify_enabled`` in config; off means no message
  is ever sent.
- When on: send on BOTH success and failure. Never send for a mere
  node-count/content change with no success/failure status.
- Failure messages name the step and the reason.
- Messages include filter settings, duration in minutes and the next schedule.
- The webhook URL is never logged.
"""
from __future__ import annotations

import logging

import httpx

from app.fetcher import make_client

log = logging.getLogger(__name__)


async def send_wechat(webhook: str, content: str) -> bool:
    if not webhook:
        return False
    try:
        async with make_client(timeout=30) as client:
            resp = await client.post(
                webhook,
                json={"msgtype": "text",
                      "text": {"content": content.rstrip()}})
        ok = resp.status_code == 200
        if not ok:
            log.warning("notify: wechat HTTP %s", resp.status_code)
        return ok
    except Exception as e:  # noqa: BLE001 - notification must not crash a run
        log.warning("notify: wechat send failed: %s", e)
        return False


def _minutes(seconds) -> str:
    return f"{float(seconds or 0) / 60:.2f}".rstrip("0").rstrip(".")


def _execution_text(report: dict) -> str:
    threshold = f"{float(report.get('purity_threshold', 15)):g}%"
    filtering = (f"开启（≤{threshold}）" if report.get("purity_filter_enabled")
                 else f"关闭（配置值：{threshold}）")
    return (
        f"纯净度过滤：{filtering}\n"
        f"执行间隔：{_minutes(report.get('interval_seconds'))} 分钟\n"
        f"执行时段：每天 {report.get('execution_start', '08:00')}–{report.get('execution_end', '23:59')}（北京时间）\n"
        f"下次执行时间：{report.get('next_run') or '待安排'}（北京时间）\n"
        f"本轮用时：{_minutes(report.get('duration_seconds'))} 分钟\n"
    )


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
        f"本轮连通性：通过 {report.get('connectivity_passed', 0)} 个，排除 {report.get('connectivity_excluded', 0)} 个\n"
        f"纯净度：已测 {report.get('purity_measured', 0)} 个，未测 {report.get('purity_unmeasured', 0)} 个\n"
        f"后置节点：{report.get('backend_total', 0)} 个\n"
        f"{_execution_text(report)}"
        f"最终地址：{report.get('subscription_url') or '未配置（请在服务环境中设置 CLASH_MERGE_PUBLIC_BASE）'}"
    )


def failure_text(step: str, reason: str, report: dict | None = None) -> str:
    return (
        "Clash 合并订阅：本次执行失败\n"
        f"失败步骤：{step}\n"
        f"原因：{reason}\n"
        f"{_execution_text(report) if report is not None else ''}"
        "已保留上一次的 jay_filtered.yaml，未用不完整结果覆盖。"
    )
