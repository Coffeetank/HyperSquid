import time
from typing import Dict, List, Optional, Union, Any
from hyperliquid.info import Info
from hyperliquid.utils import constants
import eth_account

class Tracker:
    def __init__(
        self,
        wallet_or_address: Union[eth_account.Account, str],
        network: str = 'mainnet',
        skip_ws: bool = True
    ):
        # Determine network URL
        if network.lower() == 'mainnet':
            self.api_url = constants.MAINNET_API_URL
        elif network.lower() == 'testnet':
            self.api_url = constants.TESTNET_API_URL
        else:
            raise ValueError("Network must be 'mainnet' or 'testnet'")

        # Initialize Info client for data retrieval
        self.info = Info(self.api_url, skip_ws=skip_ws)

        # Store wallet/address
        if isinstance(wallet_or_address, eth_account.Account):
            self.wallet = wallet_or_address
            self.address = wallet_or_address.address
        elif isinstance(wallet_or_address, str):
            self.wallet = None
            self.address = wallet_or_address
        else:
            raise ValueError("wallet_or_address must be an eth_account.Account or string address")

        # Cache for performance
        self._last_update = 0
        self._cache_timeout = 30  # seconds
        self._cached_user_state = None

    def _get_user_state(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Get user state with caching to avoid excessive API calls.

        Args:
            force_refresh: Force refresh of cached data

        Returns:
            User state dictionary containing positions, margin, etc.
        """
        current_time = time.time()

        if not force_refresh and self._cached_user_state and \
           (current_time - self._last_update) < self._cache_timeout:
            return self._cached_user_state

        try:
            user_state = self.info.user_state(self.address)
            self._cached_user_state = user_state
            self._last_update = current_time
            return user_state
        except Exception as e:
            raise RuntimeError(f"Failed to fetch user state: {e}")

    def get_current_pnl(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Get current profit and loss information.

        Args:
            force_refresh: Force refresh of data instead of using cache

        Returns:
            Dictionary containing PNL information including:
            - total_unrealized_pnl: Total unrealized P&L across all positions
            - positions_pnl: P&L breakdown by position
            - account_value: Current account value
        """
        user_state = self._get_user_state(force_refresh)

        total_unrealized_pnl = 0.0
        positions_pnl = []

        # Calculate P&L from positions
        for asset_position in user_state.get("assetPositions", []):
            position = asset_position["position"]
            coin = position["coin"]
            unrealized_pnl = float(position.get("unrealizedPnl", 0))

            total_unrealized_pnl += unrealized_pnl
            positions_pnl.append({
                "coin": coin,
                "unrealized_pnl": unrealized_pnl,
                "size": float(position.get("szi", 0)),
                "entry_price": float(position.get("entryPx", 0)),
                "leverage": position.get("leverage", {})
            })

        margin_summary = user_state.get("marginSummary", {})
        account_value = float(margin_summary.get("accountValue", 0))

        return {
            "total_unrealized_pnl": total_unrealized_pnl,
            "positions_pnl": positions_pnl,
            "account_value": account_value,
            "timestamp": time.time()
        }

    def get_open_positions(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get all open positions.

        Args:
            force_refresh: Force refresh of data instead of using cache

        Returns:
            List of open position dictionaries with details
        """
        user_state = self._get_user_state(force_refresh)

        positions = []
        for asset_position in user_state.get("assetPositions", []):
            position = asset_position["position"]
            positions.append({
                "coin": position["coin"],
                "size": float(position.get("szi", 0)),
                "entry_price": float(position.get("entryPx", 0)),
                "unrealized_pnl": float(position.get("unrealizedPnl", 0)),
                "leverage": position.get("leverage", {}),
                "liquidation_price": position.get("liquidationPx"),
                "margin_used": float(position.get("marginUsed", 0))
            })

        return positions

    def get_transaction_history(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get transaction history (fills).

        Args:
            start_time: Start time in milliseconds (optional)
            end_time: End time in milliseconds (optional, defaults to now)
            limit: Maximum number of transactions to return (optional)

        Returns:
            List of transaction dictionaries with fill details
        """
        try:
            if start_time and end_time:
                fills = self.info.user_fills_by_time(
                    self.address,
                    start_time,
                    end_time,
                    aggregate_by_time=False
                )
            else:
                fills = self.info.user_fills(self.address)

            # Sort by time (most recent first) and apply limit
            fills.sort(key=lambda x: x.get("time", 0), reverse=True)

            if limit:
                fills = fills[:limit]

            # Format transactions
            transactions = []
            for fill in fills:
                transactions.append({
                    "coin": fill["coin"],
                    "side": fill["side"],
                    "price": float(fill["px"]),
                    "size": float(fill["sz"]),
                    "time": fill["time"],
                    "closed_pnl": float(fill.get("closedPnl", 0)),
                    "fee": float(fill.get("fee", 0)),
                    "tid": fill.get("tid"),
                    "oid": fill.get("oid")
                })

            return transactions

        except Exception as e:
            raise RuntimeError(f"Failed to fetch transaction history: {e}")

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all current open orders.

        Returns:
            List of open order dictionaries with order details
        """
        try:
            orders = self.info.open_orders(self.address)

            formatted_orders = []
            for order in orders:
                formatted_orders.append({
                    "coin": order["coin"],
                    "side": order["side"],
                    "size": float(order["sz"]),
                    "price": float(order["limitPx"]),
                    "order_id": order["oid"],
                    "timestamp": order["timestamp"],
                    "reduce_only": order.get("reduceOnly", False),
                    "order_type": order.get("orderType", {})
                })

            return formatted_orders

        except Exception as e:
            raise RuntimeError(f"Failed to fetch open orders: {e}")

    def get_trading_summary(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Get comprehensive trading summary including PNL, positions, orders, and recent transactions.

        Args:
            force_refresh: Force refresh of all data

        Returns:
            Dictionary containing complete trading status
        """
        try:
            pnl_data = self.get_current_pnl(force_refresh)
            positions = self.get_open_positions(force_refresh)
            open_orders = self.get_open_orders()
            recent_transactions = self.get_transaction_history(limit=10)

            return {
                "pnl": pnl_data,
                "positions": positions,
                "open_orders": open_orders,
                "recent_transactions": recent_transactions,
                "timestamp": time.time()
            }

        except Exception as e:
            raise RuntimeError(f"Failed to get trading summary: {e}")

    def clear_cache(self):
        """Clear cached data to force fresh API calls."""
        self._cached_user_state = None
        self._last_update = 0
