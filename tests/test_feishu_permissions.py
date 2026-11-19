import http.client
import sys
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feishu_api as api

ID_A = "ou_" + "a" * 32
ID_B = "ou_" + "b" * 32
ID_OTHER = "ou_" + "c" * 32
DOC = "fake_document_token"


def member(member_id, perm, member_type="openid"):
    return {"member_type": member_type, "member_id": member_id, "perm": perm}


class FakeClient(api.FeishuClient):
    def __init__(self, members=()):
        super().__init__("fake_app", "fake_secret", retries=2)
        self.members = [dict(m) for m in members]
        self.calls = []
        self.list_count = 0
        self.fail_read_on = set()
        self.fail_write = False
        self.apply_writes = True
        self.list_payload = None

    def call(self, method, path, payload=None, params=None, **kwargs):
        self.calls.append((method, path, payload, params, kwargs))
        if method == "GET":
            self.list_count += 1
            if self.list_count in self.fail_read_on:
                raise RuntimeError("private id token https://private.invalid")
            if self.list_payload is not None:
                return self.list_payload
            return {"items": [dict(m) for m in self.members]}
        if self.fail_write:
            raise RuntimeError("private id token https://private.invalid")
        if self.apply_writes:
            if method == "POST":
                self.members.append(dict(payload))
            elif method == "PUT":
                for item in self.members:
                    if item["member_type"] == "openid" and path.endswith("/" + item["member_id"]):
                        item["perm"] = payload["perm"]
        return {}


class PermissionTests(unittest.TestCase):
    def test_create_upgrade_and_skip_preserve_other_members(self):
        already = "ou_" + "d" * 32
        client = FakeClient([member(ID_A, "view"), member(already, "full_access"),
                             member(ID_OTHER, "edit"), member("group", "view", "groupid")])
        before_other = [dict(m) for m in client.members if m["member_id"] in (ID_OTHER, "group")]
        client.ensure_doc_full_access(DOC, [ID_A, ID_B, already])
        self.assertEqual(client.list_count, 2)
        writes = [call for call in client.calls if call[0] != "GET"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], ("PUT", f"/open-apis/drive/v1/permissions/{DOC}/members/{ID_A}",
                                     {"member_type": "openid", "perm": "full_access"},
                                     {"type": "docx", "need_notification": "false"}, {"retry": False}))
        self.assertEqual(writes[1], ("POST", f"/open-apis/drive/v1/permissions/{DOC}/members",
                                     member(ID_B, "full_access"),
                                     {"type": "docx", "need_notification": "false"}, {"retry": False}))
        self.assertEqual([m for m in client.members if m["member_id"] in (ID_OTHER, "group")], before_other)
        self.assertEqual({m["member_id"]: m["perm"] for m in client.members}[ID_A], "full_access")
        self.assertEqual({m["member_id"]: m["perm"] for m in client.members}[ID_B], "full_access")

    def test_edit_is_upgraded_and_full_access_is_not_written(self):
        client = FakeClient([member(ID_A, "edit"), member(ID_B, "full_access")])
        client.ensure_doc_full_access(DOC, [ID_A, ID_B])
        self.assertEqual([c[0] for c in client.calls], ["GET", "PUT", "GET"])

    def test_all_existing_full_still_rechecks_independently(self):
        client = FakeClient([member(ID_A, "full_access"), member(ID_B, "full_access")])
        client.ensure_doc_full_access(DOC, [ID_A, ID_B])
        self.assertEqual([c[0] for c in client.calls], ["GET", "GET"])

    def test_incomplete_or_malformed_listing_fails_without_writes(self):
        payloads = [None, {}, {"items": None}, {"items": [], "has_more": True},
                    {"items": [], "has_more": "false"}, {"items": [], "page_token": "next"},
                    {"items": [], "next_page_token": "next"},
                    {"items": [{"member_id": ID_A, "perm": "view"}]}]
        for payload in payloads:
            with self.subTest(payload=type(payload).__name__):
                client = FakeClient()
                if payload is None:
                    client.fail_read_on.add(1)
                else:
                    client.list_payload = payload
                with self.assertRaises(api.FeishuError):
                    client.ensure_doc_full_access(DOC, [ID_A])
                self.assertEqual([c[0] for c in client.calls], ["GET"])

    def test_explicit_non_paginated_listing_is_accepted(self):
        client = FakeClient()
        client.list_payload = {"items": [], "has_more": False}
        self.assertEqual(client.list_doc_permission_members(DOC), [])
        self.assertEqual(client.calls[0][3], {"type": "docx"})

    def test_unknown_or_owner_permission_is_not_downgraded(self):
        for perm in ("owner", "unknown"):
            with self.subTest(perm=perm):
                client = FakeClient([member(ID_A, "view"), member(ID_B, perm)])
                with self.assertRaises(api.FeishuError):
                    client.ensure_doc_full_access(DOC, [ID_A, ID_B])
                self.assertEqual([c[0] for c in client.calls], ["GET"])

    def test_ambiguous_duplicate_member_is_rejected_before_write(self):
        client = FakeClient([member(ID_A, "view"), member(ID_A, "edit")])
        with self.assertRaises(api.FeishuError):
            client.ensure_doc_full_access(DOC, [ID_A])
        self.assertEqual([c[0] for c in client.calls], ["GET"])

    def test_failed_write_is_redacted_and_not_retried(self):
        client = FakeClient()
        client.fail_write = True
        with self.assertRaises(api.FeishuError) as ctx:
            client.ensure_doc_full_access(DOC, [ID_A])
        self.assertEqual([c[0] for c in client.calls], ["GET", "POST"])
        for value in (ID_A, DOC, "private", "https://"):
            self.assertNotIn(value, str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_readback_failure_or_missing_grant_fails(self):
        for fail_read in (True, False):
            with self.subTest(fail_read=fail_read):
                client = FakeClient()
                if fail_read:
                    client.fail_read_on.add(2)
                else:
                    client.apply_writes = False
                with self.assertRaises(api.FeishuError):
                    client.ensure_doc_full_access(DOC, [ID_A])
                self.assertEqual([c[0] for c in client.calls], ["GET", "POST", "GET"])

    def test_non_target_member_change_is_detected(self):
        client = FakeClient([member(ID_OTHER, "view")])
        original = client.list_doc_permission_members

        def concurrent_change(token):
            if client.list_count == 1:
                client.members[0]["perm"] = "edit"
            return original(token)

        client.list_doc_permission_members = concurrent_change
        with self.assertRaises(api.FeishuError) as ctx:
            client.ensure_doc_full_access(DOC, [ID_A])
        self.assertEqual([c[0] for c in client.calls], ["GET", "POST", "GET"])
        self.assertNotIn(ID_OTHER, str(ctx.exception))

    def test_non_target_permission_scope_change_is_detected(self):
        missing = object()
        for before, after in (("container", "single_page"), (missing, "container"),
                              ("container", missing), (missing, None), (None, missing)):
            with self.subTest(before_missing=before is missing, after_missing=after is missing,
                              before_null=before is None, after_null=after is None):
                other = member(ID_OTHER, "view")
                if before is not missing:
                    other["perm_type"] = before
                client = FakeClient([other])
                original = client.list_doc_permission_members

                def change_scope(token):
                    if client.list_count == 1:
                        if after is missing:
                            client.members[0].pop("perm_type", None)
                        else:
                            client.members[0]["perm_type"] = after
                    return original(token)

                client.list_doc_permission_members = change_scope
                with self.assertRaises(api.FeishuError) as ctx:
                    client.ensure_doc_full_access(DOC, [ID_A])
                self.assertEqual([c[0] for c in client.calls], ["GET", "POST", "GET"])
                self.assertNotIn(ID_OTHER, str(ctx.exception))
                self.assertTrue(ctx.exception.__suppress_context__)

    def test_non_target_order_and_benign_metadata_changes_are_ignored(self):
        other = member(ID_OTHER, "view")
        other.update(perm_type="container", name="before")
        group = member("group", "edit", "groupid")
        group["perm_type"] = None
        client = FakeClient([other, group])
        original = client.list_doc_permission_members

        def reorder(token):
            if client.list_count == 1:
                client.members[0]["name"] = "after"
                client.members[0]["avatar"] = "new benign metadata"
                client.members.reverse()
            return original(token)

        client.list_doc_permission_members = reorder
        client.ensure_doc_full_access(DOC, [ID_A])
        self.assertEqual(client.list_count, 2)

    def test_readback_requires_every_target_to_be_full_access(self):
        client = FakeClient([member(ID_A, "full_access"), member(ID_B, "edit")])
        client.apply_writes = False
        with self.assertRaises(api.FeishuError):
            client.ensure_doc_full_access(DOC, [ID_A, ID_B])
        self.assertEqual([c[0] for c in client.calls], ["GET", "PUT", "GET"])

    def test_public_link_policy_is_unchanged(self):
        client = api.FeishuClient("fake_app", "fake_secret")
        client.patch = MagicMock(return_value={})
        client.grant_public_access(DOC)
        client.patch.assert_called_once_with(
            f"/open-apis/drive/v2/permissions/{DOC}/public",
            {"link_share_entity": "anyone_readable"}, params={"type": "docx"})

    def test_disconnect_is_not_replayed_for_single_attempt_writes(self):
        for method in ("POST", "PUT"):
            for failure in (OSError, http.client.HTTPException):
                with self.subTest(method=method, failure=failure.__name__):
                    client = api.FeishuClient("fake_app", "fake_secret")
                    connection = MagicMock()
                    connection.getresponse.side_effect = failure("uncertain after send")
                    client._connection = MagicMock(return_value=connection)
                    with self.assertRaises(failure):
                        client._send(method, "/private", retry_on_disconnect=False)
                    self.assertEqual(connection.request.call_count, 1)
                    self.assertEqual(client._connection.call_count, 1)

    def test_default_transport_still_reconnects_for_other_operations(self):
        client = api.FeishuClient("fake_app", "fake_secret")
        first, second = MagicMock(), MagicMock()
        first.getresponse.side_effect = OSError("stale")
        second.getresponse.return_value.read.return_value = b'{"code": 0}'
        second.getresponse.return_value.status = 200
        client._connection = MagicMock(side_effect=[first, second])
        self.assertEqual(client._send("GET", "/example"), ({"code": 0}, 200))
        first.request.assert_called_once()
        second.request.assert_called_once()

    def test_api_failures_do_not_retry_single_attempt_writes(self):
        for status, code in ((500, 500), (200, 99991400)):
            with self.subTest(status=status, code=code):
                client = api.FeishuClient("fake_app", "fake_secret", retries=2)
                client.token = MagicMock(return_value="fake_token")
                client._send = MagicMock(return_value=({"code": code, "msg": "uncertain"}, status))
                with patch.object(api.time, "sleep") as sleep, self.assertRaises(api.FeishuError):
                    client.call("POST", "/private", {}, retry=False)
                client._send.assert_called_once()
                self.assertFalse(client._send.call_args.kwargs["retry_on_disconnect"])
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
