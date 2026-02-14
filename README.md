# Paradex × Variational 跨所永续合约价差套利机器人

> **双边零手续费**套利策略 — Paradex (Interactive Token taker 0费) + Variational (taker 0费)

```
利润 = 纯价差收益，无需扣除任何手续费
```

---

## 目录

- [项目概述](#项目概述)
- [策略原理](#策略原理)
- [系统架构](#系统架构)
- [环境准备](#环境准备)
- [安装部署](#安装部署)
- [获取认证信息](#获取认证信息)
- [配置说明](#配置说明)
- [使用方法](#使用方法)
- [策略参数调优](#策略参数调优)
- [项目结构](#项目结构)
- [API 端点文档](#api-端点文档)
- [Cloudflare 绕过原理](#cloudflare-绕过原理)
- [风险控制](#风险控制)
- [日志与监控](#日志与监控)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 项目概述

本项目实现 **[Paradex](https://paradex.trade)** 与 **[Variational (Omni)](https://omni.variational.io)** 之间的永续合约跨所价差套利。

### 为什么选这两个交易所？

| 特性 | Paradex | Variational (Omni) |
|------|---------|-------------------|
| 手续费 | Interactive Token: **0 maker + 0 taker** | **0 手续费** (所有订单) |
| 流动性 | 订单簿深度好 | RFQ 模式，做市商报价 |
| API | 官方 SDK (`paradex-py`) | 无公开 API，需逆向 |
| 认证 | StarkNet 签名 + JWT | Cookie (vr-token JWT) |
| 永续合约 | BTC/ETH/SOL 等 | 473+ 市场 |

**核心优势：两边都是零手续费，利润 = 纯价差。**

---

## 策略原理

### 价差计算

```
做多价差 = Variational_bid - Paradex_ask
            (在 Paradex 低价买入，在 Variational 高价卖出)

做空价差 = Paradex_bid - Variational_ask
            (在 Paradex 高价卖出，在 Variational 低价买入)
```

### 动态均值阈值

不使用固定阈值，而是动态追踪价差均值：

```
触发条件: 瞬时价差 > 滚动均值 + 用户设定的 offset
```

1. **预热阶段**: 启动后收集前 N 条（默认 100）价差数据
2. **滑动窗口**: 维护最近 500 条价差数据的滚动均值
3. **信号触发**: 当瞬时价差显著偏离均值时执行交易

### 执行流程

```
检测到价差信号
    │
    ▼
┌─────────────────────────────┐
│ 1. Paradex 直接吃单          │  ← taker, Interactive Token 零手续费
│    BUY@ask / SELL@bid        │     秒成交，无需等待
└──────────────┬──────────────┘
               │ 确认成交
               ▼
┌─────────────────────────────┐
│ 2. Variational 市价对冲      │  ← taker, 零手续费
│    (RFQ: 获取报价 → 提交)    │
└─────────────────────────────┘
               │
    ▼ 仓位更新，等待下一个信号
```

### 为什么两边都用 taker？

- **Paradex**: Interactive Token 下 **maker 和 taker 都是零手续费**，直接吃单秒成交，避免挂单超时
- **Variational**: 本身所有订单都零手续费，市价单保证成交速度

---

## 系统架构

```
┌───────────────────────────────────────────────────────────┐
│                       main.py                             │
│                    (命令行入口)                             │
├───────────────────────────────────────────────────────────┤
│                                                           │
│  ┌─────────────┐    ┌───────────────────────────────┐     │
│  │  config.py   │    │   strategy/                    │     │
│  │  (.env 配置) │    │   ├─ arbitrage_engine.py       │     │
│  └─────────────┘    │   │  (核心套利引擎)             │     │
│                      │   ├─ spread_analyzer.py         │     │
│                      │   │  (价差分析+信号检测)        │     │
│                      │   └─ position_tracker.py        │     │
│                      │      (双边仓位跟踪)             │     │
│                      └───────────────────────────────┘     │
│                                                           │
│  ┌──────────────────────────────────────────────────┐     │
│  │ exchanges/                                        │     │
│  │ ├─ base.py           (标准化接口基类)              │     │
│  │ ├─ paradex_client.py  (Paradex SDK + HTTP)         │     │
│  │ │   └─ aiohttp + paradex-py SDK                   │     │
│  │ └─ variational_client.py (Variational 逆向 API)    │     │
│  │     └─ curl_cffi (Chrome TLS 指纹绕过 Cloudflare)  │     │
│  └──────────────────────────────────────────────────┘     │
│                                                           │
│  ┌──────────────────────────────────────────────────┐     │
│  │ helpers/                                          │     │
│  │ ├─ logger.py          (日志系统)                   │     │
│  │ ├─ telegram_bot.py    (Telegram 通知)              │     │
│  │ └─ data_logger.py     (CSV 数据记录)               │     │
│  └──────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────┘
```

---

## 环境准备

### 系统要求

- **Python**: 3.9+ (推荐 3.10 - 3.12)
- **操作系统**: Windows / Linux / macOS
- **网络**: 需要能访问 `paradex.trade` 和 `omni.variational.io`

### 前置条件

1. **Paradex 账户**: 需要 StarkNet L2 私钥和地址
   - 在 [Paradex](https://app.paradex.trade) 注册并充值 USDC
   - 导出你的 L2 私钥和地址

2. **Variational 账户**: 需要以太坊钱包
   - 在 [Variational Omni](https://omni.variational.io) 连接钱包
   - 充值 USDC 到 Variational

3. **浏览器**: Chrome/Edge (用于获取 Variational 认证信息)

---

## 安装部署

### 1. 克隆项目

```bash
git clone https://github.com/wuyutanhongyuxin-cell/P-V.git
cd P-V
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> **注意**: `curl_cffi` 是关键依赖，用于绕过 Cloudflare。如果安装失败：
> ```bash
> # 使用国内镜像
> pip install curl_cffi -i https://mirrors.aliyun.com/pypi/simple/
>
> # 如果遇到代理问题
> NO_PROXY="*" pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
> ```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的密钥和认证信息
```

---

## 获取认证信息

### Paradex 认证

Paradex 使用 StarkNet L2 密钥对认证，通过 `paradex-py` SDK 自动处理：

1. 登录 [Paradex](https://app.paradex.trade)
2. 导出你的 **L2 Private Key** 和 **L2 Address**
3. 填入 `.env` 文件：
   ```env
   PARADEX_L2_PRIVATE_KEY=0x你的L2私钥
   PARADEX_L2_ADDRESS=0x你的L2地址
   ```

### Variational 认证 (重要!)

Variational 的交易 API **尚未公开**，需要从浏览器获取认证信息。

#### 方法一：获取完整 Cookie (推荐)

1. 打开 Chrome，访问 https://omni.variational.io
2. 连接钱包并登录
3. 按 **F12** 打开开发者工具
4. 切换到 **Console** 面板
5. 输入以下命令并回车：
   ```javascript
   document.cookie
   ```
6. 复制输出的**整个字符串**，粘贴到 `.env`：
   ```env
   VARIATIONAL_COOKIES=粘贴整个cookie字符串
   ```

#### 方法二：只获取 vr-token

1. 打开 Chrome，访问 https://omni.variational.io
2. 连接钱包并登录
3. 按 **F12** → **Application** → **Cookies** → 找到 `vr-token`
4. 复制 `vr-token` 的值：
   ```env
   VARIATIONAL_VR_TOKEN=eyJ0eXAiOiJKV1Qi...
   ```

#### 获取钱包地址

在 `.env` 中填入你在 Variational 上连接的钱包地址：
```env
VARIATIONAL_WALLET_ADDRESS=0x你的钱包地址
```

> **重要提示**:
> - vr-token 通常在 **7 天**后过期，过期后需重新获取
> - 推荐使用**完整 Cookie** (方法一)，包含 Cloudflare 的 `cf_clearance`
> - 如果只提供 vr-token，curl_cffi 通常也能绕过 Cloudflare，但不如完整 Cookie 稳定

---

## 配置说明

### .env 文件

```env
# ===== Paradex 配置 =====
PARADEX_L2_PRIVATE_KEY=0x...    # StarkNet L2 私钥
PARADEX_L2_ADDRESS=0x...        # StarkNet L2 地址
PARADEX_ENVIRONMENT=prod        # prod 或 testnet

# ===== Variational 配置 =====
VARIATIONAL_VR_TOKEN=eyJ...     # vr-token JWT
VARIATIONAL_WALLET_ADDRESS=0x...  # 钱包地址
VARIATIONAL_COOKIES=...         # 完整 Cookie (推荐)
VARIATIONAL_BASE_URL=https://omni.variational.io  # 一般不需要改

# ===== Telegram 通知 (可选) =====
TELEGRAM_BOT_TOKEN=...          # Bot Token
TELEGRAM_GROUP_ID=...           # 群组 ID
ACCOUNT_LABEL=A1                # 账号标签
```

---

## 使用方法

### 基本用法

```bash
python main.py --ticker BTC --size 0.001 --max-position 0.01
```

### 使用 screen 后台运行 (推荐)

在 Linux 服务器上建议使用 `screen` 保持程序在后台运行，断开 SSH 后不会中断：

```bash
# 1. 创建一个新的 screen 会话（命名为 arb）
screen -S arb

# 2. 在 screen 内启动机器人
cd /path/to/P-V
python main.py --ticker BTC --size 0.001 --max-position 0.01

# 3. 分离 screen（程序继续在后台运行）
#    快捷键: Ctrl+A 然后按 D

# 4. 重新连接到 screen（查看运行状态）
screen -r arb

# 5. 查看所有 screen 会话
screen -ls
```

#### 多交易对同时运行

```bash
# 为每个交易对创建单独的 screen
screen -S arb-btc
python main.py --ticker BTC --size 0.001 --max-position 0.01
# Ctrl+A, D 分离

screen -S arb-eth
python main.py --ticker ETH --size 0.01 --max-position 0.1
# Ctrl+A, D 分离
```

#### 停止机器人

```bash
# 重新连接到 screen
screen -r arb

# 按 Ctrl+C 触发优雅退出（自动平仓）
# 等待平仓完成后 screen 会话自动结束

# 或强制关闭 screen 会话（不推荐，不会平仓）
screen -X -S arb quit
```

### 完整参数

```bash
python main.py \
  --ticker BTC \
  --size 0.001 \
  --max-position 0.01 \
  --long-threshold 10 \
  --short-threshold 10 \
  --fill-timeout 5 \
  --min-balance 10 \
  --warmup-samples 100 \
  --env-file .env
```

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|-------|------|
| `--ticker` | str | BTC | 交易对 (BTC/ETH/SOL 等) |
| `--size` | Decimal | **必填** | 每笔订单数量 |
| `--max-position` | Decimal | **必填** | 最大持仓限制 (单边绝对值) |
| `--long-threshold` | Decimal | 10 | 做多触发: 价差 > 均值 + 此值 |
| `--short-threshold` | Decimal | 10 | 做空触发: 价差 > 均值 + 此值 |
| `--fill-timeout` | int | 5 | 吃单成交确认超时 (秒) |
| `--min-balance` | Decimal | 10 | 最低余额 (USDC)，低于此值平仓退出 |
| `--warmup-samples` | int | 100 | 预热采样数量 |
| `--env-file` | str | .env | 环境变量文件路径 |

### 支持的交易对

| Ticker | Paradex 合约 | Variational 合约 |
|--------|-------------|-----------------|
| BTC | BTC-USD-PERP | P-BTC-USDC-3600 |
| ETH | ETH-USD-PERP | P-ETH-USDC-3600 |
| SOL | SOL-USD-PERP | P-SOL-USDC-3600 |
| ARB | ARB-USD-PERP | P-ARB-USDC-3600 |
| DOGE | DOGE-USD-PERP | P-DOGE-USDC-3600 |
| AVAX | AVAX-USD-PERP | P-AVAX-USDC-3600 |
| LINK | LINK-USD-PERP | P-LINK-USDC-3600 |
| OP | OP-USD-PERP | P-OP-USDC-3600 |

---

## 策略参数调优

### threshold (价差触发偏移)

- **较小值** (如 5): 更频繁触发，交易次数多，但每笔利润小
- **较大值** (如 20): 触发少但每笔利润大，可能长时间无交易
- **建议**: 先用默认值 10 运行，观察 `logs/arb_BTC_bbo.csv` 中的价差分布后调整

### size (单笔数量)

- 建议从小额开始测试 (如 BTC 0.001)
- 过大的单笔数量可能导致 Variational RFQ 报价滑点增加

### max-position (最大持仓)

- 控制单边最大暴露量
- 建议设为 `size` 的 10-20 倍
- 例: size=0.001, max-position=0.01

### warmup-samples (预热样本)

- 采样间隔约 1 秒，100 样本 ≈ 100 秒预热
- 过少可能导致均值不稳定
- 过多则启动后等待时间太长

---

## 项目结构

```
paradex-variational-arb/
│
├── main.py                          # 主入口 (命令行参数 → 配置 → 启动引擎)
├── config.py                        # 配置管理 (.env 加载 + 参数验证)
├── requirements.txt                 # Python 依赖
├── .env.example                     # 环境变量模板
├── .gitignore                       # Git 忽略规则
│
├── exchanges/                       # 交易所客户端
│   ├── base.py                      # 标准化接口基类 (BBO, OrderResult, PositionInfo)
│   ├── paradex_client.py            # Paradex 客户端
│   │                                #   SDK签名 + HTTP发送 + Interactive Token
│   └── variational_client.py        # Variational 客户端
│                                    #   curl_cffi (Chrome TLS) + Cookie 认证
│                                    #   RFQ 两步下单 + 逆向 API
│
├── strategy/                        # 策略模块
│   ├── arbitrage_engine.py          # 核心套利引擎 (主循环 + 执行 + 优雅退出)
│   ├── spread_analyzer.py           # 价差分析器 (动态均值 + 信号检测)
│   └── position_tracker.py          # 双边仓位跟踪器 (限制 + 不平衡检测)
│
├── helpers/                         # 辅助工具
│   ├── logger.py                    # 日志系统 (控制台 + 文件 + 时区)
│   ├── telegram_bot.py              # Telegram 通知
│   └── data_logger.py               # CSV 数据记录 (BBO + 交易)
│
└── logs/                            # 运行时日志 (自动创建)
    ├── arb_BTC_activity.log         # 详细运行日志
    ├── arb_BTC_bbo.csv              # BBO 价差数据
    ├── arb_BTC_trades.csv           # 交易记录
    └── arb_BTC_trades_detail.csv    # 交易详情
```

---

## API 端点文档

### Paradex (官方 API)

使用 `paradex-py` SDK，主要端点：

| 操作 | 方法 | 端点 |
|------|------|------|
| 认证 | POST | `/v1/auth?token_usage=interactive` |
| 下单 | POST | `/v1/orders` |
| 取消 | DELETE | `/v1/orders/{id}` |
| 持仓 | GET | `/v1/positions` |
| 余额 | GET | `/v1/balance` |
| 订单簿 | GET | `/v1/orderbook/{market}?depth=1` |

### Variational (逆向 API)

所有端点通过浏览器 F12 抓包确认：

| 操作 | 方法 | 端点 | 说明 |
|------|------|------|------|
| 市场统计 | GET | `/metadata/stats` | 公开，473 个市场 |
| 获取持仓 | GET | `/api/positions` | 需认证 |
| 查询挂单 | GET | `/api/orders/v2?status=pending` | 需认证 |
| RFQ 报价 | POST | `/api/quotes/indicative` | 市价单第1步 |
| 市价单 | POST | `/api/orders/new/market` | 需 quote_id |
| 限价单 | POST | `/api/orders/new/limit` | 一步完成 |
| 取消订单 | POST | `/api/orders/cancel` | body: {rfq_id} |
| 余额 | GET | `/api/portfolio?compute_margin=true` | 需认证 |
| 支持资产 | GET | `/api/metadata/supported_assets` | 需认证 |

#### Variational RFQ 下单流程

```
市价单 (两步):
  1. POST /api/quotes/indicative
     Body: {"instrument": {...}, "qty": "0.001"}
     Response: {"quote_id": "xxx", "bid": "68811", "ask": "68814", ...}

  2. POST /api/orders/new/market
     Body: {"quote_id": "xxx", "side": "buy", "max_slippage": 0.005}

限价单 (一步):
  POST /api/orders/new/limit
  Body: {
    "order_type": "limit",
    "limit_price": "68950",
    "side": "buy",
    "instrument": {"underlying":"BTC","instrument_type":"perpetual_future",
                   "settlement_asset":"USDC","funding_interval_s":3600},
    "qty": "0.001",
    "is_auto_resize": false,
    "use_mark_price": false,
    "is_reduce_only": false
  }
```

#### Variational Instrument 格式

```
格式: P-{underlying}-{settlement_asset}-{funding_interval_s}
示例: P-BTC-USDC-3600 = BTC 永续合约, USDC 结算, 1小时资金费

API 请求中的 instrument 对象:
{
  "underlying": "BTC",
  "instrument_type": "perpetual_future",
  "settlement_asset": "USDC",
  "funding_interval_s": 3600
}
```

---

## Cloudflare 绕过原理

Variational 的前端 (`omni.variational.io`) 使用 Cloudflare Bot Management 保护。普通的 Python HTTP 库 (`requests`, `aiohttp`) 会被拦截 (HTTP 403)。

### 为什么被拦截？

Cloudflare 检测以下特征：
1. **TLS 指纹** (JA3/JA4): Python 库的 TLS 握手特征与浏览器不同
2. **HTTP/2 指纹**: 帧顺序、窗口大小等
3. **Cookie**: `cf_clearance` 绑定了 TLS 指纹

### 解决方案: curl_cffi

`curl_cffi` 库使用修补的 `curl` 底层，可以模拟 Chrome 的完整 TLS 指纹：

```python
from curl_cffi.requests import AsyncSession

async with AsyncSession(impersonate="chrome131") as session:
    # 发出的请求具有与 Chrome 131 相同的 TLS/HTTP2 指纹
    r = await session.get("https://omni.variational.io/api/positions")
```

### 双 URL 策略

- **公开数据** (`/metadata/stats`): 使用后端 URL 直连 (无 Cloudflare)
  ```
  https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats
  ```

- **交易数据** (`/api/*`): 使用前端 URL + curl_cffi
  ```
  https://omni.variational.io/api/positions
  ```

---

## 风险控制

### 内置安全机制

| 机制 | 说明 |
|------|------|
| **最大持仓限制** | 单边持仓不超过 `--max-position` |
| **余额不足退出** | 任一交易所余额 < `--min-balance` 时自动平仓退出 |
| **仓位不平衡检测** | 每 30 分钟检查，净仓位 > 2倍单笔时报警 |
| **订单超时取消** | Paradex 吃单超时 (默认 5s) 未成交自动取消 |
| **优雅退出** | Ctrl+C 触发: 取消挂单 → 市价平仓 → 确认仓位归零 |
| **Telegram 报警** | 对冲失败、仓位不平衡等异常发送即时通知 |

### 主要风险

1. **对冲失败风险**: Paradex 成交但 Variational 对冲失败，导致单边暴露
   - 缓解: Telegram 即时报警，优雅退出时强制平仓

2. **Token 过期风险**: Variational vr-token 过期导致无法交易
   - 缓解: 程序检测 401 错误并记录日志

3. **Cloudflare 封锁**: Cookie 过期或 IP 变更导致请求被拦截
   - 缓解: curl_cffi TLS 指纹模拟，检测 403 状态

4. **价格剧烈波动**: 极端行情下价差可能瞬间消失
   - 缓解: 直接 taker 秒成交锁定价差，3 秒冷却防止连续亏损

---

## 日志与监控

### 日志文件

| 文件 | 说明 |
|------|------|
| `logs/arb_{ticker}_activity.log` | 完整运行日志 (DEBUG 级别) |
| `logs/arb_{ticker}_bbo.csv` | 实时 BBO 价差数据 |
| `logs/arb_{ticker}_trades.csv` | 交易记录摘要 |
| `logs/arb_{ticker}_trades_detail.csv` | 交易详情 (含两边方向和价格) |

### Telegram 通知

配置 `.env` 中的 Telegram 参数后，会在以下事件发送通知：
- 机器人启动/退出
- 每笔套利成交
- 对冲失败报警
- 每 30 分钟状态报告 (余额、仓位、价差均值)

---

## 常见问题

### Q: 安装 curl_cffi 失败？

```bash
# Windows 用户可能需要 Visual C++ Build Tools
# 或直接使用预编译包:
pip install curl_cffi -i https://mirrors.aliyun.com/pypi/simple/

# 如果有代理问题:
set NO_PROXY=*
pip install curl_cffi
```

### Q: Variational API 返回 403？

1. Cookie 可能已过期，重新从浏览器获取
2. 确保使用完整 Cookie (包含 `cf_clearance`)
3. curl_cffi 版本过旧，升级: `pip install --upgrade curl_cffi`

### Q: Variational API 返回 401？

vr-token 已过期 (通常 7 天有效)，需要：
1. 在浏览器中重新登录 Variational
2. F12 → Console → `document.cookie` → 复制新的 Cookie
3. 更新 `.env` 文件
4. 重启程序

### Q: Paradex 认证失败？

1. 检查 L2 私钥和地址是否正确
2. 确认 `paradex-py` 已安装且版本 >= 0.5
3. 检查网络是否能访问 `api.prod.paradex.trade`

### Q: 长时间没有交易？

1. 检查 threshold 是否设置过大
2. 查看 `logs/arb_BTC_bbo.csv` 中的价差范围
3. 确认预热是否完成 (需要 warmup_samples 秒)

### Q: 如何在 Windows 上运行？

```bash
# 使用 Anaconda 或直接 Python
python main.py --ticker BTC --size 0.001 --max-position 0.01

# 如果控制台中文乱码，设置编码:
set PYTHONIOENCODING=utf-8
python main.py --ticker BTC --size 0.001 --max-position 0.01
```

### Q: 如何同时套利多个交易对？

使用 `screen` 为每个交易对创建独立会话（参见[使用方法](#使用-screen-后台运行-推荐)）：
```bash
screen -S arb-btc -dm python main.py --ticker BTC --size 0.001 --max-position 0.01
screen -S arb-eth -dm python main.py --ticker ETH --size 0.01 --max-position 0.1

# 查看所有会话
screen -ls

# 连接到某个会话查看日志
screen -r arb-btc
```

---

## 免责声明

- 本项目仅供**学习和研究**使用
- 加密货币交易存在高风险，可能导致资金损失
- Variational API 为非公开接口，可能随时变更
- 作者不对使用本项目造成的任何损失负责
- 请遵守相关交易所的服务条款

---

## License

MIT
