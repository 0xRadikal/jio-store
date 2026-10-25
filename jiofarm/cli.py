"""JioFarm — Typer CLI with Rich live dashboard."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from jiofarm.config import Config, load_config
from jiofarm.console import console
from jiofarm.grizzly.refund import RefundWorker
from jiofarm.hunter import Phase, SMSProvider, WorkerState, create_provider, hunt_once
from jiofarm.shield import install_doh_shield
from jiofarm.storage.store import Store

# ------------------------------------------------------------------- app

app = typer.Typer(
    name="jiofarm",
    help="Jio Google AI Pro Hunter — panen link redeem Gemini Pro",
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
)

# ------------------------------------------------------------------- helpers


def _parse_duration(s: str) -> float:
    """Parse a duration string like '2h', '30m', '1h30m' into seconds."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s.strip())
    if m and any(m.groups()):
        h, mi, se = (int(g) if g else 0 for g in m.groups())
        seconds = h * 3600 + mi * 60 + se
        if seconds > 0:
            return seconds
    raise typer.BadParameter(f"durasi tidak valid: '{s}' (contoh: 2h, 30m, 1h30m)")


def _phase_icon(phase: Phase) -> str:
    icons = {
        Phase.IDLE: "⏳",
        Phase.RENTING: "📱",
        Phase.CHECKING: "🔍",
        Phase.SENDING_OTP: "📤",
        Phase.WAITING_OTP: "⏳",
        Phase.VALIDATING: "🔐",
        Phase.HUNTING: "🎯",
        Phase.DONE: "✅",
        Phase.ERROR: "❌",
    }
    return icons.get(phase, "❓")


def _phase_color(phase: Phase) -> str:
    colors = {
        Phase.IDLE: "dim",
        Phase.RENTING: "cyan",
        Phase.CHECKING: "cyan",
        Phase.SENDING_OTP: "yellow",
        Phase.WAITING_OTP: "yellow",
        Phase.VALIDATING: "yellow",
        Phase.HUNTING: "magenta",
        Phase.DONE: "green",
        Phase.ERROR: "red",
    }
    return colors.get(phase, "white")


# ------------------------------------------------------------------- live dashboard


def _build_layout(
    provider: SMSProvider,
    workers: list[WorkerState],
    found: int,
    elapsed: float,
    refunds: RefundWorker,
    cfg: Config,
    links: list[str],
    initial_balance: float | None = None,
) -> Layout:
    """Build the Rich Layout for the live dashboard."""
    layout = Layout()
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )

    # header ---------------------------------------------------------------
    header_text = Text()
    header_text.append("🦁 JioFarm Hunter", style="bold yellow")
    header_text.append(f"  |  workers: {cfg.effective_concurrency}", style="dim")
    header_text.append(f"  |  max-price: {cfg.effective_max_price} (account currency)", style="dim")
    header_text.append(f"  |  refund pending: {refunds.pending}", style="dim")
    header_text.append(f"\n⏱  {int(elapsed)}s elapsed", style="cyan")
    # Rendering must not issue blocking/rate-limited provider requests.
    if initial_balance is not None:
        header_text.append(f"  |  initial balance: {initial_balance:,.4f} (account currency)", style="bold green")
    header_text.append(f"  |  🎯 {found} links", style="bold green")
    layout["header"].update(Panel(header_text, box=box.ROUNDED, border_style="cyan"))

    # body -----------------------------------------------------------------
    body_layout = Layout()
    body_layout.split_row(
        Layout(name="workers", ratio=2),
        Layout(name="links_panel", ratio=1),
    )
    layout["body"].update(body_layout)

    # worker table
    wt = Table(
        title="Workers",
        box=box.SIMPLE_HEAVY,
        border_style="dim",
        show_header=True,
        header_style="bold cyan",
    )
    wt.add_column("W", style="dim", width=3)
    wt.add_column("Phase", width=12)
    wt.add_column("Phone", width=14)
    wt.add_column("Status", width=28)
    for w in workers:
        status = ""
        if w.phase == Phase.DONE:
            status = f"[green]LINK![/]" if w.link else f"[yellow]{w.error or 'no promo'}[/]"
        elif w.phase == Phase.ERROR:
            status = f"[red]{w.error}[/]"
        elif w.phase == Phase.WAITING_OTP:
            status = "menunggu OTP..."
        elif w.phase == Phase.HUNTING:
            status = "[magenta]mencari link...[/]"
        elif w.phase == Phase.RENTING:
            status = "menyewa nomor..."
        elif w.phase == Phase.CHECKING:
            status = "cek pelanggan..."
        elif w.phase == Phase.SENDING_OTP:
            status = "kirim OTP..."
        elif w.phase == Phase.VALIDATING:
            status = "validasi OTP..."
        else:
            status = "[dim]idle[/]"
        phone = w.phone if w.phone else "-"
        wt.add_row(
            str(w.wid),
            f"[{_phase_color(w.phase)}]{_phase_icon(w.phase)} {w.phase.name}[/]",
            phone,
            status,
        )
    body_layout["workers"].update(Panel(wt, box=box.ROUNDED, border_style="cyan"))

    # links sidebar
    if links:
        links_text = "\n\n".join(
            f"[link]{l}[/]" for l in links[-10:]
        )
    else:
        links_text = "[dim]belum ada link ditemukan[/]"
    body_layout["links_panel"].update(Panel(
        links_text,
        title="🎯 Links Found",
        border_style="green",
        box=box.ROUNDED,
    ))

    # footer ---------------------------------------------------------------
    footer_text = Text("Ctrl+C to stop", style="dim")
    layout["footer"].update(Panel(footer_text, box=box.ROUNDED, border_style="dim"))

    return layout


# ------------------------------------------------------------------- commands


@app.command()
def balance(
    ctx: typer.Context,
) -> None:
    """Cek saldo provider."""
    cfg = load_config()
    provider = create_provider(cfg)
    label = cfg.provider.upper()
    try:
        bal = provider.balance()
        console.print(f"💰 Saldo {label}: [success]{bal:,.4f} (account currency)[/]")
    except Exception as e:
        console.print("Balance check failed: " + cfg.redact(str(e)), markup=False)
        raise typer.Exit(code=1) from None


@app.command()
def run(
    count: Optional[int] = typer.Option(None, "--count", min=1, help="Hard cap on purchase attempts, including duration/target modes"),
    target: Optional[int] = typer.Option(None, "--target", min=1, help="Berhenti setelah N link ketemu"),
    duration: Optional[str] = typer.Option(None, "--duration", help="Jalan selama (2h/30m/1h30m)"),
    concurrency: Optional[int] = typer.Option(None, "-c", "--concurrency", min=1, help="Jumlah sewa paralel aktif"),
    max_price: Optional[float] = typer.Option(None, "--max-price", min=0, help="Maximum price per number in provider account currency"),
    db: Optional[str] = typer.Option(None, "--db", help="SQLite path; defaults to DB_PATH"),
) -> None:
    """Jalankan perburuan link Google AI Pro."""
    cfg = load_config()
    cfg.max_price_override = max_price
    cfg.concurrency_override = concurrency
    if db is not None:
        cfg.db_path = db
    if (target is not None or duration is not None) and count is None:
        raise typer.BadParameter("--target and --duration require an explicit --count purchase cap")
    cfg.count = 1 if count is None else count

    if target is not None:
        cfg.target = target
    if duration is not None:
        cfg.duration = _parse_duration(duration)

    try:
        cfg.validate()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None

    install_doh_shield()
    provider = create_provider(cfg)
    store = Store(cfg.db_path)

    # initial balance
    try:
        bal = provider.balance()
        console.print(f"💰 Saldo ({cfg.provider}): [success]{bal:,.4f} (account currency)[/]")
    except Exception as exc:
        store.conn.close()
        console.print("Cannot verify provider balance: " + cfg.redact(str(exc)), markup=False)
        raise typer.Exit(code=1) from None

    refunds = RefundWorker(provider, log=lambda msg: console.log(cfg.redact(msg), markup=False))
    workers_count = cfg.effective_concurrency
    workers = [WorkerState(wid=i + 1) for i in range(workers_count)]
    slots = threading.Semaphore(workers_count)
    found: list[str] = []
    done_count = [0]
    stop = threading.Event()
    start = time.monotonic()
    lock = threading.Lock()
    failed = [False]

    def hit_stop() -> bool:
        if stop.is_set():
            return True
        if cfg.duration and time.monotonic() - start >= cfg.duration:
            return True
        if cfg.target and len(found) >= cfg.target:
            return True
        if done_count[0] >= cfg.count:
            return True
        return False

    def job(ws: WorkerState) -> None:
        while not hit_stop():
            with lock:
                if hit_stop():
                    return
                done_count[0] += 1
            try:
                link = hunt_once(provider, store, refunds, slots, cfg, ws)
            except Exception as e:
                ws.phase = Phase.ERROR
                ws.error = cfg.redact(str(e))
                link = None
            if ws.phase == Phase.ERROR:
                failed[0] = True
                stop.set()  # No further purchases after an unresolved failure.
            if link:
                with lock:
                    found.append(link)
            elif not hit_stop():
                time.sleep(3)

    # start worker threads
    threads = [
        threading.Thread(target=job, args=(workers[i],), daemon=False)
        for i in range(min(workers_count, cfg.count))
    ]
    # Cleanup also covers a failed thread start or dashboard renderer.
    try:
        for t in threads:
            t.start()
        with Live(
            _build_layout(provider, workers, len(found), 0, refunds, cfg, found, bal),
            console=console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            while any(t.is_alive() for t in threads):
                if hit_stop():
                    stop.set()
                time.sleep(0.5)
                elapsed = time.monotonic() - start
                live.update(
                    _build_layout(provider, workers, len(found), elapsed, refunds, cfg, found, bal)
                )
                # Stop new work, but never abandon an active order.
            stop.set()
    except KeyboardInterrupt:
        console.print("\n[warning]Dihentikan oleh user.[/]")
        stop.set()

    except Exception as exc:
        failed[0] = True
        console.print(f"Execution/display failure ({type(exc).__name__}); draining active orders.", markup=False)
    finally:
        stop.set()
        # A purchased activation can outlive 30 seconds. Drain every worker
        # before waiting for refunds, so late refund jobs cannot be lost.
        for t in threads:
            if t.ident is not None:
                t.join()
        try:
            refunded = refunds.wait_all(timeout=900)
        finally:
            store.conn.close()

    elapsed = time.monotonic() - start
    if not refunded:
        console.print("Refunds remain unconfirmed; reconcile activation IDs in the database before running again.", markup=False)
        raise typer.Exit(code=1)

    # final summary
    console.print()
    summary = Panel(
        f"[bold]Selesai dalam {int(elapsed)}s[/]\n\n"
        f"📱 Nomor diproses : {done_count[0]}\n"
        f"🎯 Link ditemukan  : {len(found)}",
        title="🦁 JioFarm Summary",
        border_style="green",
        box=box.ROUNDED,
    )
    console.print(summary)

    if found:
        console.print("Links found:")
        for l in found:
            console.print(l, markup=False)
    if failed[0]:
        for worker in workers:
            if worker.error:
                console.print(cfg.redact(worker.error), markup=False)
        raise typer.Exit(code=1)


@app.command()
def links(
    out: Optional[str] = typer.Option(None, "--out", help="Ekspor ke file txt"),
    db: str = typer.Option("results.db", "--db", help="Path database SQLite"),
) -> None:
    """Lihat semua link yang sudah ditemukan."""
    store = Store(db)
    all_links = store.all_links()
    if out:
        Path(out).write_text("\n".join(all_links), encoding="utf-8")
        console.print(f"[success]{len(all_links)} link diekspor ke {out}[/]")
    else:
        if not all_links:
            console.print("[warning]Belum ada link tersimpan.[/]")
        for i, l in enumerate(all_links, 1):
            console.print(f"[dim]{i}.[/] [link]{l}[/]")


@app.command()
def stats(
    db: str = typer.Option("results.db", "--db", help="Path database SQLite"),
) -> None:
    """Ringkasan statistik perburuan."""
    st = Store(db).stats()
    table = Table(title="📊 Statistik JioFarm", box=box.ROUNDED, border_style="cyan")
    table.add_column("Metrik", style="bold cyan")
    table.add_column("Nilai", style="bold white", justify="right")
    table.add_row("Total percobaan", str(st["hunts"]))
    table.add_row("Login sukses", str(st["logins"]))
    table.add_row("Link ditemukan", f"[success]{st['links']}[/]")
    console.print(table)


# ------------------------------------------------------------------- interactive menu (no args)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
) -> None:
    """Jio Google AI Pro Hunter — interactive menu when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return

    # interactive menu
    console.clear()
    console.print()
    console.print(
        Panel.fit(
            "[bold yellow]🦁  JioFarm — Google AI Pro Hunter[/]\n\n"
            "Panen link redeem Gemini Pro dari Jio",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )
    console.print()

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    table.add_column("", style="bold cyan", width=4)
    table.add_column("")
    table.add_row("1", "Run Hunter")
    table.add_row("2", "Check Balance")
    table.add_row("3", "View Stats")
    table.add_row("4", "Export Links")
    table.add_row("5", "Quit")
    console.print(table)
    console.print()

    choice = Prompt.ask("[bold]Pilih menu[/]", choices=["1", "2", "3", "4", "5"], default="1")

    if choice == "1":
        # interactive run setup
        console.print()
        mp = Prompt.ask("  Maximum price (provider account currency)", default="0.3")
        mode = Prompt.ask(
            "  Mode",
            choices=["single", "duration", "target"],
            default="single",
        )

        args = ["run", "--max-price", mp]
        if mode == "duration":
            dur = Prompt.ask("  Durasi (contoh: 2h, 30m, 1h30m)", default="1h")
            args.extend(["--duration", dur])
        elif mode == "target":
            tgt = Prompt.ask("  Target jumlah link", default="3")
            args.extend(["--target", tgt])
        else:
            args.extend(["--count", "1"])

        if mode != "single":
            cap = Prompt.ask("  Maximum purchase attempts", default="1")
            args.extend(["--count", cap])
        c = Prompt.ask("  Concurrency", default="1")
        args.extend(["-c", c])

        # relaunch the run command
        from jiofarm.cli import app as _app
        import sys

        sys.argv = ["jiofarm"] + args
        _app()

    elif choice == "2":
        from jiofarm.cli import app as _app
        import sys

        sys.argv = ["jiofarm", "balance"]
        _app()

    elif choice == "3":
        from jiofarm.cli import app as _app
        import sys

        sys.argv = ["jiofarm", "stats"]
        _app()

    elif choice == "4":
        from jiofarm.cli import app as _app
        import sys

        sys.argv = ["jiofarm", "links"]
        _app()

    else:
        console.print("[dim]Bye! 👋[/]")