"""
kite/order_manager.py — Live order placement via Zerodha KiteConnect.

** STUB — intentionally raises NotImplementedError until paper trading
   confirms edge (positive win rate over 2+ weeks). **

When ready to go live:
1. Get KITE_API_KEY and KITE_API_SECRET from Zerodha app developer portal
2. Add to .env
3. Run `python -m kite.client --login` each morning to refresh access token
4. Uncomment the real implementation below

MIS = Margin Intraday Squareoff. Must close by 3:15 PM or Zerodha auto-squares at 3:20 PM.
"""

from __future__ import annotations

from loguru import logger


class OrderManager:
    """Live order placement via KiteConnect. Requires Kite API subscription."""

    def __init__(self):
        raise NotImplementedError(
            "Live orders are disabled until paper trading confirms alpha.\n"
            "Run `python -m kite.paper_trader` and collect 2+ weeks of data first.\n"
            "Once win rate > 40% on DIRECT_CALL signals, uncomment the live implementation."
        )

    async def place_mis_buy(
        self,
        ticker: str,
        quantity: int,
        order_type: str = "MARKET",
    ) -> str:
        """Place MIS BUY order. Returns order_id."""
        raise NotImplementedError

    async def place_mis_sell(
        self,
        ticker: str,
        quantity: int,
        order_type: str = "MARKET",
        trigger_price: float = 0.0,
    ) -> str:
        """Place MIS SELL order. Returns order_id."""
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    async def get_positions(self) -> list[dict]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# TODO: Live implementation (uncomment when paper validated)
# ---------------------------------------------------------------------------
#
# from kiteconnect import KiteConnect
# import os, asyncio
#
# class OrderManager:
#     def __init__(self):
#         api_key = os.environ["KITE_API_KEY"]
#         access_token = os.environ["KITE_ACCESS_TOKEN"]
#         self.kite = KiteConnect(api_key=api_key)
#         self.kite.set_access_token(access_token)
#
#     async def place_mis_buy(self, ticker, quantity, order_type="MARKET"):
#         loop = asyncio.get_event_loop()
#         def _place():
#             return self.kite.place_order(
#                 variety=self.kite.VARIETY_REGULAR,
#                 exchange=self.kite.EXCHANGE_NSE,
#                 tradingsymbol=ticker,
#                 transaction_type=self.kite.TRANSACTION_TYPE_BUY,
#                 quantity=quantity,
#                 product=self.kite.PRODUCT_MIS,
#                 order_type=self.kite.ORDER_TYPE_MARKET,
#             )
#         order_id = await loop.run_in_executor(None, _place)
#         logger.info("LIVE BUY order placed: {} x {} | order_id={}", ticker, quantity, order_id)
#         return order_id
#
#     async def place_mis_sell(self, ticker, quantity, order_type="MARKET", trigger_price=0.0):
#         loop = asyncio.get_event_loop()
#         def _place():
#             return self.kite.place_order(
#                 variety=self.kite.VARIETY_REGULAR,
#                 exchange=self.kite.EXCHANGE_NSE,
#                 tradingsymbol=ticker,
#                 transaction_type=self.kite.TRANSACTION_TYPE_SELL,
#                 quantity=quantity,
#                 product=self.kite.PRODUCT_MIS,
#                 order_type=self.kite.ORDER_TYPE_MARKET,
#             )
#         order_id = await loop.run_in_executor(None, _place)
#         logger.info("LIVE SELL order placed: {} x {} | order_id={}", ticker, quantity, order_id)
#         return order_id
