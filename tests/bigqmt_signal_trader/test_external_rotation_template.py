"""Safety invariants for the offline rotation reference; no QMT connection."""

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "examples"))

from external_rotation_template import (  # noqa: E402
    AccountView, InstrumentRule, Position, Quote, Settings, TargetPortfolio,
    build_plan, select_targets,
)


class RotationSafetyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        self.settings = Settings("demo", "rotation", max_holdings=2,
                                 max_symbol_weight=Decimal("0.5"),
                                 max_order_value=Decimal("100000"))
        self.signal = TargetPortfolio("demo", "rotation", 1, self.now,
                                      self.now - timedelta(days=1),
                                      self.now + timedelta(hours=1), ("B", "C"))
        self.account = AccountView("demo", 1, self.now, Decimal("100000"),
                                   Decimal("80000"),
                                   {"A": Position(1000, 1000), "B": Position(1000, 1000)},
                                   reconciled=True, trading_open=True)
        quote = Quote(self.now, Decimal("10"), Decimal("10"), Decimal("10"),
                      Decimal("9"), Decimal("11"))
        rule = InstrumentRule(100, 100, 100, 100, Decimal("0.01"), True)
        self.quotes = dict.fromkeys(("A", "B", "C"), quote)
        self.rules = dict.fromkeys(self.quotes, rule)

    def plan(self, **changes):
        return build_plan(changes.get("signal", self.signal),
                          changes.get("account", self.account),
                          changes.get("quotes", self.quotes),
                          changes.get("rules", self.rules),
                          changes.get("settings", self.settings), self.now)

    def test_outgoing_position_is_sold_before_any_buy(self):
        plan = self.plan()
        self.assertEqual(plan.stage, "SELL")
        self.assertEqual([(o.symbol, o.side, o.volume) for o in plan.orders],
                         [("A", "SELL", 1000)])

    def test_any_live_order_blocks_the_next_wave(self):
        view = replace(self.account, active_order_ids=("cancel_pending",))
        self.assertEqual(self.plan(account=view).stage, "WAIT_RECONCILE")
        self.assertEqual(self.plan(account=view).orders, ())

    def test_unknown_submission_blocks_even_with_no_visible_order(self):
        view = replace(self.account, unresolved_instruction_ids=("sending_before_crash",))
        self.assertEqual(self.plan(account=view).orders, ())

    def test_recent_rpc_reply_does_not_replace_reconciliation(self):
        self.assertEqual(self.plan(account=replace(self.account, reconciled=False)).stage,
                         "WAIT_RECONCILE")

    def test_locked_outgoing_position_does_not_free_a_slot(self):
        view = replace(self.account, positions={"A": Position(1000, 0),
                                               "B": Position(1000, 1000)})
        plan = self.plan(account=view)
        self.assertNotIn("C", [o.symbol for o in plan.orders])
        self.assertIn("no_free_slot:C", plan.notes)
        self.assertEqual(plan.budget_per_target, Decimal("42500"))

    def test_partial_sell_never_exceeds_broker_available(self):
        view = replace(self.account, positions={"A": Position(1000, 400),
                                               "B": Position(1000, 1000)})
        orders = self.plan(account=view).orders
        self.assertEqual([(o.symbol, o.side, o.volume) for o in orders],
                         [("A", "SELL", 400)])

    def test_confirmed_exit_allows_new_target(self):
        view = replace(self.account, positions={"B": Position(1000, 1000)},
                       available_cash=Decimal("90000"))
        plan = self.plan(account=view)
        self.assertEqual(plan.stage, "BUY")
        self.assertIn("C", [o.symbol for o in plan.orders])
        cost = sum((o.volume * o.limit_price * (1 + self.settings.fee_rate_reserve)
                    + self.settings.fee_fixed_reserve for o in plan.orders), Decimal("0"))
        self.assertLessEqual(cost, view.available_cash - Decimal("5000"))
        projected_names = set(view.positions) | {o.symbol for o in plan.orders}
        self.assertLessEqual(len(projected_names), self.settings.max_holdings)
        projected_value = Decimal("10000") + sum(
            (o.volume * o.limit_price for o in plan.orders), Decimal("0"))
        self.assertLessEqual(projected_value, view.equity * self.settings.invest_fraction)

    def test_reserved_cash_is_not_spent(self):
        view = replace(self.account, positions={}, available_cash=Decimal("5001"))
        self.assertEqual(self.plan(account=view).orders, ())

    def test_suspended_exit_still_uses_money_and_slot(self):
        quotes = dict(self.quotes, A=replace(self.quotes["A"], tradable=False))
        plan = self.plan(quotes=quotes)
        self.assertNotIn("C", [o.symbol for o in plan.orders])
        self.assertIn("not_tradable:A", plan.notes)

    def test_stale_quote_halts_planning(self):
        quotes = dict(self.quotes, A=replace(self.quotes["A"],
                                            observed_at=self.now - timedelta(seconds=6)))
        self.assertEqual(self.plan(quotes=quotes).stage, "HALTED")

    def test_stale_account_halts_planning(self):
        view = replace(self.account, observed_at=self.now - timedelta(seconds=6))
        self.assertEqual(self.plan(account=view).stage, "HALTED")

    def test_old_or_expired_target_never_trades(self):
        self.assertEqual(self.plan(signal=replace(self.signal, version=2)).orders, ())
        self.assertEqual(self.plan(signal=replace(self.signal, expires_at=self.now)).orders, ())

    def test_account_and_strategy_identity_are_required(self):
        self.assertEqual(self.plan(signal=replace(self.signal, account_id="other")).orders, ())
        self.assertEqual(self.plan(signal=replace(self.signal, strategy_id="other")).orders, ())

    def test_empty_target_is_not_accidental_liquidation(self):
        signal = replace(self.signal, symbols=())
        self.assertEqual(self.plan(signal=signal).stage, "HALTED")
        plan = self.plan(signal=signal, settings=replace(self.settings, allow_empty_target=True))
        self.assertEqual({o.symbol for o in plan.orders}, {"A", "B"})
        self.assertTrue(all(o.side == "SELL" for o in plan.orders))

    def test_duplicate_or_excess_target_names_are_rejected(self):
        self.assertEqual(self.plan(signal=replace(self.signal, symbols=("B", "B"))).orders, ())
        self.assertEqual(self.plan(signal=replace(self.signal, symbols=("A", "B", "C"))).orders, ())

    def test_odd_lot_full_exit_is_explicit_and_partial_exit_is_rounded(self):
        all_available = replace(self.account, positions={"A": Position(105, 105)})
        partial = replace(self.account, positions={"A": Position(105, 100)})
        self.assertEqual(self.plan(account=all_available).orders[0].volume, 105)
        self.assertEqual(self.plan(account=partial).orders[0].volume, 100)

    def test_buy_rules_are_per_instrument_and_not_always_100(self):
        view = replace(self.account, positions={})
        rule = InstrumentRule(200, 1, 200, 1, Decimal("0.01"), True)
        settings = replace(self.settings, max_order_value=Decimal("2470"))
        plan = self.plan(account=view, rules=dict.fromkeys(self.rules, rule), settings=settings)
        self.assertTrue(plan.orders)
        self.assertTrue(all(o.volume == 247 for o in plan.orders))

    def test_risk_pause_still_allows_exit_sells(self):
        view = replace(self.account, buys_paused=True)
        self.assertEqual(self.plan(account=view).stage, "SELL")
        flat = replace(view, positions={})
        self.assertEqual(self.plan(account=flat).orders, ())

    def test_existing_unfixable_risk_breach_prevents_more_buying(self):
        view = replace(self.account, positions={"A": Position(6000, 0),
                                               "B": Position(1000, 1000)},
                       available_cash=Decimal("30000"))
        plan = self.plan(account=view)
        self.assertEqual(plan.orders, ())
        self.assertIn("existing_risk_breach_requires_reduction", plan.notes)

    def test_invalid_or_nonfinite_funds_cannot_generate_orders(self):
        for value in (None, "not-a-number", Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.plan(account=replace(self.account, available_cash=value))

    def test_single_order_notional_is_capped(self):
        settings = replace(self.settings, max_order_value=Decimal("3000"))
        plan = self.plan(settings=settings)
        self.assertEqual(plan.orders[0].volume, 300)
        self.assertTrue(all(o.volume * o.limit_price <= settings.max_order_value for o in plan.orders))

    def test_missing_instrument_metadata_never_uses_guessed_defaults(self):
        rules = dict(self.rules)
        del rules["A"]
        self.assertEqual(self.plan(rules=rules).stage, "HALTED")

    def test_price_deviation_and_limit_lock_skip_the_order(self):
        quotes = dict(self.quotes, A=replace(self.quotes["A"], bid=Decimal("9.8")))
        self.assertFalse(any(o.symbol == "A" for o in self.plan(quotes=quotes).orders))
        quotes["A"] = replace(self.quotes["A"], bid=Decimal("9"))
        self.assertIn("limit_locked:A", self.plan(quotes=quotes).notes)

    def test_rank_buffer_reduces_churn_and_ties_are_stable(self):
        scores = {"A": 1, "B": 3, "C": 2}
        self.assertEqual(select_targets(scores, ["A", "B"], 2), ("B", "C"))
        self.assertEqual(select_targets(scores, ["A", "B"], 2, retain_rank=3), ("B", "A"))
        self.assertEqual(select_targets({"B": 1, "A": 1}, [], 2), ("A", "B"))


if __name__ == "__main__":
    unittest.main()
