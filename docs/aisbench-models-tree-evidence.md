# What is actually known about AISBench's configs/models tree

Recorded from `ls` on the test server before it became unreachable.

| Path | Evidence |
| --- | --- |
| `configs/models/` | Listed: hf_models, lmm_models, mindie_api, tgi_api, triton_api, vita, vllm_api, vllm_offline_models |
| `configs/models/vllm_api/` | Listed: vllm_api_function_call_chat.py, vllm_api_general_chat.py, vllm_api_general.py, vllm_api_general_stream.py, vllm_api_stream_chat_multiturn.py, vllm_api_stream_chat.py |
| `configs/models/lmm_models/` | Listed, truncated at 5: qwen_image_edit.py |
| `vllm_api_general_chat.py` | Read in full |
| `vllm_api_general_stream.py` | Read in full |
| `vllm_api_stream_chat.py` | Read in full |
| Everything else | **Not seen.** File names under mindie_api, tgi_api, triton_api, hf_models, vita and vllm_offline_models are unknown, as is whether their configs declare `attr="service"`. |

The local tree under `scratchpad/realtree` contains only the entries with evidence,
plus `vllm_offline_models/vllm_qwen.py`, which is written by hand as an offline example and
is not a copy of anything.
