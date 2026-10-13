#!/usr/bin/env bash
set -euo pipefail
mkdir -p _research/repos
clone_if_missing() {
  local url="$1"
  local target="$2"
  if [ -d "$target/.git" ]; then
    echo "[skip] $target already cloned"
  else
    echo "[clone] $url -> $target"
    git clone "$url" "$target"
  fi
}
if [ -d "_research/repos/hermes-agent/.git" ]; then
  echo "[skip] Hermes already cloned"
else
  git clone --recurse-submodules https://github.com/NousResearch/hermes-agent.git _research/repos/hermes-agent
fi
clone_if_missing https://github.com/FoundationAgents/OpenManus.git _research/repos/OpenManus
clone_if_missing https://github.com/langchain-ai/langgraph.git _research/repos/langgraph
clone_if_missing https://github.com/huggingface/smolagents.git _research/repos/smolagents
clone_if_missing https://github.com/camel-ai/owl.git _research/repos/owl
clone_if_missing https://github.com/ollama/ollama.git _research/repos/ollama
clone_if_missing https://github.com/QwenLM/Qwen3.git _research/repos/Qwen3

echo "Reference repositories cloned under _research/repos/."
