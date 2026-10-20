# OpenAI Docs orchestration guidance

Reviewed 2026-10-15. These guides describe different runtimes. The rules in
this repository use their decision guidance while retaining Pi's deterministic
router and existing worker backends.

| Official guide | Guidance applied here | Runtime boundary |
| --- | --- | --- |
| [Responses API Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent) | Parallelize independent bounded work; favor one agent for dependent steps, shared mutable state, fixed graphs, or one slow external operation. Account for extra token use. | Its beta flag, hosted collaboration actions, model eligibility, and `max_concurrent_subagents` setting apply to Responses API requests, not Pi. |
| [Agents SDK orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration) | Keep a manager responsible for synthesis and the final reply when specialists perform bounded work. Add specialists when their contract materially differs. | SDK handoffs transfer conversation ownership and are not part of this workflow. |
| [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) | Use compact task contracts and results; start parallel work with read-heavy tasks; coordinate parallel edits; verify model, effort, and permissions at launch. | Native Codex subagent configuration and inheritance do not configure Pi's `spawn_subagent` or `ClaudeWorker` backends. Prompt instructions do not create a read-only sandbox. |

The parent orchestrator owns classification, routing, integration, verification,
and the final response. Each worker receives only its assigned scope and
returns decision-relevant evidence. The validated policy and its prompt rules
remain the executable source of truth.
