"""
cascade/core/knowledge_graph.py
───────────────────────────────
Builds structural and historical repository indexes (AST + Git Churn/Ownership).
"""

from __future__ import annotations

import ast
import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

class RepositoryKnowledgeGraph:
    """
    Constructs a knowledge graph of a repository by merging static AST structures
    with git history logs (modification churn and author ownership).
    """

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    def build_graph(self, max_files: int = 150) -> dict[str, Any]:
        """Builds and returns the full repository knowledge graph."""
        files_data = {}
        
        # 1. Walk files and build AST static structures
        python_files = self._collect_python_files(max_files)
        for py_file in python_files:
            rel_path = str(py_file.relative_to(self.repo_path)).replace("\\", "/")
            try:
                ast_info = self._parse_ast(py_file)
                files_data[rel_path] = ast_info
            except Exception:
                # Skip unparseable files
                pass

        # 2. Integrate Git history logs (churn + ownership)
        git_metadata = self._get_git_metadata()
        for rel_path, meta in git_metadata.items():
            if rel_path in files_data:
                files_data[rel_path].update(meta)
            else:
                # Include non-python files (e.g. config, yaml) that are active in git history
                files_data[rel_path] = {
                    "classes": [],
                    "functions": [],
                    "imports": [],
                    "line_count": 0,
                    **meta
                }

        # 3. Calculate co-changes (simplified correlation of file modifications)
        co_changes = self._get_co_changes()

        return {
            "files": files_data,
            "co_changes": co_changes,
            "total_files": len(files_data),
        }

    def _collect_python_files(self, max_files: int) -> list[Path]:
        """Collect Python files, excluding virtual environments and packages."""
        exclude_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build"}
        files = []
        for root, dirs, filenames in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for f in filenames:
                if f.endswith(".py"):
                    files.append(Path(root) / f)
                    if len(files) >= max_files:
                        return files
        return files

    def _parse_ast(self, file_path: Path) -> dict[str, Any]:
        """Parse AST of a Python file to extract classes, functions, and imports."""
        content = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(content)

        imports = []
        classes = []
        functions = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                methods = [
                    item.name
                    for item in ast.iter_child_nodes(node)
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                classes.append({
                    "name": node.name,
                    "methods": methods,
                    "bases": [ast.unparse(b) for b in node.bases]
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)

        return {
            "classes": classes,
            "functions": functions,
            "imports": list(set(imports)),
            "line_count": len(content.splitlines()),
        }

    def _get_git_metadata(self) -> dict[str, dict[str, Any]]:
        """Parse git log to compute file churn (commit counts) and author ownership."""
        metadata = {}
        try:
            # Run git log to list files changed per commit with their author
            # Format: <author_name>|<file_path>
            result = subprocess.run(
                ["git", "log", "--name-only", "--pretty=format:AUTH:%an", "-n", "300"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15
            )
            if result.returncode != 0:
                return {}

            current_author = None
            file_commits = [] # List of tuples: (file_path, author)
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("AUTH:"):
                    current_author = line.split("AUTH:", 1)[1]
                elif current_author:
                    # Normalized relative path
                    file_commits.append((line.replace("\\", "/"), current_author))

            # Aggregate churn and ownership
            file_to_authors = {}
            for path, author in file_commits:
                if not path or "/" not in path and not path.endswith(".py"):
                    # Ignore top-level generic files unless important
                    if not path.endswith((".py", ".toml", ".json", ".yml", ".yaml")):
                        continue
                file_to_authors.setdefault(path, []).append(author)

            for path, authors in file_to_authors.items():
                churn = len(authors)
                author_counter = Counter(authors)
                primary_owner, ownership_commits = author_counter.most_common(1)[0]
                ownership_fraction = ownership_commits / churn

                metadata[path] = {
                    "git_churn": churn,
                    "primary_owner": primary_owner,
                    "ownership_fraction": round(ownership_fraction, 2),
                }

        except Exception:
            pass
        return metadata

    def _get_co_changes(self) -> list[dict[str, Any]]:
        """Identify pairs of files that frequently change together in commits."""
        co_changes = []
        try:
            # Get changes per commit
            result = subprocess.run(
                ["git", "log", "--name-only", "--pretty=format:COMMIT", "-n", "50"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15
            )
            if result.returncode != 0:
                return []

            commits = []
            current_commit = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line == "COMMIT":
                    if current_commit:
                        commits.append(current_commit)
                        current_commit = []
                elif line:
                    current_commit.append(line.replace("\\", "/"))
            if current_commit:
                commits.append(current_commit)

            # Find pairs
            pair_counter = Counter()
            for files in commits:
                # Remove duplicates in same commit
                unique_files = list(set(files))
                if len(unique_files) < 2 or len(unique_files) > 10:
                    continue # Skip huge commits (e.g. merge/refactor commits)
                for i in range(len(unique_files)):
                    for j in range(i + 1, len(unique_files)):
                        f1, f2 = sorted([unique_files[i], unique_files[j]])
                        pair_counter[(f1, f2)] += 1

            # Keep top correlations
            for (f1, f2), count in pair_counter.most_common(10):
                co_changes.append({
                    "files": [f1, f2],
                    "count": count
                })

        except Exception:
            pass
        return co_changes
