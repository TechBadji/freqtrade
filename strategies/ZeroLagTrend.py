# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort:skip_file

from datetime import datetime
from functools import reduce
from typing import Optional

import pandas_ta as pta
from pandas import DataFrame

from freqtrade.strategy import (
    DecimalParameter,
    IStrategy,
    IntParameter,
    informative,
    stoploss_from_open,
)
from freqtrade.persistence import Trade

# ============================================================================
# ZeroLagTrend Strategy — v5.0 ADAPTIVE INTELLIGENCE
# ============================================================================
# Version   : 2026.04.07 (Adaptive Intelligence)
# Budget    : $1,000 USDT (Binance Spot)
# Timeframe : 1h
#
# Nouveautés v5.0 :
#   🧠 Détection de régime : TRENDING / RANGING / VOLATILE
#   📉 Stop dynamique ATR : adapté à la volatilité réelle du marché
#   🔒 Lock progressif des profits par paliers (+1.5% → BE, etc.)
#   🎯 Exits multi-signaux : RSI peak, MACD collapse, EMA flip, timeouts
#   💰 Mise adaptée à la force du signal (50% / 75% / 100%)
#   🚫 Filtre anti-chaos : pas d'entrée si volatile ET sans tendance
# ============================================================================


class ZeroLagTrend(IStrategy):
    """
    ZeroLagTrend v5.0 — Adaptive Intelligence

    Le bot analyse le régime de marché à chaque bougie et adapte :
    - Son stop-loss (basé sur l'ATR réel, non fixe)
    - La taille de sa mise (selon la force du signal)
    - Ses sorties (multi-signaux intelligents)
    """

    INTERFACE_VERSION = 3
    timeframe = "1h"
    use_custom_stoploss = True

    @property
    def protections(self):
        return [
            # Attendre 2 bougies après chaque sortie avant de re-rentrer (même paire)
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 2
            },
            # Si 3 stop-loss en 24h sur tout le portefeuille → pause 6h
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 3,
                "stop_duration_candles": 6,
                "only_per_pair": False
            },
            # Si 2 stop-loss en 6h sur la même paire → pause 3h sur cette paire
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 6,
                "trade_limit": 2,
                "stop_duration_candles": 3,
                "only_per_pair": True
            },
            # Si drawdown > 10% en 48h → pause 12h (protection compte)
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 1,
                "stop_duration_candles": 12,
                "max_allowed_drawdown": 0.10
            }
        ]

    # --- ROI table (laisser courir les trades, le custom_stoploss gère) ---
    minimal_roi = {
        "0":    0.10,   # Cible max 10%
        "120":  0.05,   # 5% après 2h
        "480":  0.025,  # 2.5% après 8h
        "1440": 0.01,   # 1% après 24h (cleanup des trades bloqués)
    }

    # --- Stop absolu de sécurité (filet de dernier recours) ---
    stoploss = -0.05

    # --- Trailing Stop (backup si custom_stoploss retourne -1) ---
    trailing_stop = True
    trailing_stop_positive = 0.012         # Trailing à 1.2% du prix courant
    trailing_stop_positive_offset = 0.025  # S'active dès +2.5% de profit
    trailing_only_offset_is_reached = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": True,
    }

    startup_candle_count: int = 200
    process_only_new_candles = True

    # =========================================================================
    # HYPEROPT PARAMETERS
    # =========================================================================

    buy_rsi_min       = IntParameter(25, 50, default=30, space="buy", optimize=True)
    buy_rsi_max       = IntParameter(60, 80, default=72, space="buy", optimize=True)
    buy_adx_min       = IntParameter(15, 35, default=18, space="buy", optimize=True)
    buy_volume_factor = DecimalParameter(0.9, 1.5, default=1.0, decimals=2, space="buy", optimize=True)
    buy_bb_ratio      = DecimalParameter(0.95, 1.05, default=1.02, decimals=2, space="buy", optimize=True)

    # Multiplicateur ATR pour le stop dynamique
    atr_stop_mult = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space="sell", optimize=True)

    # =========================================================================
    # INDICATORS
    # =========================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # --- Volume ---
        dataframe["volume_sma"] = dataframe["volume"].rolling(window=20).mean()

        # --- EMAs ---
        for period in [9, 21, 50, 100, 200]:
            dataframe[f"ema_{period}"] = pta.ema(dataframe["close"], length=period)

        # --- RSI ---
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=14)

        # --- Bollinger Bands ---
        bb = pta.bbands(dataframe["close"], length=20, std=2.0)
        dataframe["bb_upper"] = bb.iloc[:, 2]
        dataframe["bb_lower"] = bb.iloc[:, 0]
        dataframe["bb_mid"]   = bb.iloc[:, 1]
        # BB Width = mesure de volatilité normalisée
        dataframe["bb_width"] = (
            (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_mid"]
        )

        # --- MACD ---
        macd = pta.macd(dataframe["close"])
        dataframe["macd"]          = macd.iloc[:, 0]
        dataframe["macd_signal"]   = macd.iloc[:, 1]
        dataframe["macd_hist"]     = macd.iloc[:, 2]
        dataframe["macd_hist_prev"] = dataframe["macd_hist"].shift(1)

        # --- ADX (Force de tendance) ---
        adx_df = pta.adx(dataframe["high"], dataframe["low"], dataframe["close"], length=14)
        dataframe["adx"]       = adx_df.iloc[:, 0]
        dataframe["adx_plus"]  = adx_df.iloc[:, 1]
        dataframe["adx_minus"] = adx_df.iloc[:, 2]

        # --- ATR (Volatilité absolue → base du stop dynamique) ---
        dataframe["atr"]     = pta.atr(dataframe["high"], dataframe["low"], dataframe["close"], length=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]  # ATR en % du prix

        # --- RÉGIME DE MARCHÉ ---
        emas_aligned = (
            (dataframe["ema_9"]  > dataframe["ema_21"]) &
            (dataframe["ema_21"] > dataframe["ema_50"])
        )
        # TRENDING  : Tendance claire (ADX > 25 + EMAs alignées)
        dataframe["regime_trending"] = (dataframe["adx"] > 25) & emas_aligned
        # VOLATILE  : Forte dispersion des prix (BB Width > 7%)
        dataframe["regime_volatile"] = dataframe["bb_width"] > 0.07
        # RANGING   : Pas de tendance (ADX < 20)
        dataframe["regime_ranging"]  = dataframe["adx"] < 20

        # --- SCORE DE MOMENTUM (0 à 4) — sert à calibrer la mise ---
        dataframe["momentum_score"] = (
            (dataframe["macd_hist"] > 0).astype(int) +
            (dataframe["macd_hist"] > dataframe["macd_hist_prev"]).astype(int) +
            (dataframe["rsi"] > 50).astype(int) +
            (dataframe["adx"] > 22).astype(int)
        )

        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_200"] = pta.ema(dataframe["close"], length=200)
        dataframe["ema_50"]  = pta.ema(dataframe["close"], length=50)

        # Macro bull simple : au-dessus de l'EMA200
        dataframe["is_bull"] = dataframe["close"] > dataframe["ema_200"]
        # Macro bull fort : EMA50 au-dessus de EMA200 (tendance longue durée)
        dataframe["is_strong_bull"] = (
            (dataframe["close"] > dataframe["ema_200"]) &
            (dataframe["ema_50"] > dataframe["ema_200"])
        )
        macd_4h = pta.macd(dataframe["close"])
        dataframe["macd_hist_4h"]      = macd_4h.iloc[:, 2]
        dataframe["macd_hist_4h_prev"] = dataframe["macd_hist_4h"].shift(1)

        return dataframe

    # =========================================================================
    # ENTRY CONDITIONS — Conscience du Régime
    # =========================================================================

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []

        # 1. Filtre 4h : bull OU MACD 4h en reprise (permet entrées en early recovery)
        macro_ok = (
            dataframe["is_bull_4h"].astype(bool) |
            (dataframe["macd_hist_4h"].astype(float) > dataframe["macd_hist_4h_prev"].astype(float))
        )
        conditions.append(macro_ok)

        # 2. Prix au-dessus EMA200 (1h)
        conditions.append(dataframe["close"] > dataframe["ema_200"])

        # 3. Micro tendance haussière (EMA9 > EMA21)
        conditions.append(dataframe["ema_9"] > dataframe["ema_21"])

        # 4. RSI dans la zone de momentum (pas trop faible, pas suracheté)
        conditions.append(dataframe["rsi"] >= self.buy_rsi_min.value)
        conditions.append(dataframe["rsi"] <= self.buy_rsi_max.value)

        # 5. MACD positif (momentum haussier — pas obligatoirement croissant)
        conditions.append(dataframe["macd_hist"] > 0)

        # 6. Confirmation volume
        conditions.append(
            dataframe["volume"] > dataframe["volume_sma"] * self.buy_volume_factor.value
        )

        # 7. Prix pas en zone de surachat (BB upper)
        conditions.append(
            dataframe["close"] < dataframe["bb_upper"] * self.buy_bb_ratio.value
        )

        # 8. ADX minimum (éviter les marchés complètement plats)
        conditions.append(dataframe["adx"] >= self.buy_adx_min.value)

        # 9. FILTRE ANTI-CHAOS : Pas d'entrée si marché volatil ET sans tendance
        anti_chaos = ~(dataframe["regime_volatile"] & dataframe["regime_ranging"])
        conditions.append(anti_chaos)

        if conditions:
            mask = reduce(lambda x, y: x & y, conditions)
            dataframe.loc[mask, "enter_long"] = 1

            # Tag selon régime pour analyse post-trade
            dataframe.loc[mask & dataframe["regime_trending"],  "enter_tag"] = "trending_momentum_v5"
            dataframe.loc[mask & ~dataframe["regime_trending"], "enter_tag"] = "early_momentum_v5"

        return dataframe

    # =========================================================================
    # EXIT CONDITIONS — Multi-Signal
    # =========================================================================

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit 1 : Retournement macro (4h devient bearish)
        macro_exit = ~dataframe["is_bull_4h"].astype(bool)

        # Exit 2 : Croisement MACD bearish (perte de momentum)
        macd_cross = (
            (dataframe["macd_hist"] < 0) &
            (dataframe["macd_hist_prev"] >= 0)
        )

        dataframe.loc[macro_exit, "exit_long"] = 1
        dataframe.loc[macro_exit, "exit_tag"]  = "macro_reversal_4h"

        dataframe.loc[~macro_exit & macd_cross, "exit_long"] = 1
        dataframe.loc[~macro_exit & macd_cross, "exit_tag"]  = "macd_bearish_cross"

        return dataframe

    # =========================================================================
    # CUSTOM STOPLOSS — LE CERVEAU ADAPTATIF 🧠
    # =========================================================================

    def custom_stoploss(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, after_fill: bool, **kwargs,
    ) -> float:
        """
        Stop-loss intelligent en deux modes :

        MODE PROFIT → Lock progressif des gains :
          +1.5% → on passe au break-even
          +2.5% → on lock +0.8% depuis l'open
          +4.0% → on lock +2.0% depuis l'open
          +7.0% → on lock +4.0% depuis l'open

        MODE PERTE → Stop basé sur l'ATR (volatilité réelle) :
          Marché tendanciel : stop = -1.2× ATR (serré)
          Marché volatile   : stop = -1.8× ATR (large)
          Bornes : jamais pire que -5%, jamais plus serré que -1.2%
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return -1  # fallback: trailing stop par défaut

        last        = dataframe.iloc[-1].squeeze()
        atr_pct     = float(last.get("atr_pct", 0.02))
        is_trending = bool(last.get("regime_trending", False))
        is_volatile = bool(last.get("regime_volatile", False))

        # ── LOCK PROGRESSIF DES PROFITS ───────────────────────────────────────
        if current_profit >= 0.07:
            # +7% → lock +4% depuis l'open (trail serré)
            return stoploss_from_open(0.04, current_profit)

        if current_profit >= 0.04:
            # +4% → lock +2% depuis l'open
            return stoploss_from_open(0.02, current_profit)

        if current_profit >= 0.025:
            # +2.5% → lock +0.8% depuis l'open
            return stoploss_from_open(0.008, current_profit)

        if current_profit >= 0.015:
            # +1.5% → break-even (ne plus perdre d'argent)
            return stoploss_from_open(0.001, current_profit)

        # ── STOP DYNAMIQUE EN PERTE : ATR × MULTIPLICATEUR ───────────────────
        mult = self.atr_stop_mult.value

        if is_trending:
            # Marché en tendance → stop plus serré (ATR × 0.8)
            dynamic_stop = -(mult * 0.8 * atr_pct)
        elif is_volatile:
            # Marché volatile → stop plus large pour respirer (ATR × 1.2)
            dynamic_stop = -(mult * 1.2 * atr_pct)
        else:
            # Marché ranging/neutre → stop standard (ATR × 1.0)
            dynamic_stop = -(mult * atr_pct)

        # Bornes absolues de sécurité
        # Min: -5% (jamais pire), Max: -1.2% (jamais trop serré)
        return max(min(dynamic_stop, -0.012), -0.05)

    # =========================================================================
    # CUSTOM EXIT — Sorties Intelligentes Multi-Signaux 🎯
    # =========================================================================

    def custom_exit(
        self, pair: str, trade: Trade, current_time: datetime,
        current_rate: float, current_profit: float, **kwargs,
    ) -> Optional[str]:
        """
        Sorties intelligentes basées sur le contexte :
        1. RSI > 82 en profit  → sortir au sommet (overbought)
        2. MACD collapse        → sécuriser si en profit
        3. Trade mort 24h       → couper les trades stagnants
        4. Timeout 48h          → sortie forcée si toujours en perte
        5. Marché chaotique     → sortir si en profit
        6. EMA flip précoce     → limiter les petites pertes
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return None

        last = dataframe.iloc[-1].squeeze()
        trade_duration_h = (current_time - trade.open_date_utc).total_seconds() / 3600

        # 1. RSI Overbought → sortir au sommet de la vague
        if last["rsi"] > 82 and current_profit > 0.01:
            return "rsi_overbought_82"

        # 2. Effondrement MACD alors qu'on est en profit → sécuriser
        if current_profit > 0.018:
            if last["macd_hist"] < 0 and last["macd_hist_prev"] >= 0:
                return "macd_momentum_collapse"

        # 3. Trade mort depuis 24h (stagnant, légèrement négatif)
        if trade_duration_h > 24 and -0.015 < current_profit < 0.008:
            return "dead_trade_timeout_24h"

        # 4. Timeout forcé 48h si toujours en perte
        if trade_duration_h > 48 and current_profit < 0:
            return "force_exit_48h"

        # 5. Marché devenu chaotique (volatile + ranging) → sécuriser profits
        if last["regime_volatile"] and last["regime_ranging"]:
            if current_profit > 0:
                return "exit_chaotic_market_profit"
            elif current_profit < -0.01:
                return "exit_chaotic_market_loss"

        # 6. Retournement micro-tendance en profit modeste → limiter la casse
        if 0.005 < current_profit < 0.02:
            if last["ema_9"] < last["ema_21"]:
                return "ema_micro_reversal_early"

        return None

    # =========================================================================
    # CUSTOM STAKE — Mise Adaptée à la Force du Signal 💰
    # =========================================================================

    def custom_stake_amount(
        self, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: Optional[float],
        max_stake: float, leverage: float, entry_tag: Optional[str],
        side: str, **kwargs,
    ) -> float:
        """
        Adapte la mise en fonction de la qualité du signal et du régime :

        Signal FORT  (trending + score 3-4) → 100% de la mise proposée
        Signal MOYEN (score 2)               → 75%
        Signal FAIBLE (score 0-1)            → 50%
        Pénalité VOLATILITÉ                  → ×0.75 appliqué en plus
        """
        try:
            pair = kwargs.get("pair", "")
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or df.empty:
                return proposed_stake

            last        = df.iloc[-1].squeeze()
            score       = int(last.get("momentum_score", 2))
            is_trending = bool(last.get("regime_trending", False))
            is_volatile = bool(last.get("regime_volatile", False))

            # Calcul du multiplicateur selon la force du signal
            if is_trending and score >= 3:
                multiplier = 1.0    # Signal fort → mise complète
            elif score == 2:
                multiplier = 0.75   # Signal moyen → 75%
            else:
                multiplier = 0.5    # Signal faible → 50%

            # Pénalité si marché trop volatil
            if is_volatile:
                multiplier *= 0.75

            stake = proposed_stake * multiplier
            return max(stake, min_stake or 10.0)

        except Exception:
            return proposed_stake
