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
# ZeroLagTrend Strategy
# ============================================================================
# Author      : Senior Trader — ZeroLag.com
# Version     : 3.0.0
# Budget      : $1,000 USDT (Binance Spot)
# Timeframe   : 1h
# Description : Trend-following strategy combining EMA stack, RSI momentum,
#               Bollinger Bands and volume confirmation.
#               Hyperopt-ready with ROI/stoploss/trailing optimization.
#
# Entry logic:
#   - Price above EMA200 (macro uptrend)
#   - EMA21 > EMA50 (medium-term bullish structure)
#   - RSI in 35-65 range (momentum room, not overbought)
#   - Price > EMA9 (immediate micro-trend confirmation)
#   - Volume spike above SMA (conviction candle)
#   - MACD bullish (histogram positive or crossing up)
#
# Exit logic:
#   - Tiered ROI (lock profits early)
#   - Trailing stop (protect gains on runners)
#   - RSI overbought signal
#   - EMA9 crosses below EMA21 (trend weakening)
# ============================================================================


class ZeroLagTrend(IStrategy):
    """
    ZeroLag Trend Strategy
    Budget: $1,000 USDT | Exchange: Binance Spot | Timeframe: 1h
    """

    INTERFACE_VERSION = 3

    # --- Timeframe ---
    timeframe = "1h"

    # --- ROI table (tiered profit targets) ---
    # hyperopt will optimize these values — these are starting defaults
    minimal_roi = {
        "0": 0.04,
        "60": 0.03,
        "240": 0.02,
        "720": 0.01,
    }

    # --- Stoploss ---
    # hyperopt will find the optimal value between -2% and -8%
    stoploss = -0.05

    # --- Custom stoploss enabled ---
    use_custom_stoploss = True

    # --- Trailing Stop ---
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.035
    trailing_only_offset_is_reached = True

    # --- Order type ---
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,        # Place stoploss on Binance directly
    }

    # --- Startup candles needed ---
    startup_candle_count: int = 200

    # --- Process only new candles ---
    process_only_new_candles = True

    # =========================================================================
    # HYPEROPT PARAMETERS
    # =========================================================================

    # Entry — EMA periods
    buy_ema_short = IntParameter(7, 15, default=9, space="buy", optimize=True)
    buy_ema_medium = IntParameter(15, 30, default=21, space="buy", optimize=True)
    buy_ema_long = IntParameter(40, 65, default=50, space="buy", optimize=True)

    # Entry — RSI thresholds
    buy_rsi_min = IntParameter(25, 50, default=35, space="buy", optimize=True)
    buy_rsi_max = IntParameter(50, 70, default=65, space="buy", optimize=True)

    # Entry — Volume filter
    buy_volume_factor = DecimalParameter(
        1.0, 2.5, default=1.3, decimals=1, space="buy", optimize=True
    )

    # Entry — Bollinger Band position (price below upper band ratio)
    buy_bb_upperband_ratio = DecimalParameter(
        0.95, 1.0, default=0.99, decimals=2, space="buy", optimize=True
    )

    # =========================================================================
    # INDICATORS
    # =========================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Calculate all indicators."""

        # --- Volume SMA ---
        dataframe["volume_sma"] = dataframe["volume"].rolling(window=20).mean()

        # --- EMA stack — all periods needed by hyperopt search spaces ---
        # buy_ema_short: 7-15, buy_ema_medium: 15-30, buy_ema_long: 40-65
        ema_periods = set(range(7, 16)) | set(range(15, 31)) | set(range(40, 66)) | {100, 200}
        for period in sorted(ema_periods):
            dataframe[f"ema_{period}"] = pta.ema(dataframe["close"], length=period)

        # --- RSI ---
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=14)

        # --- Bollinger Bands (20, 2.0) ---
        # Column names vary by pandas_ta version; use positional access (lower, mid, upper)
        bb = pta.bbands(dataframe["close"], length=20, std=2.0)
        if bb is not None:
            dataframe["bb_lower"] = bb.iloc[:, 0]
            dataframe["bb_mid"] = bb.iloc[:, 1]
            dataframe["bb_upper"] = bb.iloc[:, 2]

        # --- MACD (12, 26, 9) ---
        macd = pta.macd(dataframe["close"], fast=12, slow=26, signal=9)
        if macd is not None:
            dataframe["macd_hist"] = macd["MACDh_12_26_9"]

        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """4h context: macro trend filter.
        Column names here are renamed by the decorator: {column}_4h in the main dataframe.
        So 'ema_21' here → 'ema_21_4h', 'is_bull' here → 'is_bull_4h'.
        """
        dataframe["ema_21"] = pta.ema(dataframe["close"], length=21)
        dataframe["ema_50"] = pta.ema(dataframe["close"], length=50)
        dataframe["ema_200"] = pta.ema(dataframe["close"], length=200)
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=14)
        # Bull filter: EMA50>EMA200 AND price>EMA21 AND RSI above neutral
        dataframe["is_bull"] = (
            (dataframe["ema_50"] > dataframe["ema_200"])
            & (dataframe["close"] > dataframe["ema_21"])
            & (dataframe["rsi"] > 45)
        )
        return dataframe

    # =========================================================================
    # ENTRY CONDITIONS
    # =========================================================================

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Entry conditions — all must be True simultaneously:
        1. Macro uptrend: price > EMA200 on 1h
        2. Medium bullish structure: EMA21 > EMA50
        3. 4h trend is bullish (EMA50_4h > EMA200_4h)
        4. RSI in optimal zone (not overbought, has momentum)
        5. Price > EMA9 (micro-trend positive)
        6. Volume spike (conviction)
        7. MACD bullish (histogram positive)
        8. Price below 99% of BB upper (not overextended)
        """
        conditions = []

        # 1. Macro trend: price above EMA200
        conditions.append(dataframe["close"] > dataframe["ema_200"])

        # 2. Medium-term structure: EMA21 > EMA50
        conditions.append(
            dataframe[f"ema_{self.buy_ema_medium.value}"]
            > dataframe[f"ema_{self.buy_ema_long.value}"]
        )

        # 3. v2.0: stronger 4h macro trend filter
        conditions.append(dataframe["is_bull_4h"].astype(bool))

        # 4. RSI in optimal buy zone
        conditions.append(dataframe["rsi"] >= self.buy_rsi_min.value)
        conditions.append(dataframe["rsi"] <= self.buy_rsi_max.value)

        # 5. Price above short EMA (micro trend up)
        conditions.append(
            dataframe["close"] > dataframe[f"ema_{self.buy_ema_short.value}"]
        )

        # 6. Volume above SMA (conviction)
        conditions.append(
            dataframe["volume"] > dataframe["volume_sma"] * self.buy_volume_factor.value
        )

        # 7. MACD bullish — histogram positive
        conditions.append(dataframe["macd_hist"] > 0)

        # 8. Not overextended — price below upper BB threshold
        conditions.append(
            dataframe["close"] < dataframe["bb_upper"] * self.buy_bb_upperband_ratio.value
        )

        # 9. Safety: no NaN values in key indicators
        conditions.append(dataframe["ema_200"].notna())
        conditions.append(dataframe["rsi"].notna())
        conditions.append(dataframe["volume"] > 0)

        # Combine all conditions
        mask = reduce(lambda x, y: x & y, conditions)
        dataframe.loc[mask, "enter_long"] = 1
        dataframe.loc[mask, "enter_tag"] = "ema_rsi_macd_vol"

        return dataframe

    # =========================================================================
    # EXIT CONDITIONS
    # =========================================================================

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        v2.0 exit logic:
        - Only exit when market regime turns bearish (4h filter broken)
        - This avoids cutting winners early while still protecting against trend reversals
        """
        # Exit when 4h macro trend turns bearish (bull conditions no longer hold)
        bear_mask = ~dataframe["is_bull_4h"].astype(bool)
        dataframe.loc[bear_mask, "exit_long"] = 1
        dataframe.loc[bear_mask, "exit_tag"] = "trend_reversal_exit"

        return dataframe

    # =========================================================================
    # CUSTOM STOPLOSS (optional dynamic stoploss based on ATR)
    # =========================================================================

    def custom_stoploss(
        self,
        pair: str,
        trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        """
        Dynamic stoploss: tighten stop at breakeven + 0.5% once trade is profitable.
        Falls back to static stoploss if not profitable enough.
        """
        # Once we're at 1.5% profit, move stoploss to breakeven + 0.3%
        if current_profit >= 0.015:
            return max(self.stoploss, -0.003)

        # Default: use the static stoploss
        return None

    # =========================================================================
    # CUSTOM EXIT (force exit on specific conditions)
    # =========================================================================

    def custom_exit(
        self,
        pair: str,
        trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        """
        Force exit if trade is stuck with small loss after 48h.
        Avoids tying up capital in dead trades.
        """
        trade_duration = (current_time - trade.open_date_utc).total_seconds() / 3600  # hours

        # If trade is losing and has been open more than 48h: cut it
        if trade_duration > 48 and current_profit < -0.01:
            return "timeout_exit_48h"

        # If trade is barely profitable after 72h: take it and move on
        if trade_duration > 72 and 0 < current_profit < 0.005:
            return "timeout_take_small_profit_72h"

        return None
