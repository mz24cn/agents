"""Agent management with CRUD operations and JSON persistence.

Provides AgentManager for managing agents. Follows the same patterns as
PromptTemplateManager.

Zero third-party dependencies — only Python standard library.
"""

import datetime
import json
import os
from typing import Optional

from runtime.common import parse_labels


class AgentManager:
    """Manages agents with CRUD operations and JSON persistence.

    Stores agent data in memory and persists to individual JSON files in
    the agents directory.
    """

    def __init__(self, agents_dir: Optional[str] = None) -> None:
        self._agents: dict[str, dict] = {}
        self._agents_dir = agents_dir or os.environ.get(
            "AGENTS_RUNTIME_DIR",
            os.path.join(os.path.expanduser("~"), ".agents_runtime"),
        )
        self._agents_dir = os.path.join(self._agents_dir, "agents")

    def list_all(self) -> list[dict]:
        """Return a list of all agents sorted by last_modified descending."""
        agents = list(self._agents.values())
        agents.sort(key=lambda a: a.get("last_modified", ""), reverse=True)
        return agents

    def get(self, agent_id: str) -> Optional[dict]:
        """Retrieve an agent by its ID."""
        return self._agents.get(agent_id)

    def create(
        self,
        agent_id: str,
        model_id: str,
        nickname: str,
        tool_ids: Optional[list] = None,
        template_id: Optional[str] = None,
        template_arguments: Optional[dict] = None,
        system_prompt: str = "",
        myself_view: str = "",
        description: str = "",
        avatar: str = "",
        labels: Optional[list] = None,
    ) -> dict:
        """Create a new agent."""
        agent = {
            "agent_id": agent_id,
            "model_id": model_id,
            "tool_ids": tool_ids or [],
            "template_id": template_id,
            "template_arguments": template_arguments or {},
            "system_prompt": system_prompt,
            "nickname": nickname,
            "myself_view": myself_view,
            "description": description,
            "labels": parse_labels(labels),
            "last_modified": datetime.datetime.now().isoformat(),
            "avatar": avatar,
        }
        self._agents[agent_id] = agent
        self._save_to_disk(agent_id, agent)
        return agent

    def update(self, agent_id: str, updates: dict) -> Optional[dict]:
        """Update an existing agent.

        Args:
            agent_id: The agent ID to update.
            updates: Dict of fields to update.

        Returns:
            The updated agent, or None if not found.
        """
        if agent_id not in self._agents:
            return None
        agent = self._agents[agent_id]
        for key in ("model_id", "tool_ids", "template_id", "template_arguments",
                     "system_prompt", "nickname", "myself_view", "description", "avatar"):
            if key in updates:
                agent[key] = updates[key]

        if "labels" in updates:
            agent["labels"] = parse_labels(updates["labels"])

        agent["last_modified"] = datetime.datetime.now().isoformat()
        self._save_to_disk(agent_id, agent)
        return agent

    def delete(self, agent_id: str) -> bool:
        """Delete an agent by its ID.

        Returns:
            True if deleted, False if not found.
        """
        if agent_id not in self._agents:
            return False
        del self._agents[agent_id]
        self._delete_from_disk(agent_id)
        return True

    def _get_file_path(self, agent_id: str) -> str:
        return os.path.join(self._agents_dir, f"{agent_id}.json")

    def _save_to_disk(self, agent_id: str, agent: dict) -> None:
        os.makedirs(self._agents_dir, exist_ok=True)
        fpath = self._get_file_path(agent_id)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(agent, f, ensure_ascii=False, indent=2)

    def _delete_from_disk(self, agent_id: str) -> None:
        fpath = self._get_file_path(agent_id)
        if os.path.isfile(fpath):
            os.remove(fpath)

    def load(self) -> None:
        """Load all agents from disk into memory."""
        os.makedirs(self._agents_dir, exist_ok=True)
        self._agents.clear()
        if not os.path.isdir(self._agents_dir):
            return
        for fname in sorted(os.listdir(self._agents_dir), reverse=True):
            if fname.endswith(".json"):
                fpath = os.path.join(self._agents_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        agent = json.load(f)
                        agent_id = agent.get("agent_id")
                        if agent_id:
                            # Migration: convert old "group" field to "labels"
                            if "group" in agent and "labels" not in agent:
                                agent["labels"] = parse_labels(agent.pop("group"))
                            elif "group" in agent:
                                # Both exist: drop legacy group key
                                del agent["group"]
                            agent.setdefault("labels", [])
                            self._agents[agent_id] = agent
                except (json.JSONDecodeError, OSError):
                    pass
