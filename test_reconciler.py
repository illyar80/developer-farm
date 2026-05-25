#!/usr/bin/env python3
"""
Demo: Reconciler monitoring real threads
-----------------------------------------
This shows how the reconciler would monitor actual pipeline runs.

Usage:
    Terminal 1: python test_reconciler.py --reconciler
    Terminal 2: python test_reconciler.py --run-pipeline
"""

import argparse
import asyncio
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from graph.reconciler import get_reconciler


def create_test_checkpoint(
    thread_id: str, iteration: int = 0, final_passed: bool = None
):
    """Creates a test checkpoint in the database."""
    checkpoint_db = "./data/checkpoints.db"
    Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)

    with SqliteSaver.from_conn_string(checkpoint_db) as checkpointer:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

        # Simulate a checkpoint structure
        checkpoint = {
            "v": 1,
            "id": f"checkpoint-{iteration}",
            "ts": "2026-05-25T20:00:00.000000+00:00",
            "channel_values": {
                "user_spec_path": "work/mvp/user-spec.md",
                "feature_name": "test-feature",
                "thread_id": thread_id,
                "iteration": iteration,
                "artifacts": [],
                "verdicts": [],
            },
            "channel_versions": {},
            "versions_seen": {},
        }

        if final_passed is not None:
            checkpoint["channel_values"]["final_passed"] = final_passed

        checkpointer.put(config, checkpoint, {}, {})
        print(
            f"✅ Created checkpoint for {thread_id} (iteration={iteration}, final_passed={final_passed})"
        )


async def run_reconciler():
    """Runs the reconciler in monitoring mode."""
    reconciler = get_reconciler()

    # Register some threads
    reconciler.register_thread("pipeline-001", {"feature": "calculator"})
    reconciler.register_thread("pipeline-002", {"feature": "auth"})

    # Run forever
    await reconciler.run_forever()


async def simulate_pipeline():
    """Simulates a running pipeline with checkpoints."""
    reconciler = get_reconciler()
    thread_id = "pipeline-001"

    reconciler.register_thread(thread_id, {"feature": "calculator"})

    print("\n🚀 Simulating pipeline run...")

    # Iteration 0: Initial state
    create_test_checkpoint(thread_id, iteration=0)
    reconciler.update_heartbeat(thread_id)
    await asyncio.sleep(2)

    # Iteration 1: In progress
    print("📝 Iteration 1...")
    create_test_checkpoint(thread_id, iteration=1)
    reconciler.update_heartbeat(thread_id)
    await asyncio.sleep(2)

    # Iteration 2: Still working
    print("📝 Iteration 2...")
    create_test_checkpoint(thread_id, iteration=2)
    reconciler.update_heartbeat(thread_id)
    await asyncio.sleep(2)

    # Final: Success!
    print("✅ Final iteration (passed)...")
    create_test_checkpoint(thread_id, iteration=3, final_passed=True)
    reconciler.update_heartbeat(thread_id)

    print("\n✨ Pipeline simulation complete!")
    print("The reconciler should now see this thread as 'completed'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconciler", action="store_true", help="Run reconciler")
    parser.add_argument(
        "--run-pipeline", action="store_true", help="Simulate a pipeline run"
    )
    parser.add_argument(
        "--create-test-data", action="store_true", help="Create test checkpoints"
    )
    args = parser.parse_args()

    if args.reconciler:
        asyncio.run(run_reconciler())
    elif args.run_pipeline:
        asyncio.run(simulate_pipeline())
    elif args.create_test_data:
        # Create some test checkpoints (matching the reconciler thread IDs)
        create_test_checkpoint("pipeline-001", iteration=1)
        create_test_checkpoint("pipeline-002", iteration=2, final_passed=True)
        print("\n✅ Test data created! Now run the reconciler to see it in action:")
        print("   python test_reconciler.py --reconciler")
    else:
        parser.print_help()
