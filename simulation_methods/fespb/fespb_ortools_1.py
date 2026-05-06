"""
OR-Tools FJSPB solver based on the original fespb_ortools.py implementation,
with only one modeling change: tasks that overlap on the same batch machine
must start and end together.

Usage:
    from fespb.fespb_ortools_batchsync import fespb
    makespan, machines_list, batch_capacities = fespb(cur_ptr, conn_sql, time_limit=120)
"""

import collections
import json
import sqlite3
from collections import defaultdict

from ortools.sat.python import cp_model

from utils.db_tools import construct_fjspb_jobs_data_from_db, find_capacity_by_ws_code_from_db


def _make_demand(model, presence, name):
    """Create a demand variable that is 1 when present, 0 otherwise."""
    demand = model.NewIntVar(0, 1, name)
    if isinstance(presence, int):
        model.Add(demand == presence)
    else:
        model.Add(demand == 1).OnlyEnforceIf(presence)
        model.Add(demand == 0).OnlyEnforceIf(presence.Not())
    return demand


def _add_aligned_overlap_constraint(model, machine_to_intervals, batch_capacities):
    """If two selected intervals overlap on a machine, align both boundaries."""
    for m_id, entries in machine_to_intervals.items():
        if batch_capacities.get(m_id, 1) <= 1:
            continue
        for i in range(len(entries)):
            itv_i, p_i, s_i, e_i, d_i = entries[i]
            for j in range(i + 1, len(entries)):
                itv_j, p_j, s_j, e_j, d_j = entries[j]

                if d_i != d_j:
                    model.AddNoOverlap([itv_i, itv_j])
                    continue

                i_before_j = model.NewBoolVar(f"sync_before_{m_id}_{i}_{j}")
                j_before_i = model.NewBoolVar(f"sync_after_{m_id}_{i}_{j}")
                same_batch = model.NewBoolVar(f"sync_same_{m_id}_{i}_{j}")

                model.Add(e_i <= s_j).OnlyEnforceIf(i_before_j)
                model.Add(e_j <= s_i).OnlyEnforceIf(j_before_i)
                model.Add(s_i == s_j).OnlyEnforceIf(same_batch)
                model.Add(e_i == e_j).OnlyEnforceIf(same_batch)

                literals = [i_before_j, j_before_i, same_batch]
                if not isinstance(p_i, int):
                    literals.append(p_i.Not())
                elif p_i == 0:
                    continue

                if not isinstance(p_j, int):
                    literals.append(p_j.Not())
                elif p_j == 0:
                    continue

                model.AddBoolOr(literals)


def fespb(cur_ptr, conn_sql: sqlite3.Connection, time_limit=120):
    jobs_data = construct_fjspb_jobs_data_from_db(conn_sql)

    machines_list = []
    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, _, _) in enumerate(job):
            for m_id in machines:
                if m_id is not None and m_id not in machines_list:
                    machines_list.append(m_id)

    batch_capacities = {}
    for code in machines_list:
        cap = find_capacity_by_ws_code_from_db(code, conn_sql)
        batch_capacities[code] = cap if cap else 1

    horizon = sum(task[1] for job_id, job in jobs_data.items() for task in job)
    if horizon == 0:
        horizon = 1

    model = cp_model.CpModel()

    # all_tasks[job_id, task_id] = (start, end, interval)
    all_tasks = {}
    # task_to_machine[job_id, task_id][m_id] = (presence, start, end, interval)
    task_to_machine = collections.defaultdict(dict)
    # machine_to_intervals[m_id] = [(interval, presence, start, end, duration), ...]
    machine_to_intervals = collections.defaultdict(list)

    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, _, sche_info_dict) in enumerate(job):
            is_fixed = (
                sche_info_dict["start_time"] is not None
                and sche_info_dict["start_time"] < cur_ptr
            )

            if is_fixed:
                fixed_start = int(sche_info_dict["start_time"])
                fixed_end = int(sche_info_dict["end"])
                fixed_dur = fixed_end - fixed_start

                start_var = model.NewConstant(fixed_start)
                end_var = model.NewConstant(fixed_end)
                interval = model.NewFixedSizeIntervalVar(
                    start_var, fixed_dur, f"I_{job_id}_{task_id}"
                )
                all_tasks[job_id, task_id] = (start_var, end_var, interval)

                m_id = machines[0]
                task_to_machine[job_id, task_id][m_id] = (1, start_var, end_var, interval)
                machine_to_intervals[m_id].append((interval, 1, start_var, end_var, duration))
            else:
                start_var = model.NewIntVar(cur_ptr, horizon, f"S_{job_id}_{task_id}")
                end_var = model.NewIntVar(cur_ptr, horizon, f"E_{job_id}_{task_id}")
                model.Add(end_var == start_var + duration)
                interval = model.NewFixedSizeIntervalVar(
                    start_var, duration, f"I_{job_id}_{task_id}"
                )
                all_tasks[job_id, task_id] = (start_var, end_var, interval)

                if len(machines) == 1:
                    m_id = machines[0]
                    task_to_machine[job_id, task_id][m_id] = (
                        1, start_var, end_var, interval,
                    )
                    machine_to_intervals[m_id].append((interval, 1, start_var, end_var, duration))
                else:
                    for m_id in machines:
                        if m_id is None:
                            continue
                        p = model.NewBoolVar(f"P_{job_id}_{task_id}_{m_id}")
                        opt_s = model.NewIntVar(cur_ptr, horizon, f"OS_{job_id}_{task_id}_{m_id}")
                        opt_e = model.NewIntVar(cur_ptr, horizon, f"OE_{job_id}_{task_id}_{m_id}")
                        itv = model.NewOptionalFixedSizeIntervalVar(
                            opt_s, duration, p, f"I_{job_id}_{task_id}_M{m_id}"
                        )
                        model.Add(opt_s == start_var).OnlyEnforceIf(p)
                        model.Add(opt_e == end_var).OnlyEnforceIf(p)
                        task_to_machine[job_id, task_id][m_id] = (p, opt_s, opt_e, itv)
                        machine_to_intervals[m_id].append((itv, p, opt_s, opt_e, duration))

    # Routing: each flexible task assigned to exactly one machine
    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, _, sche_info_dict) in enumerate(job):
            is_fixed = (
                sche_info_dict["start_time"] is not None
                and sche_info_dict["start_time"] < cur_ptr
            )
            if not is_fixed and len(machines) > 1:
                presences = [
                    task_to_machine[job_id, task_id][m_id][0]
                    for m_id in machines if m_id is not None
                ]
                model.AddExactlyOne(presences)

    # Sequencing: precedence within each job
    for job_id, job in jobs_data.items():
        for task_id in range(len(job) - 1):
            _, end_prev, _ = all_tasks[job_id, task_id]
            start_next, _, _ = all_tasks[job_id, task_id + 1]
            model.Add(end_prev <= start_next)

    _add_aligned_overlap_constraint(model, machine_to_intervals, batch_capacities)

    # Capacity constraints: cumulative for all non-centrifuge machines.
    centrifuge_machines = {m for m in machines_list if "centrifugation" in m}

    for m_id in machines_list:
        if m_id in centrifuge_machines:
            continue
        entries = machine_to_intervals[m_id]
        if not entries:
            continue
        cap = batch_capacities.get(m_id, 1)
        intervals = [itv for itv, _, _, _, _ in entries]
        demands = [_make_demand(model, p, f"dem_{m_id}_{i}") for i, (_, p, _, _, _) in enumerate(entries)]
        model.AddCumulative(intervals, demands, cap)

    # Electrochemical / XRD: dripping, test, recycle must not overlap
    test_intervals = {}
    dripping_intervals = {}
    recycle_intervals = {}

    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, _, _) in enumerate(job):
            for m_id in machines:
                if m_id is None:
                    continue
                if "test" in m_id:
                    test_intervals[(job_id, task_id)] = all_tasks[job_id, task_id][2]
                elif "dripping" in m_id:
                    dripping_intervals[(job_id, task_id)] = all_tasks[job_id, task_id][2]
                elif "recycle" in m_id:
                    recycle_intervals[(job_id, task_id)] = all_tasks[job_id, task_id][2]

    for _, itv_d in dripping_intervals.items():
        for _, itv_t in test_intervals.items():
            model.AddNoOverlap([itv_d, itv_t])
        for _, itv_r in recycle_intervals.items():
            model.AddNoOverlap([itv_d, itv_r])

    for _, itv_t in test_intervals.items():
        for _, itv_r in recycle_intervals.items():
            model.AddNoOverlap([itv_t, itv_r])

    # Dripping -> test -> recycle back-to-back
    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, _, _) in enumerate(job):
            for m_id in machines:
                if m_id is None:
                    continue
                if "dripping" in m_id and task_id + 2 < len(job):
                    _, end_d, _ = all_tasks[job_id, task_id]
                    s_next, _, _ = all_tasks[job_id, task_id + 1]
                    model.Add(end_d == s_next)
                    _, end_next, _ = all_tasks[job_id, task_id + 1]
                    s_next2, _, _ = all_tasks[job_id, task_id + 2]
                    model.Add(end_next == s_next2)

    # Muffle furnace: different temperatures cannot overlap
    muffle_temp_dict = defaultdict(lambda: defaultdict(list))
    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, parameters, _) in enumerate(job):
            for m_id in machines:
                if m_id is None:
                    continue
                if "muffle_furnace" in m_id:
                    try:
                        temp = json.loads(parameters[0]["param"]["custom_param"])["temperature"]
                        muffle_temp_dict[m_id][temp].append(all_tasks[job_id, task_id][2])
                    except (IndexError, KeyError, json.JSONDecodeError):
                        pass

    for m_id in muffle_temp_dict:
        temps = list(muffle_temp_dict[m_id].keys())
        for i in range(len(temps)):
            for j in range(i + 1, len(temps)):
                for itv1 in muffle_temp_dict[m_id][temps[i]]:
                    for itv2 in muffle_temp_dict[m_id][temps[j]]:
                        model.AddNoOverlap([itv1, itv2])

    # Dryer workstation: different temperatures cannot overlap
    dryer_temp_dict = defaultdict(lambda: defaultdict(list))
    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, parameters, _) in enumerate(job):
            for m_id in machines:
                if m_id is None:
                    continue
                if "dryer_workstation" in m_id:
                    try:
                        temp = json.loads(parameters[0]["param"]["temperature"])
                        dryer_temp_dict[m_id][temp].append(all_tasks[job_id, task_id][2])
                    except (IndexError, KeyError, json.JSONDecodeError):
                        pass

    for m_id in dryer_temp_dict:
        temps = list(dryer_temp_dict[m_id].keys())
        for i in range(len(temps)):
            for j in range(i + 1, len(temps)):
                for itv1 in dryer_temp_dict[m_id][temps[i]]:
                    for itv2 in dryer_temp_dict[m_id][temps[j]]:
                        model.AddNoOverlap([itv1, itv2])

    # Centrifuge even-count constraint + cumulative capacity
    for m_id in centrifuge_machines:
        cent_entries = []
        for job_id, job in jobs_data.items():
            for task_id, (machines, dur, _, _) in enumerate(job):
                if m_id in machines:
                    entry = task_to_machine.get((job_id, task_id), {}).get(m_id)
                    if entry is not None:
                        p, _, _, itv = entry
                        cent_entries.append((job_id, task_id, p, itv))

        n = len(cent_entries)
        if n == 0:
            continue

        cap = batch_capacities.get(m_id, n)

        intervals = [itv for _, _, _, itv in cent_entries]
        demands = [_make_demand(model, p, f"cdem_{jid}_{tid}")
                   for jid, tid, p, _ in cent_entries]
        model.AddCumulative(intervals, demands, cap)

        if n <= 1:
            continue

        event_vars = []
        for jid, tid, _, _ in cent_entries:
            s_var, e_var, _ = all_tasks[jid, tid]
            event_vars.append(s_var)
            event_vars.append(e_var)

        for t_idx, t_var in enumerate(event_vars):
            bools = []
            for jid, tid, p, _ in cent_entries:
                s_var, e_var, _ = all_tasks[jid, tid]
                b = model.NewBoolVar(f"cb_{jid}_{tid}_{t_idx}")
                s_le = model.NewBoolVar(f"sle_{jid}_{tid}_{t_idx}")
                e_gt = model.NewBoolVar(f"egt_{jid}_{tid}_{t_idx}")

                model.Add(s_var <= t_var).OnlyEnforceIf(s_le)
                model.Add(s_var > t_var).OnlyEnforceIf(s_le.Not())
                model.Add(e_var > t_var).OnlyEnforceIf(e_gt)
                model.Add(e_var <= t_var).OnlyEnforceIf(e_gt.Not())

                if isinstance(p, int) and p == 1:
                    model.AddBoolAnd([s_le, e_gt]).OnlyEnforceIf(b)
                    model.AddBoolOr([s_le.Not(), e_gt.Not()]).OnlyEnforceIf(b.Not())
                else:
                    inner = model.NewBoolVar(f"inn_{jid}_{tid}_{t_idx}")
                    model.AddBoolAnd([s_le, e_gt]).OnlyEnforceIf(inner)
                    model.AddBoolOr([s_le.Not(), e_gt.Not()]).OnlyEnforceIf(inner.Not())
                    model.AddBoolAnd([inner, p]).OnlyEnforceIf(b)
                    model.AddBoolOr([inner.Not(), p.Not()]).OnlyEnforceIf(b.Not())

                bools.append(b)

            active_sum = model.NewIntVar(0, n, f"cas_{m_id}_{t_idx}")
            model.Add(active_sum == sum(bools))
            half = model.NewIntVar(0, n, f"ch_{m_id}_{t_idx}")
            model.Add(2 * half == active_sum)

    # Same experiment's first tasks must start together
    expr_no_task_0_jobs = defaultdict(list)
    for job_id, job in jobs_data.items():
        for task_id, (machines, duration, _, sche_info_dict) in enumerate(job):
            if task_id == 0:
                expr_no = job_id.split("_")[0]
                expr_no_task_0_jobs[expr_no].append(all_tasks[job_id, task_id])

    for expr_no, tasks in expr_no_task_0_jobs.items():
        if len(tasks) > 1:
            base_start = tasks[0][0]
            for i in range(1, len(tasks)):
                model.Add(tasks[i][0] == base_start)

    # Objective: minimize makespan
    makespan_var = model.NewIntVar(0, horizon, "makespan")
    for job_id, job in jobs_data.items():
        _, end_var, _ = all_tasks[job_id, len(job) - 1]
        model.Add(makespan_var >= end_var)

    model.Minimize(makespan_var)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    solver.parameters.log_search_progress = True

    total_tasks = sum(len(j) for j in jobs_data.values())
    print(f"Model: {len(jobs_data)} jobs, {total_tasks} tasks, {len(machines_list)} machines")
    print(f"Solving with time_limit={time_limit}s ...")

    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        status_str = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
        print(f"OR-Tools Status: {status_str}")
        print(f"Objective (makespan): {int(solver.ObjectiveValue())}")
        print(f"Wall time: {solver.WallTime():.2f}s")

        cursor = conn_sql.cursor()
        cursor.execute("BEGIN")

        for job_id, job in jobs_data.items():
            for task_id, (machines, duration, _, _) in enumerate(job):
                for m_id in machines:
                    if m_id is None:
                        continue
                    entry = task_to_machine.get((job_id, task_id), {}).get(m_id)
                    if entry is None:
                        continue
                    p, s, e, itv = entry
                    p_val = p if isinstance(p, int) else solver.Value(p)
                    if p_val == 1:
                        start_val = solver.Value(s)
                        end_val = solver.Value(e)
                        name = f"B{job_id}-T{task_id}-M{m_id}-D{duration}"

                        cursor.execute(
                            """
                            UPDATE task_scheduled
                            SET
                                name = ?,
                                ws_code_fjspb = ?,
                                start_time = ?,
                                end = ?,
                                duration = ?,
                                job_length = ?
                            WHERE b_id = ? AND fjspb_index = ?""",
                            (
                                name,
                                m_id,
                                start_val,
                                end_val,
                                duration,
                                len(job),
                                job_id,
                                task_id,
                            ),
                        )

        conn_sql.commit()

        cursor.execute("BEGIN")
        cursor.execute("SELECT * FROM task_scheduled")
        column_names = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

        for row in rows:
            row_data = dict(zip(column_names, row))
            cur_b_id = row_data["b_id"]
            cur_idx = row_data["fjspb_index"]

            cursor.execute(
                """
                SELECT ws_code_fjspb
                FROM task_scheduled
                WHERE b_id = ? AND fjspb_index = ?
                """,
                (cur_b_id, cur_idx + 1),
            )
            next_ws = cursor.fetchone()
            if next_ws:
                cursor.execute(
                    """
                    UPDATE task_scheduled
                    SET next_step_ws_code_fjspb = ?
                    WHERE b_id = ? AND fjspb_index = ?""",
                    (next_ws[0], cur_b_id, cur_idx),
                )

        conn_sql.commit()

        cursor.execute("""SELECT MAX(end) FROM task_scheduled""")
        makespan = cursor.fetchone()[0]

        return makespan, machines_list, batch_capacities
    else:
        print(f"OR-Tools solve status: {solver.StatusName(status)}")
        print(f"Best bound: {solver.BestObjectiveBound()}")
        print(f"Wall time: {solver.WallTime():.2f}s")
        print("No solution found.")
        return None, machines_list, batch_capacities


def main():
    pass


if __name__ == "__main__":
    main()
