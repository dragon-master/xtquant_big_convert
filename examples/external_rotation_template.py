"""External fixed-count rotation planner. Python 3.8+, standard library only.

Run: python examples/external_rotation_template.py
This example never connects to QMT and never submits/cancels an order.
See docs/EXTERNAL_STRATEGY_ROTATION_TEMPLATE.md for the live coordinator contract.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping, Optional, Sequence, Tuple


def number(value, name="number"):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("%s must be numeric" % name) from exc
    if not result.is_finite():
        raise ValueError("%s must be finite" % name)
    return result


def aware(value):
    return isinstance(value, datetime) and value.utcoffset() is not None


@dataclass(frozen=True)
class TargetPortfolio:
    account_id: str
    strategy_id: str
    version: int
    created_at: datetime
    data_cutoff: datetime
    expires_at: datetime
    symbols: Tuple[str, ...]


@dataclass(frozen=True)
class Position:
    volume: int
    available: int  # Broker-reported sellable quantity, already net of freezes.


@dataclass(frozen=True)
class AccountView:
    account_id: str
    accepted_target_version: int
    observed_at: datetime
    equity: Decimal
    available_cash: Decimal  # Already excludes broker-frozen cash.
    positions: Mapping[str, Position] = field(default_factory=dict)
    active_order_ids: Tuple[str, ...] = ()
    unresolved_instruction_ids: Tuple[str, ...] = ()
    reconciled: bool = False  # Local orders/fills agree with broker snapshots.
    trading_open: bool = False  # Calendar + session + account health verified.
    buys_paused: bool = False


@dataclass(frozen=True)
class InstrumentRule:
    # Explicit, verified rules per instrument. No market-wide 100-share default.
    min_buy: int
    buy_step: int
    min_sell: int
    sell_step: int
    price_tick: Decimal
    allow_full_exit_odd_lot: bool = False


@dataclass(frozen=True)
class Quote:
    observed_at: datetime  # Source quote time, NOT the time get_full_tick returns.
    last: Decimal  # Unadjusted executable-market price.
    bid: Decimal
    ask: Decimal
    lower_limit: Decimal
    upper_limit: Decimal
    tradable: bool = True


@dataclass(frozen=True)
class Settings:
    account_id: str
    strategy_id: str
    max_holdings: int = 5
    invest_fraction: Decimal = Decimal("0.95")
    max_symbol_weight: Decimal = Decimal("0.25")
    max_order_value: Decimal = Decimal("20000")
    drift_tolerance: Decimal = Decimal("0.02")
    max_price_deviation: Decimal = Decimal("0.005")
    fee_rate_reserve: Decimal = Decimal("0.002")
    fee_fixed_reserve: Decimal = Decimal("5")
    max_account_age_seconds: float = 5.0
    max_quote_age_seconds: float = 5.0
    allow_empty_target: bool = False


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    volume: int
    limit_price: Decimal
    reason: str


@dataclass(frozen=True)
class Plan:
    stage: str
    orders: Tuple[OrderIntent, ...] = ()
    notes: Tuple[str, ...] = ()
    budget_per_target: Decimal = Decimal("0")


def select_targets(scores: Mapping[str, float], current: Sequence[str],
                   count: int, retain_rank: Optional[int] = None) -> Tuple[str, ...]:
    """Rank verified eligible candidates; optionally retain names within a buffer.

    Higher scores rank first. This function has no market-data or trading calls.
    Exclude cooldown/risk-blocked names BEFORE calling it. Missing/bad factor
    data must be handled by the data layer, not converted to an empty portfolio.
    """
    if count < 1 or not scores or (retain_rank is not None and retain_rank < count):
        raise ValueError("invalid count, empty scores, or retain_rank < count")
    ranked = sorted(scores, key=lambda code: (-number(scores[code], code), code))
    held = set(current)
    keep = [] if retain_rank is None else [
        code for code in ranked[:retain_rank] if code in held
    ][:count]
    return tuple((keep + [code for code in ranked if code not in keep])[:count])


def _validate(signal, account, settings, now):
    if not all(aware(t) for t in (
        now, signal.created_at, signal.data_cutoff, signal.expires_at, account.observed_at
    )):
        return "timezone_required"
    if not settings.account_id or not settings.strategy_id:
        return "missing_account_or_strategy"
    if signal.account_id != settings.account_id or account.account_id != settings.account_id:
        return "account_mismatch"
    if signal.strategy_id != settings.strategy_id:
        return "strategy_mismatch"
    if (type(signal.version) is not int or signal.version < 1
            or signal.version != account.accepted_target_version):
        return "target_version_not_accepted"
    if not signal.data_cutoff <= signal.created_at <= now < signal.expires_at:
        return "invalid_or_expired_target_time"
    age = (now - account.observed_at).total_seconds()
    if not 0 <= age <= settings.max_account_age_seconds:
        return "stale_account"
    if (type(settings.max_holdings) is not int or settings.max_holdings < 1
            or len(signal.symbols) > settings.max_holdings):
        return "invalid_target_count"
    if len(set(signal.symbols)) != len(signal.symbols) or any(not s for s in signal.symbols):
        return "duplicate_or_empty_symbol"
    if not signal.symbols and not settings.allow_empty_target:
        return "empty_target_requires_explicit_policy"
    if number(account.equity) <= 0 or number(account.available_cash) < 0:
        return "invalid_account_funds"
    if not 0 < number(settings.invest_fraction) <= 1:
        return "invalid_invest_fraction"
    if not 0 < number(settings.max_symbol_weight) <= 1:
        return "invalid_symbol_weight"
    if number(settings.max_order_value) <= 0:
        return "invalid_order_cap"
    if not 0 <= number(settings.drift_tolerance) < 1:
        return "invalid_drift_tolerance"
    if not 0 <= number(settings.max_price_deviation) < 1:
        return "invalid_price_deviation"
    if number(settings.fee_rate_reserve) < 0 or number(settings.fee_fixed_reserve) < 0:
        return "invalid_fee_reserve"
    for code, pos in account.positions.items():
        if (not code or type(pos.volume) is not int or type(pos.available) is not int
                or not 0 <= pos.available <= pos.volume):
            return "invalid_position:%s" % code
    return ""


def _market_issue(code, quotes, rules, now, settings):
    quote, rule = quotes.get(code), rules.get(code)
    if quote is None or rule is None:
        return "missing_quote_or_instrument_rule:%s" % code
    if not aware(quote.observed_at):
        return "quote_timezone_required:%s" % code
    age = (now - quote.observed_at).total_seconds()
    if not 0 <= age <= settings.max_quote_age_seconds:
        return "stale_quote:%s" % code
    if (min(rule.min_buy, rule.buy_step, rule.min_sell, rule.sell_step) < 1
            or any(type(n) is not int for n in (
                rule.min_buy, rule.buy_step, rule.min_sell, rule.sell_step))
            or number(rule.price_tick) <= 0):
        return "invalid_instrument_rule:%s" % code
    if not 0 < number(quote.lower_limit) <= number(quote.last) <= number(quote.upper_limit):
        return "invalid_price_or_limits:%s" % code
    return ""


def _price(code, side, quotes, rules, settings):
    quote, rule = quotes[code], rules[code]
    if not quote.tradable:
        return None, "not_tradable:%s" % code
    raw = number(quote.ask if side == "BUY" else quote.bid)
    if raw <= 0:
        return None, "missing_book_price:%s" % code
    tick = number(rule.price_tick)
    rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
    price = (raw / tick).to_integral_value(rounding=rounding) * tick
    if not number(quote.lower_limit) <= price <= number(quote.upper_limit):
        return None, "price_outside_limits:%s" % code
    # Conservative example policy: skip buying at upper/selling at lower limit.
    if ((side == "BUY" and price >= number(quote.upper_limit))
            or (side == "SELL" and price <= number(quote.lower_limit))):
        return None, "limit_locked:%s" % code
    if abs(price / number(quote.last) - 1) > number(settings.max_price_deviation):
        return None, "price_deviation:%s" % code
    return price, ""


def _floor_quantity(value, step):
    return max(0, int(value // step) * step)


def _sell_intent(code, wanted, pos, quote, rule, settings, reason):
    price, issue = _price(code, "SELL", {code: quote}, {code: rule}, settings)
    if issue:
        return None, issue
    cap = int(number(settings.max_order_value) / price)
    raw = min(wanted, pos.available, cap)
    if raw == pos.volume and rule.allow_full_exit_odd_lot:
        volume = raw
    else:
        volume = _floor_quantity(raw, rule.sell_step)
        if volume < rule.min_sell:
            volume = 0
    if volume <= 0:
        return None, "no_sellable_lot:%s" % code
    return OrderIntent(code, "SELL", volume, price, reason), ""


def build_plan(signal: TargetPortfolio, account: AccountView,
               quotes: Mapping[str, Quote], rules: Mapping[str, InstrumentRule],
               settings: Settings, now: datetime) -> Plan:
    """Plan one wave. Caller must record/execute it and reconcile before replanning.

    Deliberately serial: any live order or unknown submission blocks a new wave.
    This pure function does NOT provide locks, durable IDs, retries or live risk
    monitoring. Repeated calls with the same account data yield the SAME plan;
    the coordinator must never submit both copies.
    """
    issue = _validate(signal, account, settings, now)
    if issue:
        return Plan("HALTED", notes=(issue,))
    if not account.trading_open:
        return Plan("HALTED", notes=("calendar_session_or_account_gate_closed",))
    if (not account.reconciled or account.active_order_ids
            or account.unresolved_instruction_ids):
        return Plan("WAIT_RECONCILE", notes=("live_orders_or_unresolved_account_state",))
    held = {code for code, pos in account.positions.items() if pos.volume > 0}
    targets = set(signal.symbols)
    for code in sorted(held | targets):
        issue = _market_issue(code, quotes, rules, now, settings)
        if issue:
            return Plan("HALTED", notes=(issue,))

    # Phase 1: liquidate outgoing names. Expected proceeds never fund buys.
    notes, sells = [], []
    for code in sorted(held - targets):
        pos = account.positions[code]
        intent, issue = _sell_intent(
            code, pos.volume, pos, quotes[code], rules[code], settings, "leave_target"
        )
        if intent:
            sells.append(intent)
        if issue:
            notes.append(issue)
        if pos.available < pos.volume:
            notes.append("locked_exit_still_occupies_slot:%s" % code)
    if sells:
        return Plan("SELL", tuple(sells), tuple(notes))

    # Unremovable outgoing positions consume both money and slots. Divide the
    # remaining budget by N, not by the number of currently eligible names.
    equity = number(account.equity)
    gross_cap = equity * number(settings.invest_fraction)
    locked_value = sum((account.positions[code].volume * number(quotes[code].last)
                        for code in held - targets), Decimal("0"))
    budget = min(max(Decimal("0"), gross_cap - locked_value) / settings.max_holdings,
                 equity * number(settings.max_symbol_weight)) if targets else Decimal("0")

    # Phase 2: trim retained positions, then reconcile before any buy wave.
    for code in signal.symbols:
        pos = account.positions.get(code, Position(0, 0))
        excess = pos.volume * number(quotes[code].last) - budget
        if excess <= budget * number(settings.drift_tolerance):
            continue
        wanted = int(excess / number(quotes[code].last))
        if wanted < rules[code].min_sell:
            continue
        intent, issue = _sell_intent(
            code, wanted, pos, quotes[code], rules[code], settings, "trim_weight"
        )
        if intent:
            sells.append(intent)
        if issue:
            notes.append(issue)
    if sells:
        return Plan("SELL", tuple(sells), tuple(notes), budget)

    # Phase 3: buy from confirmed AVAILABLE cash, reserving cash and fees once.
    if account.buys_paused:
        return Plan("WAIT", notes=tuple(notes + ["risk_gate_paused_buys"]),
                    budget_per_target=budget)
    gross_value = sum((account.positions[code].volume * number(quotes[code].last)
                       for code in held), Decimal("0"))
    if (len(held) > settings.max_holdings or gross_value > gross_cap
            or any(account.positions[code].volume * number(quotes[code].last)
                   > equity * number(settings.max_symbol_weight) for code in held)):
        return Plan("WAIT", notes=tuple(notes + ["existing_risk_breach_requires_reduction"]),
                    budget_per_target=budget)
    cash_reserve = equity * (1 - number(settings.invest_fraction))
    cash_left = max(Decimal("0"), number(account.available_cash) - cash_reserve)
    occupied, buys = set(held), []
    fee_rate, fee_fixed = number(settings.fee_rate_reserve), number(settings.fee_fixed_reserve)
    for code in signal.symbols:
        pos = account.positions.get(code, Position(0, 0))
        rule, quote = rules[code], quotes[code]
        if code not in occupied and len(occupied) >= settings.max_holdings:
            notes.append("no_free_slot:%s" % code)
            continue
        price, issue = _price(code, "BUY", quotes, rules, settings)
        if issue:
            notes.append(issue)
            continue
        headroom = min(budget - pos.volume * number(quote.last), gross_cap - gross_value)
        if headroom <= 0:
            continue
        if headroom < budget * number(settings.drift_tolerance) and pos.volume > 0:
            continue
        affordable = max(Decimal("0"), cash_left - fee_fixed) / (price * (1 + fee_rate))
        raw = min(headroom / price, number(settings.max_order_value) / price, affordable)
        volume = _floor_quantity(raw, rule.buy_step)
        if volume < rule.min_buy:
            notes.append("cash_budget_or_min_lot:%s" % code)
            continue
        buys.append(OrderIntent(code, "BUY", volume, price, "fill_target"))
        cash_left -= volume * price * (1 + fee_rate) + fee_fixed
        gross_value += volume * price
        occupied.add(code)  # Reserve a slot for every proposed new-name buy.
    if buys:
        return Plan("BUY", tuple(buys), tuple(notes), budget)
    return Plan("WAIT" if notes else "ON_TARGET", notes=tuple(notes), budget_per_target=budget)


def demo():
    now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    settings = Settings("DEMO_ACCOUNT", "rotation_demo", max_holdings=2,
                        max_symbol_weight=Decimal("0.5"))
    # Fictional symbols and scores; these are not investment recommendations.
    targets = select_targets({"AAA.SIM": 1, "BBB.SIM": 3, "CCC.SIM": 2},
                             ["AAA.SIM", "BBB.SIM"], count=2)
    signal = TargetPortfolio(settings.account_id, settings.strategy_id, 1, now,
                             now - timedelta(days=1), now + timedelta(hours=1), targets)
    quote = Quote(now, Decimal("10"), Decimal("10"), Decimal("10"),
                  Decimal("9"), Decimal("11"))
    rule = InstrumentRule(100, 100, 100, 100, Decimal("0.01"), True)
    quotes = {code: quote for code in ("AAA.SIM", "BBB.SIM", "CCC.SIM")}
    rules = {code: rule for code in quotes}
    scenarios = (
        ("1. Outgoing position must sell first", {"AAA.SIM": Position(1000, 1000),
                                                  "BBB.SIM": Position(1000, 1000)}, (), True),
        ("2. A live order blocks the next wave", {"AAA.SIM": Position(1000, 0),
                                                 "BBB.SIM": Position(1000, 1000)}, ("SELL_1",), True),
        ("3. Locked outgoing name keeps its slot", {"AAA.SIM": Position(1000, 0),
                                                    "BBB.SIM": Position(1000, 1000)}, (), True),
        ("4. Confirmed liquidation frees a slot", {"BBB.SIM": Position(1000, 1000)}, (), True),
    )
    for title, positions, active, reconciled in scenarios:
        available_cash = Decimal("100000") - sum(
            (pos.volume * Decimal("10") for pos in positions.values()), Decimal("0"))
        view = AccountView(settings.account_id, 1, now, Decimal("100000"),
                           available_cash, positions, active, (), reconciled, True)
        plan = build_plan(signal, view, quotes, rules, settings, now)
        print("\n%s -> %s" % (title, plan.stage))
        for order in plan.orders:
            print("  %s %s %d @ %s (%s)" % (
                order.side, order.symbol, order.volume, order.limit_price, order.reason))
        for note in plan.notes:
            print("  WAIT: %s" % note)


if __name__ == "__main__":
    demo()
