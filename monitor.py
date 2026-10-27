# -*- coding: utf-8 -*-
"""
基金定投监控脚本 v2
工作流：
1. 读取腾讯文档《何沛聪基金定投记录》的「持仓基金」表，获得持仓基金代码
2. 按基金名找到同名操作日志工作表，读取正式内容（空行以下为模板，忽略）：
   - 最新一行的「累计投资金额」「累计基金份额」
   - 从最新行向上找第一个「收益」行，其下一行起到最新行的「当期投资金额」求和 = 本轮本金
3. 抓取新浪财经实时盘中估值（fu_ 接口：估值、估值涨跌幅、时间戳）；
   取不到估值时不回退历史净值，直接标记失败并报告原因
4. 基金现值 = 估值 x 累计基金份额；收益率 = 现值 / 累计投资金额 - 1
   分类：>=10% 建议止盈；8%~10% 建议关注；<8% 继续定投
5. 生成简报，优先经 wecom_webhook 群机器人推送（markdown），
   失败时回退 wecom_chat_id 单聊 CLI（纯文本）
"""
import csv
import io
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

CONFIG_PATH = r"C:\Users\hp\WorkBuddy\基金定投监控\config.json"
TDOC_PY = (r"C:\Users\hp\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docs-plugin"
           r"\5.8.2-wb.41111488.gb8f6c953.haebd5991e0e2\skills\tencent-docs\tencentdocs.py")
PYTHON = sys.executable
HEADER_SCAN_ROWS = 10
MAX_LOG_ROWS = 400


def tdoc(service, tool, args):
    out = subprocess.run([PYTHON, TDOC_PY, "tdoc_call", service, tool, json.dumps(args)],
                         capture_output=True, text=True, timeout=90)
    stdout = (out.stdout or "").strip()
    if not stdout.startswith("{"):
        # 非 JSON 输出（如 ERROR:no_token ...），直接透出原始错误
        raise RuntimeError("%s.%s 调用失败: %s" % (service, tool, stdout[:300] or out.stderr[:300]))
    resp = json.loads(stdout)
    if resp.get("error"):
        raise RuntimeError("%s.%s 调用失败: %s" % (service, tool, resp["error"]))
    return resp["result"]["structuredContent"]


def num(s):
    s = (s or "").replace(",", "").replace("%", "").strip()
    return float(s) if s else 0.0


def load_holdings(file_id):
    """读持仓基金表 -> funds=[(name, code)], sheets={name: (sheet_id, row_count)}"""
    info = tdoc("sheet-mcp", "get_sheet_info", {"file_id": file_id})
    sheets = {s["sheet_name"].strip(): (s["sheet_id"], s["row_count"]) for s in info["sheets"]}
    if "持仓基金" not in sheets:
        raise RuntimeError("未找到「持仓基金」工作表")
    hold_id = sheets["持仓基金"][0]
    data = tdoc("sheet-mcp", "get_cell_data",
                {"file_id": file_id, "sheet_id": hold_id,
                 "start_row": 0, "end_row": 50, "start_col": 0, "end_col": 1, "return_csv": True})
    funds = []
    for r in list(csv.reader(io.StringIO(data["csv_data"])))[1:]:
        if len(r) >= 2 and r[1].strip():
            funds.append((r[0].strip(), r[1].strip()))
    return funds, sheets


def load_log(file_id, sheet_id, row_count):
    """读一只基金的操作日志 -> 最新累计值与本轮本金"""
    data = tdoc("sheet-mcp", "get_cell_data",
                {"file_id": file_id, "sheet_id": sheet_id,
                 "start_row": 0, "end_row": min(row_count - 1, MAX_LOG_ROWS),
                 "start_col": 0, "end_col": 7, "return_csv": True})
    rows = list(csv.reader(io.StringIO(data["csv_data"])))
    hdr = next(i for i, r in enumerate(rows[:HEADER_SCAN_ROWS]) if any(c.strip() == "摘要" for c in r))
    official = []
    for r in rows[hdr + 1:]:
        if not any(c.strip() for c in r):
            break
        official.append(r)
    if not official:
        raise RuntimeError("日志表无正式数据")
    latest = official[-1]
    start = 0
    for i in range(len(official) - 1, -1, -1):
        if len(official[i]) > 3 and official[i][3].strip() == "收益":
            start = i + 1
            break
    return {"cum_amount": num(latest[5]), "cum_shares": num(latest[7]),
            "principal": sum(num(r[4]) for r in official[start:] if len(r) > 4),
            "date": "-".join(x.strip() for x in latest[0:3] if x.strip())}


def fetch_price(code):
    """新浪财经 fu_ 接口：实时盘中估值 + 估值涨跌幅（GBK 文本）。
    格式：名称,HH:MM:SS,估值,昨净值,累计净值,0,估值涨跌幅%,日期,...
    取不到估值（空数据/非交易时段清空）时抛错，不回退官方净值。"""
    url = "https://hq.sinajs.cn/list=fu_%s" % code
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode("gbk", errors="ignore")
    m = re.search(r'="([^"]*)"', text)
    payload = m.group(1) if m else ""
    parts = payload.split(",")
    if len(parts) < 8 or not parts[2].strip():
        raise RuntimeError("新浪估值接口无盘中估值数据（可能非交易时段或接口异常）")
    return {"price": float(parts[2]), "chg": float(parts[6]),
            "time": "%s %s" % (parts[7], parts[1]), "kind": "盘中估值"}


def classify(pct, strategy):
    if pct >= strategy.get("take_profit_pct", 10):
        return "建议止盈"
    if pct >= strategy.get("watch_pct", 8):
        return "建议关注"
    return "继续定投"


def build_text_report(results, price_kind, strategy):
    """生成企业微信文本简报（wecom-cli 仅支持 text，上限 2048 字符）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["【定投监控简报】%s ｜ %s" % (now, price_kind)]
    for r in results:
        arrow = "▲" if r["chg"] >= 0 else "▼"
        pct_sign = "+" if r["pct"] >= 0 else ""
        lines.append("")
        lines.append("◆ %s（%s）" % (r["name"], r["code"]))
        lines.append("现价 %.4f（%s%.2f%%）" % (r["price"], arrow, abs(r["chg"])))
        lines.append("现值 %.2f 元 ｜ 收益率 %s%.2f%%" % (r["value"], pct_sign, r["pct"]))
        if r["category"] == "建议止盈":
            lines.append("🔔 建议止盈：本轮止盈卖出金额 %.2f 元" % r["principal"])
        elif r["category"] == "建议关注":
            lines.append("⚠ 建议关注：收益率已超 %.0f%%，接近止盈线" % strategy.get("watch_pct", 8))
    return "\n".join(lines)


def build_report(results, price_kind, strategy):
    """生成企业微信 markdown 简报（webhook 群机器人渠道用）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["## 定投监控简报", "> %s ｜ %s" % (now, price_kind)]
    for r in results:
        sign = "+" if r["chg"] >= 0 else ""
        pct_sign = "+" if r["pct"] >= 0 else ""
        pct_color = "warning" if r["pct"] >= 0 else "info"
        lines.append("")
        lines.append("**%s（%s）**" % (r["name"], r["code"]))
        lines.append("现价 **%.4f**（%s%.2f%%）" % (r["price"], sign, r["chg"]))
        lines.append("现值 **%.2f** 元 ｜ 收益率 **<font color=\"%s\">%s%.2f%%</font>**" % (
            r["value"], pct_color, pct_sign, r["pct"]))
        if r["category"] == "建议止盈":
            lines.append("<font color=\"warning\">**建议止盈**</font>　本轮止盈卖出金额 **%.2f** 元" % r["principal"])
        elif r["category"] == "建议关注":
            lines.append("<font color=\"comment\">建议关注：收益率已超 %.0f%%，接近止盈线</font>"
                         % strategy.get("watch_pct", 8))
    return "\n".join(lines)


WECOM_CLI = r"C:\Users\hp\.workbuddy\binaries\node\cli-connector-packages\wecom-cli.cmd"


def push_wecom_cli(chat_id, content):
    """通过企业微信连接器 CLI 发送文本消息（单聊传 userid，群聊传群会话 ID）。"""
    body = json.dumps({"chat_id": chat_id, "msg_type": "text",
                       "text": {"content": content}}, ensure_ascii=False)
    out = subprocess.run([WECOM_CLI, "message", "send", "--json", body],
                         capture_output=True, text=True, timeout=60)
    return (out.stdout + out.stderr).strip()


def push_wecom(webhook, content):
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    file_id = cfg["tencent_doc"]["file_id"]
    strategy = cfg.get("strategy", {})
    webhook = cfg.get("wecom_webhook", "").strip()

    funds, sheets = load_holdings(file_id)
    if not funds:
        print("「持仓基金」表为空。")
        return 0

    results = []
    kinds = set()
    for name, code in funds:
        if name not in sheets:
            print("- **%s（%s）**：未找到同名日志工作表，跳过" % (name, code))
            continue
        try:
            log = load_log(file_id, sheets[name][0], sheets[name][1])
            price = fetch_price(code)
        except Exception as e:
            print("- **%s（%s）**：处理失败（%s）" % (name, code, e))
            continue
        value = price["price"] * log["cum_shares"]
        pct = (value / log["cum_amount"] - 1) * 100 if log["cum_amount"] else 0
        kinds.add(price["kind"])
        results.append({"name": name, "code": code, "price": price["price"],
                        "chg": price["chg"], "value": value, "pct": pct,
                        "principal": log["principal"],
                        "category": classify(pct, strategy)})
        print("[核对] %s(%s) 日志最新行 %s：累计投资金额 %.2f，累计份额 %.2f，本轮本金 %.2f；%s %.4f（%+.2f%%，%s）-> 现值 %.2f，收益率 %+.2f%%，%s"
              % (name, code, log["date"], log["cum_amount"], log["cum_shares"], log["principal"],
                 price["kind"], price["price"], price["chg"], price["time"],
                 value, pct, results[-1]["category"]))
    if not results:
        return 1
    price_kind = " / ".join(sorted(kinds))
    report = build_report(results, price_kind, strategy)
    print("\n" + report)
    chat_id = cfg.get("wecom_chat_id", "").strip()
    pushed = False
    if webhook:
        try:
            print("\n[企业微信推送结果-Webhook] " + push_wecom(webhook, report))
            pushed = True
        except Exception as e:
            print("\n[Webhook 推送失败，尝试 CLI 兜底] %s" % e)
    if not pushed and chat_id:
        text_report = build_text_report(results, price_kind, strategy)
        print("\n[企业微信推送结果-CLI] " + push_wecom_cli(chat_id, text_report))
        pushed = True
    if not pushed:
        print("\n[提示] 无可用推送渠道（wecom_webhook / wecom_chat_id 均不可用），简报未推送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
