# 开发错误总结与最佳实践

## 📌 目的

记录 Paradex × Variational 套利脚本开发过程中的所有错误、失误和教训，避免在未来项目中重复犯错。

---

## ❌ 错误分类目录

1. [技术选型错误](#1-技术选型错误)
2. [架构设计失误](#2-架构设计失误)
3. [信号逻辑缺陷](#3-信号逻辑缺陷)
4. [风控机制遗漏](#4-风控机制遗漏)
5. [用户体验问题](#5-用户体验问题)
6. [代码质量问题](#6-代码质量问题)
7. [测试不充分](#7-测试不充分)
8. [监控缺失](#8-监控缺失)

---

## 1. 技术选型错误

### ❌ 错误 1.1: 使用缓存接口而非实时数据

**问题描述**:
```python
# ❌ 错误做法
async def get_bbo(self, market: str):
    # 使用 /metadata/stats 公开接口
    resp = await self.session.get(f"{BASE_URL}/metadata/stats?instrument={market}")
    data = await resp.json()
    return BBO(bid=data["bid"], ask=data["ask"])

# 问题: 该接口有 2 秒缓存，导致价格延迟
```

**后果**:
- 实盘半天亏损 -4.05 USDC
- 信号触发时价格已过期，实际价差远低于预期
- 滑点损失 > 预期利润

**正确做法**:
```python
# ✅ 正确做法
async def get_bbo(self, market: str, size: Decimal):
    # 使用 RFQ 实时报价接口
    resp = await self.session.post(
        f"{BASE_URL}/api/quotes/indicative",
        json={
            "instrument_name": market,
            "amount": str(size),
            "is_buy": True
        }
    )
    data = await resp.json()
    return BBO(
        bid=Decimal(data["bid_price"]),
        ask=Decimal(data["ask_price"])
    )
```

**教训**:
> 🎓 **关键决策**: 在选择数据源时，优先级为: 实时 > 准实时 > 缓存
> - WebSocket 推送 > REST API 轮询 > 公开统计接口
> - 务必确认接口的缓存策略（Cache-Control, ETag）
> - 套利系统对延迟极度敏感，哪怕 1 秒延迟都可能致命

---

### ❌ 错误 1.2: 限价单当市价单用

**问题描述**:
```python
# ❌ 错误做法
async def execute_trade(self, side: str):
    # Paradex 用 GTC 限价单
    p_order = await self.paradex.place_limit_order(
        side=side,
        price=bbo.ask,  # 或 bbo.bid
        post_only=False  # 允许 taker
    )

    # Variational 用市价单
    v_order = await self.variational.place_market_order(side=side)

    # 问题:
    # - Paradex 限价单可能部分成交或挂单
    # - Variational 市价单立即全部成交
    # → 仓位不平衡！
```

**后果**:
```
实盘日志:
Paradex: -0.006 BTC
Variational: +0.008 BTC
净仓位: +0.002 BTC  ❌ 不平衡！
```

**正确做法**:
```python
# ✅ 正确做法
async def execute_trade(self, side: str):
    # 双边都用市价单
    p_order = await self.paradex.place_market_order(side=side)
    v_order = await self.variational.place_market_order(side=side)

    # 确保双边立即全部成交 → 净仓位 = 0
```

**教训**:
> 🎓 **订单类型选择**:
> - 套利系统**必须确保双边同时成交**，否则承担方向性风险
> - 限价单适合做市，市价单适合套利
> - 如果交易所不支持市价单，使用**激进限价单**（偏离 BBO 0.5-1%）
> - 提交订单后**必须验证成交状态**，不能仅凭 API 返回 `success=True` 判断

---

### ❌ 错误 1.3: 顺序执行而非并发

**问题描述**:
```python
# ❌ 错误做法
async def execute_trade(self):
    # 先提交 Paradex
    p_result = await self.paradex.place_order(...)  # 耗时 1-3s

    # 再提交 Variational
    v_result = await self.variational.place_order(...)  # 耗时 1-3s

    # 总延迟: 2-6 秒
    # 问题: 第二笔订单提交时，价格可能已剧烈波动
```

**后果**:
- 双腿执行总延迟 2-8 秒
- Variational 下单时价格已偏移，实际价差缩小
- 错过最佳入场时机

**正确做法**:
```python
# ✅ 正确做法
async def execute_trade(self):
    # 并发提交
    p_coro = self.paradex.place_order(...)
    v_coro = self.variational.place_order(...)

    results = await asyncio.gather(p_coro, v_coro, return_exceptions=True)
    p_result, v_result = results

    # 总延迟: max(T_paradex, T_variational) ≈ 1s
```

**教训**:
> 🎓 **并发执行原则**:
> - 凡是**独立操作**（无依赖关系），必须并发执行
> - 使用 `asyncio.gather()` 而非 `await` 串行等待
> - 双腿下单、双边 BBO 查询、多市场监控都应并发
> - 注意 `return_exceptions=True`，避免一个失败导致全部中断

---

## 2. 架构设计失误

### ❌ 错误 2.1: 没有单腿失败对冲机制

**问题描述**:
```python
# ❌ 错误做法（初版）
results = await asyncio.gather(p_coro, v_coro, return_exceptions=True)
p_result, v_result = results

if p_result.success and v_result.success:
    logger.info("双边成交")
    self.trade_count += 1
else:
    logger.error("有订单失败")
    # ⚠️ 没有处理单腿成功的情况！
    # 如果 Paradex 成功 + Variational 失败 → Paradex 持有裸仓
```

**后果**:
- 单腿失败时，另一腿持有方向性风险
- 需要人工介入平仓
- 可能因价格反向波动产生亏损

**正确做法**:
```python
# ✅ 正确做法
if p_ok and v_ok:
    # 双边成功
    logger.info("双边成交")
    self.trade_count += 1

elif p_ok and not v_ok:
    # Paradex 成功，Variational 失败 → 反向平仓 Paradex
    logger.error(f"Variational 失败: {v_err}，反向平仓 Paradex")
    await self.paradex.close_position(market)
    telegram.send("🚨 单腿失败! 已紧急平仓 Paradex")

elif not p_ok and v_ok:
    # Variational 成功，Paradex 失败 → 反向平仓 Variational
    logger.error(f"Paradex 失败: {p_err}，反向平仓 Variational")
    await self.variational.close_position(market)
    telegram.send("🚨 单腿失败! 已紧急平仓 Variational")

else:
    # 双边都失败
    logger.warning("双边都失败，无需处理")
```

**教训**:
> 🎓 **异常处理完整性**:
> - 双腿执行有 **4 种结果**: 双成功、单 A 成功、单 B 成功、双失败
> - **必须处理所有情况**，不能只考虑"全成功"和"全失败"
> - 单腿成功时，**立即平仓**比等待人工干预更安全
> - 使用 `return_exceptions=True` 捕获异常，避免程序崩溃

---

### ❌ 错误 2.2: 硬编码配置参数

**问题描述**:
```python
# ❌ 错误做法
class ArbitrageEngine:
    def __init__(self):
        self.trade_cooldown = 3.0  # 硬编码 3 秒冷却
        # 无法通过命令行调整
```

**后果**:
- 调参需要修改代码
- 无法根据市场情况动态调整
- 部署后修改需要重启程序

**正确做法**:
```python
# ✅ 正确做法
class ArbitrageEngine:
    def __init__(self, trade_cooldown: float = 3.0):
        self.trade_cooldown = trade_cooldown

# main.py
parser.add_argument("--trade-cooldown", type=float, default=3.0)
engine = ArbitrageEngine(trade_cooldown=args.trade_cooldown)
```

**教训**:
> 🎓 **配置管理原则**:
> - 凡是可能需要调整的参数，**必须可配置**
> - 优先级: 命令行参数 > 配置文件 > 环境变量 > 硬编码
> - 提供合理的默认值
> - 重要参数在启动时打印日志确认

---

### ❌ 错误 2.3: 缺少连接池复用

**问题描述**:
```python
# ❌ 错误做法
async def get_bbo(self):
    # 每次请求创建新 session
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()
    # 问题: 每次建立/销毁 TCP 连接，性能低下
```

**后果**:
- TCP 握手/挥手开销大
- 高频调用时性能瓶颈
- 可能触发交易所连接数限制

**正确做法**:
```python
# ✅ 正确做法
class ExchangeClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=100,           # 连接池大小
                    ttl_dns_cache=300    # DNS 缓存 5 分钟
                )
            )
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()
```

**教训**:
> 🎓 **HTTP 客户端最佳实践**:
> - **复用 session**，不要每次请求创建新的
> - 设置合理的连接池大小（100-500）
> - 启用 DNS 缓存减少查询开销
> - 程序退出前 `await session.close()` 优雅关闭

---

## 3. 信号逻辑缺陷

### ❌ 错误 3.1: 负均值阈值陷阱

**问题描述**:
```python
# ❌ 错误做法
class SpreadAnalyzer:
    def check_signal(self):
        trigger = self.mean + self.threshold
        if current_spread > trigger:
            return Signal(...)

# 问题场景:
# mean = -12.0  (市场负价差)
# threshold = 10.0
# trigger = -12 + 10 = -2.0  ❌ 触发线为负数！

# 只要 spread > -2 就触发，例如 spread = 0 也会触发
# 但 spread = 0 完全没有套利空间
```

**后果**:
- 低价差时频繁触发
- 手续费+滑点 > 价差利润
- 实盘亏损

**正确做法**:
```python
# ✅ 正确做法
class SpreadAnalyzer:
    def __init__(self, threshold: Decimal, min_spread: Decimal):
        self.threshold = threshold
        self.min_spread = min_spread  # 绝对下限

    def check_signal(self):
        # 取 (均值+阈值) 和 (绝对下限) 的最大值
        trigger = max(self.mean + self.threshold, self.min_spread)
        if current_spread > trigger:
            return Signal(...)

# 示例:
# mean = -12, threshold = 10, min_spread = 20
# trigger = max(-2, 20) = 20  ✅ 确保最低 20 美金价差
```

**教训**:
> 🎓 **动态阈值设计原则**:
> - 动态阈值**必须有绝对下限**，防止均值为负时失控
> - `min_spread` 应根据 **手续费 + 预期滑点 + 安全边际** 计算
> - 示例: 手续费 0.05% + 滑点 0.02% = 0.07% → min_spread = 0.1% (留安全边际)
> - 启动时打印触发条件，便于验证逻辑正确性

---

### ❌ 错误 3.2: 预热逻辑判断错误

**问题描述**:
```python
# ❌ 错误做法
class SpreadAnalyzer:
    @property
    def is_warmed_up(self) -> bool:
        return self.sample_count >= self.warmup_samples

    def add_sample(self, ...):
        self.sample_count += 1

        # 打印预热进度
        if not self.is_warmed_up and self.sample_count % 20 == 0:
            logger.info(f"预热中: {self.sample_count}/{self.warmup_samples}")

# 问题:
# 当 sample_count = 20, warmup_samples = 20 时:
# - sample_count % 20 == 0 ✅
# - is_warmed_up = True (因为 20 >= 20)
# - not is_warmed_up = False ❌
# → 条件不满足，不打印 "预热完成: 20/20"
```

**后果**:
- 用户看不到预热完成提示
- 以为脚本卡在 19/20

**正确做法**:
```python
# ✅ 正确做法
def add_sample(self, ...):
    self.sample_count += 1

    # 分开处理预热中和预热完成
    if self.sample_count < self.warmup_samples:
        # 预热中: 每 20 个打印一次
        if self.sample_count % 20 == 0:
            logger.info(f"预热中: {self.sample_count}/{self.warmup_samples}")

    elif self.sample_count == self.warmup_samples:
        # 预热完成: 正好等于目标值时打印
        logger.info(f"✅ 预热完成: {self.warmup_samples}/{self.warmup_samples}")
```

**教训**:
> 🎓 **边界条件测试**:
> - 边界值（如 `count == target`）最容易出 bug
> - 用 `<` 和 `==` 分开判断，而非 `if not warmed_up` 混合判断
> - 测试用例必须覆盖: `0`, `target-1`, `target`, `target+1`
> - 日志打印是用户体验关键，务必测试边界情况

---

## 4. 风控机制遗漏

### ❌ 错误 4.1: 没有限速保护

**问题描述**:
```python
# ❌ 错误做法（初版）
async def execute_trade(self):
    # 直接下单，不检查限速
    order = await self.paradex.place_order(...)

# 后果:
# - 超过 200 单/小时后，INTERACTIVE 标志丢失
# - 从 0% 手续费 → 0.05% 手续费
# - 所有后续交易亏损
```

**正确做法**:
```python
# ✅ 正确做法
class ParadexClient:
    def __init__(self):
        self._order_timestamps = deque(maxlen=1000)
        self._interactive_lost = False

    @property
    def orders_last_hour(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for t in self._order_timestamps if t > cutoff)

    @property
    def should_pause_trading(self) -> bool:
        # INTERACTIVE 丢失 → 暂停 10 分钟
        if self._interactive_lost:
            if time.time() - self._interactive_lost_time < 600:
                return True

        # 接近限速 → 主动减速
        if self.orders_last_hour >= 190:  # 留 10 单缓冲
            return True

        return False

# 交易前检查
async def execute_trade(self):
    if self.paradex.should_pause_trading:
        logger.warning("接近限速，暂停交易")
        return

    order = await self.paradex.place_order(...)
```

**教训**:
> 🎓 **API 限速保护**:
> - **主动监控**比被动触发更好
> - 留 5-10% 缓冲（如 200/h 限制，190 单就暂停）
> - INTERACTIVE 丢失后**暂停足够长时间**（10-30 分钟）
> - 使用 `deque(maxlen=...)` 高效统计滑动窗口

---

### ❌ 错误 4.2: 没有仓位不平衡检测

**问题描述**:
```python
# ❌ 错误做法（初版）
class PositionTracker:
    def log_positions(self):
        logger.info(f"Paradex={self.p_pos}, Variational={self.v_pos}")
        # 只打印，不检查是否平衡
```

**后果**:
- 仓位不平衡时无告警
- 持有方向性风险
- 需要人工盯盘发现问题

**正确做法**:
```python
# ✅ 正确做法
class PositionTracker:
    @property
    def net_position(self) -> Decimal:
        return self.paradex_position + self.variational_position

    def check_imbalance(self, threshold: Decimal = Decimal("0.001")):
        imbalance = abs(self.net_position)
        if imbalance > threshold:
            logger.warning(
                f"⚠️ 仓位不平衡! 净仓位={self.net_position} "
                f"(Paradex={self.paradex_position}, "
                f"Variational={self.variational_position})"
            )
            # 可配置: 暂停交易 / 自动平仓 / Telegram 告警
            return True
        return False

# 定期检查
async def periodic_checks(self):
    await self._refresh_positions()
    if self.position_tracker.check_imbalance():
        self.telegram.send("🚨 仓位不平衡，请检查!")
```

**教训**:
> 🎓 **风控监控原则**:
> - 关键指标（仓位、余额、PnL）**必须主动检测**
> - 异常情况触发多重告警: 日志 + Telegram + 邮件
> - 设置合理阈值（如 0.001 BTC = $69）
> - 考虑自动化响应: 暂停交易 > 自动平仓 > 人工介入

---

### ❌ 错误 4.3: 没有余额不足保护

**问题描述**:
```python
# ❌ 错误做法
async def execute_trade(self):
    # 直接下单，不检查余额
    order = await self.exchange.place_order(...)

# 后果:
# - 余额不足时订单被拒绝
# - 单腿失败 → 仓位不平衡
# - 需要紧急充值
```

**正确做法**:
```python
# ✅ 正确做法
async def periodic_checks(self):
    # 每 30 分钟检查余额
    p_balance = await self.paradex.get_balance()
    v_balance = await self.variational.get_balance()

    logger.info(f"余额: Paradex={p_balance} USDC, Variational={v_balance} USDC")

    # 余额不足停止交易
    if p_balance < 10 or v_balance < 10:
        logger.critical("⚠️ 余额不足，停止交易!")
        self.stop_flag = True
        self.telegram.send(
            f"🛑 余额不足，脚本已停止\n"
            f"Paradex: {p_balance} USDC\n"
            f"Variational: {v_balance} USDC"
        )
```

**教训**:
> 🎓 **资金安全原则**:
> - 定期检查余额（建议 10-30 分钟）
> - 设置**安全阈值**（如 10 USDC = 10 笔订单储备）
> - 余额不足时**立即停止交易**，而非等订单失败
> - Telegram 推送 + 日志双重告警

---

## 5. 用户体验问题

### ❌ 错误 5.1: 预热阶段完全静默

**问题描述**:
```python
# ❌ 错误做法
async def run(self):
    logger.info("开始预热...")

    # 20 秒内完全没有输出
    while not self.spread_analyzer.is_warmed_up:
        await self._monitor_once()
        await asyncio.sleep(1)

    logger.info("预热完成")

# 用户体验: "是不是卡死了？要不要重启？"
```

**正确做法**:
```python
# ✅ 正确做法
def add_sample(self, ...):
    self.sample_count += 1

    if self.sample_count < self.warmup_samples:
        # 每 20% 进度打印一次
        if self.sample_count % (self.warmup_samples // 5) == 0:
            progress = self.sample_count / self.warmup_samples * 100
            logger.info(f"预热进度: {progress:.0f}% ({self.sample_count}/{self.warmup_samples})")

    elif self.sample_count == self.warmup_samples:
        logger.info(f"✅ 预热完成!")
```

**教训**:
> 🎓 **进度反馈原则**:
> - 超过 5 秒的操作**必须有进度提示**
> - 进度条、百分比、`N/Total` 任选其一
> - 预热、下载、批处理等长时间操作尤其重要
> - 使用 Emoji 提升可读性: ✅ 完成、⏳ 进行中、❌ 失败

---

### ❌ 错误 5.2: 失败时静默

**问题描述**:
```python
# ❌ 错误做法
async def get_bbo(self):
    try:
        resp = await self.session.get(url)
        return BBO(...)
    except Exception:
        return None  # 静默失败，不打印日志

# 后果:
# - 用户不知道 API 调用失败
# - 无法排查问题
# - 可能长时间无输出，以为脚本卡死
```

**正确做法**:
```python
# ✅ 正确做法
async def get_bbo(self):
    try:
        resp = await self.session.get(url)
        return BBO(...)
    except Exception as e:
        self._bbo_fail_count += 1

        # 预热阶段每次都打印
        if not self.is_warmed_up:
            logger.warning(f"[预热中] BBO 获取失败: {e}")

        # 预热后每 10 次打印一次（避免刷屏）
        elif self._bbo_fail_count % 10 == 1:
            logger.warning(
                f"BBO 获取失败，已连续失败 {self._bbo_fail_count} 次: {e}"
            )

        return None
```

**教训**:
> 🎓 **错误日志策略**:
> - 失败时**必须打印日志**，至少 WARNING 级别
> - 高频操作失败时**采样打印**（如每 10 次打印一次）
> - 预热等关键阶段**每次都打印**
> - 包含错误原因 (`{e}`) 方便排查

---

### ❌ 错误 5.3: 长时间无输出

**问题描述**:
```python
# ❌ 错误做法
async def run(self):
    while not self.stop_flag:
        # 检测信号
        signal = self.spread_analyzer.check_signal()

        if signal:
            await self.execute_trade(signal)

        await asyncio.sleep(1)

# 问题:
# - 如果 1 小时无信号，完全没有输出
# - 用户: "是不是挂了？要不要重启？"
```

**正确做法**:
```python
# ✅ 正确做法
async def run(self):
    self.last_heartbeat_time = time.time()

    while not self.stop_flag:
        # ... 业务逻辑 ...

        # 每 5 分钟打印心跳
        if time.time() - self.last_heartbeat_time >= 300:
            self._print_heartbeat()
            self.last_heartbeat_time = time.time()

def _print_heartbeat(self):
    runtime = (time.time() - self.start_time) / 3600
    logger.info("=" * 60)
    logger.info(f"💓 心跳 | 运行 {runtime:.1f}h | 交易 {self.trade_count} 笔")
    logger.info(f"📊 做多价差: {long_spread:.2f} | 触发线: {long_trigger:.2f}")
    logger.info(f"📊 做空价差: {short_spread:.2f} | 触发线: {short_trigger:.2f}")
    logger.info(f"💰 仓位: Paradex={p_pos} | Variational={v_pos}")
    logger.info("=" * 60)
```

**教训**:
> 🎓 **心跳日志设计**:
> - 长期运行的程序**必须有心跳**（5-10 分钟）
> - 心跳内容: 运行时长、关键指标、当前状态
> - 配合 Telegram 推送，方便移动端监控
> - 使用分隔符（`===`）提升可读性

---

## 6. 代码质量问题

### ❌ 错误 6.1: 变量命名不清晰

**问题描述**:
```python
# ❌ 错误做法
async def execute_trade(self):
    # 改用市价单后，order_price 变量仍然存在
    order_price = p_bbo.ask  # 实际不再使用

    # 后续代码还在引用
    logger.info(f"下单价格: {order_price}")  # ❌ 误导性

    # 改用市价单后应该用 actual_price
    actual_price = p_result.price or p_bbo.ask
```

**后果**:
- 日志显示"下单价格"，实际是市价单（无固定价格）
- 代码可维护性差
- 新人接手困惑

**正确做法**:
```python
# ✅ 正确做法
async def execute_trade(self):
    # 市价单下单
    p_result = await self.paradex.place_market_order(...)

    # 从订单返回获取实际成交价
    actual_price = p_result.price if p_result.price else p_bbo.ask

    logger.info(f"实际成交价: ~${actual_price:.2f}")  # 用 ~ 表示近似
```

**教训**:
> 🎓 **变量命名规范**:
> - 变量名应**准确反映当前用途**
> - 代码重构后，及时更新变量名
> - 避免误导性命名（如 `order_price` 用于市价单）
> - 使用前缀区分: `expected_`, `actual_`, `estimated_`

---

### ❌ 错误 6.2: 魔法数字

**问题描述**:
```python
# ❌ 错误做法
if time.time() - self.last_trade_time < 3.0:
    return  # 3.0 是什么？

if self.orders_last_hour >= 190:
    return  # 190 是怎么来的？

if p_balance < 10:
    self.stop_flag = True  # 10 表示什么？
```

**正确做法**:
```python
# ✅ 正确做法
# 定义常量
TRADE_COOLDOWN_SECONDS = 3.0
PARADEX_HOURLY_LIMIT = 200
PARADEX_SAFE_BUFFER = 10  # 留 10 单缓冲
MIN_BALANCE_USDC = 10.0  # 最低 10 USDC 余额

# 使用常量
if time.time() - self.last_trade_time < TRADE_COOLDOWN_SECONDS:
    return

if self.orders_last_hour >= PARADEX_HOURLY_LIMIT - PARADEX_SAFE_BUFFER:
    return

if p_balance < MIN_BALANCE_USDC:
    self.stop_flag = True
```

**教训**:
> 🎓 **常量定义原则**:
> - 所有数字都应**命名常量**，除了 0, 1, -1
> - 常量名用 `UPPER_SNAKE_CASE`
> - 在文件顶部或类开头集中定义
> - 附加注释说明含义（如 `# 留 10 单缓冲`）

---

### ❌ 错误 6.3: 异常处理过于宽泛

**问题描述**:
```python
# ❌ 错误做法
async def get_bbo(self):
    try:
        resp = await self.session.get(url)
        data = await resp.json()
        return BBO(bid=data["bid"], ask=data["ask"])
    except Exception:
        # 吞掉所有异常，无法区分具体问题
        return None
```

**问题**:
- 网络超时？API 限流？数据格式错误？全都返回 `None`
- 无法针对性处理
- 排查困难

**正确做法**:
```python
# ✅ 正确做法
async def get_bbo(self):
    try:
        resp = await self.session.get(url, timeout=10)

        if resp.status != 200:
            logger.error(f"BBO API 错误: HTTP {resp.status}")
            return None

        data = await resp.json()

        if "bid" not in data or "ask" not in data:
            logger.error(f"BBO 数据格式错误: {data}")
            return None

        return BBO(bid=Decimal(data["bid"]), ask=Decimal(data["ask"]))

    except asyncio.TimeoutError:
        logger.warning("BBO 请求超时")
        return None

    except aiohttp.ClientError as e:
        logger.error(f"BBO 网络错误: {e}")
        return None

    except (KeyError, ValueError, TypeError) as e:
        logger.error(f"BBO 数据解析错误: {e}")
        return None

    except Exception as e:
        logger.exception(f"BBO 未知错误: {e}")
        return None
```

**教训**:
> 🎓 **异常处理最佳实践**:
> - **分类捕获**异常，不要一个 `except Exception` 包打天下
> - 超时、网络、解析错误分开处理
> - `logger.exception()` 会自动打印堆栈，方便排查
> - 关键错误用 `ERROR`，预期内的失败用 `WARNING`

---

## 7. 测试不充分

### ❌ 错误 7.1: 直接生产环境测试

**问题描述**:
```
开发流程:
1. 本地写完代码
2. 直接部署到 VPS
3. 用真实资金测试
4. 发现 bug（已经亏损）
5. 紧急修复
```

**后果**:
- P0 修复前亏损 -4.05 USDC
- 每次 bug 都是真金白银损失
- 压力大，容错率低

**正确做法**:
```
测试流程:
1. 本地单元测试（Mock 交易所响应）
2. 测试网测试（Paradex Testnet）
3. 生产环境小资金测试（0.001 BTC）
4. 观察 24 小时无问题
5. 逐步加大资金
```

**示例**:
```python
# ✅ 单元测试
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_execute_trade_success():
    engine = ArbitrageEngine(...)

    # Mock 交易所响应
    engine.paradex.place_market_order = AsyncMock(
        return_value=OrderResult(success=True, price=Decimal("69420"))
    )
    engine.variational.place_market_order = AsyncMock(
        return_value=OrderResult(success=True, price=Decimal("69400"))
    )

    signal = SpreadSignal(direction="LONG", spread=Decimal("20"))
    await engine._execute_long_trade(p_bbo, v_bbo, signal)

    assert engine.trade_count == 1
    assert engine.paradex.place_market_order.called

@pytest.mark.asyncio
async def test_execute_trade_single_leg_failure():
    # 测试单腿失败是否正确平仓
    ...
```

**教训**:
> 🎓 **测试金字塔**:
> ```
>     /\      生产环境大资金 (慎重)
>    /  \     生产环境小资金 (0.001 BTC)
>   /____\    测试网 (无成本)
>  /______\   单元测试 (Mock, 最快)
> ```
> - **80% 单元测试** + 15% 集成测试 + 5% 生产测试
> - 用 `pytest` + `unittest.mock` 模拟交易所
> - 测试覆盖: 正常流程、异常流程、边界条件
> - 生产测试从小资金开始

---

### ❌ 错误 7.2: 边界条件未测试

**问题描述**:
```python
# 只测试了正常场景
spread = 25.0  # > min_spread = 20  ✅
signal = analyzer.check_signal()  # 触发

# 未测试边界值
spread = 20.0  # == min_spread，应该触发吗？
spread = 19.9  # < min_spread，不应触发
mean = -50.0   # 极端负值
mean = 0.0     # 零值
```

**后果**:
- 预热完成时日志不打印（`count == warmup_samples` 未测试）
- 负均值阈值陷阱（`mean < 0` 未测试）

**正确做法**:
```python
# ✅ 边界测试
@pytest.mark.parametrize("mean,threshold,min_spread,current_spread,should_trigger", [
    # 正常场景
    (10.0, 10.0, 20.0, 25.0, True),   # spread > max(20, 20)
    (10.0, 10.0, 20.0, 15.0, False),  # spread < max(20, 20)

    # 边界场景
    (10.0, 10.0, 20.0, 20.0, False),  # spread == trigger (不触发，避免频繁)
    (10.0, 10.0, 20.0, 20.1, True),   # spread > trigger (触发)

    # 负均值场景
    (-12.0, 10.0, 20.0, 0.0, False),  # max(-2, 20) = 20, 0 < 20
    (-12.0, 10.0, 20.0, 21.0, True),  # 21 > 20

    # 极端场景
    (-100.0, 10.0, 20.0, 19.0, False),  # max(-90, 20) = 20, 19 < 20
    (0.0, 0.0, 0.0, 0.1, True),         # 所有阈值为 0
])
def test_signal_trigger(mean, threshold, min_spread, current_spread, should_trigger):
    analyzer = SpreadAnalyzer(
        long_threshold=Decimal(str(threshold)),
        min_spread=Decimal(str(min_spread))
    )
    analyzer.long_mean = Decimal(str(mean))
    analyzer.current_long_spread = Decimal(str(current_spread))

    signal = analyzer.check_signal()

    if should_trigger:
        assert signal is not None
    else:
        assert signal is None
```

**教训**:
> 🎓 **边界测试清单**:
> - **等于边界**: `x == threshold`, `count == target`
> - **刚好超过**: `x == threshold + epsilon`
> - **刚好不足**: `x == threshold - epsilon`
> - **极端值**: `x = 0`, `x = -infinity`, `x = +infinity`
> - **空值**: `None`, `""`, `[]`
> - 使用 `@pytest.mark.parametrize` 批量测试

---

## 8. 监控缺失

### ❌ 错误 8.1: 只有日志没有告警

**问题描述**:
```python
# ❌ 错误做法
if imbalance > threshold:
    logger.warning("仓位不平衡")
    # 只打印日志，用户不会实时看到
```

**后果**:
- 用户不盯着终端就不知道出问题
- 凌晨发生异常，早上才发现
- 损失扩大

**正确做法**:
```python
# ✅ 正确做法
if imbalance > threshold:
    msg = (
        f"🚨 仓位不平衡!\n"
        f"净仓位: {self.net_position}\n"
        f"Paradex: {self.paradex_position}\n"
        f"Variational: {self.variational_position}"
    )
    logger.warning(msg)
    self.telegram.send(msg)  # 立即推送手机
```

**教训**:
> 🎓 **告警渠道**:
> - **Telegram**: 实时推送，移动端友好 ⭐
> - **邮件**: 重要事件备份通知
> - **Discord/Slack**: 团队协作场景
> - **短信**: 极端紧急情况（成本高）
>
> 告警分级:
> - 🔴 Critical: 余额不足、仓位不平衡 → Telegram + 邮件
> - 🟡 Warning: BBO 失败、限速接近 → Telegram
> - 🟢 Info: 心跳、成交记录 → 日志

---

### ❌ 错误 8.2: 没有性能监控

**问题描述**:
```python
# ❌ 错误做法
async def execute_trade(self):
    await self.paradex.place_order(...)
    # 不知道这个操作花了多少时间
```

**后果**:
- API 延迟飙升时无感知
- 无法定位性能瓶颈
- 优化没有数据支撑

**正确做法**:
```python
# ✅ 正确做法
import time

async def execute_trade(self):
    start = time.time()

    results = await asyncio.gather(
        self.paradex.place_order(...),
        self.variational.place_order(...)
    )

    elapsed = time.time() - start

    logger.info(f"双腿执行耗时: {elapsed:.3f}s")

    # 慢查询告警
    if elapsed > 2.0:
        logger.warning(f"⚠️ 执行过慢: {elapsed:.3f}s")
        self.telegram.send(f"执行延迟: {elapsed:.3f}s")
```

**更好的做法（Prometheus）**:
```python
from prometheus_client import Summary, Counter, Gauge

# 定义指标
trade_latency = Summary('arb_trade_latency_seconds', 'Trade execution latency')
trade_count = Counter('arb_trade_total', 'Total trades', ['direction'])
position_net = Gauge('arb_position_net', 'Net position')

# 记录指标
@trade_latency.time()
async def execute_trade(self):
    ...
    trade_count.labels(direction="LONG").inc()
    position_net.set(float(self.net_position))
```

**教训**:
> 🎓 **性能监控指标**:
> - **延迟**: 双腿执行、BBO 查询、订单提交
> - **成功率**: 订单成功率、BBO 成功率
> - **吞吐量**: 每小时交易次数
> - **资源**: CPU、内存、网络带宽
>
> 工具选择:
> - 简单: 日志 + 手动分析
> - 中等: Prometheus + Grafana
> - 复杂: ELK Stack (Elasticsearch + Logstash + Kibana)

---

## 9. 安全问题

### ❌ 错误 9.1: 私钥硬编码

**问题描述**:
```python
# ❌ 错误做法
PRIVATE_KEY = "0x1234567890abcdef..."  # 硬编码在代码中
paradex = ParadexClient(private_key=PRIVATE_KEY)

# Git commit 后私钥泄露！
```

**正确做法**:
```python
# ✅ 正确做法
import os
from dotenv import load_dotenv

load_dotenv()  # 从 .env 文件加载

PRIVATE_KEY = os.getenv("PARADEX_PRIVATE_KEY")
if not PRIVATE_KEY:
    raise ValueError("PARADEX_PRIVATE_KEY not set in .env")

paradex = ParadexClient(private_key=PRIVATE_KEY)

# .gitignore 中添加
# .env
```

**教训**:
> 🎓 **密钥管理**:
> - 永远不要硬编码私钥、API 密钥
> - 使用环境变量或 `.env` 文件
> - `.env` 文件加入 `.gitignore`
> - 生产环境用 Vault、AWS Secrets Manager 等
> - 定期轮换密钥

---

### ❌ 错误 9.2: 日志打印敏感信息

**问题描述**:
```python
# ❌ 错误做法
logger.info(f"登录成功: {private_key}")
logger.info(f"JWT Token: {jwt_token}")
```

**后果**:
- 日志文件包含敏感信息
- 分享日志时泄露
- 日志集中存储时风险扩大

**正确做法**:
```python
# ✅ 正确做法
def mask_secret(secret: str, show_chars: int = 4) -> str:
    if len(secret) <= show_chars * 2:
        return "***"
    return f"{secret[:show_chars]}...{secret[-show_chars:]}"

logger.info(f"登录成功: {mask_secret(private_key)}")  # 0x12...cdef
logger.info(f"JWT Token: {mask_secret(jwt_token)}")   # eyJh...xY3
```

**教训**:
> 🎓 **日志安全**:
> - 私钥、密码、Token 只打印前后 4 位
> - 用户地址可以打印（公开信息）
> - 订单 ID、交易哈希可以打印
> - 开发环境可以详细打印，生产环境必须脱敏

---

## 10. 总结：开发检查清单

### 🎯 开发前（设计阶段）

- [ ] 确认数据源是否实时（无缓存）
- [ ] 确认订单类型（市价单 vs 限价单）
- [ ] 设计异常处理机制（单腿失败怎么办？）
- [ ] 设计风控指标（仓位、余额、限速）
- [ ] 确认关键参数可配置（不要硬编码）

### 🛠️ 开发中（编码阶段）

- [ ] 独立操作必须并发执行（`asyncio.gather`）
- [ ] HTTP 客户端使用连接池复用
- [ ] 所有数字定义为命名常量
- [ ] 异常处理分类捕获（不要 `except Exception`）
- [ ] 边界条件测试（等于、刚好超过、极端值）
- [ ] 变量命名准确反映用途

### 🧪 测试阶段

- [ ] 单元测试覆盖核心逻辑（Mock 交易所）
- [ ] 边界条件测试（0, target, target±1, 极端值）
- [ ] 测试网测试（无成本）
- [ ] 生产小资金测试（0.001 BTC）
- [ ] 观察 24 小时无异常再加大资金

### 🚀 部署前（上线阶段）

- [ ] 私钥使用环境变量（不硬编码）
- [ ] 日志脱敏（私钥、Token 只显示前后 4 位）
- [ ] 配置 Telegram 告警
- [ ] 心跳日志（5-10 分钟）
- [ ] 预热进度提示
- [ ] 失败日志打印

### 📊 运行中（监控阶段）

- [ ] 定期检查仓位平衡（每次交易后）
- [ ] 定期检查余额（10-30 分钟）
- [ ] 限速监控（接近 90% 暂停）
- [ ] 性能监控（延迟 > 2s 告警）
- [ ] 关键事件 Telegram 推送

---

## 📚 参考资料

### 推荐阅读

1. **异步编程**:
   - [Python asyncio 官方文档](https://docs.python.org/3/library/asyncio.html)
   - [aiohttp 最佳实践](https://docs.aiohttp.org/en/stable/client_advanced.html)

2. **测试**:
   - [pytest 官方文档](https://docs.pytest.org/)
   - [unittest.mock 使用指南](https://docs.python.org/3/library/unittest.mock.html)

3. **监控**:
   - [Prometheus Python Client](https://github.com/prometheus/client_python)
   - [Telegram Bot API](https://core.telegram.org/bots/api)

4. **安全**:
   - [OWASP Top 10](https://owasp.org/www-project-top-ten/)
   - [Python 安全最佳实践](https://python.readthedocs.io/en/stable/library/security_warnings.html)

### 开源参考

- **本项目**: https://github.com/wuyutanhongyuxin-cell/P-V
- **参考脚本**: https://github.com/your-quantguy/cross-exchange-arbitrage

---

## 🤝 贡献

欢迎补充更多错误案例和最佳实践！

**提交格式**:
```markdown
### ❌ 错误 X.Y: 标题

**问题描述**: ...
**后果**: ...
**正确做法**: ...
**教训**: ...
```

---

**文档版本**: 1.0
**最后更新**: 2026-02-15
**维护者**: Claude Opus 4.6 + Human Developer
