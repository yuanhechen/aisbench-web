# 多数据集任务与每数据集进度

一次任务可以勾选多个数据集（上限 16 个）。生成的配置为每个数据集 import 一份
AISBench 数据集配置（`datasets = [*ds_0, *ds_1, ...]`）；accuracy 模式下 AISBench 为每个
（模型 × 数据集）组合启动独立 task，本服务据此逐数据集展示进度与结果。

## 数据源（上游 AISBench 的事实）

- **结构化进度**：`<outputs>/<时间戳>/status_tmp/tmp_<task>.json`。每个 task 一个文件，
  JSON 数组、task 每 0.5s 追加一个快照后整体覆写。读取取最后一个元素。字段：
  `task_name`（`"<model_abbr>/<dataset_abbr>"`）、`status`（start / load model /
  inferencing / write cache / warmup / finish / error）、`finish_count` / `total_count`（API
  模型为样本数，本地模型为 batch 数）、`progress_description`（如 `[41.7 it/s]`）、
  `task_log_path`、`other_kwargs`（API 模型为 `POST/RECV/FINISH/FAIL` 计数）。
- **两个必须容忍的竞态**：TasksMonitor 进程周期把文件清空为 `[]`（读侧沿用上次状态）；
  快照数组无限增长（大文件只读尾部，`read_last_snapshot` 从文件末尾反向扫描定位最后一
  个完整元素——末尾必然在字符串外，反向扫描不会失步）。
- **阶段边界**：主进程 stdout 的阶段行（`Starting inference tasks...` /
  `Inference tasks completed.` / `Starting evaluation tasks...` / ...）与失败行
  （`<task> failed with code <n>`）。阶段行用于兜底补记可能被 monitor 先吃掉的 finish
  快照。infer 阶段结束后 `status_tmp` 目录被删除，eval 阶段以相同 task_name 重建——已
  finished 的行据此演进为 evaluating。
- **合并场景**（perf 模式或 `--merge-ds`）：所有数据集合并为一个 task（名字为数据集
  type 类名小写），无法按数据集拆分进度；该行按 AISBench 的原始名追加在配置的数据集
  之后展示。完成后的结果（summary CSV / performances json）仍按数据集拆分，届时按结果
  重建行。

## 本服务的实现

- `jobs/dataset_progress.py`：`DatasetProgressCollector`（worker 每 0.5s 驱动一次
  `scan()`；启动时按快照 abbr 预置 queued 行）+ 阶段/失败行解析。
- 数据库迁移 8：`job_dataset_progress` 表（PK `(job_id, dataset)`），进度先落库再推
  WebSocket（`{"type":"datasets"}`，REST 权威）。完成态由 `parse_accuracy` 的
  per-dataset 结果（含 `results/<model>/<ds>.json` 的 correct/total）回填同一批行。
- API：`GET /api/jobs/{id}` 返回 `datasets: [{name, phase, completed, total, rate,
  counters, log_available, metrics, correct_count, total_count}]`；
  `GET /api/jobs/{id}/datasets/{name}/logs?offset=` 提供该数据集自己的 task 日志（名字
  只用于匹配库内行，永不拼路径）。
- 前端（`components/dataset-progress.tsx`）：同一组行贯穿始终——运行中显示
  `37/100` + 细进度条，可展开（阶段/速率/API 计数/该数据集日志尾部）；成功后原位变成
  `accuracy 62.5 (7/8)`。`datasets` 为空（旧任务、无 status_tmp）时回退旧的单进度条。

## 已知边界

- 快照 v1（单数据集任务）读取兼容，不需要迁移。
- perf 模式的进度只有合并行 + 总进度条（tqdm 管线）双保险。
- 数据集 abbr 与快照名不一致的行（如合并行）追加在配置顺序之后。
