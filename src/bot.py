"""
Main Bot Orchestrator.

Coordinates all modules:
1. WalletTracker detects on-chain activity
2. EventParser converts raw events to structured trades
3. DecisionEngine decides whether to copy
4. RiskManager enforces limits
5. PolymarketExecutor places orders (or simulates in dry-run)

All activity is logged for audit and analysis.
"""

from __future__ import annotations

import asyncio
import signal
import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from src.config.settings import AppConfig, load_config
from src.engine.decision_engine import DecisionAction, DecisionEngine
from src.execution.polymarket_executor import PolymarketExecutor
from src.logging_mod.logger import get_logger, setup_logging
from src.parser.event_parser import EventParser, ParsedTrade
from src.risk.risk_manager import RiskManager
from src.tracker.wallet_tracker import RawTradeEvent, WalletTracker

logger = get_logger(__name__)
console = Console()


class CopyTradingBot:
    """
    Main orchestrator for the Polymarket copy-trading bot.

    Lifecycle:
    1. Load config
    2. Initialize all modules
    3. Start wallet tracking loop
    4. Process events through parser -> decision -> execution pipeline
    5. Log everything
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.tracker = WalletTracker(config)
        self.parser = EventParser()
        self.risk_manager = RiskManager(config)
        self.decision_engine = DecisionEngine(config, self.risk_manager)
        self.executor = PolymarketExecutor(config)

        self.event_queue: asyncio.Queue[RawTradeEvent] = asyncio.Queue()
        self._running = False
        self._stats = {
            "events_detected": 0,
            "trades_parsed": 0,
            "trades_executed": 0,
            "trades_skipped": 0,
            "errors": 0,
            "start_time": 0.0,
        }

    async def start(self) -> None:
        """Start the bot."""
        self._running = True
        self._stats["start_time"] = time.time()

        mode = "DRY RUN" if self.config.is_dry_run else "LIVE"
        console.print(f"\n[bold green]Polymarket Copy-Trading Bot[/bold green] - Mode: [bold yellow]{mode}[/bold yellow]")

        if self.config.is_dry_run:
            console.print("[yellow]No real trades will be executed. All activity is simulated.[/yellow]\n")
        else:
            console.print("[bold red]WARNING: LIVE MODE - Real trades will be executed![/bold red]\n")

        # Print watched wallets
        self._print_wallet_table()

        # Initialize modules
        await self.executor.initialize()
        self.tracker.load_wallets(self.config.active_wallets)

        # Update balance for percent-based sizing
        balance = await self.executor.get_balance()
        self.decision_engine.set_balance(balance)
        console.print(f"Account balance: [bold]${balance:,.2f}[/bold]\n")

        logger.info(
            "bot_started",
            mode=mode,
            wallets=len(self.config.active_wallets),
            balance=balance,
        )

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Run tracker and processor concurrently
        await asyncio.gather(
            self.tracker.run(self.event_queue),
            self._process_events(),
        )

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        logger.info("bot_stopping")
        console.print("\n[yellow]Shutting down...[/yellow]")

        self._running = False
        self.tracker.stop()
        await self.parser.close()

        self._print_stats()
        logger.info("bot_stopped", stats=self._stats)

    async def _process_events(self) -> None:
        """
        Main event processing loop.

        Reads raw events from the queue, parses them, evaluates through
        the decision engine, and executes if approved.
        """
        while self._running:
            try:
                # Wait for events with timeout so we can check _running
                try:
                    raw_event = await asyncio.wait_for(
                        self.event_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                self._stats["events_detected"] += 1
                logger.info(
                    "processing_event",
                    tx_hash=raw_event.tx_hash,
                    wallet=raw_event.matched_wallet.label,
                )

                # Parse the event
                parsed_trade = await self.parser.parse_event(raw_event)
                if parsed_trade is None:
                    logger.debug("event_not_parseable", tx_hash=raw_event.tx_hash)
                    continue

                self._stats["trades_parsed"] += 1

                # Log the detected trade
                console.print(f"\n[cyan]Trade detected:[/cyan] {parsed_trade.summary}")

                # Make decision
                decision = self.decision_engine.evaluate(parsed_trade)

                if decision.action == DecisionAction.SKIP:
                    self._stats["trades_skipped"] += 1
                    console.print(f"  [dim]Skipped: {decision.reason}[/dim]")
                    continue

                # Execute the trade
                console.print(
                    f"  [green]Executing {decision.action.value}: "
                    f"${decision.amount_usd:.2f} on {parsed_trade.outcome}[/green]"
                )

                if decision.action == DecisionAction.EXECUTE_BUY:
                    result = await self.executor.execute_buy(decision)
                    if result.success:
                        self.risk_manager.record_trade_opened(parsed_trade, result.filled_amount)
                else:
                    result = await self.executor.execute_sell(decision)
                    if result.success:
                        self.risk_manager.record_trade_closed(parsed_trade, result.filled_amount)

                if result.success:
                    self._stats["trades_executed"] += 1
                    console.print(f"  [green]{result.summary}[/green]")
                else:
                    self._stats["errors"] += 1
                    console.print(f"  [red]Execution failed: {result.error}[/red]")

                logger.info(
                    "trade_result",
                    success=result.success,
                    summary=result.summary,
                )

                # Refresh balance after trade
                balance = await self.executor.get_balance()
                self.decision_engine.set_balance(balance)

            except Exception:
                self._stats["errors"] += 1
                logger.exception("event_processing_error")

    def _print_wallet_table(self) -> None:
        """Print a table of watched wallets."""
        table = Table(title="Watched Wallets")
        table.add_column("Label", style="cyan")
        table.add_column("Address", style="dim")
        table.add_column("Buys", justify="center")
        table.add_column("Sells", justify="center")
        table.add_column("Max Risk", justify="right", style="yellow")

        for w in self.config.active_wallets:
            table.add_row(
                w.label or "unnamed",
                f"{w.address[:8]}...{w.address[-6:]}",
                "Y" if w.copy_buys else "N",
                "Y" if w.copy_sells else "N",
                f"${w.max_risk_usd:,.0f}",
            )

        console.print(table)

    def _print_stats(self) -> None:
        """Print session statistics."""
        runtime = time.time() - self._stats["start_time"]
        minutes = int(runtime / 60)
        seconds = int(runtime % 60)

        table = Table(title="Session Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        table.add_row("Runtime", f"{minutes}m {seconds}s")
        table.add_row("Events Detected", str(self._stats["events_detected"]))
        table.add_row("Trades Parsed", str(self._stats["trades_parsed"]))
        table.add_row("Trades Executed", str(self._stats["trades_executed"]))
        table.add_row("Trades Skipped", str(self._stats["trades_skipped"]))
        table.add_row("Errors", str(self._stats["errors"]))

        risk_status = self.risk_manager.get_status()
        table.add_row("Open Positions", str(risk_status["open_positions"]))
        table.add_row("Total Exposure", f"${risk_status['total_exposure_usd']:,.2f}")
        table.add_row("Daily PnL", f"${risk_status['daily_pnl_usd']:,.2f}")

        console.print(table)


async def run_bot(config_path: str = "config.json") -> None:
    """Entry point to run the bot."""
    config = load_config(config_path)
    setup_logging(config.env.log_level, config.env.log_file)

    bot = CopyTradingBot(config)
    await bot.start()
