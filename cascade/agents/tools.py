"""
cascade/agents/tools.py
───────────────────────
Workspace Tools for ReAct Coder Agent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

class WorkspaceTools:
    """
    Self-contained tools for local file operations within the cloned workspace.
    Allows ReAct agents to read, write, patch, search, and list files.
    """

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = workspace_path.resolve()

    def _resolve_path(self, path: str) -> Path:
        """Resolve a tool path and keep it inside the workspace."""
        candidate = (self.workspace_path / path).resolve()
        if candidate != self.workspace_path and self.workspace_path not in candidate.parents:
            raise ValueError(f"Path '{path}' escapes the workspace.")
        return candidate

    def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Dispatcher to run the requested tool."""
        if tool_name == "read_file":
            return self.read_file(tool_input.get("path", ""))
        elif tool_name == "write_file":
            return self.write_file(tool_input.get("path", ""), tool_input.get("content", ""))
        elif tool_name == "edit_file":
            return self.edit_file(
                tool_input.get("path", ""),
                tool_input.get("target", ""),
                tool_input.get("replacement", "")
            )
        elif tool_name == "grep_search":
            return self.grep_search(tool_input.get("query", ""))
        elif tool_name == "list_dir":
            return self.list_dir(tool_input.get("path", "."))
        else:
            return f"Error: Tool '{tool_name}' is not recognized."

    def read_file(self, path: str) -> str:
        """Read the contents of a file relative to the workspace path."""
        try:
            full_path = self._resolve_path(path)
        except ValueError as e:
            return f"Error: {e}"
        if not full_path.exists():
            return f"Error: File '{path}' does not exist."
        if not full_path.is_file():
            return f"Error: '{path}' is not a file."
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            # Limit returned length to keep context clean
            lines = content.splitlines()
            if len(lines) > 500:
                return (
                    f"Warning: File is too large ({len(lines)} lines). Showing first 500 lines.\n"
                    + "\n".join(lines[:500])
                )
            return content
        except Exception as e:
            return f"Error reading file: {e}"

    def write_file(self, path: str, content: str) -> str:
        """Write full content to a file relative to the workspace path."""
        try:
            full_path = self._resolve_path(path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            return f"Success: Wrote to '{path}'."
        except Exception as e:
            return f"Error writing file: {e}"

    def edit_file(self, path: str, target: str, replacement: str) -> str:
        """Replace a unique target block with replacement content in the file."""
        try:
            full_path = self._resolve_path(path)
        except ValueError as e:
            return f"Error: {e}"
        if not full_path.exists():
            return f"Error: File '{path}' does not exist."
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            occurrences = content.count(target)
            if occurrences == 0:
                return (
                    f"Error: Could not find target content in '{path}'. "
                    "Make sure target content matches exactly (spaces, indentation, newlines)."
                )
            if occurrences > 1:
                return (
                    f"Error: Target content is not unique in '{path}' (found {occurrences} matches). "
                    "Provide more surrounding lines/context."
                )
            new_content = content.replace(target, replacement, 1)
            full_path.write_text(new_content, encoding="utf-8")
            return f"Success: Replaced target content in '{path}'."
        except Exception as e:
            return f"Error editing file: {e}"

    def grep_search(self, query: str) -> str:
        """Search the codebase for a text query (case-insensitive substring search)."""
        results = []
        try:
            for root, dirs, files in os.walk(self.workspace_path):
                # Exclude common directories to keep search clean
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache", ".venv", "node_modules")]
                for file in files:
                    if file.endswith((".py", ".json", ".md", ".yml", ".yaml", ".txt", ".ini", ".toml")):
                        file_path = Path(root) / file
                        try:
                            content = file_path.read_text(encoding="utf-8", errors="replace")
                            for line_idx, line in enumerate(content.splitlines(), 1):
                                if query.lower() in line.lower():
                                    rel_path = file_path.relative_to(self.workspace_path)
                                    results.append(f"{rel_path}:{line_idx}: {line.strip()}")
                        except Exception:
                            pass
            if not results:
                return f"No matches found for query: '{query}'"
            return "\n".join(results[:50])
        except Exception as e:
            return f"Error searching codebase: {e}"

    def list_dir(self, path: str = ".") -> str:
        """List files and folders in a directory relative to the workspace path."""
        try:
            full_path = self._resolve_path(path)
        except ValueError as e:
            return f"Error: {e}"
        if not full_path.exists():
            return f"Error: Directory '{path}' does not exist."
        if not full_path.is_dir():
            return f"Error: '{path}' is not a directory."
        try:
            entries = []
            for entry in full_path.iterdir():
                rel = entry.relative_to(self.workspace_path)
                prefix = "[DIR] " if entry.is_dir() else "[FILE]"
                entries.append(f"{prefix} {rel}")
            if not entries:
                return f"Directory '{path}' is empty."
            return "\n".join(entries)
        except Exception as e:
            return f"Error listing directory: {e}"
