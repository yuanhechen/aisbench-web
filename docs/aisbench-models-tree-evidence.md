# AISBench configs/models tree

Read from the AISBench source at `~/code/benchmark`, so this is the tree itself rather than
anything recorded or reconstructed.

| Family | Configs | Drives an endpoint |
| --- | --- | --- |
| `vllm_api` | vllm_api_function_call_chat, vllm_api_general, vllm_api_general_chat, vllm_api_general_stream, vllm_api_stream_chat, vllm_api_stream_chat_multiturn | yes |
| `mindie_api` | mindie_stream_api_general | yes |
| `tgi_api` | tgi_api_general, tgi_stream_api_general | yes |
| `triton_api` | triton_api_general, triton_stream_api_general | yes |
| `vita` | vita_generate_chat | yes |
| `hf_models` | hf_base_model, hf_causal_lm, hf_chat_model, hf_model, hf_qwenvl_model | no |
| `lmm_models` | qwen_image_edit | no |
| `vllm_offline_models` | vllm_offline_vl_model | no |

Twelve of the nineteen drive an HTTP endpoint. `attr` is AISBench's own discriminator: every
config declares `"service"` or `"local"`, and `hf_model.py` documents the choice in a comment
next to it. Nothing else is needed to tell them apart, and a config that declares neither is
logged rather than passed over in silence.
