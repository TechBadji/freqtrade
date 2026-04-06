# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort:skip_file
# --- Do not remove these imports ---
from datetime import datetime
from functools import reduce

import pandas_ta as pta
from pandas import DataFrame

from freqtrade.strategy import (
    DecimalParameter,
    IStrategy,
    IntParameter,
    informative,
)

# ============================================================================
# ZeroLagTrend Strategy — AGGRESSIVE MODE
# ============================================================================
# Version     : 2026.04.06 (Aggressive)
# Budget      : $1,000 USDT (Binance Spot)
# Timeframe   : 1h
# ============================================================================


class ZeroLagTrend(IStrategy):
    """
    ZeroLag Trend Strategy - Aggressive Edition
    """

    INTERFACE_VERSION = 3
    timeframe = "1h"

    # --- ROI table (Aggressive: No small targets, let runners move) ---
    minimal_roi = {
        "0": 0.12,     # Don't exit early, target 12%
        "120": 0.05,   # 5% after 2h
        "360": 0.02,   # 2% after 6h
        "720": 0.015   # Minimal exit if trade is slow
    }

    # --- Stoploss (Reduced to -3% to balance risk) ---
    stoploss = -0.03

    # --- Trailing Stop (Nervous trail for aggressive captures) ---
    trailing_stop = True
    trailing_stop_positive = 0.008        # Trail distance: 0.8%
    trailing_stop_positive_offset = 0.018 # Activate at 1.8% profit
    trailing_only_offset_is_reached = True

    # --- Order type ---
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }

    startup_candle_count: int = 200
    process_only_new_candles = True

    # =========================================================================
    # HYPEROPT PARAMETERS (Relaxed spaces)
    # =========================================================================

    buy_ema_short = IntParameter(7, 12, default=9, space="buy", optimize=True)
    buy_ema_medium = IntParameter(15, 25, default=21, space="buy", optimize=True)
    buy_ema_long = IntParameter(40, 60, default=50, space="buy", optimize=True)

    # RSI higher max (75) to catch breakouts
    buy_rsi_min = IntParameter(25, 45, default=35, space="buy", optimize=True)
    buy_rsi_max = IntParameter(60, 80, default=75, space="buy", optimize=True)

    # Lower volume factor (1.1) to trade more often
    buy_volume_factor = DecimalParameter(
        0.9, 1.5, default=1.05, decimals=2, space="buy", optimize=True
    )

    buy_bb_upperband_ratio = DecimalParameter(
        0.95, 1.05, default=1.01, decimals=2, space="buy", optimize=True
    )

    # =========================================================================
    # INDICATORS
    # =========================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # volume SMA
        dataframe["volume_sma"] = dataframe["volume"].rolling(window=20).mean()

        # EMAs
        ema_periods = {9, 21, 50, 100, 200}
        for period in ema_periods:
            dataframe[f"ema_{period}"] = pta.ema(dataframe["close"], length=period)

        # RSI
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=14)

        # BB
        bb = pta.bbands(dataframe["close"], length=20, std=2.0)
        dataframe["bb_upper"] = bb.iloc[:, 2]

        # MACD
        macd = pta.macd(dataframe["close"])
        dataframe["macd_hist"] = macd.iloc[:, 2]
        dataframe["macd_hist_prev"] = dataframe["macd_hist"].shift(1)

        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_200"] = pta.ema(dataframe["close"], length=200)
        # Macro Bull: Price just needs to be above EMA200 4h
        dataframe["is_bull"] = dataframe["close"] > dataframe["ema_200"]
        return dataframe

    # =========================================================================
    # ENTRY CONDITIONS (Loosened)
    # =========================================================================

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []

        # 1. Price above EMA200 (1h)
        conditions.append(dataframe["close"] > dataframe["ema_200"])
        
        # 2. Bullish 4h filter (Simple macro)
        conditions.append(dataframe["is_bull_4h"].astype(bool))

        # 3. Micro trend up
        conditions.append(dataframe["ema_9"] > dataframe["ema_21"])

        # 4. RSI Range (Stronger momentum)
        conditions.append(dataframe["rsi"] > 40)
        conditions.append(dataframe["rsi"] < self.buy_rsi_max.value)

        # 5. Volume Confirmation (Minimal)
        conditions.append(dataframe["volume"] > dataframe["volume_sma"] * self.buy_volume_factor.value)

        # 6. MACD Positive and Increasing
        conditions.append(dataframe["macd_hist"] > 0)
        conditions.append(dataframe["macd_hist"] > dataframe["macd_hist_prev"])

        # 7. Low price to upper band ratio (can be slightly above)
        conditions.append(dataframe["close"] < dataframe["bb_upper"] * self.buy_bb_upperband_ratio.value)

        if conditions:
            mask = reduce(lambda x, y: x & y, conditions)
            dataframe.loc[mask, "enter_long"] = 1
            dataframe.loc[mask, "enter_tag"] = "aggr_momentum_v4.1"

        return dataframe

    # =========================================================================
    # EXIT CONDITIONS (Reactive)
    # =========================================================================

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # On laisse le Trailing Stop et le Stoploss gérer les sorties courtes.
        # On ne sort manuellement que si la macro-tendance (4h) s'effondre.
        dataframe.loc[
            (~dataframe["is_bull_4h"].astype(bool)),
            "exit_long"] = 1
        dataframe.loc[
            (~dataframe["is_bull_4h"].astype(bool)),
            "exit_tag"] = "macro_trend_reversal"
        
        return dataframe

    # =========================================================================
    # CUSTOM STOPLOSS
    # =========================================================================

    def custom_stoploss(
        self, pair: str, trade, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs,
    ) -> float:
        # Lock in 0.5% profit as soon as we hit 2%
        if current_profit >= 0.02:
            return 0.005
        
        # If at 4% profit, move stop to 1.5%
        if current_profit >= 0.04:
            return 0.015

        return -1  # use default stoploss

    # =========================================================================
    # CUSTOM EXIT
    # =========================================================================

    def custom_exit(
        self, pair: str, trade, current_time: datetime, current_rate: float,
        current_profit: float, **kwargs,
    ):
        trade_duration = (current_time - trade.open_date_utc).total_seconds() / 3600

        # Cut dead trades faster (24h instead of 48h)
        if trade_duration > 24 and current_profit < -0.01:
            return "aggressive_timeout_24h"

        # RSI Overbought Exit (Exit at the peak)
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last_candle = dataframe.iloc[-1].squeeze()
        if last_candle["rsi"] > 80:
            return "rsi_overbought_80"

        return None
