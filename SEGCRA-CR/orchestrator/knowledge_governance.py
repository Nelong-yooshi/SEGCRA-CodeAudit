"""知識治理 — 讓「使用者講定的規範」透過機制自動被定住,不需要新 UI。

核心想法:**GitLab 就是 UI,merge 就是鎖,自動起草 MR 就是機制。**
- 每條 binding 知識項 = 知識庫 repo 裡一個 .md 檔。
- 偵測到審查人員的明確指示、或同型 finding 被反覆駁回 → AI 自動起草一條 binding,
  開一個 MR 到知識庫 repo(附證據)。
- 人在 GitLab 按 merge = 這條規範被定住(權限、稽核軌跡、人閘全用現成的)。
- 合併後 sync 回 memory/knowledge/,審查管線即刻遵守。

不做全自動無人合併:一條規範釘錯就是永久錯 → 停在「AI 起草、人一鍵 merge」。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx

from .config import PKG_ROOT

sys.path.insert(0, str(PKG_ROOT))
from toolbox.knowledge_store import Item, dumps, load_items  # noqa: E402


def _load_gitlab_env() -> dict:
    env = {}
    f = PKG_ROOT / "config" / "gitlab.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):      # 支援 `export KEY=VALUE`
                    k = k[len("export "):].strip()
                env[k] = v.strip().strip("'\"")
    return env


class KnowledgeRepo:
    """知識庫 GitLab 專案的最小 API 包裝:確保專案存在、在分支上 commit 檔案、
    開 MR、merge、把 main 同步回本地 memory/knowledge/。"""

    def __init__(self, url: str | None = None, token: str | None = None,
                 path: str = "root/segcra-knowledge"):
        env = _load_gitlab_env()
        self.url = (url or os.environ.get("GITLAB_URL") or env.get("GITLAB_URL")
                    or "http://localhost:8929").rstrip("/")
        self.token = (token or os.environ.get("GITLAB_TOKEN")
                      or env.get("GITLAB_TOKEN") or "")
        self.path = path
        self.pid = None

    def _api(self, method: str, ep: str, **kw):
        last = None
        for attempt in range(3):          # tunnel 偶發斷線 → 輕量重試
            try:
                r = httpx.request(method, f"{self.url}/api/v4{ep}",
                                  headers={"PRIVATE-TOKEN": self.token},
                                  timeout=30, **kw)
                r.raise_for_status()
                return r.json() if r.text else {}
            except (httpx.TransportError,) as e:
                last = e
                time.sleep(2)
        raise last

    # -------- 專案 --------
    def ensure_project(self) -> int:
        ns, name = self.path.split("/", 1)
        # 用 search 查詢是否已存在(比 path 直查穩定,避開路徑編碼問題)
        hits = self._api("GET", "/projects", params={"search": name,
                                                     "membership": True})
        for p in hits:
            if p.get("path_with_namespace") == self.path:
                self.pid = p["id"]
                return self.pid
        # 不存在 → 建立
        try:
            p = self._api("POST", "/projects", json={
                "name": name, "path": name, "visibility": "private",
                "initialize_with_readme": True,
                "description": "SEGCRA 知識庫:binding 恆常規範 / 慣例。merge 即定住。"})
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"知識庫專案 {self.path} 不存在且無法自動建立(HTTP {e.response.status_code})。"
                f"請先在 GitLab 手動建立 {self.path},或確認 token 有建專案權限。") from e
        self.pid = p["id"]
        time.sleep(1)
        return self.pid

    @property
    def default_branch(self) -> str:
        p = self._api("GET", f"/projects/{self.pid}")
        return p.get("default_branch") or "main"

    # -------- 檔案 / 分支 / commit --------
    def get_file(self, path: str, ref: str = "main") -> str | None:
        try:
            import base64
            r = self._api("GET", f"/projects/{self.pid}/repository/files/"
                          f"{path.replace('/', '%2F')}?ref={ref}")
            return base64.b64decode(r["content"]).decode("utf-8")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def create_branch(self, branch: str, ref: str = "main"):
        try:
            self._api("POST", f"/projects/{self.pid}/repository/branches",
                      params={"branch": branch, "ref": ref})
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400,):  # 已存在
                raise

    def commit_file(self, branch: str, path: str, content: str, message: str):
        action = "update" if self.get_file(path, ref=branch) is not None else "create"
        self._api("POST", f"/projects/{self.pid}/repository/commits", json={
            "branch": branch, "commit_message": message,
            "actions": [{"action": action, "file_path": path, "content": content}]})

    def open_mr(self, source: str, title: str, description: str,
                target: str = "main") -> dict:
        return self._api("POST", f"/projects/{self.pid}/merge_requests", json={
            "source_branch": source, "target_branch": target,
            "title": title, "description": description})

    def merge_mr(self, iid: int, wait: int = 20) -> dict:
        """合併 MR。剛開的 MR mergeability 仍在計算 → 先輪詢到可合併再合併。"""
        for _ in range(wait):
            mr = self._api("GET", f"/projects/{self.pid}/merge_requests/{iid}")
            st = mr.get("detailed_merge_status") or mr.get("merge_status")
            if mr.get("state") == "merged":
                return mr
            if st in ("mergeable", "can_be_merged"):
                break
            if st in ("conflict", "broken_status", "cannot_be_merged"):
                raise RuntimeError(f"MR !{iid} 無法合併:{st}")
            time.sleep(1)
        return self._api("PUT", f"/projects/{self.pid}/merge_requests/{iid}/merge")

    def list_md(self, ref: str = "main", subdir: str = "knowledge") -> list[str]:
        try:
            tree = self._api("GET", f"/projects/{self.pid}/repository/tree",
                             params={"ref": ref, "path": subdir, "per_page": 100,
                                     "recursive": True})
        except httpx.HTTPStatusError:
            return []
        return [t["path"] for t in tree if t["type"] == "blob"
                and t["path"].endswith(".md")]

    def sync_to_local(self, dest: Path, subdir: str = "knowledge") -> list[str]:
        """把知識庫 main 分支的 knowledge/*.md 拉回本地 memory/knowledge/。
        合併後的 binding 由此變成審查管線即刻遵守的規範。"""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        pulled = []
        for path in self.list_md(subdir=subdir):
            content = self.get_file(path, ref="main")
            if content is None:
                continue
            (dest / Path(path).name).write_text(content, encoding="utf-8")
            pulled.append(Path(path).name)
        return pulled

    def seed_from_local(self, src: Path, subdir: str = "knowledge"):
        """初始化:把目前本地知識庫上傳到 repo main(一次性,建立基準)。"""
        for f in sorted(Path(src).glob("*.md")):
            self.commit_file("main", f"{subdir}/{f.name}", f.read_text(encoding="utf-8"),
                             f"seed: {f.name}")


# ---------------------------------------------------------------- 起草 binding
# 確定性觸發:審查人員留言含這些「硬指示」字樣 → 視為可能的常設規範,交 AI 結構化
_DIRECTIVE_MARKERS = re.compile(
    r"不要報|不用報|不用管|不必|不得|不應|別報|勿|一律|統一|固定用|改用|沿用|"
    r"屬核定用途|核定用途|不適用|不算|免報|免填|以後都|一向|本來就|不需再|無需")



# 檢核點語意 → 代碼(審查人員講的是白話,模型不知道內部代碼;用關鍵詞確定性補上 suppress)
_CHECK_KEYWORDS = {
    "H001": ["沖正", "退匯", "淨額"],
    "H002": ["通報粒度", "聯名戶", "重複通報", "粒度", "一對多"],
    "H003": ["規格核對", "逐項核對", "與規格"],
    "H004": ["異常查詢", "憑證", "密碼"],
    "H005": ["遮罩"],
}


def _infer_suppress(text: str) -> list[str]:
    """從指示白話推出要抑制的檢核點代碼(確定性,不靠模型記代碼)。"""
    return sorted({code for code, kws in _CHECK_KEYWORDS.items()
                   if any(k in text for k in kws)})


DRAFT_SYSTEM = """你是知識治理助理。審查人員在審查/需求討論中講了一句話,你要判斷這是不是一條
**常設的團隊硬規定**(以後每次都適用),若是,把它結構化成一條 binding 知識項。

# 判準
- 是常設規定(如「這類科目一律不過濾幣別」「通報輸出個資屬核定用途不必報」)→ 產出
- 只是這次的一次性說明、閒聊、或不確定 → 回 {"is_rule": false}

# 若是規定,輸出(嚴格 JSON):
{"is_rule": true,
 "text": "<用完整、明確的書面語重述這條規範,讓未來審查可直接遵守>",
 "scope": ["anomaly-rules"],           // 從 [anomaly-rules, settlement, crm, core-banking, etl, sql-style] 選
 "conflict_key": "<可選:同主題互斥鍵,如 reversal-handling>",
 "tags": ["..."]}
(不用管內部檢核點代碼——系統會自動對應。)只輸出 JSON。"""


async def draft_binding(cfg, directive_text: str, profile_name: str | None = None,
                        default_scope: list[str] | None = None) -> Item | None:
    """AI 把一句口語指示轉成結構化 binding 知識項(source=user-explicit,權威最高)。
    這只是『起草』——仍要開 MR 給人 merge 才會生效。"""
    from .agent import extract_json, run_agent
    from .tool_hub import ToolHub
    async with ToolHub(servers=[]) as hub:
        raw = await run_agent(cfg, cfg.profile(profile_name), DRAFT_SYSTEM,
                              f"審查人員留言:「{directive_text}」\n請判斷並輸出 JSON。",
                              hub, use_tools=False, verbose=False)
    d = extract_json(raw) or {}
    if not d.get("is_rule") or not d.get("text"):
        return None
    scope = d.get("scope") or default_scope or ["anomaly-rules"]
    if isinstance(scope, str):
        scope = [scope]
    slug = "explicit-" + re.sub(r"[^a-z0-9]+", "-",
                                (d.get("conflict_key") or d["text"][:16]).lower()).strip("-")
    # suppress 完全由白話(指示 + 重述)確定性推出——不讓模型猜內部代碼(它會亂填),
    # 確保「不用管通報粒度」這種口語能真的對應到 H002 並被抑制。
    suppress = _infer_suppress(directive_text + " " + d["text"])
    return Item(
        id=slug[:40] or f"explicit-{int(time.time())}", text=d["text"].strip(),
        tier="binding", source="user-explicit", scope=scope,
        suppress=suppress, conflict_key=d.get("conflict_key"),
        tags=d.get("tags") or ["審查人員指示"], ts=int(time.time()))


def propose_binding(repo: KnowledgeRepo, item: Item, evidence: str,
                    subdir: str = "knowledge") -> dict:
    """把一條 binding 起草成 MR:開分支、commit .md、開 MR(附證據)。回傳 MR 資訊。"""
    repo.ensure_project()
    branch = f"autopin/{item.id}-{int(time.time())}"
    repo.create_branch(branch, ref=repo.default_branch)
    repo.commit_file(branch, f"{subdir}/{item.id}.md", dumps(item),
                     f"autopin: 起草 binding「{item.id}」")
    desc = (f"## 🤖 自動起草的恆常規範(binding),待人工核准\n\n"
            f"**來源證據**:{evidence}\n\n"
            f"**規範內容**:{item.text}\n\n"
            f"- scope:`{item.scope}`　權威:`{item.source}`"
            f"{'　抑制檢核點:`' + ','.join(item.suppress) + '`' if item.suppress else ''}\n\n"
            f"> merge 此 MR = 這條規範被**定住**,審查管線即刻遵守(sync 後)。"
            f"若不成立請直接關閉。")
    mr = repo.open_mr(branch, f"[binding] {item.text[:40]}", desc)
    return {"iid": mr["iid"], "web_url": mr["web_url"], "branch": branch,
            "item_id": item.id}
