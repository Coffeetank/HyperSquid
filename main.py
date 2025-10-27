from hypersquid import Trading, CopyTrader
import eth_account
import time
import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration from environment variables
PRIVATE_KEY = os.getenv('PRIVATE_KEY', '')
SOURCE_ADDRESS = os.getenv('SOURCE_ADDRESS', '')
NETWORK = os.getenv('NETWORK', 'mainnet')
SYNC_INTERVAL_SECONDS = int(os.getenv('SYNC_INTERVAL_SECONDS', '30'))
REQUIRE_MANUAL_CONFIRMATION = os.getenv('REQUIRE_MANUAL_CONFIRMATION', 'true').lower() == 'true'

def get_user_confirmation(description: str) -> bool:
    """Get user confirmation with Y/n prompt."""
    print("\n" + "="*80)
    print("COPY TRADING SYNC PLAN")
    print("="*80)
    print(description)
    print("="*80)

    while True:
        response = input("Execute this sync plan? (Y/n): ").strip().lower()
        if response in ['y', 'yes', '']:
            return True
        elif response in ['n', 'no']:
            return False
        else:
            print("Please enter Y or n.")

def main():
    print("HyperSquid - Continuous Copy Trading")
    print(f"Source: {SOURCE_ADDRESS}")
    print(f"Network: {NETWORK}")
    print(f"Sync Interval: {SYNC_INTERVAL_SECONDS} seconds")
    print("-" * 50)

    # Initialize trader and copier
    wallet = eth_account.Account.from_key(PRIVATE_KEY)
    trader = Trading(wallet, network=NETWORK)
    copier = CopyTrader(trader, source_address=SOURCE_ADDRESS, network=NETWORK, require_confirmation=False)

    sync_count = 0

    try:
        while True:
            sync_count += 1
            print(f"\n--- Sync #{sync_count} ---")

            if sync_count == 1 and REQUIRE_MANUAL_CONFIRMATION:
                # First run: require manual confirmation
                print("Performing initial sync with manual confirmation...")
                result = copier.sync_once(manual_confirm=True)

                if result.get('requires_confirmation'):
                    description = result['description']
                    if not get_user_confirmation(description):
                        print("Sync cancelled by user.")
                        sys.exit(0)

                    # Execute the plan
                    print("\nExecuting sync plan...")
                    execution_result = copier.execute_plan(result['plan'])
                    print("Sync completed successfully!")
                    print(f"Orders placed: {len(execution_result['orders_placed'])}")
                    print(f"Orders cancelled: {len(execution_result['orders_cancelled'])}")

            else:
                # Subsequent runs: execute automatically
                manual_confirm = REQUIRE_MANUAL_CONFIRMATION if sync_count == 1 else False
                print(f"Performing sync #{sync_count}...")
                result = copier.sync_once(manual_confirm=manual_confirm)

                if result.get('requires_confirmation'):
                    description = result['description']
                    if not get_user_confirmation(description):
                        print("Sync cancelled by user.")
                        continue

                    # Execute the plan
                    print("\nExecuting sync plan...")
                    execution_result = copier.execute_plan(result['plan'])
                    print("Sync completed successfully!")
                    print(f"Orders placed: {len(execution_result['orders_placed'])}")
                    print(f"Orders cancelled: {len(execution_result['orders_cancelled'])}")
                else:
                    print("Sync completed successfully!")
                    if 'orders_placed' in result:
                        print(f"Orders placed: {len(result['orders_placed'])}")
                        print(f"Orders cancelled: {len(result['orders_cancelled'])}")
                    else:
                        print("No changes needed.")

            # Wait for next sync
            print(f"\nWaiting {SYNC_INTERVAL_SECONDS} seconds until next sync...")
            time.sleep(SYNC_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n\nCopy trading stopped by user (Ctrl+C).")
        sys.exit(0)
    except Exception as e:
        print(f"\nError during sync: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
