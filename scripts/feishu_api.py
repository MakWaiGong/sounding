"""Thin Feishu (Lark) open-platform client used by the sounding publish path.

Design notes, all of them learned the hard way:

- **One persistent TLS connection.** The content APIs answer in ~100 ms when the
  connection is reused and ~400-500 ms when it is not; a publish touches a dozen
  endpoints, so reconnecting every call doubles the wall clock.
- **No third-party dependencies.** The rest of the skill is stdlib-only and has
  to run inside agents that cannot pip install.
- **A non-zero ``code`` is an exception, never a return value.** Feishu returns
  HTTP 200 with ``{"code": 99991663, ...}`` all the time; a caller that forgets
  to inspect ``code`` will happily write a half-published note.
- **Secrets never live in code.** ``app_id``/``app_secret`` come from config (see
  ``config.py``) so a clone can point at its own app.

Endpoint coverage is deliberately narrow: auth, drive (folder + media upload +
permissions), docx (create, children, raw_content), bitable (app, table, field,
record). Nothing here knows about sounding notes.
"""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import time
import uuid
import urllib.parse

DEFAULT_BASE = "https://open.feishu.cn"

# Feishu's own throttling code; retrying is the documented remedy.
_RETRY_CODES = {99991400}
_RETRY_STATUS = {429, 500, 502, 503, 504}


class FeishuError(RuntimeError):
    """A call that Feishu answered but refused."""

    def __init__(self, path, code, msg, payload=None):
        self.path = path
        self.code = code
        self.msg = msg
        self.payload = payload
        super().__init__(f"feishu {code} on {path}: {msg}")

    @property
    def permission_denied(self):
        # 99991672 / 99991679: scope not granted; 1069902: app has no doc access.
        return self.code in {99991672, 99991679, 1069902, 1069903}


class FeishuClient:
    """Blocking client. One instance per publish run is the intended use."""

    def __init__(self, app_id, app_secret, base=DEFAULT_BASE, timeout=30,
                 domain=None, retries=2):
        if not app_id or not app_secret:
            raise ValueError("feishu app_id and app_secret are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self.host = urllib.parse.urlsplit(base).hostname
        self.scheme = urllib.parse.urlsplit(base).scheme or "https"
        self.timeout = timeout
        self.domain = domain
        self.retries = retries
        self._conn = None
        self._token = None
        self._token_expires = 0.0
        self.timings = {}

    # -- plumbing ---------------------------------------------------------

    def _connection(self, fresh=False):
        if fresh or self._conn is None:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            if self.scheme == "https":
                self._conn = http.client.HTTPSConnection(self.host, timeout=self.timeout)
            else:
                self._conn = http.client.HTTPConnection(self.host, timeout=self.timeout)
        return self._conn

    def _send(self, method, path, body=None, headers=None, timed=None):
        conn = self._connection()
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        t0 = time.perf_counter()
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
        except (http.client.HTTPException, OSError):
            # Stale keep-alive socket: reconnect once, then give up.
            conn = self._connection(fresh=True)
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            if timed is not None:
                self.timings[timed] = round((time.perf_counter() - t0) * 1000)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise FeishuError(path, resp.status, raw[:300].decode("utf-8", "replace"))
        if resp.status >= 400 and "code" not in payload:
            raise FeishuError(path, resp.status, payload.get("msg") or raw[:200].decode("utf-8", "replace"))
        return payload, resp.status

    def call(self, method, path, payload=None, params=None, timed=None, auth=True, _attempt=0):
        """One API call. Raises FeishuError unless ``code == 0``."""
        url = path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body, headers = None, {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth:
            headers["Authorization"] = "Bearer " + self.token()
        resp, status = self._send(method, url, body, headers, timed)

        code = resp.get("code", 0 if status < 400 else status)
        if code not in (0, None):
            retryable = code in _RETRY_CODES or status in _RETRY_STATUS
            if retryable and _attempt < self.retries:
                time.sleep(0.4 * (2 ** _attempt))
                return self.call(method, path, payload, params, timed, auth, _attempt + 1)
            raise FeishuError(url, code, resp.get("msg") or resp.get("message") or "", resp)
        return resp.get("data", resp) if "data" in resp else resp

    def get(self, path, params=None, **kw):
        return self.call("GET", path, params=params, **kw)

    def post(self, path, payload=None, params=None, **kw):
        return self.call("POST", path, payload if payload is not None else {}, params=params, **kw)

    def put(self, path, payload=None, params=None, **kw):
        return self.call("PUT", path, payload if payload is not None else {}, params=params, **kw)

    def patch(self, path, payload=None, params=None, **kw):
        return self.call("PATCH", path, payload if payload is not None else {}, params=params, **kw)

    def delete(self, path, payload=None, params=None, **kw):
        return self.call("DELETE", path, payload, params=params, **kw)

    # -- auth -------------------------------------------------------------

    def token(self, force=False):
        if not force and self._token and time.time() < self._token_expires - 60:
            return self._token
        data = self.call(
            "POST", "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False, timed="auth")
        token = data.get("tenant_access_token") or data.get("data", {}).get("tenant_access_token")
        if not token:
            raise FeishuError("/auth", -1, "no tenant_access_token in response", data)
        self._token = token
        self._token_expires = time.time() + int(data.get("expire", 7200))
        return token

    # -- drive ------------------------------------------------------------

    def upload_media(self, blob, name, parent_type, parent_node, timed=None, extra=None):
        """Upload bytes and return a file_token.

        ``parent_type`` is ``docx_image`` for a document figure and
        ``bitable_image`` for a record attachment. For a document image,
        ``parent_node`` must be the *block* id of the target image block (not the
        document id) and ``extra`` must carry ``drive_route_token`` = document id;
        the token only becomes visible after patching the block with
        ``replace_image``.
        """
        boundary = "----sounding" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        chunks = []
        for key, value in (("file_name", name), ("parent_type", parent_type),
                           ("parent_node", parent_node), ("size", str(len(blob)))):
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        if extra:
            chunks.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; name=\"extra\"\r\n\r\n"
                 + json.dumps(extra, ensure_ascii=False)).encode() + b"\r\n"
            )
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n"
        )
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        url = "/open-apis/drive/v1/medias/upload_all"
        headers = {
            "Authorization": "Bearer " + self.token(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        t0 = time.perf_counter()
        resp, status = self._send("POST", url, body, headers)
        if timed is not None:
            self.timings[timed] = round((time.perf_counter() - t0) * 1000)
        if resp.get("code") not in (0, None):
            raise FeishuError(url, resp.get("code"), resp.get("msg") or "", resp)
        token = (resp.get("data") or {}).get("file_token")
        if not token:
            raise FeishuError(url, -1, "no file_token in response", resp)
        return token

    def create_folder(self, name, parent=""):
        return self.post("/open-apis/drive/v1/files/create_folder",
                         {"name": name, "folder_token": parent})["token"]

    def grant_tenant_access(self, token, doc_type="docx", level="tenant_editable"):
        """Let everyone in the org open the file (and optionally edit it)."""
        return self.patch(f"/open-apis/drive/v2/permissions/{token}/public",
                          {"link_share_entity": level}, params={"type": doc_type})

    def grant_public_access(self, token, doc_type="docx", level="anyone_readable"):
        """Let anyone with the link open the file, even outside the org."""
        return self.patch(f"/open-apis/drive/v2/permissions/{token}/public",
                          {"link_share_entity": level}, params={"type": doc_type})

    # -- docx -------------------------------------------------------------

    def create_doc(self, title, folder=""):
        payload = {"title": title}
        if folder:
            payload["folder_token"] = folder
        return self.post("/open-apis/docx/v1/documents", payload)["document"]["document_id"]

    def doc_children(self, doc_id, page_size=500):
        items, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                            params=params)
            items.extend(data.get("items") or [])
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                return items

    def append_children(self, doc_id, children, index=-1):
        """Append blocks at the document root. Feishu caps one request at 50."""
        return self.append_block_children(doc_id, doc_id, children, index=index)

    def append_block_children(self, doc_id, block_id, children, index=-1):
        """Append blocks inside ``block_id`` (a table cell, for instance).

        ``index=-1`` means "at the end". Passing 0 inserts at the *start*, which
        silently reverses the document -- the traps worth naming.
        """
        out = []
        for start in range(0, len(children), 50):
            chunk = children[start:start + 50]
            data = self.post(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}/children",
                {"children": chunk, "index": index},
                params={"document_revision_id": -1})
            out.extend(data.get("children") or [])
        return out

    def block_children(self, doc_id, block_id, page_size=500):
        """Children of any block (the docx read helper is root-only)."""
        items, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}/children",
                            params=params)
            items.extend(data.get("items") or [])
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                return items

    def clear_body(self, doc_id):
        """Delete every top-level block. Returns how many were removed.

        Verified behaviour: ``children/batch_delete`` is index-based and accepts
        ``end_index`` past the real length, so one call always suffices.
        """
        existing = self.doc_children(doc_id)
        if not existing:
            return 0
        self.delete(f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete",
                    {"start_index": 0, "end_index": len(existing)},
                    params={"document_revision_id": -1})
        return len(existing)

    def doc_text(self, doc_id):
        """Plain text of the document — the readback used for drift detection."""
        return self.get(f"/open-apis/docx/v1/documents/{doc_id}/raw_content").get("content", "")

    def all_blocks(self, doc_id, page_size=500):
        """The whole block tree in one pass, nested cells included.

        Reading the tree once beats one call per table cell, which is what
        filling a 5x4 table would otherwise cost.
        """
        items, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/docx/v1/documents/{doc_id}/blocks", params=params)
            items.extend(data.get("items") or [])
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                return items

    def batch_update_blocks(self, doc_id, requests):
        """Update many blocks in one request (cells of a table, typically)."""
        if not requests:
            return []
        out = []
        for start in range(0, len(requests), 50):
            data = self.patch(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/batch_update",
                {"requests": requests[start:start + 50]},
                params={"document_revision_id": -1})
            out.extend(data.get("blocks") or [])
        return out

    def doc_url(self, doc_id):
        prefix = f"https://{self.domain}" if self.domain else ""
        return f"{prefix}/docx/{doc_id}"

    # -- bitable ----------------------------------------------------------

    def list_tables(self, app_token):
        return self.get(f"/open-apis/bitable/v1/apps/{app_token}/tables",
                        params={"page_size": 100}).get("items") or []

    def create_table(self, app_token, name, fields, default_view_name="表格"):
        return self.post(f"/open-apis/bitable/v1/apps/{app_token}/tables",
                         {"table": {"name": name, "default_view_name": default_view_name,
                                    "fields": fields}})

    def list_fields(self, app_token, table_id):
        return self.get(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                        params={"page_size": 200}).get("items") or []

    def create_field(self, app_token, table_id, field):
        return self.post(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                         field)

    def list_records(self, app_token, table_id, page_size=500):
        items, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.get(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                            params=params)
            items.extend(data.get("items") or [])
            page_token = data.get("page_token")
            if not data.get("has_more") or not page_token:
                return items

    def search_records(self, app_token, table_id, conditions, conjunction="and"):
        """Filtered search. Each condition is (field_name, operator, value_list)."""
        payload = {"filter": {"conjunction": conjunction,
                              "conditions": [{"field_name": f, "operator": o, "value": v}
                                             for f, o, v in conditions]}}
        items = []
        for row in self.post(
                f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                payload, params={"page_size": 100}).get("items") or []:
            items.append(row)
        return items

    def create_record(self, app_token, table_id, fields):
        return self.post(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                         {"fields": fields})["record"]

    def update_record(self, app_token, table_id, record_id, fields):
        return self.put(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            {"fields": fields})["record"]

    def delete_record(self, app_token, table_id, record_id):
        return self.delete(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}")

    def create_base(self, name, folder=""):
        payload = {"name": name}
        if folder:
            payload["folder_token"] = folder
        return self.post("/open-apis/bitable/v1/apps", payload)["app"]["app_token"]

    def base_domain(self, app_token):
        """Tenant host (``xxx.feishu.cn``) probed from a share URL."""
        try:
            data = self.post("/open-apis/drive/v1/metas/batch_query",
                             {"request_docs": [{"doc_token": app_token, "doc_type": "bitable"}],
                              "with_url": True})
            url = (data.get("metas") or [{}])[0].get("url") or ""
        except FeishuError:
            return None
        parts = urllib.parse.urlsplit(url)
        return parts.hostname if parts.hostname else None

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def client_from_config(cfg_section, **kw):
    """Build a client from the ``feishu`` section of config."""
    return FeishuClient(
        app_id=cfg_section.get("app_id") or os.environ.get("FEISHU_APP_ID"),
        app_secret=cfg_section.get("app_secret") or os.environ.get("FEISHU_APP_SECRET"),
        base=cfg_section.get("api_base", DEFAULT_BASE),
        domain=cfg_section.get("domain"),
        **kw)


if __name__ == "__main__":  # smoke test: python scripts/feishu_api.py
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts import config as _config

    section = _config.feishu()
    c = client_from_config(section)
    print("token ok:", bool(c.token()))
    files = c.get("/open-apis/drive/v1/files", params={"page_size": 10})
    print("root files:", len(files.get("files") or []))
    print("timings:", c.timings)
    c.close()
