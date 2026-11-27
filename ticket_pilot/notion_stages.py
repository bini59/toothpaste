"""Queue stage changes with the host transition and deliver them from a separate tick worker."""
import fcntl
import os
import subprocess
import sys
from contextlib import contextmanager
from time import time

from . import core, notion, notion_tickets, notion_write

BATCH_SIZE = 8
RETRY_SECONDS = 300
MILESTONE_DONE = "milestone_done"


def enqueue(con, ticket, stage):
    """In restage's transaction. Keep the latest mapped transition until its page write succeeds."""
    parent = stage == MILESTONE_DONE
    gid = ticket["parent_gid"] if parent else ticket["gid"]
    # A former task and its milestone share a page ID. Keep the verified milestone write through a task restage.
    if not parent and con.execute(
            "SELECT 1 FROM notion_status_queue WHERE ticket_gid=? AND stage=?", (gid, MILESTONE_DONE)).fetchone():
        return
    if (not parent and stage not in notion_tickets.WRITE_STAGES) or (stage == "closed" and not con.execute(
            "SELECT 1 FROM runs WHERE id=(SELECT max(id) FROM runs WHERE ticket_gid=?) AND state!='abandoned'",
            (gid,)).fetchone()):
        con.execute("DELETE FROM notion_status_queue WHERE ticket_gid=?", (gid,))
        return
    pid = ticket["project_gid"].removeprefix("notion:")
    cfg = notion_tickets.project_config(con, pid)
    source = (cfg or {}).get("milestone_data_source_id" if parent else "data_source_id")
    if source and cfg.get("stage_statuses", {}).get("done" if parent else stage):
        con.execute("""INSERT OR REPLACE INTO notion_status_queue(ticket_gid,project_id,data_source_id,stage,next_attempt_at)
                       VALUES(?,?,?,?,?)""", (gid, pid, source, stage, time()))


@contextmanager
def _worker_lock(mode):
    with core.DB_PATH.with_suffix(".notion-status.lock").open(mode) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            yield lock


def worker_running():
    """Mode switches and migrations check for detached writers before moving the database."""
    try:
        with _worker_lock("r") as lock:
            return lock is None
    except FileNotFoundError:
        return False


def start_worker(con):
    if not con.execute("SELECT 1 FROM notion_status_queue WHERE next_attempt_at<=? LIMIT 1", (time(),)).fetchone():
        return
    with _worker_lock("a") as lock:
        if lock is None:
            return
        # The child inherits the held lock until exit, including if the tick exits first.
        # Give it its own output so a caller capturing the tick's pipes can return immediately.
        with core.DB_PATH.with_suffix(".notion-status.log").open("a") as log:
            return subprocess.Popen([sys.executable, "-m", "ticket_pilot.notion_stages"], cwd=core.ROOT,
                                    env={**os.environ, "TP_DB": str(core.DB_PATH.resolve())}, pass_fds=(lock.fileno(),),
                                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


def flush(con):
    """Process a batch of due pages, oldest due first. Keep network I/O outside SQLite transactions."""
    from . import milestone
    pending = con.execute("""SELECT id, ticket_gid FROM notion_status_queue WHERE next_attempt_at<=?
                             ORDER BY next_attempt_at, id LIMIT ?""", (time(), BATCH_SIZE)).fetchall()
    for job in pending:
        gid = job["ticket_gid"]
        page_id = gid.removeprefix("notion:")
        with notion_write.page_lock(page_id):
            row = con.execute("SELECT * FROM notion_status_queue WHERE id=? AND next_attempt_at<=?",
                              (job["id"], time())).fetchone()
            if not row:
                continue
            try:
                cfg = notion_tickets.project_config(con, row["project_id"])
                parent = row["stage"] == MILESTONE_DONE
                option = (cfg or {}).get("stage_statuses", {}).get("done" if parent else row["stage"])
                source = (cfg or {}).get("milestone_data_source_id" if parent else "data_source_id")
                if option and source == row["data_source_id"]:
                    pages = notion_write.ProjectPages(con, cfg, source=source)
                    page = pages.client.page(page_id, pages.source)
                    tagged_milestone = notion_tickets.milestone_tag_filter(pages.schema, cfg)
                    same_role = not tagged_milestone or tagged_milestone(page) == parent
                    props = pages.status_change(page, option, by_id=True) if same_role else {}
                    # A host transition or settings save may have finished during the page/schema reads.
                    if (not con.execute("SELECT 1 FROM notion_status_queue WHERE id=?", (row["id"],)).fetchone()
                            or notion_tickets.project_config(con, row["project_id"]) != cfg):
                        continue
                    if same_role and (not parent or milestone.all_repos_live(con, gid)):
                        pages.apply_properties(page, props)
                        core.event(con, "notion_write", f"Notion 상태 반영 · {row['stage']} · {notion_tickets.ticket_key(page_id)}", gid)
                con.execute("DELETE FROM notion_status_queue WHERE id=?", (row["id"],))
            except (notion.NotionError, KeyError, TypeError, ValueError) as e:
                updated = con.execute("UPDATE notion_status_queue SET last_error=?, next_attempt_at=? WHERE id=?",
                                      (str(e), time() + RETRY_SECONDS, row["id"]))
                if updated.rowcount and str(e) != row["last_error"]:
                    core.event(con, "error", f"Notion 상태 반영 대기 · {row['stage']} · {e}", gid)
            con.commit()


if __name__ == "__main__":
    flush(core.db())
