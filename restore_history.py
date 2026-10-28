#!/usr/bin/env python3
"""Restore the latest validated history artifact through the official GitHub API.

Missing first-run history is allowed. HTTP/auth/ZIP/schema failures are NOT
silently treated as empty history. Nothing is extracted from the ZIP to disk;
only its bounded history.json member is read. Credentials never leave api.github.com.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from render import MAX_BYTES, loads_json, validate_history, write_json

ARTIFACT_NAME = "ambient-panel-history-v1"
API = "https://api.github.com"
MAX_DOWNLOAD = 5 * 1024 * 1024
MAX_PAGES = 20


class HistoryRestoreError(Exception):
    """Only fixed, non-sensitive diagnostic messages are exposed to logs."""


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or not target.hostname:
            raise HistoryRestoreError("Refusing a non-HTTPS artifact redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # Signed artifact download URLs need no GitHub bearer token.
            # Strip it on EVERY redirect, including same-origin redirects.
            redirected.remove_header("Authorization")
        return redirected


def request_bytes(url: str, token: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise HistoryRestoreError("Unexpected initial API host")
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ambient-panel-history",
    })
    opener = urllib.request.build_opener(SafeRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                data = response.read(MAX_DOWNLOAD + 1)
            if len(data) > MAX_DOWNLOAD:
                raise HistoryRestoreError("API response exceeds the size limit")
            return data
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise HistoryRestoreError(
                f"GitHub API returned HTTP {code}; history was NOT reset. "
                "Check actions: read and artifact availability, then retry."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise HistoryRestoreError("Network error restoring history; history was NOT reset") from None
    raise HistoryRestoreError("History request did not complete")


def api_json(path: str, token: str) -> dict:
    value = loads_json(request_bytes(API + path, token))
    if not isinstance(value, dict):
        raise HistoryRestoreError("Unexpected API response format")
    return value


def list_artifacts(repo: str, token: str) -> list[dict]:
    artifacts = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({"name": ARTIFACT_NAME, "per_page": 100, "page": page})
        response = api_json(f"/repos/{repo}/actions/artifacts?{query}", token)
        batch = response.get("artifacts")
        if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
            raise HistoryRestoreError("Unexpected artifact list format")
        artifacts.extend(batch)
        if len(batch) < 100:
            return artifacts
    raise HistoryRestoreError("Too many history artifacts; check retention-days before retrying")


def select_artifact(artifacts: list[dict], repo_id: int, branch: str) -> dict | None:
    named = [a for a in artifacts if a.get("name") == ARTIFACT_NAME]
    active = [a for a in named if a.get("expired") is False]
    candidates = []
    for artifact in active:
        run = artifact.get("workflow_run", {})
        if (not isinstance(run, dict) or run.get("repository_id") != repo_id
                or run.get("head_repository_id") != repo_id or run.get("head_branch") != branch):
            continue
        if type(artifact.get("id")) is not int or artifact["id"] <= 0:
            raise HistoryRestoreError("Invalid artifact identifier")
        candidates.append(artifact)
    if active and not candidates:
        raise HistoryRestoreError("History artifacts do not match the default branch/repository; refusing to reset")
    return max(candidates, key=lambda artifact: artifact["id"]) if candidates else None


def unpack_history(data: bytes, digest: str | None = None) -> dict:
    if digest:
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise HistoryRestoreError("Unexpected artifact digest format")
        if hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1].lower():
            raise HistoryRestoreError("Artifact checksum mismatch; history was NOT reset")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(members) != 1 or members[0].filename != "history.json":
                raise HistoryRestoreError("History artifact must contain only history.json")
            member = members[0]
            if member.file_size > MAX_BYTES or member.flag_bits & 1:
                raise HistoryRestoreError("History artifact member is too large or encrypted")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise HistoryRestoreError("Symbolic links are not allowed in history artifacts")
            history = validate_history(loads_json(archive.read(member)))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise HistoryRestoreError("History archive could not be read; history was NOT reset") from None
    if not history["samples"]:
        raise HistoryRestoreError("Stored history is empty; refusing to replace prior observations")
    return history


def restore(repo: str, token: str, repo_id: int, branch: str, workflow: str, output: Path) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise HistoryRestoreError("Invalid repository configuration")
    if not token or repo_id <= 0 or not branch:
        raise HistoryRestoreError("Missing GitHub token, repository ID, or default branch")
    artifacts = list_artifacts(repo, token)
    selected = select_artifact(artifacts, repo_id, branch)
    if selected is None:
        if artifacts:
            print("::warning::Stored history has expired. History will restart with the next observation.")
        else:
            print("::notice::No history artifact yet. The first observation will start the history.")
        # Never remove a pre-existing local state file on ambiguous state.
        if output.exists():
            raise HistoryRestoreError("A local history file already exists; refusing an ambiguous reset")
        return False
    run_id = selected["workflow_run"].get("id")
    if type(run_id) is not int or run_id <= 0:
        raise HistoryRestoreError("Invalid workflow run identifier")
    run = api_json(f"/repos/{repo}/actions/runs/{run_id}", token)
    path = run.get("path", "")
    if (not isinstance(path, str) or path.split("@", 1)[0] != workflow
            or run.get("event") not in ("repository_dispatch", "workflow_dispatch")):
        raise HistoryRestoreError("History was produced by an unexpected workflow; refusing to reset")
    # A completed artifact is useful even when a subsequent Pages deployment failed.
    raw = request_bytes(f"{API}/repos/{repo}/actions/artifacts/{selected['id']}/zip", token)
    history = unpack_history(raw, selected.get("digest"))
    write_json(output, history)
    print(f"Restored history artifact {selected['id']}; {len(history['samples'])} snapshots. Values not logged.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="state/history.json")
    args = parser.parse_args()
    try:
        repo_id = int(os.environ.get("GITHUB_REPOSITORY_ID", "0"))
    except ValueError:
        raise HistoryRestoreError("Invalid repository ID") from None
    restore(
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""), repo_id,
        os.environ.get("DEFAULT_BRANCH", ""), os.environ.get("EXPECTED_WORKFLOW", ".github/workflows/pages.yml"),
        Path(args.output),
    )


if __name__ == "__main__":
    try:
        main()
    except (HistoryRestoreError, ValueError) as exc:
        print(f"History restore refused: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"History restore failed ({type(exc).__name__}); no new history published.", file=sys.stderr)
        sys.exit(1)
