import argparse
import json
import os
import shutil
import sqlite3

from fespb.fespb_ortools_1 import fespb
from utils.db_tools import update_db


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_DIR = os.path.join(BASE_DIR, "database_paper")


def resolve_path(path, root):
    return path if os.path.isabs(path) else os.path.join(root, path)


def main():
    parser = argparse.ArgumentParser(
        description="Generate dynamic insertion schedule by copying an existing base sqlite first."
    )
    parser.add_argument("--base-db", default="4_experiments.sqlite")
    parser.add_argument("--out-db", default="5_experiments.sqlite")
    parser.add_argument("--json", default=os.path.join("examples", "5_experiments.json"))
    parser.add_argument("--cur-ptr", type=int, default=800)
    parser.add_argument("--time-limit", type=int, default=300)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    base_db = resolve_path(args.base_db, DATABASE_DIR)
    out_db = resolve_path(args.out_db, DATABASE_DIR)
    json_path = resolve_path(args.json, BASE_DIR)

    if not os.path.exists(base_db):
        raise FileNotFoundError(base_db)
    if not os.path.exists(json_path):
        raise FileNotFoundError(json_path)
    if os.path.exists(out_db) and not args.overwrite:
        raise FileExistsError(f"{out_db} exists; pass --overwrite to replace it")

    shutil.copy2(base_db, out_db)

    with open(json_path, "r", encoding="utf-8") as file:
        payload = json.load(file)

    conn_sql = sqlite3.connect(out_db)
    try:
        update_db(True, args.cur_ptr, json.dumps(payload), conn_sql)
        makespan, _, _ = fespb(args.cur_ptr, conn_sql, time_limit=args.time_limit)
        print(f"Generated {out_db}")
        print(f"cur_ptr={args.cur_ptr}, makespan={makespan}")
    finally:
        conn_sql.close()


if __name__ == "__main__":
    main()
