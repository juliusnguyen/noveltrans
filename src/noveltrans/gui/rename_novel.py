"""Renaming a novel, from wherever the user asks for it.

One function, two callers (the Tải truyện tab's ✏️ button and the Video tab's "Tên hiển
thị" box), so the two cannot drift into two different renames with two different sets of
consequences.

**The name is `display_title`.** It already wins the first slot of `novel_label()` and is
already what `display_name()` puts on the video, the thumbnail, the description, the
YouTube title, the novel tab and the picker — it is the novel's name everywhere except
filenames. Renaming is therefore mostly a question about filenames, and the answer is the
`slug` pin: `NovelProject.rename_novel` writes the new name and the stem the files keep,
in one `meta.json` write.

**Keeping the files is the default.** Confirmed with the user. Moving them is offered,
never assumed: it is the branch that can half-finish, that invalidates every OneDrive
manifest key, and that costs a re-upload. Keeping them is not a compromise — the pin is
what makes it correct, because a kept stem stays *findable* afterwards, which before this
feature it would not have been.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from noveltrans.rename import apply_rename, plan_rename
from noveltrans.slug import slugify


def _human_size(total: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024
    return f"{total:.1f} GB"


def rename_novel(
    parent, project, new_name: str, *, busy: bool = False, offer_migration: bool = True
) -> bool:
    """Rename `project` to `new_name`. True if anything was written.

    `busy` is the workspace's `has_running_workers()`. A running VideoWorker holds its
    stem in a local and writes `<stem>.mp4` minutes later, so moving files out from under
    it would strand a half-rendered part — while it is set, only the display-only branch
    is on offer.

    `offer_migration=False` skips the question and keeps the files, for the Video tab's
    "Tên hiển thị" box: that field says what it does, and it commits on focus-out, so a
    modal question there would ambush someone who only tabbed past it. The ✏️ button on
    the Tải truyện tab is the deliberate rename, and it is the one that asks.
    """
    new_name = (new_name or "").strip()
    if new_name == project.meta.display_title:
        return False

    meta = project.meta
    current_slug = meta.slug_name()
    wanted_slug = slugify(new_name or meta.translated_title or meta.title)

    # Nothing on disk would move anyway: pin what we already use and take the fast path.
    if wanted_slug == current_slug or not offer_migration:
        project.rename_novel(new_name, pin_slug=current_slug)
        return True

    plan = plan_rename(project.path, current_slug, wanted_slug)
    if plan.is_empty:
        # No rendered part or merged file carries the old stem, so there is no question to
        # ask: adopt the new stem and let the first render use it.
        project.rename_novel(new_name, pin_slug=wanted_slug)
        return True

    if not plan.is_safe:
        QMessageBox.warning(
            parent,
            "Không đổi được tên file",
            f"Đã có sẵn “{plan.collisions[0].name}” trong thư mục xuất của truyện này, "
            "nên đổi tên file sẽ ghi đè lên nó.\n\n"
            "Tên hiển thị vẫn được đổi; các file đã tạo giữ nguyên tên cũ.",
        )
        project.rename_novel(new_name, pin_slug=current_slug)
        return True

    keep, move = _ask(parent, plan, busy)
    if keep is None:
        return False
    if move:
        try:
            done = apply_rename(plan)
        except OSError as exc:
            # Pin the OLD stem: some files may have moved, and re-running the rename is
            # what finishes the job. Pinning the new one here would point the app at names
            # that only half exist.
            project.rename_novel(new_name, pin_slug=current_slug)
            QMessageBox.warning(
                parent,
                "Đổi tên file chưa xong",
                f"Không đổi được tên hết các file: {exc}\n\n"
                "Tên hiển thị đã đổi, file giữ tên cũ. Thử đổi tên lại sau.",
            )
            return True
        project.rename_novel(new_name, pin_slug=wanted_slug)
        moved = len([m for m in done if m.kind != "video-dir"])
        QMessageBox.information(
            parent,
            "Đã đổi tên",
            f"Đã đổi tên truyện và {moved} file đã tạo.\n\n"
            "Nếu bạn có sao lưu OneDrive, lần đồng bộ tới sẽ tải lên lại số file này; "
            "bản cũ trên OneDrive vẫn còn và bạn tự xoá.",
        )
    else:
        project.rename_novel(new_name, pin_slug=current_slug)
    return True


KEEP_LABEL = "Chỉ đổi tên hiển thị"
CANCEL_LABEL = "Huỷ"


def rename_prompt(plan, busy: bool) -> tuple[str, str | None]:
    """What the consequences dialog says, and the label of its "move the files" button.

    Pure, so what the user is told about an irreversible-ish operation can be asserted
    without a modal box — and a modal box in a test hangs the suite outright.

    `None` for the second value means the move is not on offer at all: a running
    VideoWorker holds its stem in a local and writes `<stem>.mp4` minutes later, so moving
    the folder now would strand a half-rendered part.
    """
    count = len([m for m in plan.moves if m.kind != "video-dir"])
    lines = [
        f"Truyện này đã có {count} file được tạo ({_human_size(plan.total_bytes)}) "
        f"mang tên “{plan.old_slug}”.",
        "",
        "Đổi tên hiển thị là đủ cho hầu hết mọi việc: tên mới sẽ hiện trên video, ảnh bìa, "
        "mô tả, tiêu đề YouTube, thẻ truyện và danh sách — chỉ tên file trên ổ đĩa là giữ "
        "nguyên.",
    ]
    if plan.published:
        lines += [
            "",
            f"• {plan.published} phần đã có bản ghi tải lên YouTube. Bản ghi đi theo file "
            "nên sẽ không bị đăng lại — nhưng tiêu đề video ĐÃ ĐĂNG trên YouTube vẫn giữ "
            "tên cũ, phải tự sửa trên kênh.",
        ]
    lines += [
        "",
        "• Ảnh bìa đã tạo vẫn in tên cũ — bấm “Tạo lại tất cả ảnh bìa” nếu muốn đổi.",
    ]
    if busy:
        lines += [
            "",
            "⚠️ Đang có việc chạy (tải/dịch/tạo audio hoặc video) nên chỉ đổi được tên "
            "hiển thị. Dừng rồi thử lại nếu muốn đổi cả tên file.",
        ]
        return "\n".join(lines), None

    lines += [
        "",
        f"Nếu muốn, có thể đổi tên cả {count} file sang “{plan.new_slug}”. Việc này sẽ "
        "khiến OneDrive phải tải lên lại số file đó.",
    ]
    return "\n".join(lines), f"Đổi tên cả {count} file"


def _ask(parent, plan, busy: bool):
    """Show the consequences dialog. Returns `(chosen, move_files)`; `(None, False)` = huỷ."""
    text, move_label = rename_prompt(plan, busy)
    box = QMessageBox(parent)
    box.setWindowTitle("Đổi tên truyện")
    box.setText(text)
    keep_button = box.addButton(KEEP_LABEL, QMessageBox.ButtonRole.AcceptRole)
    move_button = (
        box.addButton(move_label, QMessageBox.ButtonRole.DestructiveRole)
        if move_label
        else None
    )
    cancel_button = box.addButton(CANCEL_LABEL, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep_button)  # the safe branch is what Enter picks
    box.exec()

    clicked = box.clickedButton()
    if clicked is cancel_button or clicked is None:
        return None, False
    return clicked, clicked is move_button
