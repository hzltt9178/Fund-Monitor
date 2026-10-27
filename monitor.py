# -*- coding: utf-8 -*-
"""
基金定投监控脚本 v4.1（云端沙箱 / 本机 双后端版）

设计要点：
  - 取数双后端，--backend / TDOC_BACKEND 可选 auto/gateway/plugin，并兼容旧写法 mcp/local：
      gateway（= mcp）   ：沙箱内 tdocs-app MCP 网关（HTTP JSON-RPC，凭据由网关注入，无需 token）
      plugin （= local） ：本机 tencentdocs.py 插件（子进程调用，用于推送上云前先在本地验证）
      auto             ：先用一次真实 HTTP 调用探测网关，不可达/5xx 才回退插件
  - 网关工具名前缀自动探测（sheet. / 无前缀 / sheet-mcp. / mcp__sheet-mcp__）
  - 时区固定 Asia/Shanghai；容器缺 tzdata 时回退固定 +08:00，不会崩
  - 配置优先级 环境变量 > config.json；仓库内不含任何密钥与文档 ID
  - 企微 Webhook 校验 errcode，避免「HTTP 200 但实际失败」被当成成功
  - 打印版本号与文件 MD5，用于确认云端拉到的是最新版（对抗 raw CDN 缓存）
  - 任一基金失败不中断其余；全部失败或推送失败以非 0 退出，绝不生成假数据

工作流：
1. 读腾讯文档「持仓基金」表拿基金代码
2. 按基金名找同名操作日志表，取最新行「累计投资金额」「累计基金份额」；
   正式内容到第一个全空行止（其下为模板示例，忽略）
3. 新浪财经 fu_ 接口取实时盘中估值与估值涨跌幅（唯一估值源，取不到即报错，不回退历史净值）
4. 收益率 = 估值 x 累计份额 / 累计投资金额 - 1
   >=10% 建议止盈 / >=8% 建议关注 / 其余继续定投
5. 建议止盈时：从最新行向上找第一个「收益」行，其下一行至最新行的「当期投资金额」求和 = 本轮止盈卖出金额
6. 简报经企微群机器人 Webhook 推送（markdown），失败回退 CLI 单聊（若配置了可用 CLI）
"""
import argparse
import csv
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "4.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HEADER_SCAN_ROWS = 10
MAX_LOG_ROWS = 400
DEFAULT_MCP_URL = "http://tdocs-app.agent-gateway.auth-proxy.local/openapi/mcp"

# 后端名兼容两套写法：本文件用 gateway/plugin（指取数实现），旧文档用 mcp/local（指通道）
BACKEND_ALIASES = {"auto": "auto", "gateway": "gateway", "mcp": "gateway",
                   "plugin": "plugin", "local": "plugin"}
PLUGIN_PYTHON = sys.executable      # 可用 TDOC_PYTHON 或 config.tdoc_python 覆盖

# 网关工具名可能带不同前缀，首次调用时探测并记住可用写法
TOOL_STYLES = ["sheet.", "", "sheet-mcp.", "mcp__sheet-mcp__"]
_tool_style = None

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:                      # 容器缺 tzdata，退回固定 +08:00
    TZ = timezone(timedelta(hours=8))

PLUGIN_GLOBS = [
    os.path.join(os.path.expanduser("~"), ".workbuddy", "plugins", "cache",
                 "workbuddy-builtin", "tencent-docs-plugin", "*",
                 "skills", "tencent-docs", "tencentdocs.py"),
    "/opt/workbuddy/plugins/cache/workbuddy-builtin/tencent-docs-plugin/*/skills/tencent-docs/tencentdocs.py",
]


class TDocError(RuntimeError):
    pass


def now_cn():
    return datetime.now(TZ)


def self_sig():
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except OSError:
        return "unknown"


# ---------------------------------------------------------------- 网关后端
def _mcp_post(url, tool, args, timeout=60, retries=1):
    """发一次 MCP tools/call，返回原始响应文本。
    若设置了 TDOC_GATEWAY_AUTH=user:pass，则附带 Basic 认证（多数场景不需要）。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    auth = os.environ.get("TDOC_GATEWAY_AUTH", "").strip()
    if ":" in auth:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
            raise TDocError("网关 HTTP %s：%s" % (e.code, detail))
        except Exception as e:         # 连接/超时类，重试后放弃
            last = e
    raise TDocError("网关不可达（%s）：%s" % (url, last))


def _parse_mcp(raw, tool):
    payload = raw
    if raw.lstrip().startswith("event:") or "\ndata: " in raw:
        for line in raw.splitlines():
            if line.startswith("data: "):
                payload = line[6:]
    try:
        rpc = json.loads(payload)
    except Exception:
        raise TDocError("%s 返回非 JSON：%s" % (tool, raw[:300]))
    if rpc.get("error"):
        raise TDocError("%s 调用失败：%s" % (tool, json.dumps(rpc["error"], ensure_ascii=False)[:300]))
    content = rpc.get("result", {}).get("content") or []
    if not content:
        raise TDocError("%s 无返回内容" % tool)
    text = content[0].get("text", "")
    try:
        data = json.loads(text)
    except Exception:
        raise TDocError("%s 返回非预期内容：%s" % (tool, text[:300]))
    if isinstance(data, dict) and data.get("error"):
        raise TDocError("%s 调用失败：%s" % (tool, data["error"]))
    return data


def gateway_call(url, base_tool, args):
    """带前缀探测的 MCP 调用：第一次成功后记住写法，之后直接复用。"""
    global _tool_style
    styles = TOOL_STYLES if _tool_style is None else [_tool_style]
    errors = []
    for style in styles:
        name = style + base_tool
        try:
            data = _parse_mcp(_mcp_post(url, name, args), name)
            if _tool_style is None:
                _tool_style = style
            return data
        except TDocError as e:
            msg = str(e)
            if "不可达" in msg or "HTTP 4" in msg or "HTTP 5" in msg:
                raise          # 通道级失败，换工具名也没用
            errors.append("%s -> %s" % (name, msg[:160]))
    raise TDocError("所有工具名写法均失败：%s" % "；".join(errors))


# ---------------------------------------------------------------- 插件后端
def find_tdoc_script():
    override = os.environ.get("TDOC_PY", "").strip()
    if override and os.path.exists(override):
        return override
    hits = []
    for pattern in PLUGIN_GLOBS:
        hits.extend(glob.glob(pattern))
    hits = [h for h in hits if os.path.exists(h)]

    def ver(p):
        m = re.search(r"tencent-docs-plugin[/\\]([^/\\]+)[/\\]", p)
        if m:
            parts = re.findall(r"\d+", m.group(1))
            if parts:
                return tuple(int(x) for x in parts[:4])
        return (0, 0, 0, 0)

    hits.sort(key=ver, reverse=True)
    return hits[0] if hits else None


def plugin_call(service, tool, args):
    script = find_tdoc_script()
    if not script:
        raise TDocError("找不到 tencentdocs.py（可用 TDOC_PY 指定路径）")
    out = subprocess.run([PLUGIN_PYTHON, script, "tdoc_call", service, tool, json.dumps(args)],
                         capture_output=True, text=True, timeout=120)
    stdout = (out.stdout or "").strip()
    if not stdout.startswith("{"):
        raise TDocError("%s.%s 调用失败：%s" % (service, tool, stdout[:300] or (out.stderr or "")[:300]))
    resp = json.loads(stdout)
    if resp.get("error"):
        raise TDocError("%s.%s 调用失败：%s" % (service, tool, resp["error"]))
    return resp["result"]["structuredContent"]


# ---------------------------------------------------------------- 统一取数
class Doc:
    def __init__(self, backend, mcp_url):
        self.backend = backend
        self.mcp_url = mcp_url

    def call(self, base_tool, args):
        if self.backend == "gateway":
            return gateway_call(self.mcp_url, base_tool, args)
        return plugin_call("sheet-mcp", base_tool, args)

    def describe(self):
        if self.backend == "gateway":
            return "网关 %s" % self.mcp_url
        return "插件 %s" % find_tdoc_script()


def norm_backend(v):
    """把 mcp/local 等旧写法统一成 gateway/plugin；无法识别时按 auto。"""
    return BACKEND_ALIASES.get((v or "").strip().lower(), "auto")


def resolve_backend(pref, mcp_url):
    """auto：先探测网关（短超时、不重试），不可达则回退插件。"""
    pref = norm_backend(pref)
    if pref in ("gateway", "plugin"):
        return pref
    try:
        _mcp_post(mcp_url, "sheet.get_sheet_info", {"file_id": "__probe__"},
                  timeout=8, retries=0)
        return "gateway"
    except TDocError as e:
        msg = str(e)
        # 连接失败或网关 5xx（服务未路由/上游挂了）都视为「网关不可用」
        if "不可达" in msg or "HTTP 5" in msg:
            if find_tdoc_script():
                print("[提示] 网关不可用（%s），本次回退本机插件后端" % msg[:90])
                return "plugin"
            if "不可达" in msg:
                raise TDocError("网关不可达，本机也没有 tencentdocs.py 插件：%s" % msg)
        return "gateway"              # 网关能应答（如 4xx 权限/工具名问题），就用网关排错
    except Exception:
        if find_tdoc_script():
            return "plugin"
        return "gateway"


def num(s):
    s = (s or "").replace(",", "").replace("%", "").strip()
    return float(s) if s else 0.0


def load_holdings(doc, file_id):
    """读持仓基金表 -> funds=[(name, code)], sheets={name: (sheet_id, row_count)}"""
    info = doc.call("get_sheet_info", {"file_id": file_id})
    sheets = {s["sheet_name"].strip(): (s["sheet_id"], s["row_count"]) for s in info["sheets"]}
    if "持仓基金" not in sheets:
        raise TDocError("未找到「持仓基金」工作表")
    hold_id = sheets["持仓基金"][0]
    data = doc.call("get_cell_data",
                    {"file_id": file_id, "sheet_id": hold_id,
                     "start_row": 0, "end_row": 50, "start_col": 0, "end_col": 1,
                     "return_csv": True})
    funds = []
    for r in list(csv.reader(io.StringIO(data["csv_data"])))[1:]:
        if len(r) >= 2 and r[1].strip():
            funds.append((r[0].strip(), r[1].strip()))
    return funds, sheets


def load_log(doc, file_id, sheet_id, row_count):
    """读一只基金的操作日志 -> 最新累计值与本轮本金"""
    data = doc.call("get_cell_data",
                    {"file_id": file_id, "sheet_id": sheet_id,
                     "start_row": 0, "end_row": min(row_count - 1, MAX_LOG_ROWS),
                     "start_col": 0, "end_col": 7, "return_csv": True})
    rows = list(csv.reader(io.StringIO(data["csv_data"])))
    try:
        hdr = next(i for i, r in enumerate(rows[:HEADER_SCAN_ROWS]) if any(c.strip() == "摘要" for c in r))
    except StopIteration:
        raise TDocError("日志表前 %d 行未找到「摘要」表头" % HEADER_SCAN_ROWS)
    official = []
    for r in rows[hdr + 1:]:
        if not any(c.strip() for c in r):
            break
        official.append(r)
    if not official:
        raise TDocError("日志表无正式数据")
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
    取不到估值时抛错，不回退官方净值。"""
    url = "https://hq.sinajs.cn/list=fu_%s" % code
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("gbk", errors="ignore")
    except Exception as e:
        raise RuntimeError("新浪估值接口请求失败：%s" % e)
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
    """生成企业微信文本简报（CLI 兜底用，上限 2048 字符）。"""
    lines = ["【定投监控简报】%s ｜ %s" % (now_cn().strftime("%Y-%m-%d %H:%M"), price_kind)]
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
    lines = ["## 定投监控简报", "> %s ｜ %s" % (now_cn().strftime("%Y-%m-%d %H:%M"), price_kind)]
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


def push_wecom(webhook, content):
    """企微 webhook 失败时仍返回 HTTP 200，必须校验 errcode。"""
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    try:
        code = json.loads(body).get("errcode", 0)
    except Exception:
        code = 0
    if code != 0:
        raise RuntimeError("企微返回 errcode=%s：%s" % (code, body[:200]))
    return body


def push_wecom_cli(cli, chat_id, content):
    """可选兜底：沙箱内 wecom-cli 通常返回 403，调用失败不视为致命。"""
    body = json.dumps({"chat_id": chat_id, "msg_type": "text",
                       "text": {"content": content}}, ensure_ascii=False)
    out = subprocess.run([cli, "message", "send", "--json", body],
                         capture_output=True, text=True, timeout=60)
    return (out.stdout + out.stderr).strip()


def load_cfg(path):
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print("[警告] config.json 解析失败，忽略：%s" % e)
    doc = cfg.get("tencent_doc", {})

    # 阈值允许用环境变量覆盖：改阈值不必重新上传代码（规避 raw CDN 缓存）
    strategy = dict(cfg.get("strategy") or {})
    for env_key, field in (("TAKE_PROFIT_PCT", "take_profit_pct"), ("WATCH_PCT", "watch_pct")):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            try:
                strategy[field] = float(raw)
            except ValueError:
                print("[警告] %s=%s 不是数字，忽略" % (env_key, raw))

    return {
        "file_id": (os.environ.get("TDOC_FILE_ID") or doc.get("file_id") or "").strip(),
        "mcp_url": (os.environ.get("TDOC_MCP_URL") or doc.get("mcp_url") or DEFAULT_MCP_URL).strip(),
        "strategy": strategy,
        "webhook": (os.environ.get("WECOM_WEBHOOK") or cfg.get("wecom_webhook") or "").strip(),
        "chat_id": (os.environ.get("WECOM_CHAT_ID") or cfg.get("wecom_chat_id") or "").strip(),
        "cli": (os.environ.get("WECOM_CLI") or cfg.get("wecom_cli") or "").strip(),
        "python_exe": (os.environ.get("TDOC_PYTHON") or cfg.get("tdoc_python") or sys.executable).strip(),
    }


def run_check(cfg, backend_pref):
    """自检：逐项打印，单项失败不影响其余，最后给结论。"""
    print("[1/5] 版本 v%s  md5=%s  Python %s"
          % (VERSION, self_sig(), sys.version.split()[0]))
    bad = 0
    try:
        backend = resolve_backend(backend_pref, cfg["mcp_url"])
        doc = Doc(backend, cfg["mcp_url"])
        print("[2/5] 取数后端：%s -> %s" % (backend, doc.describe()))
    except Exception as e:
        print("[2/5] 取数后端：FAIL %s" % e)
        return 2
    print("[3/5] 配置：file_id=%s  webhook=%s"
          % ("已设置" if cfg["file_id"] else "缺失",
             "已设置" if cfg["webhook"] else "缺失"))
    if not cfg["file_id"]:
        bad += 1
    if not cfg["webhook"]:
        bad += 1
    # [4/5] 先读腾讯文档：既验证取数通道，也拿到真实持仓（用于下一步当探针）
    funds = []
    if cfg["file_id"]:
        try:
            funds, sheets = load_holdings(doc, cfg["file_id"])
            print("[4/5] 腾讯文档 OK：持仓 %d 只 %s，共 %d 个工作表"
                  % (len(funds), [f[1] for f in funds], len(sheets)))
        except Exception as e:
            print("[4/5] 腾讯文档 FAIL：%s" % e)
            bad += 1
    else:
        print("[4/5] 腾讯文档 跳过（无 file_id）")

    # [5/5] 探针不写死：用持仓表里第一只基金的代码去试新浪。
    # 代价：文档读不到时拿不到探针，本项跳过（此时结论已是"跑不了"，先修文档）。
    probe = funds[0][1] if funds else ""
    if probe:
        try:
            p = fetch_price(probe)
            print("[5/5] 新浪行情 OK：探针 %s 估值 %.4f（%+.2f%%，%s）"
                  % (probe, p["price"], p["chg"], p["time"]))
        except Exception as e:
            print("[5/5] 新浪行情 FAIL（探针 %s）：%s" % (probe, e))
            bad += 1
    else:
        print("[5/5] 新浪行情 跳过（持仓表为空或读取失败，无可用于测试的基金代码）")
    print("自检结论：%s" % ("全部通过" if bad == 0 else "存在 %d 项失败，见上" % bad))
    return 0 if bad == 0 else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", "--dry-run", dest="no_push", action="store_true",
                    help="只出简报，不推送")
    ap.add_argument("--check", action="store_true", help="自检：验证取数通道，不推送")
    ap.add_argument("--backend", default=os.environ.get("TDOC_BACKEND", "auto"),
                    help="auto|gateway|plugin，兼容旧写法 mcp/local；默认读 TDOC_BACKEND")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args()

    print("[版本] v%s md5=%s" % (VERSION, self_sig()))
    raw_backend = (args.backend or "auto").strip().lower()
    if raw_backend not in BACKEND_ALIASES:
        print("[警告] 未识别的 backend 值「%s」，按 auto 处理（可选 auto/gateway/plugin，兼容 mcp/local）"
              % args.backend)
    args.backend = norm_backend(raw_backend)
    cfg = load_cfg(args.config)

    global PLUGIN_PYTHON
    PLUGIN_PYTHON = cfg["python_exe"]

    if args.check:
        return run_check(cfg, args.backend)

    if not cfg["file_id"]:
        print("失败：缺少 TDOC_FILE_ID")
        return 2
    try:
        backend = resolve_backend(args.backend, cfg["mcp_url"])
        doc = Doc(backend, cfg["mcp_url"])
        print("[后端] %s -> %s" % (backend, doc.describe()))
        funds, sheets = load_holdings(doc, cfg["file_id"])
    except Exception as e:
        print("失败：读取持仓表出错 -> %s" % e)
        return 2
    if not funds:
        print("「持仓基金」表为空。")
        return 0

    results, kinds, failed = [], set(), []
    for name, code in funds:
        if name not in sheets:
            print("- %s（%s）：未找到同名日志工作表，跳过" % (name, code))
            failed.append("%s（%s）：未找到同名日志表" % (name, code))
            continue
        try:
            log = load_log(doc, cfg["file_id"], sheets[name][0], sheets[name][1])
            price = fetch_price(code)
        except Exception as e:
            print("- %s（%s）：处理失败（%s）" % (name, code, e))
            failed.append("%s（%s）：%s" % (name, code, e))
            continue
        value = price["price"] * log["cum_shares"]
        pct = (value / log["cum_amount"] - 1) * 100 if log["cum_amount"] else 0
        kinds.add(price["kind"])
        results.append({"name": name, "code": code, "price": price["price"],
                        "chg": price["chg"], "value": value, "pct": pct,
                        "principal": log["principal"],
                        "category": classify(pct, cfg["strategy"])})
        print("[核对] %s(%s) 最新行 %s：累计投资 %.2f，份额 %.2f，本轮本金 %.2f；%s %.4f（%+.2f%%，%s）-> 现值 %.2f，收益率 %+.2f%%，%s"
              % (name, code, log["date"], log["cum_amount"], log["cum_shares"], log["principal"],
                 price["kind"], price["price"], price["chg"], price["time"],
                 value, pct, results[-1]["category"]))

    if not results:
        print("\n[失败] 全部基金处理失败，未生成简报。")
        for f in failed:
            print("  - " + f)
        return 1

    price_kind = " / ".join(sorted(kinds))
    report = build_report(results, price_kind, cfg["strategy"])
    if failed:
        report += "\n\n> 另有 %d 只取数失败：%s" % (
            len(failed), "；".join(x.split("：")[0] for x in failed))
    print("\n" + report)

    if args.no_push:
        print("\n[提示] --no-push：已跳过推送。")
        return 0

    pushed = False
    if cfg["webhook"]:
        try:
            print("\n[推送结果-Webhook] " + push_wecom(cfg["webhook"], report))
            pushed = True
        except Exception as e:
            print("\n[Webhook 推送失败，尝试 CLI 兜底] %s" % e)
    if not pushed and cfg["chat_id"] and cfg["cli"]:
        try:
            text_report = build_text_report(results, price_kind, cfg["strategy"])
            print("\n[推送结果-CLI] " + push_wecom_cli(cfg["cli"], cfg["chat_id"], text_report))
            pushed = True
        except Exception as e:
            print("\n[CLI 兜底失败] %s" % e)
    if not pushed:
        print("\n[失败] 无可用推送渠道，简报未推送。")
        return 1
    if failed:
        print("\n[部分失败] " + "；".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
