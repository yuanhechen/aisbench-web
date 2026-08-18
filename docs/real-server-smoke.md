# Real-server smoke result

Date: 2026-08-18. Recorded from an end-to-end run on the provided test server. No credentials
appear in this file.

## Environment

| Item | Value |
| --- | --- |
| Server | aarch64, openEuler-derived (`liteserver-wulan-11-1`) |
| Python | 3.10.20 (conda env `ais_bench`) |
| AISBench | `ais_bench_benchmark` 3.1.20260813 |
| AISBench source root | `/home/yhc/benchmark` |
| Dataset root | `<ais_bench package>/datasets` |
| Model service | `da-vlm` container, vLLM-Ascend v0.11.0rc2, OpenAI-compatible on port 8000 |
| Model | `/models/Qwen2.5-VL-7B-Instruct`, `max_model_len` 8192 |
| aisbench-web | 0.1.0 wheel, port 8800 |

Installing the wheel added only `aisbench-web`, `argon2-cffi`, `argon2-cffi-bindings`, `cffi`,
`cryptography`, `fastapi`, `pycparser`, `starlette`, `uvicorn`. Nothing existing was upgraded
or removed, which is the requirement that `pip install` must not disturb a working AISBench.

## What ran

Register a user, create a private model endpoint at `http://127.0.0.1:8000/v1`, test
connectivity, install GSM8K from the packaged catalog, submit an accuracy job with
`num_prompts=4` and `max_num_workers=2`, and follow it to completion.

| Step | Result |
| --- | --- |
| Connectivity test | `ok: true`, 42 ms |
| Dataset install | downloaded, checksum verified, extracted, renamed into place |
| Job lifecycle | `queued` → `running` → `succeeded` |
| Progress | read from the run log, persisted, `1 / 1` |
| Accuracy | `gsm8k.accuracy = 50.0` |
| Artifacts | 8 indexed and classified: summary, prediction, result, log, config |
| Artifact download | returns the real `summary_*.csv` |
| Cross-user access | a second user sees empty lists and 404 for every one of the first user's resources |
| Key at rest | `api_key='***'` in the generated config once the run finished |

## Generated config

```python
from mmengine.config import read_base

with read_base():
    from ais_bench.benchmark.configs.datasets.gsm8k.gsm8k_gen_4_shot_cot_chat_prompt import gsm8k_datasets as datasets
    from ais_bench.benchmark.configs.models.vllm_api.vllm_api_general_chat import models
    from ais_bench.benchmark.configs.summarizers.example import summarizer

models[0].update(
    abbr='da-vlm',
    model='/models/Qwen2.5-VL-7B-Instruct',
    api_key='***',
    host_ip='127.0.0.1',
    host_port=8000,
    url='http://127.0.0.1:8000/',
    enable_ssl=False,
    max_out_len=512,
    stream=False,
    request_rate=0,
)

datasets[0]['reader_cfg']['test_range'] = '[0:4]'
```

Command: `ais_bench <config> --mode all --work-dir <job>/outputs --max-num-workers 2`

## Corrections this run forced

**The config named modules that do not exist.** It imported
`ais_bench.benchmark.runners.local_api.LocalAPIRunner` and
`ais_bench.benchmark.tasks.OpenICLInferTask`, copied from a shipped example config. The first
module is absent in 3.1.20260813, and the second is the non-API task; a service model needs
`OpenICLApiInferTask`. Every job failed at launch.

The fix is to emit no `infer` block at all. AISBench then builds the partitioner, runner, and
task itself, choosing the API inference task from the model's `attr="service"`, and takes the
worker count from `--max-num-workers` on its command line. The generated config now names only
dataset, model, and summarizer modules from the packaged catalog, all verified present.

**Loading a config does not prove its imports resolve.** The earlier check used
`mmengine.Config.fromfile`, which defers imports; the config loaded cleanly and still failed at
run time. Verification must run the job, not load the file. A test now asserts that every
import in a generated config comes from a verified root.

**Output lives in a timestamped run directory.** AISBench writes
`<work-dir>/<YYYYmmdd_HHMMSS>/summary/...`, not `<work-dir>/summary/...`. The parsers searched
one level too high, so a successful job reported no metrics and classified all eight artifacts
as `other`. Parsing and artifact classification now search at any depth, and the test fixtures
and stand-in executable reproduce the real layout.

## Not covered

A performance job was not run: it would occupy the shared inference service for longer than a
borrowed window allows. The performance parser is therefore still exercised only against
fixtures built from the AISBench source, not against real output.
