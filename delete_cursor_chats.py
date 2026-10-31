#!/usr/bin/env python3
"""删除本机 Cursor 对话记录，以及这些对话对应的文件夹。

只处理当前用户电脑上的记录，不访问网络。
不加 --yes 时只列出将要删除的对话，不会删除。
确认要删时，在同一条命令末尾加上 --yes。

先看有哪些对话。结果里每一条都有标题和编号:
  python3 delete_cursor_chats.py list

按完整标题删除。标题必须和对话名称一个字不差:
  python3 delete_cursor_chats.py delete --title "示例对话"

按标题开头删除。--starts 的意思是「标题开头」。
后面写标题最前面的几个字，凡是以这几个字开头的对话都会被选中。
下面这条会删除所有标题以 cursor- 开头的对话:
  python3 delete_cursor_chats.py delete --starts "cursor-" --yes

按标题里出现过的文字删除。--contains 的意思是「标题中包含」:
  python3 delete_cursor_chats.py delete --contains "简历" --yes

删除某个项目文件夹里的全部对话。--workspace 后面写文件夹路径:
  python3 delete_cursor_chats.py delete --workspace ~/Documents/案子 --yes

按对话编号删除。编号写在 list 结果的「编号:」后面:
  python3 delete_cursor_chats.py delete --id 对话编号 --yes
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

CONVERSATION_ID = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|bc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|empty-state-draft)$",
    re.IGNORECASE,
)


HOME = Path.home()
STATE_DB = HOME / ".config/Cursor/User/globalStorage/state.vscdb"
SEARCH_DB = HOME / ".config/Cursor/User/globalStorage/conversation-search.db"
PROJECTS_DIR = HOME / ".cursor/projects"
STORE_DIR = HOME / ".local/state/cursor/agent-stores/cursor_agent_stores"


@dataclass
class Workspace:
    workspace_id: str
    display: str = ""
    paths: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)

    def label(self) -> str:
        if self.display:
            return self.display
        if self.paths:
            return self.paths[0]
        if self.repos:
            return self.repos[0]
        return self.workspace_id or "（未知文件夹）"


@dataclass
class Conversation:
    cid: str
    title: str
    workspace_id: str = ""
    updated_at: int = 0
    archived: bool = False
    subagent: bool = False
    draft: bool = False
    source: str = "local"

    def shown_title(self) -> str:
        return self.title or "（无标题）"


def connect(path: Path, readonly: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"找不到数据库：{path}")
    if readonly:
        uri = path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=20)
    else:
        con = sqlite3.connect(path, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    return con


def load_json(raw: str | None) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def file_uri_path(value: str) -> str:
    if not value.startswith("file:"):
        return ""
    parsed = urlparse(value)
    return unquote(parsed.path)


def load_workspaces(cur: sqlite3.Cursor) -> dict[str, Workspace]:
    row = cur.execute(
        "select value from ItemTable where key = ?",
        ("workspaceMetadata.entries",),
    ).fetchone()
    data = load_json(row[0] if row else None)
    entries = data.get("entries", []) if isinstance(data, dict) else []
    found: dict[str, Workspace] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        workspace_id = str(entry.get("workspaceId") or "")
        paths: list[str] = []
        folder_uri = entry.get("folderUri")
        if isinstance(folder_uri, str):
            folder_path = file_uri_path(folder_uri)
            if folder_path:
                paths.append(folder_path)
        for item in entry.get("paths") or []:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or {}
            fs_path = uri.get("fsPath") if isinstance(uri, dict) else ""
            if fs_path:
                paths.append(fs_path)
        repos = []
        for repo in entry.get("trackedGitRepos") or []:
            if isinstance(repo, dict) and repo.get("repoUrl"):
                repos.append(str(repo["repoUrl"]))
        found[workspace_id] = Workspace(
            workspace_id=workspace_id,
            display=str(entry.get("displayPath") or ""),
            paths=list(dict.fromkeys(paths)),
            repos=repos,
        )
    return found


def load_conversations(cur: sqlite3.Cursor) -> dict[str, Conversation]:
    found: dict[str, Conversation] = {}
    for cid, workspace_id, updated_at, archived, subagent, raw in cur.execute(
        """
        select composerId, workspaceId, lastUpdatedAt, isArchived, isSubagent, value
        from composerHeaders
        """
    ):
        obj = load_json(raw)
        obj = obj if isinstance(obj, dict) else {}
        title = str(obj.get("name") or "")
        draft = bool(obj.get("isDraft") or obj.get("isEphemeral")) or cid == "empty-state-draft"
        found[cid] = Conversation(
            cid=cid,
            title=title,
            workspace_id=workspace_id or "",
            updated_at=int(updated_at or obj.get("lastUpdatedAt") or 0),
            archived=bool(archived),
            subagent=bool(subagent),
            draft=draft,
        )
    if SEARCH_DB.is_file():
        search = connect(SEARCH_DB, readonly=True)
        try:
            for cid, title, source, archived in search.execute(
                "select id, title, source, is_archived from conversations"
            ):
                current = found.get(cid)
                if current is None:
                    found[cid] = Conversation(
                        cid=cid,
                        title=title or "",
                        archived=bool(archived),
                        source=source or "search",
                    )
                elif not current.title and title:
                    current.title = title
        finally:
            search.close()
    return found


def conversation_dirs(cid: str) -> list[Path]:
    dirs: list[Path] = []
    if PROJECTS_DIR.is_dir():
        dirs.extend(path for path in PROJECTS_DIR.glob(f"*/agent-transcripts/{cid}") if path.is_dir())
    for path in (STORE_DIR / cid, STORE_DIR / ".control" / cid):
        if path.exists():
            dirs.append(path)
    return dirs


def format_time(stamp: int) -> str:
    if not stamp:
        return "—"
    seconds = stamp / 1000 if stamp > 10_000_000_000 else stamp
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def visible(conversation: Conversation, show_all: bool) -> bool:
    if show_all:
        return True
    return not conversation.draft and not conversation.subagent


def print_conversation(conversation: Conversation, workspace: Workspace | None) -> None:
    marks = []
    if conversation.archived:
        marks.append("已归档")
    if conversation.draft:
        marks.append("草稿")
    if conversation.subagent:
        marks.append("附属对话")
    if conversation.source != "local":
        marks.append(conversation.source)
    suffix = f"（{'，'.join(marks)}）" if marks else ""
    folder = workspace.label() if workspace else "（未关联文件夹）"
    print(f"{conversation.shown_title()}{suffix}")
    print(f"  编号: {conversation.cid}")
    print(f"  文件夹: {folder}")
    print(f"  更新: {format_time(conversation.updated_at)}")
    record_dirs = conversation_dirs(conversation.cid)
    if record_dirs:
        print("  记录目录:")
        for path in record_dirs:
            print(f"    {path}")
    else:
        print("  记录目录: 无")


def normalize_token(value: str) -> str:
    return value.strip().rstrip("/").casefold()


def workspace_matches(workspace: Workspace, query: str) -> bool:
    token = normalize_token(query)
    if not token:
        return False
    expanded = Path(query).expanduser()
    try:
        target = expanded.resolve()
    except OSError:
        target = expanded
    candidates = [workspace.display, *workspace.paths, *workspace.repos]
    for candidate in candidates:
        if not candidate:
            continue
        if normalize_token(candidate) == token:
            return True
        if normalize_token(Path(candidate).name) == token:
            return True
        repo_name = candidate.rstrip("/").split("/")[-1]
        if repo_name and normalize_token(repo_name) == token:
            return True
        candidate_path = Path(candidate).expanduser()
        try:
            if candidate_path.exists() and candidate_path.resolve() == target:
                return True
        except OSError:
            continue
    return False


def select_conversations(
    conversations: dict[str, Conversation],
    workspaces: dict[str, Workspace],
    args: argparse.Namespace,
) -> list[Conversation]:
    chosen: list[Conversation] = []
    if args.id:
        conversation = conversations.get(args.id)
        if conversation is not None:
            chosen.append(conversation)
        elif conversation_dirs(args.id):
            chosen.append(Conversation(cid=args.id, title=""))
        return chosen
    if args.title or args.starts or args.contains:
        for conversation in conversations.values():
            title = conversation.title or ""
            if args.title and title == args.title:
                chosen.append(conversation)
            elif args.starts and title.startswith(args.starts):
                chosen.append(conversation)
            elif args.contains and args.contains in title:
                chosen.append(conversation)
        return chosen
    if args.workspace:
        matched_ids = {
            workspace.workspace_id
            for workspace in workspaces.values()
            if workspace_matches(workspace, args.workspace)
        }
        for conversation in conversations.values():
            if conversation.workspace_id in matched_ids:
                chosen.append(conversation)
    return chosen


def remove_cloud_entries(cur: sqlite3.Cursor, cid: str) -> None:
    rows = cur.execute(
        """
        select key, value from ItemTable
        where key like 'cloudAgentRepository.agents.%'
           or key in ('glass.cloudAgentProjects.v1', 'glass.cloudAgentProjectMembership.v1')
        """
    ).fetchall()
    for key, raw in rows:
        data = load_json(raw)
        if isinstance(data, list):
            kept = [
                item
                for item in data
                if not (
                    isinstance(item, dict)
                    and (item.get("bcId") == cid or item.get("id") == cid or item.get("composerId") == cid)
                )
            ]
            if len(kept) != len(data):
                cur.execute(
                    "update ItemTable set value = ? where key = ?",
                    (json.dumps(kept, ensure_ascii=False, separators=(",", ":")), key),
                )
        elif isinstance(data, dict) and cid in data:
            data.pop(cid, None)
            cur.execute(
                "update ItemTable set value = ? where key = ?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), key),
            )


def require_conversation_id(cid: str) -> str:
    if not CONVERSATION_ID.fullmatch(cid):
        raise SystemExit(f"对话编号格式不正确：{cid}")
    return cid


def delete_records(cid: str) -> tuple[int, list[Path]]:
    cid = require_conversation_id(cid)
    state = connect(STATE_DB, readonly=False)
    search = connect(SEARCH_DB, readonly=False) if SEARCH_DB.is_file() else None
    removed_dirs: list[Path] = []
    try:
        state.execute("BEGIN IMMEDIATE")
        cur = state.cursor()
        remove_cloud_entries(cur, cid)
        cur.execute("delete from composerHeaders where composerId = ?", (cid,))
        cur.execute("delete from cursorDiskKV where key like ?", (f"%{cid}%",))
        cur.execute("delete from ItemTable where key like ?", (f"%{cid}%",))
        state.commit()
        if search is not None:
            search.execute("BEGIN IMMEDIATE")
            row = search.execute(
                "select fts_rowid from conversations where id = ?",
                (cid,),
            ).fetchone()
            if row is not None:
                search.execute("delete from conversation_fts where rowid = ?", (row[0],))
            search.execute("delete from conversations where id = ?", (cid,))
            search.execute("delete from conversation_search_candidates where id = ?", (cid,))
            search.commit()
    except sqlite3.OperationalError as exc:
        state.rollback()
        if search is not None:
            search.rollback()
        raise SystemExit(f"数据库正被 Cursor 占用，这次没有删除。请稍后再试。\n{exc}") from exc
    finally:
        state.close()
        if search is not None:
            search.close()

    for path in conversation_dirs(cid):
        shutil.rmtree(path)
        removed_dirs.append(path)
    return 1, removed_dirs


def list_command(show_all: bool) -> None:
    state = connect(STATE_DB, readonly=True)
    try:
        cur = state.cursor()
        workspaces = load_workspaces(cur)
        conversations = [
            item
            for item in load_conversations(cur).values()
            if visible(item, show_all)
        ]
    finally:
        state.close()
    conversations.sort(key=lambda item: item.updated_at, reverse=True)
    if not conversations:
        print("没有对话记录。")
        return
    print(f"共 {len(conversations)} 条对话。")
    print()
    for conversation in conversations:
        print_conversation(conversation, workspaces.get(conversation.workspace_id))
        print()


def delete_command(args: argparse.Namespace) -> None:
    for label, value in (
        ("标题", args.title),
        ("标题前缀", args.starts),
        ("标题关键字", args.contains),
        ("文件夹", args.workspace),
    ):
        if value is not None and not value.strip():
            raise SystemExit(f"{label}不能为空。")
    if args.id:
        require_conversation_id(args.id)
    state = connect(STATE_DB, readonly=True)
    try:
        cur = state.cursor()
        workspaces = load_workspaces(cur)
        conversations = load_conversations(cur)
    finally:
        state.close()
    chosen = select_conversations(conversations, workspaces, args)
    if not chosen:
        raise SystemExit("没有匹配的对话，未删除任何内容。")
    print(f"匹配到 {len(chosen)} 条对话：")
    print()
    for conversation in chosen:
        print_conversation(conversation, workspaces.get(conversation.workspace_id))
        print()
    if not args.yes:
        print("以上只是预览。确认删除请在同一条命令末尾加上 --yes。")
        return
    deleted = 0
    folders: list[Path] = []
    for conversation in chosen:
        count, removed = delete_records(conversation.cid)
        deleted += count
        folders.extend(removed)
    print(f"已删除 {deleted} 条对话，以及 {len(folders)} 个记录文件夹。")
    print("如果 Cursor 里还能看到这些对话，重新加载窗口后就会消失。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="删除本机 Cursor 对话记录及其文件夹。不加 --yes 时只预览。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="列出对话")
    list_parser.add_argument("--all", action="store_true", help="连草稿和附属对话一起列出")

    delete_parser = commands.add_parser("delete", help="删除对话及其记录文件夹")
    target = delete_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--title", help="完整标题。必须和对话名称一个字不差")
    target.add_argument("--starts", help="标题开头的几个字。标题以这段文字开头的对话都会被选中")
    target.add_argument("--contains", help="标题中包含的一段文字")
    target.add_argument("--id", help="对话编号。先用 list 查看，写在「编号:」后面")
    target.add_argument("--workspace", help="项目文件夹路径。删除这个文件夹里的全部对话")
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="确认删除。不加这个参数就只列出将要删除的对话",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "list":
        list_command(args.all)
    elif args.command == "delete":
        delete_command(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
