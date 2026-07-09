from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True, slots=True)
class IssueComment:
    id: int
    body: str
    user_login: str
    html_url: str
    created_at: str
    updated_at: str


class GitHubClient:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo
        self.base = f"https://api.github.com/repos/{repo}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "codex-github-bridge/1.0",
            }
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API {method} {url} failed: {resp.status_code} {resp.text[:500]}")
        if not resp.content:
            return None
        return resp.json()

    def whoami(self) -> str:
        data = self._request("GET", "https://api.github.com/user")
        return str(data["login"])

    def list_open_issues(self) -> list[dict[str, Any]]:
        data = self._request("GET", f"{self.base}/issues", params={"state": "open", "per_page": 100})
        return list(data)

    def create_issue(self, title: str, body: str) -> int:
        data = self._request("POST", f"{self.base}/issues", json={"title": title, "body": body})
        return int(data["number"])

    def find_or_create_issue(self, title: str) -> int:
        for issue in self.list_open_issues():
            if issue.get("pull_request"):
                continue
            if str(issue.get("title", "")).strip().lower() == title.strip().lower():
                return int(issue["number"])
        body = (
            "Codex GitHub Bridge control issue.\n\n"
            "Commands:\n"
            "- 状态\n"
            "- 继续 <prompt>\n"
            "- 新任务 <prompt>\n"
            "- 总结\n"
            "- diff\n"
            "- 测试\n"
            "- 停止\n"
            "- 帮助\n\n"
            "Do not put secrets or private code in this public issue."
        )
        return self.create_issue(title, body)

    def list_comments(self, issue_number: int) -> list[IssueComment]:
        comments: list[IssueComment] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"{self.base}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page, "sort": "created", "direction": "asc"},
            )
            if not data:
                break
            for item in data:
                comments.append(
                    IssueComment(
                        id=int(item["id"]),
                        body=str(item.get("body") or ""),
                        user_login=str(item.get("user", {}).get("login") or ""),
                        html_url=str(item.get("html_url") or ""),
                        created_at=str(item.get("created_at") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                    )
                )
            if len(data) < 100:
                break
            page += 1
        return comments

    def post_comment(self, issue_number: int, body: str) -> str:
        data = self._request("POST", f"{self.base}/issues/{issue_number}/comments", json={"body": body})
        return str(data.get("html_url") or "")
