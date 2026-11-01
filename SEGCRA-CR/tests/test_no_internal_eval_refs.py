"""文件、程式與測試不提內部的評測。

repo 之後會交給部署端的維運與開發人員;內部評測(題目、數字、存下來的報告)不會一起交出去,
寫在 repo 裡的話,看的人找不到、也查不到依據。講「為什麼這樣做」時寫現象本身與在 repo 裡
重現得出來的量法;要留依據的放在 PR 說明。

這支測試掃整個專案(不含虛擬環境、快取與本機產出的報告),之後新加的也擋得住。
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 不寫成字面:寫了這支測試自己就會中
WORDS = ("黑" + "盒",)
SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", "review_output", "_output"}
SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".toml", ".cfg", ".ini", ".sh", ".sql", ".env"}


def find_mentions(root: pathlib.Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if SKIP_DIRS.intersection(path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if any(w in line for w in WORDS):
                hits.append(f"{path.relative_to(root).as_posix()}:{n}")
    return hits


def test_專案裡不提內部評測():
    hits = find_mentions(ROOT)
    assert hits == [], f"這些地方提到內部評測,改成說明現象本身或在 repo 裡重現得出來的量法:{hits}"


def test_掃得到_也略過不該掃的目錄(tmp_path):
    """只驗「沒有命中」不夠:掃描整個沒跑的話照樣綠。"""
    word = WORDS[0]
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text(f"第一行\n來源是{word}評測\n", encoding="utf-8")
    (tmp_path / "x.py").write_text(f"# {word}\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "y.py").write_text(f"# {word}\n", encoding="utf-8")
    (tmp_path / "eval" / "_output").mkdir(parents=True)
    (tmp_path / "eval" / "_output" / "r.json").write_text(f'"{word}"', encoding="utf-8")
    (tmp_path / "img.png").write_bytes(word.encode("utf-8"))
    assert find_mentions(tmp_path) == ["docs/a.md:2", "x.py:1"]
