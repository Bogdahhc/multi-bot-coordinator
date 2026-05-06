# OR-Tools 复现与动态插入问题记录

记录时间：2026-04-29

## 1. 优化目标

原始 CPLEX 脚本 `fespb/fespb.py` 的目标为：

```python
mdl.minimize(makespan_var + odd_punish)
```

其中 `makespan_var` 是所有 job 最后一个 task 的最大结束时间；`odd_punish` 用于离心机同时处理数量为奇数时的惩罚。但脚本前面同时加入了：

```python
mdl.add(odd_punish == 0)
```

因此在可行解中 `odd_punish` 必须为 0，实际优化目标等价于最小化 makespan。

当前两个 OR-Tools 版本都使用 CP-SAT 的：

```python
model.Minimize(makespan_var)
```

离心机偶数约束在 OR-Tools 版本中作为硬约束建模，因此目标同样是最小化 makespan，不包含二级目标。原始 CPLEX 中曾有 `all_bias` 相关目标：

```python
# mdl.minimize(makespan_var + all_bias + odd_punish)
```

但该行在原脚本中被注释，没有参与实际求解。

`scheduling.py` 中调用求解器时使用：

```python
fespb(self.ws_ptr, main_loop_conn_sql, time_limit=300)
```

所以通过 README 流程运行 e1-e5 和 4_experiments 时，求解时间限制为 300 秒。CPLEX 和 OR-Tools 都可能在时间限制内返回可行解而非已证明最优解；当前脚本会把 FEASIBLE 结果写入 sqlite。

## 2. OR-Tools 改动版本

当前文件夹中保留了两个 OR-Tools 版本：

- `fespb/fespb_ortools.py`
- `fespb/fespb_ortools_1.py`

### 2.1 `fespb_ortools.py`：尽量还原 CPLEX 语义的版本

该版本目标是尽量贴近 `fespb.py` 的建模语义。主要改动如下：

1. 批处理同步约束

   CPLEX 使用：

   ```python
   state_function(...)
   always_equal(..., isStartAligned=True, isEndAligned=True)
   ```

   该语义不仅限制容量，还要求同一批次中重叠的任务具有相同开始和结束时间。OR-Tools 版本加入了等价约束：

   - 同一机器、容量大于 1 的任务，如果选择重叠，则必须 `start` 相同且 `end` 相同。
   - 不同时长任务不能被放入同一批次，因此加入 `NoOverlap`。
   - 容量为 1 的机器由 `Cumulative(cap=1)` 表达互斥。

   该改动修复了早期 OR-Tools 版本允许批处理任务错峰重叠的问题。例如 e2 曾得到 959 的 makespan，加入批同步后恢复到理论/原始 git 的 968。

2. 固定已开始任务

   对动态插入场景，若数据库中已有排程且：

   ```python
   start_time < cur_ptr
   ```

   则将该任务建成固定 interval，保留原 `start_time/end/ws_code_fjspb`。这用于插入点之前的任务冻结。

3. 新任务时间下界

   对未固定任务加入：

   ```python
   start_var >= cur_ptr
   ```

   动态插入时，插入点之后的旧任务和新任务都只能从 `cur_ptr` 之后重新安排。

4. 设备容量

   从数据库读取 `capacity`，并用 `AddCumulative` 约束设备容量。离心机单独加入容量约束和偶数处理约束。

5. 电化学/XRD 约束

   加入 dripping/test/recycle 互斥，以及同一 job 内 dripping -> test -> recycle 紧邻衔接约束。

6. 马弗炉/烘干工位温度约束

   对同一设备上不同温度的任务加入 `NoOverlap`，避免不同温度任务重叠。

7. 同一实验起始任务同步

   同一 `expr_no` 的第 0 个任务强制同起。

8. 已有排程 hint

   如果目标数据库中已有 `start_time/end/ws_code_fjspb`，该版本会作为 CP-SAT hint 加入。但 hint 只是搜索提示，不是约束；除非任务满足 `start_time < cur_ptr` 被固定，否则求解器仍可选择不同排程。

### 2.2 `fespb_ortools_1.py`：基于初始 OR-Tools 的批同步简化版本

该版本的初衷是保留最初 OR-Tools 建模结构，只加入“同一批重叠任务必须同起同止”的关键语义修正，便于观察单一改动的影响。主要特点如下：

1. 只对批处理机器加入同步重叠逻辑

   `_add_aligned_overlap_constraint()` 表达：

   - 任务时长相同：可以前后分离，也可以同起同止。
   - 任务时长不同：不能重叠。
   - 只有容量大于 1 的机器应用该逻辑。

2. 固定任务逻辑较直接

   如果 `start_time < cur_ptr`，直接将任务固定在数据库已有的 `start/end` 上，并使用已有机器。

3. 非固定任务的变量域从 `cur_ptr` 开始

   未固定任务的 `start/end` 变量域为 `[cur_ptr, horizon]`，用于动态插入重排。

4. 仍包含核心工艺约束

   包括电化学/XRD 互斥与紧邻、马弗炉/烘干温度互斥、离心机偶数约束、同一实验第 0 个任务同步等。

5. 当前调度入口使用该版本

   `scheduling.py` 当前导入：

   ```python
   from fespb.fespb_ortools_1 import fespb
   ```

## 3. 每次求解后绘图数据不同的原因

### 3.1 makespan 相同不代表排程相同

当前目标只最小化 makespan，没有二级目标约束任务顺序、机器选择偏好、同 makespan 下的 tie-break 规则。因此 CPLEX 和 OR-Tools 即使都得到相同 makespan，也可能输出不同的：

- `start_time`
- `end`
- `ws_code_fjspb`
- 同一时间点的任务集合
- 同一设备上的任务顺序

这些差异会写入 sqlite，并影响后续绘图脚本。

### 3.2 CP-SAT 的 FEASIBLE 解不保证和 CPLEX 原结果一致

如果求解器在 300 秒内只返回 FEASIBLE，而不是 OPTIMAL，则结果只表示满足约束，不表示已经证明最优。即使返回 OPTIMAL，也只是对当前目标 makespan 最优；由于没有二级目标，仍不能保证选择与 CPLEX 相同的最优排程。

### 3.3 `draw_schedule_comp_4_exprs.py` 会基于具体排程插入 transfer/gap 时间

绘图脚本不是简单绘制 raw makespan。它会扫描 `task_scheduled` 中的具体任务开始/结束点，并根据同一时刻需要取放、启动、转移的任务数量插入额外 gap。

因此以下变化都会改变图中的模拟实验时间：

- 哪些任务在同一时刻结束。
- 哪些任务在同一时刻开始。
- 同一设备上同一时间点有多少任务。
- 数据库查询顺序和同时间点任务的遍历顺序。
- 同 makespan 下不同机器分配导致的转移冲突差异。

这解释了为什么 raw makespan 一样时，绘图后得到的模拟实际实验时长仍可能不同。

### 3.4 4_experiments 是全局混合求解，不是拼接 e1-e4

`4_experiments.json` 会被 `update_db()` 展开到同一个 `task_scheduled` 表中，然后调用一次 `fespb()` 整体求解。它不是把单独求得的 `e1.sqlite`、`e2.sqlite`、`e3.sqlite`、`e4.sqlite` 拼接起来。

当前 `4_experiments.json` 中包含 6 个 task flow：

- `e2-bottles-10`
- `e1-bottles-3`
- `e3-bottles-4`
- `e4-bottles-10`
- `e2-bottles-4`
- `e4-bottles-5`

所以混合实验的结果取决于这 6 个 task flow 在一个统一模型里的资源竞争。单实验 sqlite 的排程差异会影响顺序绘图部分；混合绘图部分则取决于 `4_experiments.sqlite` 自己的整体求解结果。

### 3.5 数据库再生成会造成原始 git sqlite 与当前 sqlite 不一致

重新运行求解会产生新的等价解。即使某个实验的理论 makespan 匹配，具体任务行也可能与 git 原始 sqlite 大量不同。这些差异不是绘图脚本凭空产生的，而是求解器在同一目标下选取了不同可行/最优排程。

如果目标是复现 `img/1.svg`，仅匹配 makespan 不够；需要复现原始 sqlite 中的具体 `start_time/end/ws_code_fjspb`，或者加入二级目标、固定参考顺序、参考 sqlite hint/约束，使同 makespan 下的解也被限定到原图对应的排程。

## 4. 动态插入问题与解决方案

### 4.1 README 流程的问题

README 中动态插入的流程是：

1. 将 `scheduling.py` 输出数据库设为 `5_experiments.sqlite`。
2. 先发送 `4_experiments.json`。
3. 等 `scheduling.py` 在 `5_experiments.sqlite` 中求出一份 4_experiments 排程。
4. 不退出 `scheduling.py`。
5. 再发送 `5_experiments.json`。
6. 代码将 `self.ws_ptr` 设为 800，模拟在 800 时刻插入新任务。

问题在于第 2-3 步会重新求一次 4_experiments。由于求解器不保证每次得到与已有 `4_experiments.sqlite` 完全相同的具体排程，所以 `5_experiments.sqlite` 中插入点前的排程可能已经不同。后续绘图脚本基于这份不同排程插入 transfer/gap 时间，最终结果自然会与理论图不同。

### 4.2 正确复现动态插入的方式

动态插入实验应当从同一份已经排好的 4_experiments 状态继续，而不是重新求一份等价但不同的 4_experiments 初始解。

解决方案：

1. 原样复制已有的 `database_paper/4_experiments.sqlite` 为 `database_paper/5_experiments.sqlite`。
2. 读取 `examples/5_experiments.json`。
3. 调用 `update_db(True, 800, ...)`，避免清空已有数据库，并将 `start_time < 800` 的任务标记为已调度。
4. 调用 `fespb(cur_ptr=800, ...)`。
5. 求解器固定 `start_time < 800` 的旧任务，只重排 800 之后的旧任务和新增 e5 任务。

已加入脚本：

```bash
python3 generate_dynamic_insert_from_base.py --overwrite
```

脚本位置：

```text
generate_dynamic_insert_from_base.py
```

默认行为：

- `--base-db 4_experiments.sqlite`
- `--out-db 5_experiments.sqlite`
- `--json examples/5_experiments.json`
- `--cur-ptr 800`
- `--time-limit 300`

为了避免误覆盖结果，脚本默认不覆盖已有 `5_experiments.sqlite`，必须显式传入 `--overwrite`。

### 4.3 800 时刻正在运行任务的处理

当前固定条件是：

```python
start_time < cur_ptr
```

因此如果某任务满足：

```text
start_time < 800 < end
```

它会被视为已经开始的旧任务，并完整固定在原来的 `[start_time, end]` 区间上。该任务继续占用原设备容量直到原 `end`，不会被截断、暂停或按剩余时间重新建模。

如果某任务刚好：

```text
start_time == 800
```

则它不会被固定，因为代码使用的是 `< cur_ptr` 而不是 `<= cur_ptr`。这类任务会被视为尚未开始，允许在 800 之后重新规划。若实验语义要求 800 时刻已经下发的任务也不能变动，需要将固定条件改成 `start_time <= cur_ptr`，或根据数据库中的执行状态字段判断是否已经下发/开始。

## 5. 后续若要求精确复现原图

若目标是让再生成结果完全匹配论文图或 `img/1.svg`，推荐优先使用原始 git sqlite 作为绘图输入。若必须重新求解，则需要额外增加确定性偏好，例如：

- 固定参考 sqlite 中插入点前的任务。
- 对同 makespan 下的任务开始时间、机器选择、实验顺序加入二级目标。
- 从参考 sqlite 读取 `start_time/end/ws_code_fjspb` 并作为硬约束或强惩罚目标，而不只是 CP-SAT hint。
- 固定数据库查询排序，避免同时间点遍历顺序影响绘图启发式。

仅靠 makespan 一致，不能保证绘图后的模拟实验时间一致。
