import time
from typing import Dict, List, Optional, Any
from hyperliquid.info import Info
from hyperliquid.utils import constants
from .trading import Trading
from .trading_monitor import Tracker


class CopyTrader:
    def __init__(self, trader: Trading, source_address: str, network: str = 'mainnet', require_confirmation: bool = False):
        if network.lower() == 'mainnet':
            self.api_url = constants.MAINNET_API_URL
        elif network.lower() == 'testnet':
            self.api_url = constants.TESTNET_API_URL
        else:
            raise ValueError("Network must be 'mainnet' or 'testnet'")

        self.trader = trader
        self.source_address = source_address
        self.network = network
        self.info = Info(self.api_url, skip_ws=True)
        self.target_tracker = Tracker(trader.wallet.address, network)
        self.require_confirmation = require_confirmation

    def _positions_by_coin(self, user_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for ap in user_state.get("assetPositions", []):
            pos = ap["position"]
            coin = pos["coin"]
            result[coin] = pos
        return result

    def _get_tp_sl_orders_by_coin(self, address: str) -> Dict[str, List[Dict[str, Any]]]:
        # Use frontend_open_orders for TPSL/trigger details
        out: Dict[str, List[Dict[str, Any]]] = {}
        orders = self.info.frontend_open_orders(address)
        for o in orders:
            if not o.get("isTrigger"):
                continue
            coin = o["coin"]
            out.setdefault(coin, []).append(o)
        return out

    def _get_open_orders_by_coin(self, address: str) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        orders = self.info.open_orders(address)
        for o in orders:
            coin = o["coin"]
            out.setdefault(coin, []).append(o)
        return out

    def build_sync_plan(self) -> Dict[str, Any]:
        """
        Build a plan of actions to sync target with source without executing.
        """
        # Fetch states
        source_state = self.info.user_state(self.source_address)
        target_state = self.trader.get_account_balance()

        # Compute balance scaling factor based on available USDC
        src_withdrawable = float(source_state.get("withdrawable", 0))
        tgt_withdrawable = float(target_state.get("withdrawable", 0))
        src_account_value = float(source_state.get("marginSummary", {}).get("accountValue", 0))
        tgt_account_value = float(target_state.get("marginSummary", {}).get("accountValue", 0))

        # Use available as hard cap, but try to match percentage of account value
        scale = 0.0
        if src_account_value > 0:
            scale = min(1.0, (tgt_account_value / src_account_value) if tgt_account_value > 0 else 0.0)

        src_pos_by_coin = self._positions_by_coin(source_state)
        tgt_pos_by_coin = self._positions_by_coin(target_state)

        position_adjustments: List[Dict[str, Any]] = []
        closes: List[Dict[str, Any]] = []
        triggers_to_create: List[Dict[str, Any]] = []
        triggers_to_cancel: List[Dict[str, Any]] = []

        # Step 1: Match positions via market orders by percent sizing
        for coin, s_pos in src_pos_by_coin.items():
            s_size = float(s_pos.get("szi", 0.0))
            if s_size == 0:
                # If source has no position but target does, close target position
                t_pos = tgt_pos_by_coin.get(coin)
                if t_pos and float(t_pos.get("szi", 0.0)) != 0.0:
                    closes.append({
                        "type": "close_position",
                        "coin": coin,
                        "close_type": "market"
                    })
                continue

            # Desired target size = source size scaled by account ratio
            desired_tgt_size = s_size * scale

            t_pos = tgt_pos_by_coin.get(coin)
            current_tgt_size = float(t_pos.get("szi", 0.0)) if t_pos else 0.0

            diff = desired_tgt_size - current_tgt_size
            if abs(diff) < 1e-10:
                continue

            # Positive diff -> need to increase long if source is long, or reduce short
            # Determine side by sign of diff
            if diff > 0:
                # Increase in the direction of source position sign
                is_source_long = s_size > 0
                side = 'buy' if is_source_long else 'sell'
            else:
                # Decrease position magnitude
                is_source_long = s_size > 0
                side = 'sell' if is_source_long else 'buy'

            # Trade absolute difference
            trade_amount = abs(diff)
            # Place market order with reduce_only if moving opposite to current position direction
            reduce_only = (current_tgt_size != 0 and (current_tgt_size > 0) != (diff > 0))

            position_adjustments.append({
                "type": "market_order",
                "coin": coin,
                "side": side,
                "amount": trade_amount,
                "reduce_only": reduce_only
            })

        # Step 2: Replicate TP/SL triggers exactly, scaled by size
        src_triggers = self._get_tp_sl_orders_by_coin(self.source_address)
        tgt_triggers = self._get_tp_sl_orders_by_coin(self.trader.wallet.address)

        for coin, src_list in src_triggers.items():
            t_pos = tgt_pos_by_coin.get(coin)
            if not t_pos:
                # No position; cancel any triggers on target
                for o in tgt_triggers.get(coin, []):
                    triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})
                continue

            s_pos = src_pos_by_coin.get(coin)
            if not s_pos:
                # Shouldn't happen if in src_triggers, but safety
                for o in tgt_triggers.get(coin, []):
                    triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})
                continue

            s_size = abs(float(s_pos.get("szi", 0.0)))
            t_size = abs(float(t_pos.get("szi", 0.0)))
            if s_size == 0:
                for o in tgt_triggers.get(coin, []):
                    orders_cancelled.append(self.trader.cancel_order(coin, o["oid"]))
                continue

            size_ratio = t_size / s_size if s_size > 0 else 0.0

            # Map existing target triggers by type+triggerPx
            existing_map = {}
            for o in tgt_triggers.get(coin, []):
                key = (o.get("tpsl"), float(o.get("triggerPx", 0.0)), bool(o.get("isMarket")))
                existing_map.setdefault(key, []).append(o)

            # Ensure all source triggers exist on target with scaled size
            to_keep_keys = set()
            for s_o in src_list:
                key = (s_o.get("tpsl"), float(s_o.get("triggerPx", 0.0)), bool(s_o.get("isMarket")))
                to_keep_keys.add(key)

                # Size to use on target
                s_order_sz = float(s_o.get("sz", 0.0))
                t_order_sz = max(0.0, s_order_sz * size_ratio)

                # If not present or size differs materially, recreate
                needs_create = False
                if key not in existing_map:
                    needs_create = True
                else:
                    # Compare first existing size (approx)
                    existing_sz = float(existing_map[key][0].get("sz", 0.0))
                    if abs(existing_sz - t_order_sz) / (t_order_sz + 1e-9) > 0.05:
                        # Cancel existing to recreate
                        for o in existing_map[key]:
                            triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})
                        needs_create = True

                if needs_create and t_order_sz > 0:
                    is_buy = t_pos.get("szi", 0.0) < 0  # if short, TP is buy; SL depends on tpsl
                    tpsl = s_o.get("tpsl")  # 'tp' or 'sl'
                    is_market = bool(s_o.get("isMarket"))
                    trig_px = float(s_o.get("triggerPx", 0.0))

                    # Create trigger specification
                    triggers_to_create.append({
                        "coin": coin,
                        "is_buy": is_buy if tpsl == 'tp' else (not is_buy),
                        "order_type": 'stop_market' if is_market else 'stop_limit',
                        "amount": t_order_sz,
                        "price": (trig_px if not is_market else None),
                        "stop_price": trig_px,
                        "reduce_only": True
                    })

            # Cancel any extra triggers on target not present in source
            for key, olist in existing_map.items():
                if key not in to_keep_keys:
                    for o in olist:
                        triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})

        return {
            "position_adjustments": position_adjustments,
            "closes": closes,
            "triggers_to_create": triggers_to_create,
            "triggers_to_cancel": triggers_to_cancel,
            "scale_ratio": scale,
            "timestamp": time.time()
        }

    def describe_plan(self, plan: Dict[str, Any]) -> str:
        lines: List[str] = []
        lines.append(f"Scale ratio (target/source account value): {plan.get('scale_ratio', 0):.4f}")
        if plan["closes"]:
            lines.append("Close positions:")
            for c in plan["closes"]:
                lines.append(f"- {c['coin']} via {c['close_type']}")
        if plan["position_adjustments"]:
            lines.append("Position adjustments (market):")
            for a in plan["position_adjustments"]:
                ro = " (reduce-only)" if a.get("reduce_only") else ""
                lines.append(f"- {a['coin']}: {a['side']} {a['amount']} {ro}")
        if plan["triggers_to_create"]:
            lines.append("Create TP/SL triggers:")
            for t in plan["triggers_to_create"]:
                kind = t["order_type"]
                lines.append(f"- {t['coin']}: {kind} {'BUY' if t['is_buy'] else 'SELL'} sz={t['amount']} stop={t['stop_price']} px={t.get('price')}")
        if plan["triggers_to_cancel"]:
            lines.append("Cancel triggers:")
            for t in plan["triggers_to_cancel"]:
                lines.append(f"- {t['coin']} OID={t['oid']}")
        return "\n".join(lines)

    def execute_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        placed: List[Dict[str, Any]] = []
        cancelled: List[Dict[str, Any]] = []

        # Closes
        for c in plan.get("closes", []):
            placed.append(self.trader.close_position(c["coin"], c["close_type"]))

        # Position adjustments
        for a in plan.get("position_adjustments", []):
            placed.append(
                self.trader.place_order(
                    coin=a["coin"],
                    side=a["side"],
                    order_type='market',
                    amount=a["amount"],
                    reduce_only=a.get("reduce_only", False)
                )
            )

        # Cancel triggers
        for t in plan.get("triggers_to_cancel", []):
            cancelled.append(self.trader.cancel_order(t["coin"], t["oid"]))

        # Create triggers
        for t in plan.get("triggers_to_create", []):
            placed.append(
                self.trader._place_stop_order(
                    coin=t["coin"],
                    is_buy=t["is_buy"],
                    order_type=t["order_type"],
                    amount=t["amount"],
                    price=t.get("price"),
                    stop_price=t["stop_price"],
                    reduce_only=t.get("reduce_only", True)
                )
            )

        return {"orders_placed": placed, "orders_cancelled": cancelled, "timestamp": time.time()}

    def sync_once(self, manual_confirm: bool = False) -> Dict[str, Any]:
        """
        Perform a one-shot sync. If manual_confirm=True or require_confirmation=True,
        returns a plan and description without executing.
        """
        plan = self.build_sync_plan()
        if manual_confirm or self.require_confirmation:
            return {"requires_confirmation": True, "plan": plan, "description": self.describe_plan(plan)}
        return self.execute_plan(plan)


