"""
Verification script: run OR-Tools FJSPB solver on each database and compare with CPLEX results.

Usage:
    cd simulation_methods
    python test_ortools.py
"""

import os
import sys
import shutil
import sqlite3

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fespb.fespb_ortools import fespb


DATABASE_PAPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database_paper")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ortools_results")


def get_cplex_makespan(db_path):
    """Read the makespan from the CPLEX-solved database."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT MAX(end) FROM task_scheduled")
    result = cur.fetchone()
    conn.close()
    return result[0] if result and result[0] is not None else None


def run_test(db_name, time_limit=120):
    """Copy the database, run OR-Tools solver, and compare."""
    src_path = os.path.join(DATABASE_PAPER_DIR, f"{db_name}.sqlite")

    if not os.path.exists(src_path):
        print(f"[SKIP] {db_name}: file not found")
        return

    # Get CPLEX baseline
    cplex_makespan = get_cplex_makespan(src_path)
    if cplex_makespan is None:
        print(f"[SKIP] {db_name}: no CPLEX results (makespan is None)")
        return

    # Copy database for OR-Tools (don't modify the original)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ortools_db = os.path.join(RESULTS_DIR, f"{db_name}_ortools.sqlite")
    shutil.copy2(src_path, ortools_db)

    print(f"\n{'='*60}")
    print(f"Testing: {db_name}")
    print(f"CPLEX makespan: {cplex_makespan}")
    print(f"{'='*60}")

    conn = sqlite3.connect(ortools_db)

    # Clear previous schedule results but keep task definitions
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE task_scheduled SET
            name = NULL,
            ws_code_fjspb = NULL,
            start_time = NULL,
            end = NULL,
            duration = NULL,
            job_length = NULL,
            next_step_ws_code_fjspb = NULL,
            has_scheduled = NULL
    """)
    conn.commit()

    try:
        makespan, machines_list, batch_capacities = fespb(0, conn, time_limit=time_limit)

        if makespan is not None:
            gap = ((makespan - cplex_makespan) / cplex_makespan) * 100
            print(f"\nResults for {db_name}:")
            print(f"  CPLEX  makespan: {cplex_makespan}")
            print(f"  OR-Tools makespan: {makespan}")
            print(f"  Gap: {gap:+.2f}%")
            if makespan <= cplex_makespan:
                print(f"  OR-Tools is BETTER or EQUAL to CPLEX!")
            elif gap <= 5:
                print(f"  OR-Tools is close to CPLEX (within 5%)")
            elif gap <= 15:
                print(f"  OR-Tools is reasonable (within 15%)")
            else:
                print(f"  OR-Tools is significantly worse than CPLEX")
        else:
            print(f"\n{db_name}: OR-Tools FAILED to find a solution")

    except Exception as e:
        print(f"\n{db_name}: ERROR - {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()

    return


def main():
    # Test on databases that have CPLEX results
    test_dbs = ["e1", "e2", "e3", "e4", "e5"]

    print("OR-Tools FJSPB Verification")
    print("=" * 60)

    for db_name in test_dbs:
        run_test(db_name, time_limit=120)

    print("\n" + "=" * 60)
    print("Verification complete.")
    print(f"Results saved to: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
