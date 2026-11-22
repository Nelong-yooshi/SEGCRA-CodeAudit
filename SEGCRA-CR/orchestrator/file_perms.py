"""設定檔與密碼檔的權限檢查。規則只有這一份:orchestrator/config.py 匯入它,scripts/sandbox.sh
用 `python3 -I orchestrator/file_perms.py --secret <檔案>` 直接執行它(只用標準庫,不匯入這個套件
的其他模組,所以主機上沒有裝 requirements 也跑得動)。

**只看權限位元,不看擁有者**:設定檔掛進容器時擁有者是主機的 uid,比對擁有者會誤判。

- 檔案:密碼類(`secret=True`)群組或其他人**可讀或可寫**就停(像 ssh 私鑰要 600);其他設定檔
  群組或其他人**可寫**就停(改得到就能把端點換掉,金鑰與每個 MR 的程式碼都送到別人指定的地方)
- 目錄:從**實際的檔案**所在的目錄一路往上到 `/`,每一層群組或其他人可寫就停。哪一層可寫,
  就能把底下的整個目錄改名、換一份新的進來,跟「所在的目錄可寫就能換掉檔案」是同一件事
  (sshd 的 StrictModes 也是一路往上檢查)。
- 連結:先取實際的檔案(`resolve()`)檢查;路徑上有連結時,連結所在的目錄也一路往上檢查
  (改得到那一層就能換掉連結)。
- 有 sticky bit 的目錄(`/tmp` 這種 1777)不算太寬:sticky bit 讓其他帳號不能改名或刪掉
  不是自己的項目,換不掉底下的目錄。測試的暫存目錄在 `/tmp` 底下,靠的也是這一條。

檔案不存在就不檢查(呼叫端各自處理「沒有這個檔」)。Windows 沒有 POSIX 的權限位元
(st_mode 只反映唯讀屬性),不檢查;部署端是 Linux。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


class PermissionProblem(ValueError):
    """權限太寬:訊息寫出是哪一層、目前的權限與要執行的 chmod。"""


def permission_problem(mode: int, target, *, secret: bool, is_dir: bool,
                       of: str | None = None) -> str | None:
    """權限位元 → 太寬時的說明;沒問題回 None。跟讀檔分開,所以不在 POSIX 上也測得到每一種值。

    `of`:檢查的是上層的目錄時,寫出是哪個檔案的上層。"""
    if is_dir:
        if mode & stat.S_ISVTX:
            return None
        mask, need, what, how = 0o022, "755", "目錄", "改得到(可以把底下的檔案或目錄整個換掉)"
    elif secret:
        mask, need, what, how = 0o077, "600", "檔案(含密碼)", "讀得到或改得到"
    else:
        mask, need, what, how = 0o022, "644", "設定檔", "改得到"
    if not mode & mask:
        return None
    where = f"(設定檔 {of} 的上層)" if of else ""
    return (f"{what} {target}{where} 的權限太寬({stat.S_IMODE(mode) & 0o777:o}):"
            f"群組或其他帳號{how},不往下跑。請執行:chmod {need} {target}")


def _dirs_upward(start: Path) -> list:
    out = []
    d = start
    while True:
        out.append(d)
        if d.parent == d:
            return out
        d = d.parent


def dirs_to_check(path: Path) -> list:
    """要檢查的目錄:實際的檔案所在的目錄一路往上,加上原本的路徑(可能含連結)所在的目錄一路往上。"""
    seen = []
    for start in (path.resolve().parent, Path(os.path.abspath(path)).parent):
        for d in _dirs_upward(start):
            if d not in seen:
                seen.append(d)
    return seen


def check_file(path, *, secret: bool) -> None:
    """權限太寬就丟 PermissionProblem。"""
    path = Path(path)
    if os.name != "posix" or not path.exists():
        return
    real = path.resolve()
    problem = permission_problem(os.stat(real).st_mode, real if real != path else path,
                                 secret=secret, is_dir=False)
    if problem:
        raise PermissionProblem(problem)
    shown = str(path)
    for d in dirs_to_check(path):
        problem = permission_problem(os.stat(d).st_mode, d, secret=secret, is_dir=True,
                                     of=None if d in (real.parent, Path(os.path.abspath(path)).parent)
                                     else shown)
        if problem:
            raise PermissionProblem(problem)


def check_path(path, *, secret: bool = False) -> None:
    """環境變數指到的檔案或目錄(可能還不存在):存在的那一層起,本身與一路往上的每一層都檢查。
    還不存在時(例如等一下才建的輸出目錄)從最近一個存在的上層開始。"""
    path = Path(path)
    if os.name != "posix":
        return
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if probe.is_dir():
        for d in [probe] + [x for x in dirs_to_check(probe / "_") if x != probe]:
            problem = permission_problem(os.stat(d).st_mode, d, secret=secret, is_dir=True,
                                         of=None if d == probe else str(path))
            if problem:
                raise PermissionProblem(problem)
    else:
        check_file(probe, secret=secret)


def main(argv=None) -> int:
    """`file_perms.py [--secret] <檔案>`:沒問題結束碼 0;太寬時把說明寫到 stderr、結束碼 1。"""
    args = list(sys.argv[1:] if argv is None else argv)
    secret = "--secret" in args
    files = [a for a in args if a != "--secret"]
    if len(files) != 1:
        print("用法:file_perms.py [--secret] <檔案>", file=sys.stderr)
        return 2
    try:
        check_file(files[0], secret=secret)
    except PermissionProblem as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
