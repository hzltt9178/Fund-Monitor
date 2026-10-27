# fund-monitor（基金定投监控）

每个交易日读取腾讯文档里的持仓与操作日志，抓新浪财经盘中估值，计算收益率并按阈值分类，
推送到企业微信群机器人。

> 本仓库**只放代码**。文档 ID、Webhook 密钥、持仓与历史数据一律不入库。

当前版本 **v4.1.0**（`monitor.py`，24,897 字节，`md5 45b70eb9…`）。每次运行首行会打印版本与文件 MD5，
可用来确认云端跑的是不是最新版（`raw.githubusercontent.com` 有 CDN 缓存）。

> 本文件与代码中**不包含任何基金代码、文档 ID、Webhook 密钥**。持仓从腾讯文档运行时读取，
> 因此换基金只需改腾讯文档的「持仓基金」表，不必改动本仓库。

## 运行

```bash
export TDOC_FILE_ID=<腾讯文档 file_id>
export WECOM_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx'

python3 monitor.py --check     # 自检：验证取数通道，不推送
python3 monitor.py --dry-run   # 出简报但不推送
python3 monitor.py             # 正式运行并推送
```

只依赖 Python 标准库（3.9+，用到 `zoneinfo`；缺 tzdata 时自动回退固定 +08:00），无需 pip 安装。

## 取数后端

腾讯文档有两种取数方式，`--backend` / `TDOC_BACKEND` 选择，默认 `auto`：

| 后端 | 旧写法 | 说明 | 用在 |
|---|---|---|---|
| `gateway` | `mcp` | 走 `tdocs-app.agent-gateway.auth-proxy.local` MCP 网关，凭据由网关注入，无 token | 云端沙箱 |
| `plugin` | `local` | 子进程调用本机 `tencentdocs.py` 插件 | 上云前本机先验一遍 |
| `auto` | — | 用一次真实 HTTP 调用探测网关，连不通 / 5xx 才回退 plugin | 默认 |

`mcp` / `local` 两种旧写法仍可用，不必改脚本。

网关工具名可能带不同前缀（`sheet.` / 无前缀 / `sheet-mcp.` / `mcp__sheet-mcp__`），
脚本会自动探测并记住可用写法。

## 配置

优先级：**环境变量 > config.json**。config.json 已在 `.gitignore` 中，仅本机调试用。

| 环境变量 | 必填 | 含义 |
|---|---|---|
| `TDOC_FILE_ID` | 是 | 腾讯文档 file_id（代码内不设默认值） |
| `WECOM_WEBHOOK` | 是 | 企微群机器人 Webhook 完整地址（含 key） |
| `TDOC_MCP_URL` | 否 | 网关地址，已内置默认值；域名变了改这里，不用改代码 |
| `TDOC_BACKEND` | 否 | 同 `--backend` |
| `TAKE_PROFIT_PCT` / `WATCH_PCT` | 否 | 阈值覆盖（默认 10 / 8），改阈值不必重传代码 |
| `TDOC_PY` | 否 | 本机 `tencentdocs.py` 路径，默认自动扫描最高版本 |
| `TDOC_PYTHON` | 否 | 调插件用的解释器，默认当前解释器 |
| `TDOC_GATEWAY_AUTH` | 否 | `user:pass`，网关需要 Basic 认证时才用 |
| `WECOM_CHAT_ID` / `WECOM_CLI` | 否 | 可选兜底通道，默认不用 |

## 判定规则

- 现值 = 盘中估值 × 累计基金份额
- 收益率 = 现值 ÷ 累计投资金额 − 1
- ≥10% 建议止盈（附本轮止盈卖出金额＝最近一轮「当期投资金额」之和）
- ≥8% 建议关注；其余继续定投
- 取不到盘中估值时**直接报错**，不回退历史净值，不生成假数据

## 失败时的行为

- 单只基金失败不中断其余，简报末尾标注失败项
- 全部失败：以非 0 退出，不推送空简报
- 企微 Webhook 返回 HTTP 200 但 `errcode≠0` 时判为失败（不误报成功）

## 更新代码

直接改仓库文件即可，云端任务每次执行都会重新拉取。
每次运行会打印版本号与文件 MD5，可用来确认跑的是不是最新版
（`raw.githubusercontent.com` 有 CDN 缓存，改完立刻拉可能仍是旧版）。
