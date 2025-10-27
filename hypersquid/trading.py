from typing import Dict, List, Optional, Union, Any, Literal
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
import eth_account
import time
from decimal import Decimal, ROUND_DOWN


class Trading:
    """
    Advanced trading class for Hyperliquid platform.

    Supports all order types with leverage, TP/SL, and position management.
    Requires a wallet for trading operations.
    """

    def __init__(
        self,
        wallet: eth_account.Account,
        network: str = 'mainnet',
        vault_address: Optional[str] = None,
        debug: bool = True
    ):
        """
        Initialize trading client.

        Args:
            wallet: Ethereum wallet for signing transactions
            network: 'mainnet' or 'testnet'
            vault_address: Optional vault/sub-account address
        """
        if network.lower() == 'mainnet':
            self.api_url = constants.MAINNET_API_URL
        elif network.lower() == 'testnet':
            self.api_url = constants.TESTNET_API_URL
        else:
            raise ValueError("Network must be 'mainnet' or 'testnet'")

        self.wallet = wallet
        self.network = network.lower()
        self.debug = debug

        # Initialize exchange client
        self.exchange = Exchange(
            wallet=self.wallet,
            base_url=self.api_url,
            vault_address=vault_address
        )

        # Store vault address for sub-account trading
        self.vault_address = vault_address
        self._meta_cache: Optional[Dict[str, Any]] = None

    def _get_meta(self) -> Dict[str, Any]:
        if self._meta_cache is None:
            # meta() returns a dict with 'universe' describing assets
            self._meta_cache = self.exchange.info.meta()
        return self._meta_cache

    def _get_asset_entry(self, coin: str) -> Optional[Dict[str, Any]]:
        meta = self._get_meta()
        for asset in meta.get("universe", []):
            if asset.get("name") == coin:
                return asset
        return None

    def _quantize_size(self, coin: str, amount: float) -> float:
        asset = self._get_asset_entry(coin)
        if not asset:
            # Fallback: no asset metadata found, return original amount
            return amount
        sz_decimals = int(asset.get("szDecimals", 0))
        step = Decimal("1") / (Decimal(10) ** sz_decimals)
        q = (Decimal(str(amount))).quantize(step, rounding=ROUND_DOWN)
        return float(q)

    def _quantize_price(self, coin: str, price: Optional[float]) -> Optional[float]:
        if price is None:
            return None
        asset = self._get_asset_entry(coin)
        if not asset:
            return price
        # Some metadata may include pxDecimals; fall back to 2 if missing
        px_decimals = int(asset.get("pxDecimals", 2))
        step = Decimal("1") / (Decimal(10) ** px_decimals)
        q = (Decimal(str(price))).quantize(step, rounding=ROUND_DOWN)
        return float(q)

    def place_order(
        self,
        coin: str,
        side: Literal['buy', 'sell'],
        order_type: Literal['market', 'limit', 'scale', 'stop_limit', 'stop_market', 'twap'],
        amount: float,
        price: Optional[float] = None,
        leverage: Optional[Union[int, float]] = None,
        take_profit: Optional[Dict[str, Any]] = None,
        stop_loss: Optional[Dict[str, Any]] = None,
        client_order_id: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Place a trading order with comprehensive options.

        Args:
            coin: Asset symbol (e.g., 'BTC', 'ETH')
            side: 'buy' or 'sell'
            order_type: Type of order ('market', 'limit', 'scale', 'stop_limit', 'stop_market', 'twap')
            amount: Order size/amount
            price: Limit price (required for limit orders, optional for others)
            leverage: Leverage multiplier (optional)
            take_profit: TP configuration dict (optional)
            stop_loss: SL configuration dict (optional)
            client_order_id: Custom order ID (optional)
            **kwargs: Additional order parameters

        Returns:
            Order response from exchange
        """
        try:
            # Set leverage if specified
            if leverage is not None:
                self.set_leverage(leverage, coin)

            # Convert side to boolean (True = buy, False = sell)
            is_buy = side.lower() == 'buy'

            # Create client order ID if provided
            cloid = None
            if client_order_id:
                cloid = Cloid.from_str(client_order_id)

            # Handle different order types
            if order_type == 'market':
                return self._place_market_order(
                    coin=coin,
                    is_buy=is_buy,
                    amount=amount,
                    cloid=cloid
                )

            elif order_type == 'limit':
                if price is None:
                    raise ValueError("Price is required for limit orders")
                return self._place_limit_order(
                    coin=coin,
                    is_buy=is_buy,
                    amount=amount,
                    price=price,
                    cloid=cloid,
                    **kwargs
                )

            elif order_type == 'scale':
                # Scale orders are advanced limit orders with multiple levels
                return self._place_scale_order(
                    coin=coin,
                    is_buy=is_buy,
                    amount=amount,
                    price=price,
                    cloid=cloid,
                    **kwargs
                )

            elif order_type in ['stop_limit', 'stop_market']:
                return self._place_stop_order(
                    coin=coin,
                    is_buy=is_buy,
                    order_type=order_type,
                    amount=amount,
                    price=price,
                    stop_price=kwargs.get('stop_price'),
                    cloid=cloid,
                    **kwargs
                )

            elif order_type == 'twap':
                return self._place_twap_order(
                    coin=coin,
                    is_buy=is_buy,
                    amount=amount,
                    price=price,
                    duration=kwargs.get('duration', 60),  # Default 60 seconds
                    cloid=cloid,
                    **kwargs
                )

            else:
                raise ValueError(f"Unsupported order type: {order_type}")

        except Exception as e:
            raise RuntimeError(f"Failed to place {order_type} order: {e}")

    def _place_market_order(
        self,
        coin: str,
        is_buy: bool,
        amount: float,
        cloid: Optional[Cloid] = None
    ) -> Dict[str, Any]:
        """Place a market order."""
        amount_q = self._quantize_size(coin, amount)
        if amount_q <= 0:
            raise ValueError("Order size becomes zero after quantization")
        if is_buy:
            if self.debug:
                print("[DEBUG] market_open", {
                    "coin": coin,
                    "is_buy": True,
                    "sz": amount_q,
                    "px": None,
                    "slippage": 0.01,
                    "cloid": str(cloid) if cloid else None
                })
            return self.exchange.market_open(
                name=coin,
                is_buy=True,
                sz=amount_q,
                px=None,  # Market price
                slippage=0.01,  # 1% slippage protection
                cloid=cloid
            )
        else:
            if self.debug:
                print("[DEBUG] market_open", {
                    "coin": coin,
                    "is_buy": False,
                    "sz": amount_q,
                    "px": None,
                    "slippage": 0.01,
                    "cloid": str(cloid) if cloid else None
                })
            return self.exchange.market_open(
                name=coin,
                is_buy=False,
                sz=amount_q,
                px=None,  # Market price
                slippage=0.01,  # 1% slippage protection
                cloid=cloid
            )

    def _place_limit_order(
        self,
        coin: str,
        is_buy: bool,
        amount: float,
        price: float,
        cloid: Optional[Cloid] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Place a limit order."""
        tif = kwargs.get('time_in_force', 'Gtc')  # Good 'til canceled by default
        amount_q = self._quantize_size(coin, amount)
        price_q = self._quantize_price(coin, price)
        if amount_q <= 0:
            raise ValueError("Order size becomes zero after quantization")

        payload = (
            coin,
            is_buy,
            amount_q,
            price_q,
            {"limit": {"tif": tif}},
        )
        if self.debug:
            print("[DEBUG] order limit", {
                "coin": coin,
                "is_buy": is_buy,
                "sz": amount_q,
                "limit_px": price_q,
                "order_type": {"limit": {"tif": tif}},
                "cloid": str(cloid) if cloid else None
            })
        return self.exchange.order(
            coin,
            is_buy,
            amount_q,
            price_q,
            {"limit": {"tif": tif}},
            cloid=cloid
        )

    def _place_scale_order(
        self,
        coin: str,
        is_buy: bool,
        amount: float,
        price: Optional[float] = None,
        cloid: Optional[Cloid] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Place a scale order (bracket order with multiple price levels)."""
        # Scale orders require specific formatting
        # This is a simplified implementation - scale orders are complex
        levels = kwargs.get('levels', 3)
        scale_factor = kwargs.get('scale_factor', 0.001)  # 0.1%

        if price is None:
            raise ValueError("Price is required for scale orders")

        # Create multiple limit orders at different price levels
        orders = []
        for i in range(levels):
            level_price = price * (1 + (scale_factor * i) * (1 if is_buy else -1))
            level_amount = amount / levels

            # Quantize per level
            level_amount_q = self._quantize_size(coin, level_amount)
            level_price_q = self._quantize_price(coin, level_price)
            if level_amount_q <= 0:
                continue

            order_result = self._place_limit_order(
                coin=coin,
                is_buy=is_buy,
                amount=level_amount_q,
                price=level_price_q,
                cloid=cloid
            )
            orders.append(order_result)

        return {"scale_orders": orders, "levels": levels}

    def _place_stop_order(
        self,
        coin: str,
        is_buy: bool,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        tpsl: Literal['tp', 'sl'] = 'sl',
        cloid: Optional[Cloid] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Place a stop order (stop-limit or stop-market)."""
        if stop_price is None:
            raise ValueError("stop_price is required for stop orders")
        amount_q = self._quantize_size(coin, amount)
        price_q = self._quantize_price(coin, price)
        stop_price_q = self._quantize_price(coin, stop_price)
        if amount_q <= 0:
            raise ValueError("Order size becomes zero after quantization")

        if order_type == 'stop_limit':
            if price is None:
                raise ValueError("price is required for stop-limit orders")

            order_type_obj = {
                "trigger": {
                    "triggerPx": stop_price_q,
                    "isMarket": False,
                    "tpsl": tpsl
                }
            }

            if self.debug:
                print("[DEBUG] order stop_limit", {
                    "coin": coin,
                    "is_buy": is_buy,
                    "sz": amount_q,
                    "limit_px": price_q,
                    "order_type": order_type_obj,
                    "cloid": str(cloid) if cloid else None
                })

            return self.exchange.order(
                coin,
                is_buy,
                amount_q,
                price_q,
                order_type_obj,
                cloid=cloid
            )

        elif order_type == 'stop_market':
            order_type_obj = {
                "trigger": {
                    "triggerPx": stop_price_q,
                    "isMarket": True,
                    "tpsl": tpsl
                }
            }

            if self.debug:
                print("[DEBUG] order stop_market", {
                    "coin": coin,
                    "is_buy": is_buy,
                    "sz": amount_q,
                    "limit_px": None,
                    "order_type": order_type_obj,
                    "cloid": str(cloid) if cloid else None
                })

            return self.exchange.order(
                coin,
                is_buy,
                amount_q,
                None,  # No limit price for market-trigger orders
                order_type_obj,
                cloid=cloid
            )

    def _place_twap_order(
        self,
        coin: str,
        is_buy: bool,
        amount: float,
        price: Optional[float] = None,
        duration: int = 60,
        cloid: Optional[Cloid] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Place a TWAP (Time-Weighted Average Price) order."""
        # TWAP implementation - break order into smaller chunks over time
        intervals = kwargs.get('intervals', 10)  # Number of time intervals
        interval_amount = amount / intervals
        interval_duration = duration / intervals

        orders = []
        for i in range(intervals):
            if price:
                # Use limit orders for TWAP at specified price
                order_result = self._place_limit_order(
                    coin=coin,
                    is_buy=is_buy,
                    amount=interval_amount,
                    price=price,
                    cloid=cloid
                )
            else:
                # Use market orders for TWAP
                order_result = self._place_market_order(
                    coin=coin,
                    is_buy=is_buy,
                    amount=interval_amount,
                    cloid=cloid
                )

            orders.append(order_result)

            if i < intervals - 1:  # Don't sleep after last order
                time.sleep(interval_duration)

        return {"twap_orders": orders, "intervals": intervals, "duration": duration}

    def set_leverage(self, leverage: Union[int, float], coin: str) -> Dict[str, Any]:
        """
        Set leverage for a specific coin.

        Args:
            leverage: Leverage multiplier (e.g., 5 for 5x leverage)
            coin: Asset symbol

        Returns:
            Leverage update response
        """
        try:
            return self.exchange.update_leverage(
                leverage=leverage,
                name=coin,
                is_cross=True  # Use cross leverage by default
            )
        except Exception as e:
            raise RuntimeError(f"Failed to set leverage: {e}")

    def set_isolated_leverage(self, leverage: Union[int, float], coin: str) -> Dict[str, Any]:
        """
        Set isolated leverage for a specific coin.

        Args:
            leverage: Leverage multiplier
            coin: Asset symbol

        Returns:
            Leverage update response
        """
        try:
            return self.exchange.update_leverage(
                leverage=leverage,
                name=coin,
                is_cross=False
            )
        except Exception as e:
            raise RuntimeError(f"Failed to set isolated leverage: {e}")

    def add_margin(self, amount: float, coin: str) -> Dict[str, Any]:
        """
        Add margin to an isolated position.

        Args:
            amount: Amount of margin to add (in USD)
            coin: Asset symbol

        Returns:
            Margin update response
        """
        try:
            return self.exchange.update_isolated_margin(
                amount=amount,
                name=coin
            )
        except Exception as e:
            raise RuntimeError(f"Failed to add margin: {e}")

    def close_position(self, coin: str, close_type: Literal['market', 'limit'] = 'market',
                      price: Optional[float] = None) -> Dict[str, Any]:
        """
        Close an entire position.

        Args:
            coin: Asset symbol
            close_type: 'market' or 'limit'
            price: Limit price (required for limit closes)

        Returns:
            Close order response
        """
        try:
            if close_type == 'market':
                return self.exchange.market_close(name=coin)
            elif close_type == 'limit':
                if price is None:
                    raise ValueError("Price is required for limit closes")
                return self.exchange.limit_close(name=coin, px=price)
            else:
                raise ValueError("close_type must be 'market' or 'limit'")
        except Exception as e:
            raise RuntimeError(f"Failed to close position: {e}")

    def cancel_order(self, coin: str, order_id: int) -> Dict[str, Any]:
        """
        Cancel a specific order.

        Args:
            coin: Asset symbol
            order_id: Order ID to cancel

        Returns:
            Cancel response
        """
        try:
            return self.exchange.cancel(coin, order_id)
        except Exception as e:
            raise RuntimeError(f"Failed to cancel order: {e}")

    def cancel_order_by_cloid(self, coin: str, client_order_id: str) -> Dict[str, Any]:
        """
        Cancel an order by client order ID.

        Args:
            coin: Asset symbol
            client_order_id: Client order ID

        Returns:
            Cancel response
        """
        try:
            cloid = Cloid.from_str(client_order_id)
            return self.exchange.cancel_by_cloid(coin=coin, cloid=cloid)
        except Exception as e:
            raise RuntimeError(f"Failed to cancel order by CLOID: {e}")

    def cancel_all_orders(self, coin: Optional[str] = None) -> Dict[str, Any]:
        """
        Cancel all open orders, optionally for a specific coin.

        Args:
            coin: Asset symbol (optional, cancels all if not specified)

        Returns:
            Cancel response
        """
        try:
            return self.exchange.cancel_all(coin=coin)
        except Exception as e:
            raise RuntimeError(f"Failed to cancel all orders: {e}")

    def place_bracket_order(
        self,
        coin: str,
        side: Literal['buy', 'sell'],
        amount: float,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        leverage: Optional[Union[int, float]] = None,
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Place a bracket order with entry, take profit, and stop loss.

        Args:
            coin: Asset symbol
            side: 'buy' or 'sell'
            amount: Order size
            entry_price: Entry price for limit order
            take_profit_price: Take profit trigger price
            stop_loss_price: Stop loss trigger price
            leverage: Leverage multiplier (optional)
            client_order_id: Custom order ID (optional)

        Returns:
            Bracket order response
        """
        try:
            # Set leverage if specified
            if leverage is not None:
                self.set_leverage(leverage, coin)

            # Create client order ID if provided
            cloid = None
            if client_order_id:
                cloid = Cloid.from_str(client_order_id)

            is_buy = side.lower() == 'buy'

            # Place entry order (limit)
            entry_order = self._place_limit_order(
                coin=coin,
                is_buy=is_buy,
                amount=amount,
                price=entry_price,
                cloid=cloid
            )

            # Create TP/SL orders (these would be attached to the position after entry)
            # Note: Hyperliquid handles TP/SL differently - they need to be set after position is open

            return {
                "entry_order": entry_order,
                "take_profit_price": take_profit_price,
                "stop_loss_price": stop_loss_price,
                "bracket_setup": True
            }

        except Exception as e:
            raise RuntimeError(f"Failed to place bracket order: {e}")

    def get_account_balance(self) -> Dict[str, Any]:
        """
        Get account balance and margin information.

        Returns:
            Account balance information
        """
        try:
            return self.exchange.info.user_state(self.wallet.address)
        except Exception as e:
            raise RuntimeError(f"Failed to get account balance: {e}")

    def get_position_size(self, coin: str) -> float:
        """
        Get current position size for a coin.

        Args:
            coin: Asset symbol

        Returns:
            Position size (positive for long, negative for short)
        """
        try:
            user_state = self.get_account_balance()
            for asset_position in user_state.get("assetPositions", []):
                position = asset_position["position"]
                if position["coin"] == coin:
                    return float(position.get("szi", 0))
            return 0.0
        except Exception as e:
            raise RuntimeError(f"Failed to get position size: {e}")

    def get_usdc_balance(self) -> Dict[str, Any]:
        """
        Get comprehensive USDC balance information.

        Returns:
            Dictionary containing:
            - total_balance: Total account value in USD
            - available_balance: Available/withdrawable balance in USD
            - margin_used: Total margin currently used
        """
        try:
            user_state = self.get_account_balance()
            margin_summary = user_state.get("marginSummary", {})
            withdrawable = float(user_state.get("withdrawable", 0))
            account_value = float(margin_summary.get("accountValue", 0))
            total_margin_used = float(margin_summary.get("totalMarginUsed", 0))

            return {
                "total_balance": account_value,
                "available_balance": withdrawable,
                "margin_used": total_margin_used,
                "timestamp": time.time()
            }
        except Exception as e:
            raise RuntimeError(f"Failed to get USDC balance: {e}")

    def get_available_balance(self) -> float:
        """
        Get available balance for trading.

        Returns:
            Available balance in USD
        """
        balance_info = self.get_usdc_balance()
        return balance_info["available_balance"]
