#!/usr/bin/env python3
"""
Main entry point for TikTok → YouTube automation.

Usage examples:
  python run.py --slot 1                          # First daily upload (all channels)
  python run.py --slot 2                          # Second daily upload (all channels)
  python run.py --slot 1 --channel channel_1      # One channel only (testing)
  python run.py --slot 1 --dry-run                # Full pipeline without uploading
  python run.py --slot 1 --dry-run --channel channel_1
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Project root on sys.path so "from src.x" works
sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config
from src.db import init_db
from src.orchestrator import run_all_channels


def setup_logging(log_level: str) -> Path:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)),
        ],
    )
    # Suppress noisy third-party loggers
    logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
    logging.getLogger("google.auth").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TikTok → YouTube Automation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--slot", type=int, choices=[1, 2], default=None,
        help="Upload slot: 1=first daily upload, 2=second daily upload",
    )
    parser.add_argument(
        "--channel", type=str, default=None, metavar="CHANNEL_ID",
        help="Only run this channel ID. Useful for testing a single channel.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run full pipeline but skip the actual YouTube upload.",
    )
    parser.add_argument(
        "--summary-only", action="store_true",
        help="Skip uploads — just send the Discord daily summary and exit.",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    if not args.summary_only and args.slot is None:
        parser.error("--slot is required unless --summary-only is set")

    log_file = setup_logging(args.log_level)
    logger = logging.getLogger("run")

    logger.info("=" * 60)
    if args.summary_only:
        logger.info("TikTok→YouTube Automation | Summary Only")
    else:
        logger.info("TikTok→YouTube Automation | Slot %d | dry_run=%s", args.slot, args.dry_run)
    logger.info("=" * 60)

    init_db()

    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    # Allow --dry-run from .env too (e.g. DRY_RUN=true in GitHub Actions for testing)
    effective_dry_run = args.dry_run or config.get("dry_run", False)
    if effective_dry_run and not args.dry_run:
        logger.info("DRY_RUN=true in .env — no uploads will happen")

    # --summary-only: just send the Discord daily summary and exit
    if args.summary_only:
        from src.db import get_todays_run_summary
        from src.notifier import send_daily_summary
        webhook_url = config.get("discord_webhook_url", "")
        if not webhook_url:
            logger.error("No DISCORD_WEBHOOK_URL configured — cannot send summary")
            sys.exit(1)
        channel_names = {
            ch["id"]: ch.get("youtube_channel_name", "")
            for ch in config.get("channels", [])
        }
        send_daily_summary(
            webhook_url=webhook_url,
            db_rows=get_todays_run_summary(),
            channel_names=channel_names,
        )
        logger.info("Daily summary sent. Log saved to: %s", log_file)
        sys.exit(0)

    results = run_all_channels(
        config=config,
        slot=args.slot,
        channel_filter=args.channel,
        dry_run=effective_dry_run,
    )

    # Exit code signals success/failure to GitHub Actions
    failed = sum(1 for r in results if r["status"] == "failed")
    logger.info("Log saved to: %s", log_file)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
