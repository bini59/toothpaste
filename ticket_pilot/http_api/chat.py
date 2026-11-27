"""Project chat, attachments and inbox HTTP handlers."""
import mimetypes
from urllib.parse import quote

from .. import chat as CH
from .. import notifications as N
from ..core import db, event


def get_conversations(req):
    try:
        return req._send(200, {"conversations": CH.conversations((req.query().get("project") or [""])[0])})
    except SystemExit as e:
        return req._send(404, {"error": str(e)})


def get_messages(req):
    try:
        return req._send(200, CH.messages((req.query().get("id") or [""])[0]))
    except SystemExit as e:
        return req._send(404, {"error": str(e)})


def get_compactions(req):
    period = req.query().get("days", ["30"])[0]
    if period not in ("7", "30", "90", "all"):
        return req._send(400, {"error": "기간은 7일, 30일, 90일 또는 전체로 선택해 주세요"})
    return req._send(200, {"rows": CH.compaction_report(None if period == "all" else int(period))})


def get_inbox(req):
    return req._send(200, {"items": CH.inbox(), "notifications": N.unread()})


def get_file(req):
    # a chat attachment (chat.file_path: only files inside data/chat-uploads/<conversation>/). Only images,
    # pdf and plain text open inline; anything else (html, svg, ...) downloads so it never runs as this origin
    q = req.query()
    f = CH.file_path((q.get("id") or [""])[0], (q.get("name") or [""])[0])
    if not f:
        return req._send(404, {"error": "not found"})
    ctype = mimetypes.guess_type(f.name)[0] or ""
    inline = ctype in CH.IMAGES or ctype in ("application/pdf", "text/plain")
    req.send_response(200)
    req.send_header("Content-Type", (ctype + "; charset=utf-8" if ctype == "text/plain" else ctype)
                     if inline else "application/octet-stream")
    req.send_header("Content-Disposition",
                     f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(f.name.split('-', 1)[-1])}")
    req.send_header("X-Content-Type-Options", "nosniff")
    req.send_header("Cache-Control", "private, max-age=86400")
    req.end_headers()
    return req.wfile.write(f.read_bytes())


def post_new(req, body):
    return req._send(200, {"ok": True, "id": CH.create(body["project"], body.get("repo") or None)})


def post_send(req, body):
    turn = CH.send(body["id"], body.get("text") or "", body.get("files") or None)
    con = db()
    event(con, "chat", f"chat {body['id']}: message sent")
    con.commit()
    return req._send(200, {"ok": True, "turn": turn})


def post_rename(req, body):
    CH.rename(body["id"], body.get("title") or "")
    return req._send(200, {"ok": True})


def post_stop(req, body):
    CH.stop(body["id"])
    return req._send(200, {"ok": True})


def post_read(req, body):
    CH.read(body["id"], body.get("upto"))
    return req._send(200, {"ok": True})


def post_read_all(req, body):
    upto = body.get("notifications_upto")  # 수신함 alerts listed with them (TP-105)
    if upto is not None:
        N.check_id(upto)  # before the chat marks commit
    CH.read_all(body.get("items"))
    if upto is not None:
        N.read_all(upto)
    return req._send(200, {"ok": True})


def post_notifications_read(req, body):
    N.read(body.get("id"))
    return req._send(200, {"ok": True})


def post_delete(req, body):
    CH.delete(body["id"])
    return req._send(200, {"ok": True})
