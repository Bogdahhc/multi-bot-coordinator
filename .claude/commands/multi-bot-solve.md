# Multi-Bot 调度求解器

根据用户指定的参数，使用 OR-Tools CP-SAT 求解器解决多机器人多任务调度问题(FJSPB)，并与 CPLEX 基准结果对比。

## 参数解析

从用户输入 `$ARGUMENTS` 中解析以下参数：
- 第一个参数：操作模式
  - `run` — 在指定数据库上运行求解器
  - `compare` — 对比 OR-Tools 结果与 CPLEX 基准
  - `all` — 对所有测试集运行并对比（默认）
- 第二个参数（可选）：数据集名称，如 `e1`、`e2`、`e3`、`e4`、`e5`、`4_experiments`、`5_experiments`
- 第三个参数（可选）：求解时间限制（秒），默认 `120`

## 执行流程

### 1. 环境准备

确认以下路径和工具可用：

- **项目目录**：`/home/multi-bot-coordinator_licko/multi-robot-multi-task_scheduling/simulation_methods/`
- **OR-Tools 虚拟环境**：`/home/hehaochen/projects/ortools_env/bin/activate`
- **求解器脚本**：`fespb/fespb_ortools.py`
- **验证脚本**：`test_ortools.py`
- **CPLEX 基准数据**：`database_paper/e1.sqlite` ~ `e5.sqlite`
- **结果输出目录**：`ortools_results/`

### 2. 运行求解器

根据操作模式执行：

**模式 `run`**：在指定数据集上运行 OR-Tools 求解器。
```bash
cd /home/multi-bot-coordinator_licko/multi-robot-multi-task_scheduling/simulation_methods
source /home/hehaochen/projects/ortools_env/bin/activate
python3 -c "
import os, sys, shutil, sqlite3
sys.path.insert(0, '.')
from fespb.fespb_ortools import fespb

db_name = '$DATASET'  # 用户指定的数据集名
time_limit = $TIME_LIMIT  # 用户指定的时间限制

src = f'database_paper/{db_name}.sqlite'
dst = f'ortools_results/{db_name}_ortools.sqlite'
os.makedirs('ortools_results', exist_ok=True)
shutil.copy2(src, dst)

# 读取 CPLEX 基准
conn_cplex = sqlite3.connect(src)
cplex_makespan = conn_cplex.execute('SELECT MAX(end) FROM task_scheduled').fetchone()[0]
conn_cplex.close()

# 清理解析结果，保留问题定义
conn = sqlite3.connect(dst)
conn.execute('''UPDATE task_scheduled SET name=NULL, ws_code_fjspb=NULL, start_time=NULL, end=NULL, duration=NULL, job_length=NULL, next_step_ws_code_fjspb=NULL, has_scheduled=NULL''')
conn.commit()

# 运行 OR-Tools 求解
makespan, ml, bc = fespb(0, conn, time_limit=time_limit)
print(f'\\n=== 结果 ===')
print(f'CPLEX makespan: {cplex_makespan}')
print(f'OR-Tools makespan: {makespan}')
if makespan is not None:
    gap = ((makespan - cplex_makespan) / cplex_makespan) * 100
    print(f'Gap: {gap:+.2f}%')
conn.close()
"
```

**模式 `all`**：运行验证脚本对所有测试集求解并对比。
```bash
cd /home/multi-bot-coordinator_licko/multi-robot-multi-task_scheduling/simulation_methods
rm -rf ortools_results
source /home/hehaochen/projects/ortools_env/bin/activate
python3 test_ortools.py
```

**模式 `compare`**：读取已有的 OR-Tools 结果与 CPLEX 基准对比。
```bash
cd /home/multi-bot-coordinator_licko/multi-robot-multi-task_scheduling/simulation_methods
source /home/hehaochen/projects/ortools_env/bin/activate
python3 -c "
import sqlite3, os
db_dir = 'database_paper'
res_dir = 'ortools_results'
for f in sorted(os.listdir(res_dir)):
    if f.endswith('_ortools.sqlite'):
        db_name = f.replace('_ortools.sqlite', '')
        cplex_conn = sqlite3.connect(os.path.join(db_dir, f'{db_name}.sqlite'))
        ortools_conn = sqlite3.connect(os.path.join(res_dir, f))
        cplex_ms = cplex_conn.execute('SELECT MAX(end) FROM task_scheduled').fetchone()[0]
        ortools_ms = ortools_conn.execute('SELECT MAX(end) FROM task_scheduled').fetchone()[0]
        gap = ((ortools_ms - cplex_ms) / cplex_ms * 100) if cplex_ms and ortools_ms else None
        print(f'{db_name}: CPLEX={cplex_ms}, OR-Tools={ortools_ms}, Gap={gap:+.2f}%' if gap else f'{db_name}: CPLEX={cplex_ms}, OR-Tools={ortools_ms}')
        cplex_conn.close()
        ortools_conn.close()
"
```

### 3. 结果展示

将运行结果整理为表格形式展示给用户：

| 数据集 | Jobs | Tasks | CPLEX | OR-Tools | Gap | 状态 |
|--------|------|-------|-------|----------|-----|------|

包含以下信息：
- 每个 CPLEX 基准结果对应的 OR-Tools 结果
- Gap 百分比（负值表示 OR-Tools 更优）
- 求解状态（OPTIMAL / FEASIBLE）
- 求解耗时

## 注意事项

- OR-Tools 安装在隔离虚拟环境 `/home/hehaochen/projects/ortools_env` 中，必须先激活
- `database_paper` 中的原始数据库不会被修改（操作前会复制）
- `has_scheduled` 字段在清理时需要重置为 NULL，否则 `construct_fjspb_jobs_data_from_db` 会使用已调度的机器分配而非完整候选列表
- 对于无 CPLEX 基准的数据集（如 `4_experiments`、`5_experiments`），直接运行并展示结果
