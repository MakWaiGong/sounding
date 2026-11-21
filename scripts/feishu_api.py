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

from collections import Counter
from email.utils import parsedate_to_datetime
import http.client
import json
import mimetypes
import os
import random
import re
import time
import uuid
import urllib.parse

DEFAULT_BASE = "https://open.feishu.cn"

# Retry-After beyond this limit fails closed instead of retrying too early.
_RATE_LIMIT_CODE = 99991400
_MAX_RETRIES = 5
_MAX_RETRY_DELAY = 8.0
_BASE_RETRY_DELAY = 0.4


def _retry_after_seconds(value):
    """Parse HTTP Retry-After, without retaining the header or accepting negatives."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0") or "0"
        return float(digits) if len(digits) < 9 else float("inf")
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, date.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def validate_editor_open_ids(ids):
    """Require explicit, distinct Feishu open IDs before any document write."""
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(value, str) or not re.fullmatch(r"ou_[0-9a-f]{32}", value)
                   for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("feishu.editor_open_ids must be a non-empty list of unique, valid open IDs")
    return list(ids)


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


class FeishuRateLimitError(FeishuError):
    """An explicit HTTP 429 or Feishu rate-limit response rejected the request."""


class FeishuUncertainError(FeishuError):
    """A write may have succeeded; reconcile its state before trying again."""


class FeishuClient:
    """Blocking client. Use separate instances for concurrent publish runs."""

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
        self.retries = max(0, min(int(retries), _MAX_RETRIES))
        self._conn = None
        self._retry_after = None
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

    def _discard_connection(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _send(self, method, path, body=None, headers=None, timed=None, retry_on_disconnect=True):
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        self._retry_after = None
        t0 = time.perf_counter()
        try:
            conn = self._connection()
            for attempt in range(2):
                try:
                    conn.request(method, path, body=body, headers=hdrs)
                    resp = conn.getresponse()
                    self._retry_after = _retry_after_seconds(resp.getheader("Retry-After"))
                    raw = resp.read()
                    break
                except (http.client.HTTPException, OSError):
                    self._discard_connection()
                    if not retry_on_disconnect or method.upper() != "GET" or attempt:
                        raise
                    conn = self._connection(fresh=True)
        finally:
            if timed is not None:
                self.timings[timed] = round((time.perf_counter() - t0) * 1000)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            if resp.status >= 400:
                payload = {}
            else:
                raise FeishuError("request", resp.status, "invalid JSON response") from None
        if not isinstance(payload, dict):
            if resp.status >= 400:
                payload = {}
            else:
                raise FeishuError("request", resp.status, "invalid JSON response")
        return payload, resp.status

    def _retry_wait(self, attempt, retry_after=None):
        if retry_after is not None and retry_after > _MAX_RETRY_DELAY:
            return False
        ceiling = min(_MAX_RETRY_DELAY, _BASE_RETRY_DELAY * (2 ** attempt))
        delay = random.uniform(0, ceiling)
        if retry_after is not None:
            delay = max(delay, retry_after)
        time.sleep(delay)
        return True

    def _request(self, method, url, body, headers, timed=None, retry=True, attempt=0):
        safe = method.upper() == "GET"
        attempt = max(0, int(attempt))
        while True:
            self._retry_after = None
            try:
                resp, status = self._send(method, url, body, headers, timed,
                                          retry_on_disconnect=False)
            except (http.client.HTTPException, OSError) as exc:
                if not safe:
                    raise FeishuUncertainError("request", -1,
                        "write outcome is uncertain after transport failure; reconcile before retrying") from None
                if not retry or attempt >= self.retries:
                    message = "GET transport failure; " + ("retry limit reached" if retry else "retries disabled")
                    if isinstance(exc, OSError):
                        raise OSError(message) from None
                    raise http.client.HTTPException(message) from None
                self._retry_wait(attempt)
                attempt += 1
                continue
            except FeishuError as exc:
                if not safe:
                    raise FeishuUncertainError("request", exc.code,
                        "write outcome is uncertain after invalid response; reconcile before retrying") from None
                raise

            code = resp.get("code", 0 if status < 400 else status)
            if status >= 500:
                if not safe:
                    raise FeishuUncertainError("request", status,
                        "write outcome is uncertain after server failure; reconcile before retrying")
                if (retry and attempt < self.retries
                        and self._retry_wait(attempt, self._retry_after)):
                    attempt += 1
                    continue
                raise FeishuError("request", status, "server failure; retry budget or maximum wait exceeded"
                                  if retry else "server failure")
            if status == 429 or code == _RATE_LIMIT_CODE:
                if (retry and attempt < self.retries
                        and self._retry_wait(attempt, self._retry_after)):
                    attempt += 1
                    continue
                raise FeishuRateLimitError(
                    "request", _RATE_LIMIT_CODE if code == _RATE_LIMIT_CODE else 429,
                    "rate limited; retry budget or maximum wait exceeded" if retry
                    else "rate limited; retries disabled")
            if status >= 400 or code not in (0, None):
                if code in (0, None):
                    code = status
                raise FeishuError(url, code, resp.get("msg") or resp.get("message") or "", resp)
            return resp

    def call(self, method, path, payload=None, params=None, timed=None, auth=True, _attempt=0,
             retry=True):
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
        resp = self._request(method, url, body, headers, timed, retry, _attempt)
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
        try:
            resp = self._request("POST", url, body, headers)
        finally:
            if timed is not None:
                self.timings[timed] = round((time.perf_counter() - t0) * 1000)
        data = resp.get("data")
        token = data.get("file_token") if isinstance(data, dict) else None
        if not token:
            raise FeishuUncertainError("request", -1,
                "upload outcome is uncertain: no file token returned; reconcile before retrying")
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

    def list_doc_permission_members(self, token):
        """List collaborators; reject any indication that the result is incomplete."""
        path = f"/open-apis/drive/v1/permissions/{urllib.parse.quote(token, safe='')}/members"
        try:
            data = self.get(path, params={"type": "docx"})
            if (not isinstance(data, dict) or not isinstance(data.get("items"), list)
                    or ("has_more" in data and data["has_more"] is not False)
                    or data.get("page_token") or data.get("next_page_token")):
                raise ValueError("incomplete collaborator list")
            for item in data["items"]:
                if (not isinstance(item, dict) or any(
                        not isinstance(item.get(key), str) or not item[key]
                        for key in ("member_type", "member_id", "perm"))):
                    raise ValueError("invalid collaborator")
            return data["items"]
        except Exception:
            raise FeishuError("document permissions", -1, "could not read collaborator permissions") from None

    def ensure_doc_full_access(self, token, editor_open_ids):
        """Grant only configured users full_access and verify via a fresh listing."""
        try:
            editor_open_ids = validate_editor_open_ids(editor_open_ids)
            targets = set(editor_open_ids)

            def states(members):
                found = {}
                for member in members:
                    member_id = member["member_id"]
                    if member["member_type"] == "openid" and member_id in targets:
                        if member_id in found:
                            raise ValueError("ambiguous collaborator")
                        found[member_id] = member["perm"]
                return found

            def others(members):
                return Counter((member["member_type"], member["member_id"], member["perm"],
                                "perm_type" in member,
                                json.dumps(member.get("perm_type"), sort_keys=True))
                               for member in members
                               if not (member["member_type"] == "openid"
                                       and member["member_id"] in targets))

            members_before = self.list_doc_permission_members(token)
            before = states(members_before)
            if any(perm not in ("view", "edit", "full_access") for perm in before.values()):
                raise ValueError("unknown or higher collaborator permission")
            path = f"/open-apis/drive/v1/permissions/{urllib.parse.quote(token, safe='')}/members"
            for member_id in editor_open_ids:
                perm = before.get(member_id)
                if perm == "full_access":
                    continue
                if perm in ("view", "edit"):
                    self.put(f"{path}/{urllib.parse.quote(member_id, safe='')}",
                             {"member_type": "openid", "perm": "full_access"},
                             params={"type": "docx", "need_notification": "false"}, retry=False)
                else:
                    self.post(path, {"member_type": "openid", "member_id": member_id,
                                     "perm": "full_access"},
                              params={"type": "docx", "need_notification": "false"}, retry=False)
            members_after = self.list_doc_permission_members(token)
            after = states(members_after)
            if (any(after.get(member_id) != "full_access" for member_id in editor_open_ids)
                    or others(members_before) != others(members_after)):
                raise ValueError("collaborator verification failed")
        except Exception:
            raise FeishuError("document permissions", -1,
                              "could not grant or verify full_access; permissions may have changed") from None

    # -- docx -------------------------------------------------------------

    def create_doc(self, title, folder=""):
        payload = {"title": title}
        if folder:
            payload["folder_token"] = folder
        data = self.post("/open-apis/docx/v1/documents", payload)
        document = data.get("document") if isinstance(data, dict) else None
        document_id = document.get("document_id") if isinstance(document, dict) else None
        if not isinstance(document_id, str) or not document_id:
            raise FeishuUncertainError("request", -1,
                "document creation outcome is uncertain: no document id returned; reconcile before retrying")
        return document_id

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
        data = self.post(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                         {"fields": fields})
        record = data.get("record") if isinstance(data, dict) else None
        record_id = record.get("record_id") if isinstance(record, dict) else None
        if not isinstance(record_id, str) or not record_id:
            raise FeishuUncertainError("request", -1,
                "record creation outcome is uncertain: no record id returned; reconcile before retrying")
        return record

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
