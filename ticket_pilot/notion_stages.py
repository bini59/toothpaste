"""Deliver configured Notion stage changes after the host commits its local transition."""
from . import notion, notion_tickets, notion_write
from .core import event


def enqueue(con, ticket, stage):
    """In restage's transaction. Keep the latest mapped transition until its page write succeeds."""
    gid = ticket["gid"]
    if stage not in notion_tickets.WRITE_STAGES or (stage == "closed" and not con.execute(
            "SELECT 1 FROM runs WHERE id=(SELECT max(id) FROM runs WHERE ticket_gid=?) AND state!='abandoned'",
            (gid,)).fetchone()):
        con.execute("DELETE FROM notion_status_queue WHERE ticket_gid=?", (gid,))
        return
    pid = ticket["project_gid"].removeprefix("notion:")
    cfg = notion_tickets.project_config(con, pid)
    if cfg and cfg.get("stage_statuses", {}).get(stage):
        con.execute("""INSERT OR REPLACE INTO notion_status_queue(ticket_gid,project_id,data_source_id,stage)
                       VALUES(?,?,?,?)""", (gid, pid, cfg["data_source_id"], stage))


def flush(con):
    """Run outside the pipeline transaction. Recheck the pending change under the same lock as CLI edits."""
    pending = con.execute("SELECT id, ticket_gid FROM notion_status_queue ORDER BY id").fetchall()
    for job in pending:
        gid = job["ticket_gid"]
        page_id = gid.removeprefix("notion:")
        with notion_write.page_lock(page_id):
            row = con.execute("SELECT * FROM notion_status_queue WHERE id=?", (job["id"],)).fetchone()
            if not row:
                continue  # superseded or delivered by another host process
            cfg = notion_tickets.project_config(con, row["project_id"])
            option = (cfg or {}).get("stage_statuses", {}).get(row["stage"])
            try:
                if option and cfg["data_source_id"] == row["data_source_id"]:
                    pages = notion_write.ProjectPages(con, cfg)
                    page = pages.client.page(page_id, pages.source)
                    props = pages.status_change(page, option, by_id=True)
                    # A host transition or settings save may have finished during the page/schema reads.
                    if (not con.execute("SELECT 1 FROM notion_status_queue WHERE id=?", (row["id"],)).fetchone()
                            or notion_tickets.project_config(con, row["project_id"]) != cfg):
                        continue
                    if props:
                        pages.patch(page_id, props)
                    else:
                        # A previous PATCH can have succeeded before its response reached the host.
                        notion_tickets.page_edited(con, gid, page, pages.source, pages.schema)
                    event(con, "notion_write", f"Notion 상태 반영 · {row['stage']} · {notion_tickets.ticket_key(page_id)}", gid)
                con.execute("DELETE FROM notion_status_queue WHERE id=?", (row["id"],))
            except notion.NotionError as e:
                if str(e) != row["last_error"]:
                    event(con, "error", f"Notion 상태 반영 대기 · {row['stage']} · {e}", gid)
                    con.execute("UPDATE notion_status_queue SET last_error=? WHERE id=?", (str(e), row["id"]))
            con.commit()
