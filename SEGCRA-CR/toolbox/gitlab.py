"""gitlab 工具模組 — MR 讀取與審查結果回寫,行程內直接呼叫。

兩種模式(以環境變數切換):
- mock(預設):MR 從 fixtures/*.json 讀,post_* 寫到 review_output/,離線 demo 用
- real:設定 GITLAB_URL + GITLAB_TOKEN + GITLAB_PROJECT 後走 GitLab REST API v4
"""
import json
import os
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", PKG_ROOT / "fixtures"))
OUTPUT_DIR = Path(os.environ.get("REVIEW_OUTPUT", PKG_ROOT / "review_output"))

GITLAB_URL = os.environ.get("GITLAB_URL", "")          # e.g. http://localhost:8929
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
GITLAB_PROJECT = os.environ.get("GITLAB_PROJECT", "")  # project id 或 URL-encoded path
REAL_MODE = bool(GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECT)


# --- real mode helpers ---------------------------------------------------

def _api(method: str, path: str, **kwargs):
    import httpx
    r = httpx.request(
        method, f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}{path}",
        headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()


# --- mock mode helpers ---------------------------------------------------

def _mock_mr(mr_id: str) -> dict:
    f = FIXTURES_DIR / f"mr_{mr_id}.json"
    if not f.exists():
        raise FileNotFoundError(f"fixture 不存在: {f.name};現有: "
                                + ", ".join(p.name for p in FIXTURES_DIR.glob('mr_*.json')))
    return json.loads(f.read_text(encoding="utf-8"))


def _mock_output(mr_id: str, kind: str, payload: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    f = OUTPUT_DIR / f"mr_{mr_id}_review.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {
        "mr_id": mr_id, "inline_comments": [], "summary": None, "labels": [],
        "commit_status": None}
    if kind == "inline":
        data["inline_comments"].append(payload)
    elif kind == "summary":
        data["summary"] = payload
    elif kind == "label":
        data["labels"].append(payload["label"])
    elif kind == "commit_status":
        data["commit_status"] = payload
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- tools ---------------------------------------------------------------

def get_mr_diff(mr_id: str) -> str:
    """取得 MR 的標題、描述、head commit sha 與 diff(逐檔)。"""
    if REAL_MODE:
        mr = _api("GET", f"/merge_requests/{mr_id}")
        changes = _api("GET", f"/merge_requests/{mr_id}/diffs")
        files = [{"path": c["new_path"], "diff": c["diff"]} for c in changes]
        return json.dumps({"title": mr["title"], "description": mr.get("description", ""),
                           "sha": mr.get("sha", ""), "files": files}, ensure_ascii=False)
    mr = _mock_mr(mr_id)
    return json.dumps({"title": mr["title"], "description": mr.get("description", ""),
                       "sha": mr.get("sha", f"mock-{mr_id}"), "files": mr["files"]},
                      ensure_ascii=False)


def get_file(path: str, ref: str = "main", mr_id: str = "") -> str:
    """取得 repo 內檔案完整內容(如 specs/R-xxx.md;看 diff 不夠時也可用)。
    mock 模式下 ref 無作用。"""
    if REAL_MODE:
        import urllib.parse
        import httpx
        r = httpx.get(
            f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/repository/files/"
            f"{urllib.parse.quote(path, safe='')}/raw",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, params={"ref": ref}, timeout=30)
        if r.status_code == 404:
            return f"(repo 內無 {path})"
        r.raise_for_status()
        return r.text
    mr = _mock_mr(mr_id) if mr_id else None
    if mr:
        for f in mr["files"]:
            if f["path"] == path and "full_content" in f:
                return f["full_content"]
    return f"(mock 模式無 {path} 的完整內容,請依 diff 判斷)"


def post_inline_comment(mr_id: str, file: str, line: int, body: str,
                        severity: str = "info") -> str:
    """在 MR 的特定檔案行號留下審查意見。"""
    if REAL_MODE:
        text = f"**[{severity}]** {body}"
        try:
            mr = _api("GET", f"/merge_requests/{mr_id}")
            pos = {"position_type": "text", "new_path": file, "new_line": line,
                   **{k: mr["diff_refs"][k] for k in ("base_sha", "head_sha", "start_sha")}}
            _api("POST", f"/merge_requests/{mr_id}/discussions",
                 json={"body": text, "position": pos})
        except Exception:
            # 行號對不上 diff 時退成一般留言,不讓單一 finding 卡住整份審查
            _api("POST", f"/merge_requests/{mr_id}/notes",
                 json={"body": f"`{file}:{line}`\n\n{text}"})
    else:
        _mock_output(mr_id, "inline", {"file": file, "line": line,
                                       "severity": severity, "body": body})
    return json.dumps({"ok": True}, ensure_ascii=False)


def post_summary(mr_id: str, body: str, score: int, verdict: str) -> str:
    """發布審查總結留言(總評 + 分數 + 結論)。"""
    text = f"## 🤖 AI 審查報告\n\n**評分:{score}/100 — {verdict}**\n\n{body}"
    if REAL_MODE:
        _api("POST", f"/merge_requests/{mr_id}/notes", json={"body": text})
    else:
        _mock_output(mr_id, "summary", {"score": score, "verdict": verdict, "body": body})
    return json.dumps({"ok": True}, ensure_ascii=False)


def set_commit_status(sha: str, state: str, name: str = "segcra/review",
                      description: str = "", mr_id: str = "") -> str:
    """回寫 commit status(external pipeline)。state: success | failed | pending。
    CE 的 merge 閘門靠它:專案設定「Pipelines must succeed」後,
    failed 的 commit 無法 merge。"""
    if state not in ("success", "failed", "pending"):
        return json.dumps({"error": "state 必須是 success/failed/pending"}, ensure_ascii=False)
    if REAL_MODE:
        if not sha:
            return json.dumps({"error": "缺 sha,無法回寫 commit status"}, ensure_ascii=False)
        _api("POST", f"/statuses/{sha}",
             json={"state": state, "name": name, "description": description[:250]})
    else:
        _mock_output(mr_id or "unknown", "commit_status",
                     {"sha": sha, "state": state, "name": name,
                      "description": description[:250]})
    return json.dumps({"ok": True, "state": state}, ensure_ascii=False)


def create_rule_mr(file_path: str, content: str, title: str, description: str) -> str:
    """建立 branch + commit + MR(autopin 起草 binding 等流程用)。回傳 MR id。"""
    if REAL_MODE:
        import re as _re
        import time as _time
        branch = "feat/" + _re.sub(r"[^a-z0-9]+", "-",
                                   Path(file_path).stem.lower()) + f"-{int(_time.time())}"
        _api("POST", "/repository/commits", json={
            "branch": branch, "start_branch": "main",
            "commit_message": f"feat: {title}",
            "actions": [{"action": "create", "file_path": file_path, "content": content}]})
        mr = _api("POST", "/merge_requests", json={
            "source_branch": branch, "target_branch": "main",
            "title": title, "description": description})
        return json.dumps({"mr_iid": mr["iid"], "web_url": mr.get("web_url", "")},
                          ensure_ascii=False)
    ids = [int(p.stem.removeprefix("mr_")) for p in FIXTURES_DIR.glob("mr_*.json")
           if p.stem.removeprefix("mr_").isdigit()]
    new_id = f"{(max(ids) + 1 if ids else 1):03d}"
    (FIXTURES_DIR / f"mr_{new_id}.json").write_text(json.dumps({
        "title": title, "description": description,
        "files": [{"path": file_path, "diff": "(generated)", "full_content": content}],
        "_generated": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({"mr_iid": new_id}, ensure_ascii=False)


def set_label(mr_id: str, label: str) -> str:
    """為 MR 加上標籤(如 ai-review::needs-changes)。"""
    if REAL_MODE:
        _api("PUT", f"/merge_requests/{mr_id}", json={"add_labels": label})
    else:
        _mock_output(mr_id, "label", {"label": label})
    return json.dumps({"ok": True}, ensure_ascii=False)

