from src.doorman_agent.agent import CeleryDoorman
import argparse
import sys

from src.doorman_agent.config import load_config
from src.doorman_agent.simulator import run_simulation


def main():
    parser = argparse.ArgumentParser(
        description='Doorman Agent - Celery/Redis Monitoring Agent',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run agent (production mode - sends metrics to doorman.com)
  DOORMAN_API_KEY=your-api-key doorman-agent --config config.yaml
  
  # Single check (for testing)
  DOORMAN_API_KEY=your-api-key doorman-agent --once
  
  # Run without API (local logging only)
  doorman-agent --config config.yaml

Simulation Mode (for demos/testing - no API key required):
  # Healthy scenario: 1 worker, no pending tasks
  doorman-agent --simulate --workers 1
  
  # Workers down scenario
  doorman-agent --simulate --workers 0
  
  # EMERGENCY scenario: workers down + tasks pending
  doorman-agent --simulate --workers 0 --enqueue 5

Environment Variables:
  DOORMAN_API_KEY      - Your doorman.com API key (required for production)
  DOORMAN_API_URL      - API URL (default: https://api.doorman.com)
  REDIS_URL            - Redis connection URL
  CELERY_BROKER_URL    - Celery broker URL
  CHECK_INTERVAL       - Check interval in seconds

Get your API key at: https://doorman.com/dashboard/api-keys
        """
    )
    
    # Production arguments
    parser.add_argument(
        '--config', '-c',
        help='Path to YAML configuration file'
    )
    parser.add_argument(
        '--once', '-1',
        action='store_true',
        help='Run only once (for testing)'
    )
    parser.add_argument(
        '--api-key', '-k',
        help='Doorman API key (can also use DOORMAN_API_KEY env var)'
    )
    parser.add_argument(
        '--api-url',
        help='Doorman API URL (default: https://api.doorman.com)'
    )
    
    # Simulation arguments
    parser.add_argument(
        '--simulate', '-s',
        action='store_true',
        help='Run in simulation mode (starts local Redis + workers, no API required)'
    )
    parser.add_argument(
        '--workers', '-w',
        type=int,
        default=1,
        help='Number of workers to simulate (0 = no workers, simulates failure)'
    )
    parser.add_argument(
        '--enqueue', '-e',
        type=int,
        default=0,
        help='Number of tasks to enqueue in simulation'
    )
    
    args = parser.parse_args()
    
    # Verify minimum dependencies
    if not REDIS_AVAILABLE or not CELERY_AVAILABLE:
        print("\n❌ Missing dependencies. Install with:")
        print("   pip install redis celery pyyaml")
        sys.exit(1)
    
    # Simulation mode (no API key required)
    if args.simulate:
        run_simulation(args.workers, args.enqueue)
        return
    
    # Production mode
    # Load configuration
    config = load_config(args.config)
    
    # Override with CLI arguments if provided
    if args.api_key:
        config.api_key = args.api_key
    if args.api_url:
        config.api_url = args.api_url
    
    # Warn if no API key (but allow for local testing)
    if not config.api_key:
        print("\n⚠️  No API key configured. Metrics will only be logged locally.")
        print("   Set DOORMAN_API_KEY or use --api-key to enable cloud monitoring.")
        print("   Get your API key at: https://doorman.com/dashboard/api-keys\n")
    
    # Create and run agent
    doorman = CeleryDoorman(config)
    
    if not doorman.collector.connect():
        print("❌ Could not connect to Redis/Celery")
        sys.exit(1)
    
    if args.once:
        # Single check mode
        doorman.check_once()
    else:
        # Daemon mode
        doorman.run()


if __name__ == '__main__':
    main()
