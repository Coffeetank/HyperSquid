import time
from typing import Dict, List, Optional, Any
from hyperliquid.info import Info
from hyperliquid.utils import constants
from .trading import Trading
from .trading_monitor import Tracker
from decimal import Decimal, ROUND_DOWN


class CopyTrader:
    def __init__(self, trader: Trading, source_address: str, network: str = 'mainnet', require_confirmation: bool = False, debug: bool = True, allow_equivalent_resting_as_trigger: bool = True):
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
        self.debug = debug
        self._meta_cache = None
        self._mids_cache = None
        self._last_meta_fetch = 0.0
        self._last_mids_fetch = 0.0
        self.allow_equivalent_resting_as_trigger = allow_equivalent_resting_as_trigger

    def _positions_by_coin(self, user_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for ap in user_state.get("assetPositions", []):
            pos = ap["position"]
            coin = pos["coin"]
            result[coin] = pos
        return result

    def _get_meta(self) -> Dict[str, Any]:
        now = time.time()
        if self._meta_cache is None or (now - self._last_meta_fetch) > 60:
            self._meta_cache = self.info.meta()
            self._last_meta_fetch = now
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
            return amount
        sz_decimals = int(asset.get("szDecimals", 0))
        step = Decimal("1") / (Decimal(10) ** sz_decimals)
        q = (Decimal(str(amount))).quantize(step, rounding=ROUND_DOWN)
        return float(q)

    def _quantize_price(self, coin: str, price: float) -> float:
        asset = self._get_asset_entry(coin)
        if not asset:
            return price
        px_decimals = int(asset.get("pxDecimals", 2))
        step = Decimal("1") / (Decimal(10) ** px_decimals)
        q = (Decimal(str(price))).quantize(step, rounding=ROUND_DOWN)
        return float(q)

    def _get_mids(self) -> Dict[str, float]:
        now = time.time()
        if self._mids_cache is None or (now - self._last_mids_fetch) > 5:
            self._mids_cache = self.info.all_mids()
            self._last_mids_fetch = now
        return self._mids_cache

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            v = value.strip().lower()
            return v in ("true", "1", "yes", "y")
        return False

    def _get_px_step(self, coin: str) -> float:
        asset = self._get_asset_entry(coin)
        px_decimals = int(asset.get("pxDecimals", 2)) if asset else 2
        return float(Decimal("1") / (Decimal(10) ** px_decimals))

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

    def _get_non_trigger_frontend_by_coin(self, address: str) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        orders = self.info.frontend_open_orders(address)
        for o in orders:
            if o.get("isTrigger"):
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

        # Compute balance scaling factor accounting for available USDC and account value
        src_withdrawable = float(source_state.get("withdrawable", 0))
        tgt_withdrawable = float(target_state.get("withdrawable", 0))
        src_account_value = float(source_state.get("marginSummary", {}).get("accountValue", 0))
        tgt_account_value = float(target_state.get("marginSummary", {}).get("accountValue", 0))

        scale_candidates = []
        if src_account_value > 0 and tgt_account_value > 0:
            scale_candidates.append(tgt_account_value / src_account_value)
        if src_withdrawable > 0 and tgt_withdrawable >= 0:
            scale_candidates.append(tgt_withdrawable / src_withdrawable)
        scale = min(scale_candidates) if scale_candidates else 0.0
        # Prevent oversizing relative to source
        scale = min(scale, 1.0)

        src_pos_by_coin = self._positions_by_coin(source_state)
        tgt_pos_by_coin = self._positions_by_coin(target_state)
        # Preload target non-trigger frontend orders for equivalence check
        tgt_open_nt = self._get_non_trigger_frontend_by_coin(self.trader.wallet.address)
        equivalent_non_trigger_oids_keep = set()

        position_adjustments: List[Dict[str, Any]] = []
        closes: List[Dict[str, Any]] = []
        triggers_to_create: List[Dict[str, Any]] = []
        triggers_to_cancel: List[Dict[str, Any]] = []
        non_trigger_to_cancel: List[Dict[str, Any]] = []

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

            # Trade absolute difference between our holding and scaled source
            trade_amount = abs(diff)
            # For market orders, we handle position reduction by sizing appropriately
            # If moving opposite to current position, reduce the position
            if current_tgt_size != 0 and (current_tgt_size > 0) != (diff > 0):
                # This is reducing the position - use the smaller of trade_amount and current position size
                trade_amount = min(trade_amount, abs(current_tgt_size))

            # Quantize and skip tiny trades by size and by USD notional
            trade_amount_q = self._quantize_size(coin, trade_amount)
            if trade_amount_q <= 0:
                continue

            mids = self._get_mids()
            px = float(mids.get(coin, 0.0))
            notional = trade_amount_q * px if px > 0 else trade_amount_q
            if notional < 5.0:  # Skip if < $5 notional
                continue

            position_adjustments.append({
                "type": "market_order",
                "coin": coin,
                "side": side,
                "amount": trade_amount_q
            })
            if self.debug:
                print("[COPY][POS]", {
                    "coin": coin,
                    "src": s_size,
                    "tgt": current_tgt_size,
                    "desired": desired_tgt_size,
                    "diff": diff,
                    "trade": trade_amount_q
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
                    triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})
                continue

            size_ratio = t_size / s_size if s_size > 0 else 0.0

            # Build a working list of unmatched existing orders for cancellation later
            existing = list(tgt_triggers.get(coin, []))
            matched_oids = set()
            px_step = self._get_px_step(coin)

            # Ensure all source triggers exist on target with scaled size
            for s_o in src_list:
                trig_px_q = self._quantize_price(coin, float(s_o.get("triggerPx", 0.0)))
                s_order_sz = float(s_o.get("sz", 0.0))
                t_order_sz = max(0.0, s_order_sz * size_ratio)

                # Normalize desired attributes
                desired = {
                    "tpsl": s_o.get("tpsl") if s_o.get("tpsl") in ("tp", "sl") else "sl",
                    "isMarket": self._to_bool(s_o.get("isMarket")),
                    "triggerPx": trig_px_q,
                }

                # Try to find a matching existing trigger
                found_idx = None
                size_diff_too_large = False
                for idx, o in enumerate(existing):
                    if o.get("oid") in matched_oids:
                        continue
                    o_tpsl = o.get("tpsl") if o.get("tpsl") in ("tp", "sl") else "sl"
                    o_is_market = self._to_bool(o.get("isMarket"))
                    o_px_q = self._quantize_price(coin, float(o.get("triggerPx", 0.0)))

                    # Consider equal if triggerPx within one tick, and type matches
                    if (
                        o_tpsl == desired["tpsl"]
                        and o_is_market == desired["isMarket"]
                        and abs(o_px_q - desired["triggerPx"]) <= px_step
                    ):
                        # Compare size drift
                        existing_sz_q = self._quantize_size(coin, float(o.get("sz", 0.0)))
                        desired_sz_q = self._quantize_size(coin, t_order_sz)
                        if desired_sz_q <= 0:
                            found_idx = idx
                            break
                        drift = abs(existing_sz_q - desired_sz_q) / (desired_sz_q + 1e-9)
                        if drift <= 0.05:
                            found_idx = idx
                            break
                        else:
                            # mark for cancel and recreate
                            size_diff_too_large = True
                            found_idx = idx
                            break

                if found_idx is not None:
                    o = existing[found_idx]
                    matched_oids.add(o.get("oid"))
                    if size_diff_too_large:
                        triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})
                        # fall-through to create with desired size
                    else:
                        # matched and within tolerance; no action
                        if self.debug:
                            print("[COPY][TRIG][KEEP]", {
                                "coin": coin,
                                "tpsl": desired["tpsl"],
                                "isMarket": desired["isMarket"],
                                "triggerPx": desired["triggerPx"]
                            })
                        continue

                if t_order_sz > 0:
                    # Exit orders are opposite the position side for both TP and SL
                    is_buy_exit = float(t_pos.get("szi", 0.0)) < 0
                    tpsl = s_o.get("tpsl")  # 'tp' or 'sl'
                    if tpsl not in ("tp", "sl"):
                        tpsl = "sl"
                    is_market = self._to_bool(s_o.get("isMarket"))
                    trig_px = self._quantize_price(coin, float(s_o.get("triggerPx", 0.0)))
                    # Quantize size and skip tiny triggers by notional as well
                    t_order_sz_q = self._quantize_size(coin, t_order_sz)
                    if t_order_sz_q <= 0:
                        continue
                    mids = self._get_mids()
                    px = float(mids.get(coin, 0.0))
                    if px > 0 and (t_order_sz_q * px) < 5.0:
                        continue

                    # Optional: treat an existing resting limit as equivalent to a trigger
                    if self.allow_equivalent_resting_as_trigger and not is_market:
                        # Map trigger direction to resting side char: BUY -> 'B', SELL -> 'A'
                        side_char = 'B' if is_buy_exit else 'A'
                        px_step = self._get_px_step(coin)
                        for o_nt in tgt_open_nt.get(coin, []):
                            try:
                                o_side = o_nt.get("side")
                                o_px_q = self._quantize_price(coin, float(o_nt.get("limitPx", 0.0)))
                                o_sz_q = self._quantize_size(coin, float(o_nt.get("sz", 0.0)))
                            except Exception:
                                continue
                            if (
                                o_side == side_char and
                                abs(o_px_q - trig_px) <= px_step and
                                (abs(o_sz_q - t_order_sz_q) / (t_order_sz_q + 1e-9) <= 0.05)
                            ):
                                # Consider equivalent; keep this non-trigger and skip creating trigger
                                equivalent_non_trigger_oids_keep.add(o_nt.get("oid"))
                                if self.debug:
                                    print("[COPY][TRIG][EQUIV-KEEP]", {
                                        "coin": coin,
                                        "oid": o_nt.get("oid"),
                                        "side": o_side,
                                        "px": o_px_q,
                                        "sz": o_sz_q
                                    })
                                # matched; do not create trigger
                                break
                        else:
                            # No equivalent found; proceed to create trigger
                            pass
                        # If equivalent was found and added, skip creation
                        if any(o.get("oid") in equivalent_non_trigger_oids_keep for o in tgt_open_nt.get(coin, [])):
                            continue

                    # Create trigger specification
                    triggers_to_create.append({
                        "coin": coin,
                        "is_buy": is_buy_exit,
                        "order_type": 'stop_market' if is_market else 'stop_limit',
                        "amount": t_order_sz_q,
                        "price": (trig_px if not is_market else None),
                        "stop_price": trig_px,
                        "tpsl": tpsl
                    })
                    if self.debug:
                        print("[COPY][TRIG][CREATE]", {
                            "coin": coin,
                            "tpsl": tpsl,
                            "isMarket": is_market,
                            "triggerPx": trig_px,
                            "sz": t_order_sz_q
                        })

            # Cancel any remaining unmatched existing triggers on target
            for o in existing:
                if o.get("oid") not in matched_oids:
                    triggers_to_cancel.append({"coin": coin, "oid": o["oid"]})
                    if self.debug:
                        print("[COPY][TRIG][CANCEL-EXTRA]", {
                            "coin": coin,
                            "oid": o.get("oid"),
                            "tpsl": o.get("tpsl"),
                            "isMarket": self._to_bool(o.get("isMarket")),
                            "triggerPx": o.get("triggerPx")
                        })

        # Also cancel non-trigger frontend open orders that do not exist on source
        src_open_nt = self._get_non_trigger_frontend_by_coin(self.source_address)
        tgt_open_nt = self._get_non_trigger_frontend_by_coin(self.trader.wallet.address)
        for coin, tgt_list in tgt_open_nt.items():
            src_list = src_open_nt.get(coin, [])
            px_step = self._get_px_step(coin)
            def norm(o: Dict[str, Any]):
                return (
                    o.get("side"),
                    self._quantize_price(coin, float(o.get("limitPx", 0.0))),
                    self._quantize_size(coin, float(o.get("sz", 0.0)))
                )
            src_norms = [norm(o) for o in src_list]
            for o in tgt_list:
                s, px_q, sz_q = norm(o)
                if o.get("oid") in equivalent_non_trigger_oids_keep:
                    # Protected due to equivalence with a trigger
                    if self.debug:
                        print("[COPY][OPEN][KEEP-EQUIV]", {
                            "coin": coin,
                            "oid": o.get("oid"),
                            "side": s,
                            "px": px_q,
                            "sz": sz_q
                        })
                    continue
                matched = False
                for s2, px2, sz2 in src_norms:
                    if s == s2 and abs(px_q - px2) <= px_step and (abs(sz_q - sz2) / (sz2 + 1e-9) <= 0.05):
                        matched = True
                        break
                if not matched:
                    non_trigger_to_cancel.append({"coin": coin, "oid": o["oid"]})
                    if self.debug:
                        print("[COPY][OPEN][CANCEL-EXTRA]", {
                            "coin": coin,
                            "oid": o.get("oid"),
                            "side": o.get("side"),
                            "px": o.get("limitPx"),
                            "sz": o.get("sz")
                        })

        return {
            "position_adjustments": position_adjustments,
            "closes": closes,
            "triggers_to_create": triggers_to_create,
            "triggers_to_cancel": triggers_to_cancel,
            "open_orders_to_cancel": non_trigger_to_cancel,
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
                lines.append(f"- {a['coin']}: {a['side']} {a['amount']}")
        if plan["triggers_to_create"]:
            lines.append("Create TP/SL triggers:")
            for t in plan["triggers_to_create"]:
                kind = t["order_type"]
                lines.append(f"- {t['coin']}: {kind} {'BUY' if t['is_buy'] else 'SELL'} sz={t['amount']} stop={t['stop_price']} px={t.get('price')}")
        if plan["triggers_to_cancel"]:
            lines.append("Cancel triggers:")
            for t in plan["triggers_to_cancel"]:
                lines.append(f"- {t['coin']} OID={t['oid']}")
        if plan.get("open_orders_to_cancel"):
            lines.append("Cancel non-trigger open orders:")
            for t in plan["open_orders_to_cancel"]:
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
                    amount=a["amount"]
                )
            )

        # Cancel triggers
        for t in plan.get("triggers_to_cancel", []):
            cancelled.append(self.trader.cancel_order(t["coin"], t["oid"]))

        # Cancel non-trigger open orders
        for t in plan.get("open_orders_to_cancel", []):
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
                    tpsl=t.get("tpsl", 'sl')
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


