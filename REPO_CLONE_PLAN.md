# Reference Repository Clone Plan

## Principle

These repositories are for research, benchmarking and selective reuse. Do not vendor them into application source. Clone into `_research/repos/`.

## Tier 1 — Clone First

### Hermes Agent
https://github.com/NousResearch/hermes-agent

Use it to study persistent memory, skill creation/evolution, scheduling, subagents and the agent loop.

```bash
git clone --recurse-submodules https://github.com/NousResearch/hermes-agent.git _research/repos/hermes-agent
```

### OpenManus
https://github.com/FoundationAgents/OpenManus

Use it to study a practical autonomous agent application, tool-call/ReAct patterns and browser/tool integration.

```bash
git clone https://github.com/FoundationAgents/OpenManus.git _research/repos/OpenManus
```

### LangGraph
https://github.com/langchain-ai/langgraph

Production supervisor/state-machine candidate. Study interrupts, checkpoints and stateful graphs.

```bash
git clone https://github.com/langchain-ai/langgraph.git _research/repos/langgraph
```

### smolagents
https://github.com/huggingface/smolagents

Study lightweight agent loops, local-model support and specialist-agent patterns.

```bash
git clone https://github.com/huggingface/smolagents.git _research/repos/smolagents
```

### OWL
https://github.com/camel-ai/owl

Study multi-agent workforce and specialist collaboration patterns.

```bash
git clone https://github.com/camel-ai/owl.git _research/repos/owl
```

### Ollama
https://github.com/ollama/ollama

Local model runtime. Usually install/run rather than modify source.

```bash
git clone https://github.com/ollama/ollama.git _research/repos/ollama
```

### Qwen3
https://github.com/QwenLM/Qwen3

Model family/reference for local reasoning and agentic workloads.

```bash
git clone https://github.com/QwenLM/Qwen3.git _research/repos/Qwen3
```

## Tier 2 — Clone Later When Needed

- Suna/Kortix: https://github.com/kortix-ai/suna — approval and AI-employee patterns.
- OpenHands: https://github.com/OpenHands/OpenHands — sandbox/execution patterns.
- Agno: https://github.com/agno-agi/agno — agent-platform/control-plane patterns.
- Browser Use: https://github.com/browser-use/browser-use — agentic browser verification.
- Langfuse: https://github.com/langfuse/langfuse — self-hosted traces/evals.

## Production vs Pattern Sources

Likely production dependencies:
- LangGraph
- Ollama
- PostgreSQL + self-hosted Qdrant (ADR-0017; pgvector superseded)
- FastAPI
- Next.js
- Playwright
- possibly smolagents for narrowly scoped specialist use

Pattern sources:
- Hermes: procedural learning + skill evolution
- OpenManus: autonomous agent loop
- OWL: specialization
- Suna: permission model
- OpenHands: worker isolation
- Agno: control-plane architecture

## Licensing Rule

Before copying implementation code from a reference repository:
1. inspect LICENSE;
2. confirm compatibility;
3. document provenance;
4. prefer reimplementation of a pattern where practical.

A public GitHub repository is not automatically unrestricted for reuse.
