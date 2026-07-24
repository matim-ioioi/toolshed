#!/usr/bin/env python3
"""
Удаление всех данных Claude Code о выбранных проектах.

Сканирует ~/.claude и ~/.claude.json, показывает список известных проектов
с объёмом накопленных данных, даёт выбрать несколько и вычищает всё, что
Claude Code о них хранит:
  - транскрипты сессий      ~/.claude/projects/<slug>/
  - история промптов        ~/.claude/history.jsonl (записи с project == путь)
  - запись о проекте        ~/.claude.json -> projects[путь]
  - то же в бэкапах         ~/.claude/backups/.claude.json.backup.*
  - снапшоты правок         ~/.claude/file-history/<sessionId>/
  - окружение сессий        ~/.claude/session-env/<sessionId>/
  - todo/task/job/debug      ~/.claude/{todos,tasks,jobs,debug}/<sessionId>*
  - служебные файлы сессий  ~/.claude/{sessions,shell-snapshots,paste-cache}/*
  - временные файлы         /private/tmp/claude-<uid>/<slug>/

ФАЙЛЫ ВНУТРИ САМОГО ПРОЕКТА НЕ ТРОГАЮТСЯ. Ни рабочие файлы, ни .claude/
внутри проекта (settings.json, agents, commands, worktrees). Это гарантируется
не только логикой обхода, но и защитой в самой функции удаления: любой путь,
лежащий внутри каталога проекта или вне белого списка каталогов с данными
Claude Code, отбрасывается.

Использование:
  ./wipe-claude-project-memory.py              # интерактивный выбор
  ./wipe-claude-project-memory.py --dry-run    # показать, но не удалять
  ./wipe-claude-project-memory.py --all --yes  # снести всё без вопросов
  ./wipe-claude-project-memory.py --project /path/to/proj [--project ...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CLAUDE_JSON = HOME / ".claude.json"
PROJECTS_DIR = CLAUDE_DIR / "projects"
HISTORY = CLAUDE_DIR / "history.jsonl"
BACKUPS = CLAUDE_DIR / "backups"
SCRATCH_ROOT = Path(f"/private/tmp/claude-{os.getuid()}")

# Каталоги с per-session артефактами: имя элемента начинается с sessionId.
SESSION_ID_DIRS = ["file-history", "session-env", "todos", "tasks", "jobs", "debug"]

# Каталоги, где привязку к проекту можно установить только по содержимому файла.
# Здесь намеренно нет todos/tasks/jobs: в них лежат и общие файлы (например
# jobs/pins.json, единый для всех проектов), которые нельзя удалять целиком —
# они разбираются строго по sessionId выше.
CONTENT_SCAN_DIRS = ["shell-snapshots", "paste-cache"]

# Файлы больше этого размера не вычитываются при поиске упоминаний.
MAX_SCAN_BYTES = 20 * 1024 * 1024

# Корни, внутри которых вообще разрешено что-либо удалять.
ALLOWED_ROOTS = [CLAUDE_DIR, SCRATCH_ROOT]

# Файлы, которые никогда не удаляются целиком (правятся точечно или только
# упоминаются в отчёте): пользовательские настройки и глобальная память.
PROTECTED_FILES = {
    CLAUDE_JSON,
    CLAUDE_DIR / "CLAUDE.md",
    CLAUDE_DIR / "settings.json",
    CLAUDE_DIR / "settings.local.json",
    CLAUDE_DIR / "keybindings.json",
    CLAUDE_DIR / "remote-settings.json",
    HISTORY,
}


def slugify(path: str) -> str:
    """Путь -> имя каталога в ~/.claude/projects (так его строит Claude Code)."""
    return path.replace("/", "-").replace(".", "-").replace("_", "-")


def human(nbytes: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if nbytes < 1024 or unit == "ГБ":
            return f"{nbytes:.0f} {unit}" if unit == "Б" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} ГБ"


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += (Path(root) / f).lstat().st_size
            except OSError:
                pass
    return total


def load_json(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_json_atomic(path: Path, data) -> None:
    """Перезапись с сохранением прав доступа и без риска порчи при сбое."""
    mode = stat.S_IMODE(path.stat().st_mode)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def file_mentions(path: Path, needle: str) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size > MAX_SCAN_BYTES:
            return False
        with path.open("rb") as fh:
            return needle.encode() in fh.read()
    except OSError:
        return False


# --------------------------------------------------------------------------
# Модель проекта
# --------------------------------------------------------------------------


@dataclass
class Project:
    path: str | None          # None => каталог-сирота, исходный путь неизвестен
    slug: str
    in_config: bool = False
    transcripts: Path | None = None
    session_ids: set[str] = field(default_factory=set)
    history_lines: int = 0
    size: int = 0
    worktree_slugs: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.path if self.path else f"[осиротевшие данные] {self.slug}"

    @property
    def exists_on_disk(self) -> bool:
        return bool(self.path) and Path(self.path).is_dir()


def discover() -> list[Project]:
    by_slug: dict[str, Project] = {}

    # 1. Проекты, записанные в ~/.claude.json — источник канонических путей.
    config = load_json(CLAUDE_JSON) or {}
    for path in (config.get("projects") or {}):
        slug = slugify(path)
        by_slug[slug] = Project(path=path, slug=slug, in_config=True)

    # 2. Каталоги транскриптов. Те, что не сопоставились с путём — сироты.
    if PROJECTS_DIR.is_dir():
        for entry in PROJECTS_DIR.iterdir():
            if not entry.is_dir():
                continue
            proj = by_slug.get(entry.name)
            if proj is None:
                proj = Project(path=None, slug=entry.name)
                by_slug[entry.name] = proj
            proj.transcripts = entry
            proj.size += dir_size(entry)
            proj.session_ids.update(p.stem for p in entry.glob("*.jsonl"))

    # 3. Записи в глобальной истории промптов.
    if HISTORY.is_file():
        by_path = {p.path: p for p in by_slug.values() if p.path}
        with HISTORY.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                proj = by_path.get(rec.get("project"))
                if proj is None:
                    continue
                proj.history_lines += 1
                if rec.get("sessionId"):
                    proj.session_ids.add(rec["sessionId"])

    # 4. Привязка worktree-производных к родительскому проекту.
    #    Каталог данных вида "<slug>--claude-worktrees-<имя>" принадлежит проекту
    #    <slug>; сами worktree лежат внутри проекта и не трогаются.
    for slug, proj in list(by_slug.items()):
        marker = "--claude-worktrees-"
        if marker not in slug:
            continue
        parent_slug = slug.split(marker, 1)[0]
        parent = by_slug.get(parent_slug)
        if parent is not None and parent is not proj:
            parent.worktree_slugs.append(slug)

    child_slugs = {s for p in by_slug.values() for s in p.worktree_slugs}
    result = [p for slug, p in by_slug.items() if slug not in child_slugs]
    result.sort(key=lambda p: (-p.size, p.label))
    return result


# --------------------------------------------------------------------------
# Сбор плана удаления
# --------------------------------------------------------------------------


def is_safe_target(target: Path, forbidden: list[Path]) -> tuple[bool, str]:
    """Проверка перед удалением. Возвращает (можно ли, причина отказа)."""
    try:
        resolved = target.resolve()
    except OSError:
        return False, "путь не разрешается"

    for project_dir in forbidden:
        if resolved == project_dir or project_dir in resolved.parents:
            return False, f"лежит внутри проекта {project_dir}"

    if resolved in PROTECTED_FILES:
        return False, "защищённый файл настроек"

    for root in ALLOWED_ROOTS:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if resolved != root_resolved and root_resolved in resolved.parents:
            return True, ""

    return False, "вне каталогов данных Claude Code"


def collect_targets(proj: Project) -> list[Path]:
    """Пути к удалению для одного проекта (без правки JSON-файлов)."""
    targets: list[Path] = []
    slugs = [proj.slug] + proj.worktree_slugs

    for slug in slugs:
        transcripts = PROJECTS_DIR / slug
        if transcripts.is_dir():
            targets.append(transcripts)
            # sessionId дочерних worktree-каталогов тоже попадают в зачистку
            proj.session_ids.update(p.stem for p in transcripts.glob("*.jsonl"))
        scratch = SCRATCH_ROOT / slug
        if scratch.exists():
            targets.append(scratch)

    for sid in sorted(proj.session_ids):
        for sub in SESSION_ID_DIRS:
            base = CLAUDE_DIR / sub
            if not base.is_dir():
                continue
            for entry in base.glob(f"{sid}*"):
                targets.append(entry)

    # sessions/<pid>.json — служебные записи о запущенных сессиях. Привязка
    # точная: сверяем поля cwd/sessionId, а не ищем путь подстрокой.
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.is_dir():
        for entry in sessions_dir.glob("*.json"):
            data = load_json(entry)
            if not isinstance(data, dict):
                continue
            if data.get("cwd") == proj.path or data.get("sessionId") in proj.session_ids:
                targets.append(entry)

    if proj.path:
        for sub in CONTENT_SCAN_DIRS:
            base = CLAUDE_DIR / sub
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                if entry in targets:
                    continue
                if entry.is_file() and file_mentions(entry, proj.path):
                    targets.append(entry)

    # dedup с сохранением порядка
    seen: set[Path] = set()
    unique: list[Path] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def json_targets(projects: list[Project]) -> list[Path]:
    """Файлы, из которых вырезается ключ projects[путь], а не удаляются целиком."""
    if not any(p.path for p in projects):
        return []
    files = [CLAUDE_JSON] if CLAUDE_JSON.is_file() else []
    if BACKUPS.is_dir():
        files += sorted(BACKUPS.glob(".claude.json.backup.*"))
    return files


# --------------------------------------------------------------------------
# Выполнение
# --------------------------------------------------------------------------


def purge_paths(targets: list[Path], forbidden: list[Path], dry: bool) -> tuple[int, int]:
    removed = freed = 0
    for target in targets:
        ok, reason = is_safe_target(target, forbidden)
        if not ok:
            print(f"  [!] пропущен {target} — {reason}")
            continue
        size = dir_size(target) if target.is_dir() else target.lstat().st_size
        rel = target.relative_to(HOME) if HOME in target.parents else target
        if dry:
            print(f"  [-] {rel}  ({human(size)})")
        else:
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as exc:
                print(f"  [!] не удалось удалить {target}: {exc}")
                continue
            print(f"  [x] {rel}  ({human(size)})")
        removed += 1
        freed += size
    return removed, freed


def purge_config_keys(files: list[Path], paths: list[str], dry: bool) -> None:
    for path in files:
        data = load_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
            continue
        hits = [p for p in paths if p in data["projects"]]
        if not hits:
            continue
        if dry:
            print(f"  [-] {path.name}: убрать записей — {len(hits)}")
            continue
        for p in hits:
            del data["projects"][p]
        try:
            write_json_atomic(path, data)
            print(f"  [x] {path.name}: убрано записей — {len(hits)}")
        except OSError as exc:
            print(f"  [!] {path.name}: {exc}")


def purge_history(paths: list[str], dry: bool) -> None:
    if not HISTORY.is_file():
        return
    targets = set(paths)
    kept, dropped = [], 0
    with HISTORY.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                if json.loads(line).get("project") in targets:
                    dropped += 1
                    continue
            except Exception:
                pass
            kept.append(line)
    if not dropped:
        return
    if dry:
        print(f"  [-] history.jsonl: удалить записей — {dropped}")
        return
    mode = stat.S_IMODE(HISTORY.stat().st_mode)
    tmp = HISTORY.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.writelines(kept)
    os.chmod(tmp, mode)
    os.replace(tmp, HISTORY)
    print(f"  [x] history.jsonl: удалено {dropped}, осталось {len(kept)}")


def leftovers(paths: list[str], deep: bool = False) -> list[Path]:
    """Остаточные упоминания — только отчёт, ничего не удаляется.

    По умолчанию каталог projects/ пропускается: транскрипты ЧУЖИХ проектов
    могут упоминать наш путь просто как текст в переписке, и удалять их нельзя.
    Полный обход включается флагом --verify-deep (медленно: ~2 ГБ).
    """
    found: list[Path] = []
    skip_dirs = {"node_modules", ".git"}
    for root, dirs, files in os.walk(CLAUDE_DIR, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if not deep and Path(root) == PROJECTS_DIR:
            dirs[:] = []
            continue
        for name in files:
            if name == ".DS_Store":
                continue
            fpath = Path(root) / name
            if any(file_mentions(fpath, p) for p in paths):
                found.append(fpath)
    return found


def report_leftovers(found: list[Path], deep: bool) -> None:
    if not found:
        print("Проверка: упоминаний выбранных проектов в ~/.claude не осталось"
              + ("." if deep else " (кроме транскриптов других проектов — не проверялись)."))
        return
    groups: dict[str, list[Path]] = {}
    for f in found:
        try:
            key = f.relative_to(CLAUDE_DIR).parts[0]
        except ValueError:
            key = str(f.parent)
        groups.setdefault(key, []).append(f)
    print("\nОстаточные упоминания (файлы НЕ удалялись — это чужие данные и настройки):")
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {key}: {len(items)}")
        for f in items[:3]:
            print(f"      {f.relative_to(CLAUDE_DIR)}")
        if len(items) > 3:
            print(f"      … ещё {len(items) - 3}")
    if not deep:
        print("  (транскрипты других проектов не проверялись — нужен --verify-deep)")


# --------------------------------------------------------------------------
# Интерфейс
# --------------------------------------------------------------------------


def parse_selection(raw: str, count: int) -> list[int] | None:
    raw = raw.strip().lower()
    if raw in {"q", "quit", "выход", ""}:
        return None
    if raw in {"all", "все", "*"}:
        return list(range(count))
    picked: set[int] = set()
    for chunk in raw.replace(",", " ").split():
        if "-" in chunk[1:]:
            start, _, end = chunk.partition("-")
            try:
                lo, hi = int(start), int(end)
            except ValueError:
                print(f"  не понял диапазон: {chunk}")
                return []
            picked.update(range(lo - 1, hi))
        else:
            try:
                picked.add(int(chunk) - 1)
            except ValueError:
                print(f"  не понял номер: {chunk}")
                return []
    valid = sorted(i for i in picked if 0 <= i < count)
    if not valid:
        print("  ничего не выбрано")
        return []
    return valid


def print_table(projects: list[Project]) -> None:
    width = max((len(p.label) for p in projects), default=20)
    width = min(width, 70)
    print(f"  {'#':>3}  {'проект':<{width}}  {'сессий':>6}  {'история':>7}  {'объём':>8}")
    print(f"  {'-' * 3}  {'-' * width}  {'-' * 6}  {'-' * 7}  {'-' * 8}")
    for i, p in enumerate(projects, 1):
        label = p.label if len(p.label) <= width else "…" + p.label[-(width - 1):]
        notes = []
        if not p.in_config:
            notes.append("нет в конфиге")
        if p.path and not p.exists_on_disk:
            notes.append("каталог удалён")
        if p.worktree_slugs:
            notes.append(f"+{len(p.worktree_slugs)} worktree")
        print(
            f"  {i:>3}  {label:<{width}}  {len(p.session_ids):>6}  "
            f"{p.history_lines:>7}  {human(p.size):>8}"
            + (f"   ({', '.join(notes)})" if notes else "")
        )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # процесс есть, просто чужой
    except (OverflowError, TypeError):
        return False
    return True


def is_claude_process(pid: int) -> bool:
    """Отсеивает случай, когда PID уже переиспользован другой программой."""
    try:
        out = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return True          # проверить нечем — считаем, что это claude
    name = out.stdout.strip()
    return not name or os.path.basename(name) == "claude"


def live_sessions() -> list[dict]:
    """Работающие сейчас сессии Claude Code с их рабочими каталогами.

    Источник — ~/.claude/sessions/<pid>.json (в нём есть pid, cwd и sessionId),
    а не pgrep: на macOS pgrep по умолчанию не показывает процессы-предки
    вызывающего, из-за чего сессия, из которой запущен скрипт, не находилась.
    """
    result: list[dict] = []
    sessions_dir = CLAUDE_DIR / "sessions"
    if not sessions_dir.is_dir():
        return result
    for entry in sorted(sessions_dir.glob("*.json")):
        data = load_json(entry)
        if not isinstance(data, dict):
            continue
        pid, cwd = data.get("pid"), data.get("cwd")
        if not isinstance(pid, int) or not cwd:
            continue
        if pid_alive(pid) and is_claude_process(pid):
            result.append({"pid": pid, "cwd": cwd, "sessionId": data.get("sessionId")})
    return result


def warn_about_live_sessions(selected: list[Project]) -> bool:
    """Предупреждает только о сессиях, работающих в ВЫБРАННЫХ проектах.

    Сессии в других проектах не пересекаются с удаляемыми данными и молча
    игнорируются.
    """
    affected: list[tuple[dict, str]] = []
    for sess in live_sessions():
        try:
            cwd = Path(sess["cwd"]).resolve()
        except OSError:
            continue
        for proj in selected:
            if not proj.path:
                continue
            try:
                proj_dir = Path(proj.path).resolve()
            except OSError:
                continue
            if cwd == proj_dir or proj_dir in cwd.parents:
                affected.append((sess, proj.path))
                break
    if not affected:
        return False
    print("ВНИМАНИЕ: в выбранных проектах прямо сейчас работает Claude Code:")
    for sess, path in affected:
        print(f"  • PID {sess['pid']} — {path}")
    print("Эти сессии допишут свои данные при выходе, и часть удалённого вернётся.")
    print("Для полной зачистки выйдите из них и запустите скрипт снова.\n")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Удалить данные Claude Code о выбранных проектах "
        "(файлы внутри самих проектов не трогаются)."
    )
    ap.add_argument("--dry-run", action="store_true", help="показать план, ничего не удалять")
    ap.add_argument("--yes", "-y", action="store_true", help="не спрашивать подтверждение")
    ap.add_argument("--all", action="store_true", help="выбрать все найденные проекты")
    ap.add_argument(
        "--project", action="append", default=[], metavar="PATH",
        help="путь проекта (можно повторять); отключает интерактивный выбор",
    )
    ap.add_argument("--verify-deep", action="store_true",
                    help="искать остаточные упоминания и в транскриптах других проектов (медленно)")
    ap.add_argument("--no-verify", action="store_true",
                    help="пропустить финальную проверку остаточных упоминаний")
    args = ap.parse_args()

    if not CLAUDE_DIR.is_dir():
        print(f"Каталог {CLAUDE_DIR} не найден — данных Claude Code нет.")
        return 1

    print("Сканирую данные Claude Code…\n")
    projects = discover()
    if not projects:
        print("Проектов не найдено.")
        return 0

    if args.project:
        wanted = {str(Path(p).expanduser()).rstrip("/") for p in args.project}
        selected = [p for p in projects if p.path in wanted]
        missing = wanted - {p.path for p in selected}
        for m in sorted(missing):
            print(f"  [!] нет данных о проекте: {m}")
        if not selected:
            return 1
    elif args.all:
        selected = projects
    else:
        print_table(projects)
        print("\nНомера через пробел или запятую, диапазоны (1-3), 'all' — все, 'q' — выход.")
        while True:
            picked = parse_selection(input("Что чистим? > "), len(projects))
            if picked is None:
                print("Отменено.")
                return 0
            if picked:
                selected = [projects[i] for i in picked]
                break

    print("\nВыбрано:")
    for p in selected:
        print(f"  • {p.label}")

    forbidden = [Path(p.path).resolve() for p in selected if p.exists_on_disk]
    if forbidden:
        print("\nНе будет затронуто (файлы внутри проектов, включая .claude/):")
        for f in forbidden:
            print(f"  • {f}")

    running = warn_about_live_sessions(selected) if not args.dry_run else False

    print("\n" + ("План удаления:" if args.dry_run else "Удаляю:"))
    all_targets: list[Path] = []
    for proj in selected:
        all_targets += collect_targets(proj)

    paths = [p.path for p in selected if p.path]

    if args.dry_run:
        purge_paths(all_targets, forbidden, dry=True)
        purge_config_keys(json_targets(selected), paths, dry=True)
        purge_history(paths, dry=True)
        print("\nЭто был --dry-run, ничего не удалено.")
        return 0

    if not args.yes:
        total = sum(dir_size(t) if t.is_dir() else t.lstat().st_size for t in all_targets)
        print(f"  к удалению: {len(all_targets)} объектов, {human(total)}")
        if running:
            print("  (Claude Code запущен — зачистка может оказаться неполной)")
        answer = input("\nУдалить безвозвратно? Введите 'yes': ").strip().lower()
        if answer not in {"yes", "да"}:
            print("Отменено.")
            return 0
        print()

    removed, freed = purge_paths(all_targets, forbidden, dry=False)
    purge_config_keys(json_targets(selected), paths, dry=False)
    purge_history(paths, dry=False)

    print(f"\nУдалено объектов: {removed}, освобождено: {human(freed)}")

    if paths and not args.no_verify:
        report_leftovers(leftovers(paths, deep=args.verify_deep), deep=args.verify_deep)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(130)
