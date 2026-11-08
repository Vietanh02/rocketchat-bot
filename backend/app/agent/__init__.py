"""The assistant: conversations that outlive restarts, run one turn at a time.

- `session`: which conversation a message belongs to, and its transcript.
- `queue`: one turn per conversation at a time; later messages wait or merge.
- `runner`: one turn of the model, with a deadline, tools and timings.
- `prompt`: what the model is sent.
- `tools`: what the model may call.
- `timing`: where the time of a turn went, for Logs and benchmarks.
"""
