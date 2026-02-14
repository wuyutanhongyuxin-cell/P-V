"""
核心套利引擎
参考: cross-exchange-arbitrage/strategy/edgex_arb.py
架构:
  1. 同时获取 Paradex 和 Variational 的 BBO
  2. 价差采样 + 动态均值阈值判断
  3. Paradex 直接吃单 (taker, interactive token 零手续费)
  4. Variational 市价对冲 (taker, 零手续费)
  5. 仓位跟踪 + 风险控制
  6. 优雅退出 (取消挂单 → 市价平仓 → 确认归零 → 退出)
"""

import asyncio
import logging
import signal
import sys
import time
import traceback
from decimal import Decimal
from typing import Optional

from exchanges.paradex_client import ParadexInteractiveClient
from exchanges.variational_client import VariationalClient
from exchanges.base import BBO, OrderResult
from helpers.logger import TradingLogger
from helpers.telegram_bot import TelegramNotifier
from helpers.data_logger import DataLogger

from .spread_analyzer import SpreadAnalyzer, SpreadSignal
from .position_tracker import PositionTracker

logger = logging.getLogger("arbitrage")


class ArbitrageEngine:
    """
    Paradex × Variational 跨所价差套利引擎
    策略: Paradex (taker, interactive token 零手续费) + Variational (taker, market order)
    """

    def __init__(
        self,
        paradex: ParadexInteractiveClient,
        variational: VariationalClient,
        paradex_market: str,
        variational_market: str,
        ticker: str,
        size: Decimal,
        max_position: Decimal,
        long_threshold: Decimal = Decimal("10"),
        short_threshold: Decimal = Decimal("10"),
        min_spread: Decimal = Decimal("5"),
        fill_timeout: int = 5,
        min_balance: Decimal = Decimal("10"),
        warmup_samples: int = 100,
        telegram: Optional[TelegramNotifier] = None,
    ):
        self.paradex = paradex
        self.variational = variational
        self.paradex_market = paradex_market
        self.variational_market = variational_market
        self.ticker = ticker
        self.size = size
        self.fill_timeout = fill_timeout
        self.min_balance = min_balance

        # 策略模块
        self.spread_analyzer = SpreadAnalyzer(
            long_threshold=long_threshold,
            short_threshold=short_threshold,
            min_spread=min_spread,
            warmup_samples=warmup_samples,
        )
        self.position_tracker = PositionTracker(max_position=max_position)

        # 辅助模块
        self.telegram = telegram
        self.data_logger = DataLogger(ticker=ticker)
        self.trading_logger = TradingLogger(ticker=ticker)

        # 控制标志
        self.stop_flag = False
        self._cleanup_done = False

        # 统计
        self.trade_count = 0
        self.start_time = time.time()
        self.last_balance_report_time = time.time()
        self.last_trade_time: float = 0
        self.last_heartbeat_time: float = 0  # 上次心跳时间
        self.trade_cooldown: float = 3.0  # 交易后冷却 3 秒
        self.heartbeat_interval: float = 300.0  # 心跳间隔 5 分钟

        # BBO 失败计数器
        self._bbo_fail_count = 0

    # ========== 信号处理与优雅退出 ==========

    def setup_signal_handlers(self) -> None:
        """注册信号处理器（Ctrl+C 优雅退出）"""
        signal.signal(signal.SIGINT, self._signal_handler)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        """信号处理回调"""
        if self.stop_flag:
            return
        self.stop_flag = True
        logger.info("收到退出信号，准备优雅退出...")

    async def _graceful_shutdown(self) -> None:
        """
        优雅退出流程:
          1. 取消所有未完成订单
          2. 市价平仓两边仓位
          3. 循环检查直到两边仓位均为 0
          4. 记录最终统计
        """
        if self._cleanup_done:
            return
        self._cleanup_done = True

        logger.info("=" * 60)
        logger.info("开始优雅退出流程...")

        # 1. 取消所有挂单
        logger.info("[退出] 取消 Paradex 所有挂单...")
        await self.paradex.cancel_all_orders(self.paradex_market)

        logger.info("[退出] 取消 Variational 所有挂单...")
        await self.variational.cancel_all_orders(self.variational_market)

        # 2. 市价平仓两边
        logger.info("[退出] 市价平仓 Paradex...")
        await self.paradex.close_position(self.paradex_market)

        logger.info("[退出] 市价平仓 Variational...")
        await self.variational.close_position(self.variational_market)

        # 3. 验证仓位归零
        max_retries = 10
        for i in range(max_retries):
            await asyncio.sleep(1)

            p_pos = await self.paradex.get_position_size(self.paradex_market)
            v_pos = await self.variational.get_position_size(self.variational_market)

            logger.info(
                f"[退出] 验证仓位 ({i + 1}/{max_retries}): "
                f"Paradex={p_pos}, Variational={v_pos}"
            )

            if p_pos == 0 and v_pos == 0:
                logger.info("[退出] 两边仓位已归零")
                break

            # 如果仍有仓位，再次尝试平仓
            if p_pos != 0:
                await self.paradex.close_position(self.paradex_market)
            if v_pos != 0:
                await self.variational.close_position(self.variational_market)

        # 4. 最终统计
        elapsed = time.time() - self.start_time
        logger.info("=" * 60)
        logger.info(f"运行时间: {elapsed / 3600:.2f} 小时")
        logger.info(f"总交易次数: {self.trade_count}")
        logger.info("=" * 60)

        # 发送 Telegram 通知
        if self.telegram:
            self.telegram.send(
                f"套利机器人已退出\n"
                f"运行时间: {elapsed / 3600:.2f}h\n"
                f"交易次数: {self.trade_count}"
            )

        # 关闭连接
        await self.paradex.disconnect()
        await self.variational.disconnect()
        self.data_logger.close()

    # ========== 主循环 ==========

    async def run(self) -> None:
        """运行套利引擎"""
        self.setup_signal_handlers()

        try:
            await self._initialize()
            await self._trading_loop()
        except KeyboardInterrupt:
            logger.info("收到键盘中断...")
        except asyncio.CancelledError:
            logger.info("任务被取消...")
        except Exception as e:
            logger.error(f"套利引擎异常: {e}")
            logger.error(traceback.format_exc())
        finally:
            await self._graceful_shutdown()

    async def _initialize(self) -> None:
        """初始化 — 连接交易所、获取市场信息、初始仓位"""
        logger.info("=" * 60)
        logger.info(f"Paradex x Variational 跨所套利引擎")
        logger.info(f"交易对: {self.ticker}")
        logger.info(f"Paradex 市场: {self.paradex_market}")
        logger.info(f"Variational 市场: {self.variational_market}")
        logger.info(f"单笔数量: {self.size}")
        logger.info(f"最大持仓: {self.position_tracker.max_position}")
        logger.info(f"做多阈值: {self.spread_analyzer.long_threshold}")
        logger.info(f"做空阈值: {self.spread_analyzer.short_threshold}")
        logger.info(f"预热样本: {self.spread_analyzer.warmup_samples}")
        logger.info("=" * 60)

        # 连接交易所
        logger.info("连接 Paradex (Interactive Token)...")
        await self.paradex.connect()

        logger.info("连接 Variational...")
        await self.variational.connect()

        # 获取市场信息
        p_info = await self.paradex.get_market_info(self.paradex_market)
        v_info = await self.variational.get_market_info(self.variational_market)

        if p_info:
            logger.info(
                f"Paradex 市场参数: tick={p_info.tick_size}, step={p_info.step_size}, "
                f"min_notional={p_info.min_notional}"
            )
        if v_info:
            logger.info(
                f"Variational 市场参数: tick={v_info.tick_size}, step={v_info.step_size}"
            )

        # 获取初始仓位
        p_pos = await self.paradex.get_position_size(self.paradex_market)
        v_pos = await self.variational.get_position_size(self.variational_market)
        self.position_tracker.update_paradex(p_pos)
        self.position_tracker.update_variational(v_pos)
        self.position_tracker.log_positions()

        # 获取初始余额
        p_bal = await self.paradex.get_balance()
        v_bal = await self.variational.get_balance()
        logger.info(f"余额: Paradex={p_bal} USDC, Variational={v_bal} USDC")

        # 发送启动通知
        if self.telegram:
            self.telegram.send(
                f"套利机器人已启动\n"
                f"交易对: {self.ticker}\n"
                f"单笔: {self.size}\n"
                f"余额: P={p_bal}, V={v_bal}"
            )

        logger.info("初始化完成，开始价差监控...")

    async def _trading_loop(self) -> None:
        """主交易循环"""
        cycle = 0

        while not self.stop_flag:
            try:
                cycle += 1

                # 1. 获取双方 BBO
                p_bbo, v_bbo = await self._fetch_both_bbo()

                if not p_bbo or not v_bbo:
                    self._bbo_fail_count += 1
                    fail_reason = []
                    if not p_bbo:
                        fail_reason.append("Paradex")
                    if not v_bbo:
                        fail_reason.append("Variational")

                    # 预热阶段每次都打印，预热后每 10 次打印一次
                    if not self.spread_analyzer.is_warmed_up:
                        logger.warning(
                            f"[预热中] BBO 获取失败 ({' & '.join(fail_reason)})，"
                            f"已失败 {self._bbo_fail_count} 次，等待重试..."
                        )
                    elif self._bbo_fail_count % 10 == 1:
                        logger.warning(
                            f"BBO 获取失败 ({' & '.join(fail_reason)})，"
                            f"已连续失败 {self._bbo_fail_count} 次，等待重试..."
                        )
                    await asyncio.sleep(0.5)
                    continue
                else:
                    # 成功后重置计数器
                    if self._bbo_fail_count > 0:
                        logger.info(f"BBO 获取恢复正常（之前连续失败 {self._bbo_fail_count} 次）")
                        self._bbo_fail_count = 0

                # 2. 价差采样
                self.spread_analyzer.add_sample(
                    paradex_bid=p_bbo.bid,
                    paradex_ask=p_bbo.ask,
                    variational_bid=v_bbo.bid,
                    variational_ask=v_bbo.ask,
                )

                # 3. 记录 BBO 数据到 CSV
                self.data_logger.log_bbo(
                    paradex_bid=p_bbo.bid,
                    paradex_ask=p_bbo.ask,
                    variational_bid=v_bbo.bid,
                    variational_ask=v_bbo.ask,
                    long_spread=self.spread_analyzer.current_long_spread,
                    short_spread=self.spread_analyzer.current_short_spread,
                )

                # 4. 检查交易信号 (冷却期内跳过)
                signal_result = self.spread_analyzer.check_signal()

                if signal_result and not self.stop_flag:
                    # 冷却检查: 上次交易后等待 N 秒
                    if time.time() - self.last_trade_time < self.trade_cooldown:
                        pass  # 冷却中，跳过
                    elif self.paradex.should_pause_trading:
                        # Interactive 限速保护: 暂停开仓
                        rate_info = self.paradex.get_rate_info()
                        logger.warning(
                            f"[限速暂停] 跳过交易 | "
                            f"1h={rate_info['orders_1h']}/200 "
                            f"24h={rate_info['orders_24h']}/1000 "
                            f"interactive_lost={rate_info['interactive_lost']}"
                        )
                    elif signal_result.direction == "LONG" and self.position_tracker.can_open_long(self.size):
                        await self._execute_long_trade(p_bbo, v_bbo, signal_result)
                    elif signal_result.direction == "SHORT" and self.position_tracker.can_open_short(self.size):
                        await self._execute_short_trade(p_bbo, v_bbo, signal_result)

                # 5. 定期检查余额和仓位
                await self._periodic_checks()

                # 6. 定期打印心跳日志（让用户知道脚本还活着）
                self._print_heartbeat_if_needed(p_bbo, v_bbo)

                # 等待下一轮 (1秒间隔，匹配价差采样频率)
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"交易循环异常: {e}")
                logger.error(traceback.format_exc())
                await asyncio.sleep(1)

    # ========== 数据获取 ==========

    async def _fetch_both_bbo(self) -> tuple:
        """同时获取两边的 BBO (Variational 使用 RFQ 实时报价)"""
        try:
            p_task = asyncio.create_task(self.paradex.get_bbo(self.paradex_market))
            v_task = asyncio.create_task(
                self.variational.get_bbo(self.variational_market, size=self.size)
            )

            p_bbo, v_bbo = await asyncio.gather(p_task, v_task, return_exceptions=True)

            if isinstance(p_bbo, Exception):
                logger.warning(f"获取 Paradex BBO 失败: {p_bbo}")
                p_bbo = None
            if isinstance(v_bbo, Exception):
                logger.warning(f"获取 Variational BBO 失败: {v_bbo}")
                v_bbo = None

            return p_bbo, v_bbo

        except Exception as e:
            logger.error(f"获取 BBO 异常: {e}")
            return None, None

    def _print_heartbeat_if_needed(self, p_bbo, v_bbo) -> None:
        """
        定期打印心跳日志（5分钟一次）
        让用户知道脚本还在运行，并显示当前状态
        """
        now = time.time()
        if now - self.last_heartbeat_time < self.heartbeat_interval:
            return

        self.last_heartbeat_time = now

        # 只有预热完成后才打印心跳
        if not self.spread_analyzer.is_warmed_up:
            return

        # 如果 BBO 获取失败，不打印心跳（避免干扰错误日志）
        if not p_bbo or not v_bbo:
            return

        # 运行时长
        runtime_hours = (now - self.start_time) / 3600

        # 当前价差
        long_spread = self.spread_analyzer.current_long_spread
        short_spread = self.spread_analyzer.current_short_spread

        # 距离触发线还差多少
        long_trigger = max(
            self.spread_analyzer.long_mean + self.spread_analyzer.long_threshold,
            self.spread_analyzer.min_spread,
        )
        short_trigger = max(
            self.spread_analyzer.short_mean + self.spread_analyzer.short_threshold,
            self.spread_analyzer.min_spread,
        )
        long_gap = long_trigger - long_spread
        short_gap = short_trigger - short_spread

        logger.info("=" * 60)
        logger.info(
            f"💓 心跳 | 运行 {runtime_hours:.1f}h | 交易 {self.trade_count} 笔 | "
            f"监控周期 {self.spread_analyzer.sample_count}"
        )
        logger.info(
            f"📊 做多价差: {long_spread:.2f} | 触发线: {long_trigger:.2f} | "
            f"还差: {long_gap:.2f} {'✅' if long_gap <= 0 else ''}"
        )
        logger.info(
            f"📊 做空价差: {short_spread:.2f} | 触发线: {short_trigger:.2f} | "
            f"还差: {short_gap:.2f} {'✅' if short_gap <= 0 else ''}"
        )
        logger.info(
            f"💰 仓位: Paradex={self.position_tracker.paradex_position:+.6f} | "
            f"Variational={self.position_tracker.variational_position:+.6f}"
        )
        logger.info("=" * 60)

    # ========== 交易执行 ==========

    async def _execute_long_trade(
        self, p_bbo: BBO, v_bbo: BBO, signal: SpreadSignal
    ) -> None:
        """
        执行做多套利 — 双腿同时下单:
          Paradex BUY (taker) + Variational SELL (market) 同时发送
        """
        if self.stop_flag:
            return

        logger.info(
            f"[做多信号] spread={signal.spread:.4f} > mean({signal.mean:.4f}) + "
            f"threshold({signal.threshold:.4f})"
        )

        # 更新仓位
        await self._refresh_positions()
        self.position_tracker.log_positions()

        if not self.position_tracker.can_open_long(self.size):
            logger.info("仓位已达上限，跳过做多")
            return

        order_price = p_bbo.ask

        logger.info(
            f"[同时下单] Paradex BUY {self.size} @ {order_price} | "
            f"Variational SELL {self.size}"
        )

        # 双腿同时发送
        p_coro = self.paradex.place_limit_order(
            market=self.paradex_market,
            side="BUY",
            size=self.size,
            price=order_price,
            post_only=False,
        )
        v_coro = self.variational.place_market_order(
            market=self.variational_market,
            side="SELL",
            size=self.size,
        )

        results = await asyncio.gather(p_coro, v_coro, return_exceptions=True)
        p_result, v_result = results

        self.last_trade_time = time.time()

        p_ok = isinstance(p_result, OrderResult) and p_result.success
        v_ok = isinstance(v_result, OrderResult) and v_result.success

        p_err = p_result.error_message if isinstance(p_result, OrderResult) else str(p_result)
        v_err = v_result.error_message if isinstance(v_result, OrderResult) else str(v_result)

        if p_ok and v_ok:
            # 两边都成功
            self.trade_count += 1
            logger.info(
                f"[做多成交] #{self.trade_count} Paradex BUY {self.size} @ {order_price} | "
                f"Variational SELL {self.size}"
            )

            self.data_logger.log_trade(
                direction="LONG",
                paradex_side="BUY",
                paradex_price=order_price,
                variational_side="SELL",
                size=self.size,
                spread=signal.spread,
            )
            self.trading_logger.log_trade("LONG", self.size, order_price, signal.spread)

            if self.telegram:
                self.telegram.send(
                    f"套利成交 [做多] #{self.trade_count}\n"
                    f"Paradex BUY {self.size} @ {order_price}\n"
                    f"Variational SELL {self.size}\n"
                    f"价差: {signal.spread:.4f}"
                )

        elif p_ok and not v_ok:
            # Paradex 成功, Variational 失败 — 反向平仓 Paradex
            logger.error(
                f"[单腿失败] Variational SELL 失败: {v_err}\n"
                "反向平仓 Paradex..."
            )
            await self.paradex.close_position(self.paradex_market)
            if self.telegram:
                self.telegram.send(
                    f"单腿失败! Variational SELL 失败\n"
                    f"已反向平仓 Paradex\n错误: {v_err}"
                )

        elif not p_ok and v_ok:
            # Variational 成功, Paradex 失败 — 反向平仓 Variational
            logger.error(
                f"[单腿失败] Paradex BUY 失败: {p_err}\n"
                "反向平仓 Variational..."
            )
            await self.variational.close_position(self.variational_market)
            if self.telegram:
                self.telegram.send(
                    f"单腿失败! Paradex BUY 失败\n"
                    f"已反向平仓 Variational\n错误: {p_err}"
                )

        else:
            # 两边都失败
            logger.warning(f"[两边失败] Paradex: {p_err} | Variational: {v_err}")

    async def _execute_short_trade(
        self, p_bbo: BBO, v_bbo: BBO, signal: SpreadSignal
    ) -> None:
        """
        执行做空套利 — 双腿同时下单:
          Paradex SELL (taker) + Variational BUY (market) 同时发送
        """
        if self.stop_flag:
            return

        logger.info(
            f"[做空信号] spread={signal.spread:.4f} > mean({signal.mean:.4f}) + "
            f"threshold({signal.threshold:.4f})"
        )

        await self._refresh_positions()
        self.position_tracker.log_positions()

        if not self.position_tracker.can_open_short(self.size):
            logger.info("仓位已达上限，跳过做空")
            return

        order_price = p_bbo.bid

        logger.info(
            f"[同时下单] Paradex SELL {self.size} @ {order_price} | "
            f"Variational BUY {self.size}"
        )

        # 双腿同时发送
        p_coro = self.paradex.place_limit_order(
            market=self.paradex_market,
            side="SELL",
            size=self.size,
            price=order_price,
            post_only=False,
        )
        v_coro = self.variational.place_market_order(
            market=self.variational_market,
            side="BUY",
            size=self.size,
        )

        results = await asyncio.gather(p_coro, v_coro, return_exceptions=True)
        p_result, v_result = results

        self.last_trade_time = time.time()

        p_ok = isinstance(p_result, OrderResult) and p_result.success
        v_ok = isinstance(v_result, OrderResult) and v_result.success

        p_err = p_result.error_message if isinstance(p_result, OrderResult) else str(p_result)
        v_err = v_result.error_message if isinstance(v_result, OrderResult) else str(v_result)

        if p_ok and v_ok:
            # 两边都成功
            self.trade_count += 1
            logger.info(
                f"[做空成交] #{self.trade_count} Paradex SELL {self.size} @ {order_price} | "
                f"Variational BUY {self.size}"
            )

            self.data_logger.log_trade(
                direction="SHORT",
                paradex_side="SELL",
                paradex_price=order_price,
                variational_side="BUY",
                size=self.size,
                spread=signal.spread,
            )
            self.trading_logger.log_trade("SHORT", self.size, order_price, signal.spread)

            if self.telegram:
                self.telegram.send(
                    f"套利成交 [做空] #{self.trade_count}\n"
                    f"Paradex SELL {self.size} @ {order_price}\n"
                    f"Variational BUY {self.size}\n"
                    f"价差: {signal.spread:.4f}"
                )

        elif p_ok and not v_ok:
            # Paradex 成功, Variational 失败 — 反向平仓 Paradex
            logger.error(
                f"[单腿失败] Variational BUY 失败: {v_err}\n"
                "反向平仓 Paradex..."
            )
            await self.paradex.close_position(self.paradex_market)
            if self.telegram:
                self.telegram.send(
                    f"单腿失败! Variational BUY 失败\n"
                    f"已反向平仓 Paradex\n错误: {v_err}"
                )

        elif not p_ok and v_ok:
            # Variational 成功, Paradex 失败 — 反向平仓 Variational
            logger.error(
                f"[单腿失败] Paradex SELL 失败: {p_err}\n"
                "反向平仓 Variational..."
            )
            await self.variational.close_position(self.variational_market)
            if self.telegram:
                self.telegram.send(
                    f"单腿失败! Paradex SELL 失败\n"
                    f"已反向平仓 Variational\n错误: {p_err}"
                )

        else:
            # 两边都失败
            logger.warning(f"[两边失败] Paradex: {p_err} | Variational: {v_err}")

    async def _wait_for_fill(self, order_id: str) -> str:
        """
        等待 Paradex 订单成交
        返回: "filled" / "rejected" / "timeout"
        """
        start = time.time()
        first_check = True
        while time.time() - start < self.fill_timeout and not self.stop_flag:
            info = await self.paradex.get_order_info(order_id)
            if info:
                status = info.get("status", "")
                remaining = Decimal(info.get("remaining_size", "1"))
                logger.debug(f"订单状态: {order_id[-8:]} status={status} remaining={remaining}")

                if status == "CLOSED" and remaining == 0:
                    return "filled"
                elif status == "CLOSED":
                    # POST_ONLY 订单穿越价差被立即拒绝
                    if first_check:
                        return "rejected"
                    return "timeout"  # 等了一会儿后被取消
                elif status == "OPEN" and remaining == 0:
                    return "filled"
            else:
                # 订单未找到 — 可能被 Paradex 秒拒并清除
                if first_check:
                    await asyncio.sleep(0.5)
                    info2 = await self.paradex.get_order_info(order_id)
                    if info2 is None:
                        logger.debug(f"订单 {order_id[-8:]} 查询不到，判定为被拒")
                        return "rejected"
                    # 第二次查到了，继续正常处理
                    status = info2.get("status", "")
                    remaining = Decimal(info2.get("remaining_size", "1"))
                    if status == "CLOSED" and remaining == 0:
                        return "filled"
                    elif status == "CLOSED":
                        return "rejected"

            first_check = False
            await asyncio.sleep(0.3)

        return "timeout"

    # ========== 定期检查 ==========

    async def _refresh_positions(self) -> None:
        """刷新双边仓位"""
        try:
            p_pos = await self.paradex.get_position_size(self.paradex_market)
            v_pos = await self.variational.get_position_size(self.variational_market)
            self.position_tracker.update_paradex(p_pos)
            self.position_tracker.update_variational(v_pos)
        except Exception as e:
            logger.warning(f"刷新仓位失败: {e}")

    async def _periodic_checks(self) -> None:
        """定期检查: 余额报告 + 仓位不平衡检测 + 余额不足退出"""
        now = time.time()

        # 每 30 分钟报告一次余额
        if now - self.last_balance_report_time >= 1800:
            self.last_balance_report_time = now

            p_bal = await self.paradex.get_balance()
            v_bal = await self.variational.get_balance()

            logger.info(f"[余额报告] Paradex={p_bal} USDC, Variational={v_bal} USDC")

            # 余额不足检查
            if p_bal is not None and p_bal < self.min_balance:
                logger.error(
                    f"Paradex 余额不足 ({p_bal} < {self.min_balance})! 开始平仓退出..."
                )
                self.stop_flag = True
                return

            if v_bal is not None and v_bal < self.min_balance:
                logger.error(
                    f"Variational 余额不足 ({v_bal} < {self.min_balance})! 开始平仓退出..."
                )
                self.stop_flag = True
                return

            # 仓位不平衡检测
            await self._refresh_positions()
            self.position_tracker.check_imbalance(threshold=self.size * 2)

            # 限速状态日志
            rate_info = self.paradex.get_rate_info()
            logger.info(
                f"[限速状态] 1h={rate_info['orders_1h']}/200 "
                f"24h={rate_info['orders_24h']}/1000 "
                f"paused={rate_info['paused']}"
            )

            if self.telegram:
                status = self.spread_analyzer.get_status()
                pos = self.position_tracker.get_status()
                self.telegram.send(
                    f"[状态报告]\n"
                    f"余额: P={p_bal}, V={v_bal}\n"
                    f"仓位: P={pos['paradex']}, V={pos['variational']}\n"
                    f"交易次数: {self.trade_count}\n"
                    f"价差均值: L={status['long_mean']:.2f}, S={status['short_mean']:.2f}\n"
                    f"限速: {rate_info['orders_1h']}/200h {rate_info['orders_24h']}/1000d"
                )
