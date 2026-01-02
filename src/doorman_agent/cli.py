"""
Command-line interface for Doorman Agent
"""

from __future__ import annotations

import argparse
import sys

from doorman_agent.api_client import AGENT_VERSION


def main():
    parser = argparse.ArgumentParser(
        description="Doorman Agent - Celery/Redis Monitoring Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Version: {AGENT_VERSION}

Examples:
  # Run agent (sends metrics to doorman.com API)
  DOORMAN_API_KEY=your-api-key doorman-agent --config config.yaml

  # Run in local mode (only logs, no API calls) - for testing
  doorman-agent --config config.yaml --local

  # Single check (for testing)
  doorman-agent --once --local

  # Audit mode - one-time health check with formatted report
  doorman-agent --audit
  doorman-agent --audit --samples 3 --interval 10

  # Deep audit - includes Redis/Celery configuration analysis
  doorman-agent --audit --deep
  doorman-agent --audit --config-check  # alias for --deep

Simulation Mode (for demos/testing):
  doorman-agent --simulate --workers 1
  doorman-agent --simulate --workers 0 --enqueue 5

Environment Variables:
  DOORMAN_API_KEY      - Your doorman.com API key (required for API mode)
  DOORMAN_API_URL      - API URL (default: https://api.doorman.com)
  DOORMAN_LOCAL_MODE   - Set to 'true' for local mode
  REDIS_URL            - Redis connection URL
  CELERY_BROKER_URL    - Celery broker URL
  CHECK_INTERVAL       - Check interval in seconds

Get your API key at: https://doorman.com/dashboard/api-keys
        """,
    )

    parser.add_argument("--config", "-c", help="Path to YAML configuration file")
    parser.add_argument("--once", "-1", action="store_true", help="Run only once (for testing)")
    parser.add_argument(
        "--local",
        "-l",
        action="store_true",
        help="Local mode: only log metrics, do not send to API",
    )
    parser.add_argument(
        "--api-key", "-k", help="Doorman API key (can also use DOORMAN_API_KEY env var)"
    )
    parser.add_argument("--api-url", help="Doorman API URL (default: https://api.doorman.com)")
    parser.add_argument(
        "--version", "-v", action="version", version=f"doorman-agent {AGENT_VERSION}"
    )

    # Audit arguments
    parser.add_argument(
        "--audit",
        "-a",
        action="store_true",
        help="Run a one-time audit and print a formatted health report",
    )
    parser.add_argument(
        "--deep",
        "-d",
        action="store_true",
        help="Run deep configuration analysis (Redis/Celery settings)",
    )
    parser.add_argument(
        "--config-check",
        action="store_true",
        dest="config_check",
        help="Alias for --deep",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Number of samples for audit (default: 1, use >1 for trend detection)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Seconds between audit samples (default: 10)",
    )

    # Simulation arguments
    parser.add_argument(
        "--simulate",
        "-s",
        action="store_true",
        help="Run in simulation mode (starts local Redis + workers)",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=1,
        help="Number of workers to simulate (0 = no workers)",
    )
    parser.add_argument(
        "--enqueue", "-e", type=int, default=0, help="Number of tasks to enqueue in simulation"
    )

    args = parser.parse_args()

    # Simulation mode
    if args.simulate:
        from doorman_agent.simulator import run_simulation

        run_simulation(args.workers, args.enqueue)
        return

    # Import after arg parsing
    from doorman_agent.config import load_config

    # Load configuration
    config = load_config(args.config)

    # CLI overrides
    if args.local:
        config.local_mode = True
    if args.api_key:
        config.api_key = args.api_key
    if args.api_url:
        config.api_url = args.api_url

    # Audit mode
    if args.audit:
        from doorman_agent.audit import run_audit

        # --deep or --config-check enables deep mode
        deep_mode = args.deep or args.config_check

        exit_code = run_audit(
            config=config,
            samples=args.samples,
            interval=args.interval,
            deep=deep_mode,
        )
        sys.exit(exit_code)

    # Validate config for daemon/once modes
    if not config.local_mode and not config.api_key:
        print("\n⚠️  No API key configured.")
        print("   Either set DOORMAN_API_KEY or use --local for local mode.")
        print("   Get your API key at: https://doorman.com/dashboard/api-keys\n")
        sys.exit(1)

    # Create agent
    from doorman_agent.agent import DoormanAgent

    agent = DoormanAgent(config)

    # Run (connection happens inside run/check_once)
    if args.once:
        # For --once mode, we need to connect first
        if not agent.collector.connect():
            print("❌ Could not connect to Redis/Celery")
            sys.exit(1)
        agent.check_once()
    else:
        # run() handles connection internally
        agent.run()


if __name__ == "__main__":
    main()
