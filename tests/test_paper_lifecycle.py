from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid4, uuid5

from stockmarket.core.executors import TradingMode
from stockmarket.core.brokers import PaperBroker
from stockmarket.core.models import (
    AssetClass,
    Instrument,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalSide,
    TradingStatus,
)
from stockmarket.core.order_management import ManagedOrder
from stockmarket.core.paper_lifecycle import (
    PaperLifecycleError,
    PaperPositionManager,
    PaperProposalExecutionService,
)
from stockmarket.core.persistence import SQLiteDatabase, Store, migrate
from stockmarket.core.portfolio import PortfolioManager
from stockmarket.core.risk import OrderIntent


NOW = datetime(2026, 10, 5, 13, 50, tzinfo=timezone.utc)
SIGNAL_AT = NOW - timedelta(minutes=2)
INSTRUMENT = Instrument(
    instrument_id="XNAS:AAPL",
    symbol="AAPL",
    exchange="NASDAQ",
    market="US",
    asset_class=AssetClass.EQUITY,
    currency="USD",
    timezone="America/New_York",
    tick_size=0.01,
    trading_status=TradingStatus.ACTIVE,
)


class _SubmissionRepository:
    def __init__(self):
        self.records = {}

    def get(self, proposal_id):
        return self.records.get(proposal_id)

    def begin(self, **values):
        if values["proposal_id"] in self.records:
            return False
        self.records[values["proposal_id"]] = {
            **values,
            "state": "SUBMITTING",
        }
        return True

    def finish(self, proposal_id, *, state, quantity, error):
        self.records[proposal_id].update(
            state=state, quantity=quantity, error=error)

    def for_order(self, client_order_id):
        return next(
            (row for row in self.records.values()
             if row["client_order_id"] == client_order_id),
            None,
        )


class _ExitProposalRepository:
    def __init__(self):
        self.rows = []

    def for_entry(self, client_order_id):
        return [
            row for row in self.rows
            if row["entry_client_order_id"] == client_order_id
        ]

    def prepare(self, **row):
        self.rows.append({**row, "status": "PROPOSED"})

    def finish(self, proposal_id, *, status, risk_decision_id, error):
        row = next(row for row in self.rows if row["proposal_id"] == proposal_id)
        row.update(
            status=status,
            risk_decision_id=risk_decision_id,
            error=error,
        )


class _Store:
    def __init__(self, proposal, signal_row):
        self.proposal_submissions = _SubmissionRepository()
        self.position_exit_proposals = _ExitProposalRepository()
        self.trade_proposals = Mock()
        self.trade_proposals.get_by_id.return_value = {
            "proposal_id": proposal["proposal_id"],
            "evaluation_status": "APPROVED",
            "generation_id": proposal["generation_id"],
            "instrument_id": proposal["instrument_id"],
            "side": proposal["side"],
            "payload": proposal,
        }
        self.trade_proposals.get_by_signal.return_value = {
            "payload": proposal,
            "evaluation": {"status": "APPROVED"},
        }
        self.signals = Mock()
        self.signals.get.return_value = signal_row
        self.autonomous_research = Mock()
        self.autonomous_research.get_run.return_value = {
            "status": "COMPLETE",
            "stage": "STRATEGY_SELECTED",
            "payload": {"request": {"mode": "PAPER"}},
        }
        self.autonomous_research.get_candidate.return_value = {
            "stage": "SIGNAL_GENERATED",
        }
        self.signal_generations = Mock()
        self.signal_generations.get_by_signal.return_value = {
            "status": "SIGNAL_GENERATED",
            "run_id": proposal["run_id"],
            "candidate_id": proposal["candidate_id"],
            "generation_id": proposal["generation_id"],
            "strategy_name": proposal["strategy"],
            "strategy_version": proposal["strategy_version"],
            "signal_id": proposal["signal_id"],
            "generated_at": SIGNAL_AT.isoformat(),
        }
        self.fills = Mock()
        self.orders = Mock()


def _persisted_signal(proposal_id="proposal-1"):
    signal_id = str(uuid4())
    proposal = {
        "proposal_id": proposal_id,
        "signal_id": signal_id,
        "generation_id": "generation-1",
        "run_id": "run-1",
        "candidate_id": INSTRUMENT.instrument_id,
        "instrument_id": INSTRUMENT.instrument_id,
        "symbol": INSTRUMENT.symbol,
        "side": "BUY",
        "entry_price": 100.0,
        "stop_price": 99.0,
        "target_price": 102.0,
        "strategy": "orb_vwap",
        "strategy_version": "1.0.0",
        "risk_decision_id": str(uuid4()),
        "quantity": 10,
        "created_at": NOW.isoformat(),
        "provenance": {"evaluation_as_of": NOW.isoformat()},
    }
    signal_row = {
        "signal_id": signal_id,
        "instrument_id": INSTRUMENT.instrument_id,
        "symbol": INSTRUMENT.symbol,
        "timestamp": SIGNAL_AT.isoformat(),
        "strategy": "orb_vwap",
        "side": "BUY",
        "confidence": 0.9,
        "payload": {
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "expected_edge": 100.0,
            "reasons": ["ORB_BREAKOUT_ABOVE"],
        },
    }
    return proposal, signal_row


def _fake_order(status=OrderStatus.FILLED, client_order_id="paper-order"):
    return SimpleNamespace(
        client_order_id=client_order_id,
        instrument_id=INSTRUMENT.instrument_id,
        strategy="orb_vwap",
        status=status,
        error=None,
    )


class PaperProposalExecutionTests(TestCase):
    def setUp(self):
        self.proposal, signal_row = _persisted_signal()
        self.store = _Store(self.proposal, signal_row)
        self.trading = SimpleNamespace(
            mode=TradingMode.PAPER,
            portfolio=SimpleNamespace(positions=Mock(return_value={})),
            order_manager=SimpleNamespace(
                open_orders=Mock(return_value=()),
                get=Mock(return_value=_fake_order()),
            ),
            submit_signal=Mock(return_value=SimpleNamespace(
                order=_fake_order(OrderStatus.FILLED, "proposal-order"),
                risk=SimpleNamespace(decision_id=uuid4()),
                duplicate=False,
            )),
        )
        self.updates = []
        self.service = PaperProposalExecutionService(
            store=self.store,
            trading=self.trading,
            instruments={INSTRUMENT.instrument_id: INSTRUMENT},
            markets=SimpleNamespace(is_regular_session=Mock(return_value=True)),
            quotes=lambda _: (100.0, NOW),
            update_price=lambda *args: self.updates.append(args),
            strategies={"orb_vwap": SimpleNamespace(version="1.0.0")},
            max_age=timedelta(minutes=5),
            clock=lambda: NOW,
        )

    def test_approved_persisted_proposal_submits_via_trading_service_once(self):
        result = self.service.submit(
            "proposal-1",
            operator="operator",
            sizing_mode="MANUAL_OVERRIDE",
            quantity=5,
        )
        self.assertEqual(result["execution"], "PAPER_ORDER_CREATED")
        self.assertEqual(len(self.updates), 1)
        self.trading.submit_signal.assert_called_once()
        args, kwargs = self.trading.submit_signal.call_args
        self.assertEqual(args[1], 5)
        self.assertEqual(kwargs["client_order_id"], result["client_order_id"])
        self.assertEqual(
            self.store.proposal_submissions.records["proposal-1"]["state"],
            "ORDER_FILLED",
        )

    def test_stale_proposal_is_rejected_before_claim_or_order(self):
        self.service.clock = lambda: NOW + timedelta(minutes=10)
        with self.assertRaisesRegex(PaperLifecycleError, "stale"):
            self.service.submit(
                "proposal-1",
                operator="operator",
                sizing_mode="MANUAL_OVERRIDE",
                quantity=5,
            )
        self.trading.submit_signal.assert_not_called()
        self.assertEqual(self.store.proposal_submissions.records, {})

    def test_replay_returns_restored_order_without_second_submission(self):
        self.store.proposal_submissions.records["proposal-1"] = {
            "client_order_id": "proposal-" + uuid5(
                NAMESPACE_URL, "paper-order:proposal-1").hex,
            "state": "ORDER_FILLED",
        }
        result = self.service.submit(
            "proposal-1",
            operator="operator",
            sizing_mode="MANUAL_OVERRIDE",
            quantity=5,
        )
        self.assertTrue(result["duplicate"])
        self.trading.submit_signal.assert_not_called()


class PaperPositionManagerTests(TestCase):
    def test_stop_trigger_becomes_risk_checked_deterministic_exit(self):
        proposal, signal_row = _persisted_signal()
        store = _Store(proposal, signal_row)
        entry_client_id = "proposal-entry"
        store.fills.all.return_value = [{
            "client_order_id": entry_client_id,
            "instrument_id": INSTRUMENT.instrument_id,
            "timestamp": (NOW - timedelta(minutes=1)).isoformat(),
        }]
        store.orders.get.return_value = {
            "side": OrderSide.BUY.value,
            "strategy": "orb_vwap",
            "status": OrderStatus.FILLED.value,
        }
        store.proposal_submissions.records[proposal["proposal_id"]] = {
            "proposal_id": proposal["proposal_id"],
            "client_order_id": entry_client_id,
            "proposal_payload": proposal,
            "state": "ORDER_FILLED",
        }
        position = SimpleNamespace(
            instrument_id=INSTRUMENT.instrument_id,
            side=PositionSide.LONG,
            quantity=10,
            average_entry_price=100.0,
            opened_at=NOW - timedelta(minutes=1),
        )
        exit_order = _fake_order(OrderStatus.FILLED, "exit-order")
        exit_order = SimpleNamespace(**{
            **vars(exit_order),
            "side": OrderSide.SELL,
            "quantity": 10,
        })
        trading = SimpleNamespace(
            mode=TradingMode.PAPER,
            portfolio=SimpleNamespace(
                positions=Mock(return_value={INSTRUMENT.instrument_id: position})),
            submit=Mock(return_value=SimpleNamespace(
                order=exit_order,
                risk=SimpleNamespace(decision_id=uuid4()),
            )),
        )
        updates = []
        manager = PaperPositionManager(
            store=store,
            trading=trading,
            instruments={INSTRUMENT.instrument_id: INSTRUMENT},
            markets=SimpleNamespace(is_regular_session=Mock(return_value=True)),
            quotes=lambda _: (98.5, NOW),
            update_price=lambda *args: updates.append(args),
            max_age=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        result = manager.manage()

        self.assertEqual(result["positions"][0]["trigger_reason"], "STOP_LOSS")
        self.assertEqual(result["positions"][0]["status"], "FILLED")
        self.assertEqual(len(updates), 1)
        ticket = trading.submit.call_args.args[0]
        self.assertEqual(ticket.intent, OrderIntent.EXIT)
        self.assertEqual(ticket.side, OrderSide.SELL)
        self.assertEqual(ticket.quantity, 10)
        self.assertEqual(ticket.audit.exit_reason, "STOP_LOSS")
        self.assertEqual(store.position_exit_proposals.rows[0]["status"], "FILLED")

    def test_non_triggering_quote_does_not_create_exit_order(self):
        proposal, signal_row = _persisted_signal()
        store = _Store(proposal, signal_row)
        store.fills.all.return_value = [{
            "client_order_id": "proposal-entry",
            "instrument_id": INSTRUMENT.instrument_id,
            "timestamp": (NOW - timedelta(minutes=1)).isoformat(),
        }]
        store.orders.get.return_value = {
            "side": OrderSide.BUY.value,
            "strategy": "orb_vwap",
            "status": OrderStatus.FILLED.value,
        }
        store.proposal_submissions.records["proposal-1"] = {
            "proposal_id": "proposal-1",
            "client_order_id": "proposal-entry",
            "proposal_payload": proposal,
            "state": "ORDER_FILLED",
        }
        position = SimpleNamespace(
            instrument_id=INSTRUMENT.instrument_id,
            side=PositionSide.LONG,
            quantity=10,
            average_entry_price=100.0,
            opened_at=NOW - timedelta(minutes=1),
        )
        trading = SimpleNamespace(
            mode=TradingMode.PAPER,
            portfolio=SimpleNamespace(
                positions=Mock(return_value={INSTRUMENT.instrument_id: position})),
            submit=Mock(),
        )
        manager = PaperPositionManager(
            store=store,
            trading=trading,
            instruments={INSTRUMENT.instrument_id: INSTRUMENT},
            markets=SimpleNamespace(is_regular_session=Mock(return_value=True)),
            quotes=lambda _: (100.5, NOW),
            update_price=Mock(),
            max_age=timedelta(minutes=5),
            clock=lambda: NOW,
        )

        result = manager.manage()

        self.assertEqual(result["positions"][0]["status"], "MONITORED")
        trading.submit.assert_not_called()

    def test_pending_entry_order_is_not_extended_with_an_exit(self):
        position = SimpleNamespace(
            instrument_id=INSTRUMENT.instrument_id,
            side=PositionSide.LONG,
            quantity=5,
            average_entry_price=100.0,
            opened_at=NOW - timedelta(minutes=1),
        )
        trading = SimpleNamespace(
            mode=TradingMode.PAPER,
            portfolio=SimpleNamespace(
                positions=Mock(return_value={INSTRUMENT.instrument_id: position})),
            submit=Mock(),
        )
        updates = Mock()
        manager = PaperPositionManager(
            store=_Store(*_persisted_signal()),
            trading=trading,
            instruments={INSTRUMENT.instrument_id: INSTRUMENT},
            markets=SimpleNamespace(is_regular_session=Mock(return_value=True)),
            quotes=Mock(return_value=(98.5, NOW)),
            update_price=updates,
            max_age=timedelta(minutes=5),
            clock=lambda: NOW,
        )
        manager._entry_proposal = lambda _: {
            "client_order_id": "entry-order",
            "order": {"status": OrderStatus.PARTIALLY_FILLED.value},
            "proposal": {},
        }

        result = manager.manage()

        self.assertEqual(result["positions"][0]["status"], "ENTRY_ORDER_PENDING")
        manager.quotes.assert_not_called()
        updates.assert_not_called()
        trading.submit.assert_not_called()


class PaperFillPersistenceTests(TestCase):
    def test_paper_fill_is_linked_to_order_and_saved_idempotently(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        migrate(database)
        store = Store(database)
        portfolio = PortfolioManager("USD", 10_000.0)
        portfolio.on_fill = lambda fill: store.fills.save(fill)
        broker = PaperBroker(
            portfolio,
            {INSTRUMENT.instrument_id: INSTRUMENT},
            clock=lambda: NOW,
            market_status_fn=lambda _: True,
        )
        broker.connect()
        broker.update_price(INSTRUMENT.instrument_id, 100.0, NOW)
        order = ManagedOrder(
            client_order_id="stable-paper-order",
            broker_order_id=None,
            instrument_id=INSTRUMENT.instrument_id,
            symbol=INSTRUMENT.symbol,
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_price=None,
            timestamp=NOW,
            strategy="orb_vwap",
            signal_id=None,
            risk_decision_id=None,
            status=OrderStatus.SUBMITTED,
        )

        broker_order_id = broker.submit_order(order)

        persisted = store.fills.all()
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["client_order_id"], order.client_order_id)
        fill_id = persisted[0]["fill_id"]
        self.assertEqual(store.fills.save(portfolio.fills[0]), fill_id)
        self.assertEqual(len(store.fills.all()), 1)
        self.assertEqual(broker.order_status(broker_order_id).status, OrderStatus.FILLED)

    def test_durable_paper_order_and_fill_restore_after_restart(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        migrate(database)
        store = Store(database)

        def make_portfolio():
            portfolio = PortfolioManager("USD", 10_000.0)
            portfolio.on_fill = lambda fill: (
                store.fills.save(fill),
                store.positions.replace_all(
                    portfolio.positions().values(), fill.timestamp),
            )
            return portfolio

        portfolio = make_portfolio()
        broker = PaperBroker(
            portfolio,
            {INSTRUMENT.instrument_id: INSTRUMENT},
            clock=lambda: NOW,
            market_status_fn=lambda _: True,
            execution_repository=store.paper_execution,
            fills_repository=store.fills,
        )
        broker.connect()
        broker.update_price(INSTRUMENT.instrument_id, 100.0, NOW)
        order = ManagedOrder(
            client_order_id="durable-paper-order",
            broker_order_id=None,
            instrument_id=INSTRUMENT.instrument_id,
            symbol=INSTRUMENT.symbol,
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_price=None,
            timestamp=NOW,
            strategy="orb_vwap",
            signal_id=None,
            risk_decision_id=None,
            status=OrderStatus.SUBMITTED,
        )

        broker_id = broker.submit_order(order)
        self.assertEqual(store.paper_execution.get_by_client_order_id(
            order.client_order_id)["status"], "FILLED")
        self.assertEqual(len(store.fills.for_order(order.client_order_id)), 1)

        restored_portfolio = PortfolioManager("USD", 10_000.0)
        for fill in store.fills.all():
            restored_portfolio.apply_fill(
                INSTRUMENT, OrderSide(fill["side"]), fill["quantity"],
                fill["price"], datetime.fromisoformat(fill["timestamp"]),
                fee=fill["fee"], slippage=fill["slippage"],
                client_order_id=fill["client_order_id"],
                fill_sequence=fill["fill_sequence"])
        restored = PaperBroker(
            restored_portfolio,
            {INSTRUMENT.instrument_id: INSTRUMENT},
            clock=lambda: NOW,
            market_status_fn=lambda _: True,
            execution_repository=store.paper_execution,
            fills_repository=store.fills,
        )
        restored.connect()

        self.assertEqual(
            restored.order_status_by_client_id(order.client_order_id).broker_order_id,
            broker_id,
        )
        self.assertEqual(
            restored.order_status_by_client_id(order.client_order_id).status,
            OrderStatus.FILLED,
        )
        self.assertEqual(
            restored.submit_order(order),
            broker_id,
            "duplicate client identity must resolve to the original paper order",
        )
        self.assertEqual(len(store.fills.for_order(order.client_order_id)), 1)

    def test_failed_durable_fill_write_rolls_back_portfolio_and_order(self):
        database = SQLiteDatabase()
        self.addCleanup(database.close)
        migrate(database)
        store = Store(database)
        portfolio = PortfolioManager("USD", 10_000.0)

        def fail_persist(_fill):
            assert _fill.client_order_id == "failed-paper-write"
            raise RuntimeError("simulated durable fill write failure")

        portfolio.on_fill = fail_persist
        broker = PaperBroker(
            portfolio,
            {INSTRUMENT.instrument_id: INSTRUMENT},
            clock=lambda: NOW,
            market_status_fn=lambda _: True,
            execution_repository=store.paper_execution,
            fills_repository=store.fills,
        )
        broker.connect()
        broker.update_price(INSTRUMENT.instrument_id, 100.0, NOW)
        order = ManagedOrder(
            client_order_id="failed-paper-write",
            broker_order_id=None,
            instrument_id=INSTRUMENT.instrument_id,
            symbol=INSTRUMENT.symbol,
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_price=None,
            timestamp=NOW,
            strategy="orb_vwap",
            signal_id=None,
            risk_decision_id=None,
            status=OrderStatus.SUBMITTED,
        )

        with self.assertRaisesRegex(RuntimeError, "simulated durable fill"):
            broker.submit_order(order)

        self.assertEqual(portfolio.positions(), {})
        self.assertEqual(portfolio.cash["USD"], 10_000.0)
        self.assertIsNone(
            store.paper_execution.get_by_client_order_id(order.client_order_id))
        self.assertIsNone(
            broker.order_status_by_client_id(order.client_order_id))
