"""Folder recovery uses an explicit in-memory Drive API; no Google mutation."""
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from drivedrop.common import ApiError
from drivedrop.drive import API, FOLDER_MIME, GoogleDrive
from tests.fakes import MemoryStore


class FolderAPI:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.files = {"inbox": {"id": "inbox", "name": "DriveDrop Inbox",
            "mimeType": FOLDER_MIME, "trashed": False, "parents": ["root"],
            "appProperties": {"drivedropRoot": "v1"}}}
        self.created = []
        self.patches = []
        self.calls = []
        self.counter = 0
        self.fail_before = False
        self.fail_after = False
        self.fail_lookup = False
        self.hide_lookup_after_failure = False
        self.page_size = 100
        self.incomplete = False

    def api(self, method, url, body=None):
        self.calls.append((method, url))
        parsed = urlsplit(url)
        base_path = urlsplit(API).path
        query = parse_qs(parsed.query)
        if method == "GET" and parsed.path == base_path + "/generateIds":
            self.counter += 1
            return {"ids": ["generated-" + str(self.counter)]}
        if method == "GET" and parsed.path == base_path:
            q = query.get("q", [""])[0]
            parent = re.search(r"'([^']+)' in parents", q).group(1)
            props = dict(re.findall(r"key='([^']+)' and value='([^']+)'", q))
            matches = [copy.deepcopy(row) for row in self.files.values()
                if row.get("parents") == [parent] and row.get("trashed") is False
                and row.get("mimeType") == FOLDER_MIME
                and all(row.get("appProperties", {}).get(k) == v for k, v in props.items())]
            offset = int(query.get("pageToken", ["0"])[0])
            rows = matches[offset:offset+self.page_size]
            out = {"files": [{"id": row["id"]} for row in rows], "incompleteSearch": self.incomplete}
            if offset + self.page_size < len(matches):
                out["nextPageToken"] = str(offset + self.page_size)
            return out
        if method == "GET" and parsed.path.startswith(base_path + "/"):
            if self.fail_lookup:
                self.fail_lookup = False
                raise ApiError("SIMULATED lookup network failure", 503)
            file_id = unquote(parsed.path[len(base_path)+1:])
            if file_id not in self.files:
                raise ApiError("SIMULATED missing file", 404)
            return copy.deepcopy(self.files[file_id])
        if method == "POST" and parsed.path == base_path:
            # The ID must already exist in the durable journal, before the POST.
            journal = json.loads((self.state_dir / "drive-folders.json").read_text("utf-8"))
            records = list(journal["special"].values())
            for channel in journal["channels"].values():
                records.extend(channel.values())
            for article_tree in journal.get("articles", {}).values():
                records.extend(article_tree.values())
            assert any(row["id"] == body["id"] and row["state"] == "reserved" for row in records)
            self.created.append(copy.deepcopy(body))
            if self.fail_before:
                self.fail_before = False
                raise ApiError("SIMULATED request did not reach Drive", 503)
            if body["id"] in self.files:
                raise ApiError("SIMULATED existing ID", 409)
            self.files[body["id"]] = dict(copy.deepcopy(body), trashed=False)
            if self.fail_after:
                self.fail_after = False
                self.fail_lookup = self.hide_lookup_after_failure
                raise ApiError("SIMULATED response lost after creation", 503)
            return {"id": body["id"]}
        if method == "PATCH":
            file_id = unquote(parsed.path[len(base_path)+1:])
            assert set(body) == {"name"}, "Renaming must not move or repurpose a folder"
            self.patches.append((file_id, copy.deepcopy(body)))
            self.files[file_id].update(body)
            return copy.deepcopy(self.files[file_id])
        raise AssertionError((method, url, body))


class DriveFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drivedrop-folders-")
        self.directory = Path(self.temp.name)
        self.remote = FolderAPI(self.directory)
        self.channel = uuid.uuid4().hex
        self.drive = self.new_drive()

    def tearDown(self):
        self.temp.cleanup()

    def new_drive(self):
        drive = GoogleDrive(self.directory, MemoryStore())
        drive.api = self.remote.api
        drive.ensure_folder = lambda: "inbox"
        drive.folder_id = "inbox"
        return drive

    def ensure(self, name="Tên Kênh", code="ABC"):
        return self.drive.ensure_channel_folders(self.channel, name, code)

    def test_create_and_restart_keep_three_ids(self):
        first = self.ensure()
        self.assertEqual(set(first), {"channel", "ANH", "VIDEO"})
        self.assertEqual(self.remote.files[first["channel"]]["parents"], ["inbox"])
        self.assertEqual(self.remote.files[first["ANH"]]["parents"], [first["channel"]])
        self.drive = self.new_drive()
        self.assertEqual(self.ensure(), first)
        self.assertEqual(len(self.remote.created), 3)

    def test_rename_same_uuid_keeps_children_and_patches_name_only(self):
        first = self.ensure()
        self.assertEqual(self.ensure("Tên mới", "XYZ"), first)
        self.assertEqual(self.remote.patches, [(first["channel"], {"name": "Tên mới - XYZ"})])
        self.assertEqual(len(self.remote.created), 3)

    def test_lost_create_response_recovers_same_id_immediately(self):
        self.remote.fail_after = True
        self.ensure()
        self.assertEqual(len(self.remote.created), 3)

    def test_lost_create_and_lookup_response_recovers_after_restart(self):
        self.remote.fail_after = True
        self.remote.hide_lookup_after_failure = True
        with self.assertRaises(ApiError):
            self.ensure()
        first_id = self.remote.created[0]["id"]
        self.drive = self.new_drive()
        self.assertEqual(self.ensure()["channel"], first_id)
        self.assertEqual(len(self.remote.created), 3)

    def test_request_not_received_retries_reserved_id(self):
        self.remote.fail_before = True
        with self.assertRaises(ApiError):
            self.ensure()
        reserved_id = self.remote.created[0]["id"]
        self.drive = self.new_drive()
        self.assertEqual(self.ensure()["channel"], reserved_id)
        self.assertEqual(self.remote.created[1]["id"], reserved_id)
        self.assertEqual(self.remote.counter, 3)

    def test_journal_loss_recovers_by_private_properties(self):
        first = self.ensure()
        (self.directory / "drive-folders.json").unlink()
        self.drive = self.new_drive()
        self.assertEqual(self.ensure(), first)
        self.assertEqual(len(self.remote.created), 3)

    def test_moved_channel_replaced_without_moving_old_tree(self):
        first = self.ensure()
        self.remote.files[first["channel"]]["parents"] = ["elsewhere"]
        second = self.ensure()
        self.assertTrue(all(first[key] != second[key] for key in first))
        self.assertEqual(self.remote.files[first["channel"]]["parents"], ["elsewhere"])
        self.assertEqual(self.remote.files[first["ANH"]]["parents"], [first["channel"]])
        self.assertEqual(self.remote.patches, [])

    def test_trashed_child_replaced_independently(self):
        first = self.ensure()
        self.remote.files[first["ANH"]]["trashed"] = True
        second = self.ensure()
        self.assertNotEqual(first["ANH"], second["ANH"])
        self.assertEqual(first["channel"], second["channel"])
        self.assertEqual(first["VIDEO"], second["VIDEO"])
        self.assertTrue(self.remote.files[first["ANH"]]["trashed"])

    def test_missing_ready_id_uses_replacement_not_consumed_id(self):
        first = self.ensure()
        del self.remote.files[first["VIDEO"]]
        second = self.ensure()
        self.assertNotEqual(first["VIDEO"], second["VIDEO"])
        self.assertEqual(first["ANH"], second["ANH"])

    def test_wrong_app_marker_is_not_adopted_or_renamed(self):
        first = self.ensure()
        self.remote.files[first["channel"]]["appProperties"] = {}
        second = self.ensure("New Name")
        self.assertNotEqual(first["channel"], second["channel"])
        self.assertEqual(self.remote.files[first["channel"]]["name"], "Tên Kênh - ABC")

    def test_duplicate_on_later_page_fails_without_mutation(self):
        first = self.ensure()
        duplicate = copy.deepcopy(self.remote.files[first["channel"]])
        duplicate["id"] = "duplicate-channel"
        self.remote.files[duplicate["id"]] = duplicate
        self.remote.page_size = 1
        with self.assertRaises(ApiError) as caught:
            self.ensure("Another Name")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.remote.created), 3)
        self.assertEqual(self.remote.patches, [])

    def test_incomplete_search_never_creates(self):
        self.remote.incomplete = True
        with self.assertRaises(ApiError) as caught:
            self.ensure()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.remote.created, [])

    def test_unclassified_is_direct_single_folder_and_recovers(self):
        first = self.drive.ensure_unclassified_folder()
        self.drive = self.new_drive()
        self.assertEqual(self.drive.ensure_unclassified_folder(), first)
        self.assertEqual(self.remote.files[first]["name"], "KÊNH")
        self.assertEqual(self.remote.files[first]["parents"], ["inbox"])
        self.assertEqual(len(self.remote.created), 1)

    def test_invalid_inbox_marker_and_invalid_uuid_fail_before_create(self):
        with self.assertRaises(ApiError):
            self.drive.ensure_channel_folders("not-a-uuid", "Name", "CODE")
        self.remote.files["inbox"]["appProperties"] = {}
        with self.assertRaises(ApiError):
            self.ensure()
        self.assertEqual(self.remote.created, [])

    def assert_direct_reads(self, ids):
        self.assertEqual(len(self.remote.calls), len(ids))
        for (method, url), file_id in zip(self.remote.calls, ids):
            self.assertEqual(method, "GET")
            self.assertEqual(urlsplit(url).path, urlsplit(API).path + "/" + file_id)

    def test_healthy_resolver_reads_only_selected_branch_without_writes(self):
        first = self.ensure()
        journal_path = self.directory / "drive-folders.json"
        journal = journal_path.read_bytes()
        self.drive = self.new_drive()
        for kind in ("ANH", "VIDEO"):
            with self.subTest(kind=kind):
                self.remote.calls.clear()
                with patch.object(self.drive, "_save_folder_journal", side_effect=AssertionError("No steady-state writes")):
                    self.assertEqual(self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", kind), first[kind])
                self.assert_direct_reads(["inbox", first["channel"], first[kind]])
                self.assertEqual(journal_path.read_bytes(), journal)

    def test_healthy_fallback_resolver_has_only_two_reads(self):
        first = self.drive.ensure_unclassified_folder()
        self.remote.calls.clear()
        with patch.object(self.drive, "_save_folder_journal", side_effect=AssertionError("No steady-state writes")):
            self.assertEqual(self.drive.resolve_unclassified_folder(), first)
        self.assert_direct_reads(["inbox", first])

    def test_resolver_moved_channel_replaces_tree_and_preserves_old_parent(self):
        first = self.ensure()
        self.remote.files[first["channel"]]["parents"] = ["elsewhere"]
        resolved = self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "ANH")
        self.assertNotEqual(resolved, first["ANH"])
        self.assertEqual(self.remote.files[first["channel"]]["parents"], ["elsewhere"])
        self.assertEqual(self.remote.files[first["ANH"]]["parents"], [first["channel"]])
        new_channel = self.remote.files[resolved]["parents"][0]
        self.assertEqual(self.remote.files[new_channel]["parents"], ["inbox"])

    def test_resolver_unhealthy_child_recovers_by_existing_safe_path(self):
        first = self.ensure()
        for changed in ({"trashed": True}, {"parents": ["elsewhere"]},
                        {"mimeType": "image/jpeg"}, {"appProperties": {}}):
            with self.subTest(changed=changed):
                current = self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "ANH")
                self.remote.files[current].update(copy.deepcopy(changed))
                resolved = self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "ANH")
                self.assertNotEqual(resolved, current)
                self.assertEqual(self.remote.files[resolved]["parents"], [first["channel"]])
                self.assertEqual(self.remote.files[resolved]["mimeType"], FOLDER_MIME)
                self.assertEqual(self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "VIDEO"), first["VIDEO"])

    def test_resolver_rename_repairs_same_ids_and_name_only(self):
        first = self.ensure()
        self.assertEqual(self.drive.resolve_upload_folder(self.channel, "Tên mới", "XYZ", "ANH"), first["ANH"])
        self.assertEqual(self.remote.patches, [(first["channel"], {"name": "Tên mới - XYZ"})])
        self.remote.files[first["ANH"]]["name"] = "Changed outside app"
        self.assertEqual(self.drive.resolve_upload_folder(self.channel, "Tên mới", "XYZ", "ANH"), first["ANH"])
        self.assertEqual(self.remote.patches[-1], (first["ANH"], {"name": "ANH"}))
        self.assertEqual(len(self.remote.created), 3)

    def test_resolver_missing_journal_recovers_then_returns_to_three_reads(self):
        first = self.ensure()
        (self.directory / "drive-folders.json").unlink()
        self.drive = self.new_drive()
        self.assertEqual(self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "VIDEO"), first["VIDEO"])
        self.assertEqual(len(self.remote.created), 3)
        self.remote.calls.clear()
        self.assertEqual(self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "VIDEO"), first["VIDEO"])
        self.assert_direct_reads(["inbox", first["channel"], first["VIDEO"]])

    def test_resolver_missing_ready_child_creates_new_id(self):
        first = self.ensure()
        del self.remote.files[first["ANH"]]
        resolved = self.drive.resolve_upload_folder(self.channel, "Tên Kênh", "ABC", "ANH")
        self.assertNotEqual(resolved, first["ANH"])
        self.assertEqual(len(self.remote.created), 4)

    def test_fallback_resolver_repairs_renamed_and_moved_folder(self):
        first = self.drive.ensure_unclassified_folder()
        self.remote.files[first]["name"] = "Renamed"
        self.assertEqual(self.drive.resolve_unclassified_folder(), first)
        self.assertEqual(self.remote.patches[-1], (first, {"name": "KÊNH"}))
        self.remote.files[first]["parents"] = ["elsewhere"]
        resolved = self.drive.resolve_unclassified_folder()
        self.assertNotEqual(resolved, first)
        self.assertEqual(self.remote.files[first]["parents"], ["elsewhere"])
        self.assertEqual(self.remote.files[resolved]["parents"], ["inbox"])

    def test_resolver_rejects_invalid_kind_before_api(self):
        with self.assertRaises(ApiError) as caught:
            self.drive.resolve_upload_folder(self.channel, "Name", "ABC", "OTHER")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.remote.calls, [])

    def resolve_article(self, article="KL1_001", subfolders=()):
        return self.drive.resolve_article_folder(self.channel, "Tên Kênh", "ABC", article, subfolders)

    def test_video_and_image_article_identities_are_separate_and_survive_restart(self):
        roots = self.ensure()
        image = self.resolve_article()
        video = self.drive.resolve_article_folder(self.channel, "Tên Kênh", "ABC", "KL1_001", kind="VIDEO")
        self.assertNotEqual(image, video)
        self.assertEqual(self.remote.files[image]["parents"], [roots["ANH"]])
        self.assertEqual(self.remote.files[video]["parents"], [roots["VIDEO"]])
        self.drive = self.new_drive()
        self.assertEqual(self.resolve_article(), image)
        self.assertEqual(self.drive.resolve_article_folder(self.channel, "Tên Kênh", "ABC", "KL1_001", kind="VIDEO"), video)
        journal_path = self.directory / "drive-folders.json"
        journal = json.loads(journal_path.read_text("utf-8"))
        journal.pop("articles")
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        self.drive = self.new_drive()
        self.assertEqual(self.resolve_article(), image)
        self.assertEqual(self.drive.resolve_article_folder(self.channel, "Tên Kênh", "ABC", "KL1_001", kind="VIDEO"), video)

    def test_video_article_repair_after_move_and_lost_response(self):
        roots = self.ensure()
        self.remote.fail_after = True
        resolve = lambda: self.drive.resolve_article_folder(self.channel, "Tên Kênh", "ABC", "TH9_001", ["cuts"], kind="VIDEO")
        first = resolve()
        self.drive = self.new_drive()
        self.assertEqual(resolve(), first)
        article = self.remote.files[first]["parents"][0]
        self.remote.files[article]["parents"] = ["elsewhere"]
        second = resolve()
        self.assertNotEqual(first, second)
        new_article = self.remote.files[second]["parents"][0]
        self.assertEqual(self.remote.files[new_article]["parents"], [roots["VIDEO"]])

    def test_article_albums_and_nested_same_named_folders_have_distinct_ids(self):
        first = self.resolve_article("KL1_001", ("Ảnh chọn",))
        second = self.resolve_article("KL1_002", ("Ảnh chọn",))
        self.assertNotEqual(first, second)
        first_album = self.remote.files[first]["parents"][0]
        second_album = self.remote.files[second]["parents"][0]
        self.assertNotEqual(first_album, second_album)
        self.assertEqual(self.remote.files[first_album]["name"], "KL1_001")
        self.assertEqual(self.remote.files[second_album]["name"], "KL1_002")
        image_root = self.remote.files[first_album]["parents"][0]
        self.assertEqual(self.remote.files[image_root]["name"], "ANH")
        self.assertEqual(self.remote.files[second_album]["parents"], [image_root])
        self.assertNotEqual(self.remote.files[first]["appProperties"]["drivedropPath"],
                            self.remote.files[second]["appProperties"]["drivedropPath"])

    def test_two_hundred_album_images_use_same_id_with_only_chain_reads(self):
        first = self.resolve_article(subfolders=("Bản chọn",))
        article = self.remote.files[first]["parents"][0]
        images = self.remote.files[article]["parents"][0]
        channel = self.remote.files[images]["parents"][0]
        journal = (self.directory / "drive-folders.json").read_bytes()
        self.drive = self.new_drive()
        with patch.object(self.drive, "_save_folder_journal", side_effect=AssertionError("No steady-state writes")):
            for _ in range(200):
                self.remote.calls.clear()
                self.assertEqual(self.resolve_article(subfolders=("Bản chọn",)), first)
                self.assert_direct_reads(["inbox", channel, images, article, first])
        self.assertEqual((self.directory / "drive-folders.json").read_bytes(), journal)
        self.assertEqual(len(self.remote.created), 5)

    def test_article_create_response_loss_recovers_without_duplicate(self):
        self.ensure()
        self.remote.fail_after = True
        first = self.resolve_article()
        self.drive = self.new_drive()
        self.assertEqual(self.resolve_article(), first)
        self.assertEqual(len(self.remote.created), 4)

    def test_article_lost_create_and_lookup_recovers_reserved_id_after_restart(self):
        self.ensure()
        self.remote.fail_after = True
        self.remote.hide_lookup_after_failure = True
        with self.assertRaises(ApiError):
            self.resolve_article()
        first_id = self.remote.created[-1]["id"]
        self.drive = self.new_drive()
        self.assertEqual(self.resolve_article(), first_id)
        self.assertEqual(len(self.remote.created), 4)

    def test_article_request_not_received_retries_durable_id(self):
        self.ensure()
        self.remote.fail_before = True
        with self.assertRaises(ApiError):
            self.resolve_article()
        first_id = self.remote.created[-1]["id"]
        self.drive = self.new_drive()
        self.assertEqual(self.resolve_article(), first_id)
        self.assertEqual(self.remote.counter, 4)

    def test_article_missing_legacy_journal_map_recovers_private_identity(self):
        first = self.resolve_article(subfolders=("Ảnh",))
        journal_path = self.directory / "drive-folders.json"
        journal = json.loads(journal_path.read_text("utf-8"))
        journal.pop("articles")
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        self.drive = self.new_drive()
        self.assertEqual(self.resolve_article(subfolders=("Ảnh",)), first)
        self.assertEqual(len(self.remote.created), 5)

    def test_article_ancestor_move_replaces_descendants_without_moving_old_tree(self):
        first = self.resolve_article(subfolders=("Ảnh", "Đã chọn"))
        nested = self.remote.files[first]["parents"][0]
        article = self.remote.files[nested]["parents"][0]
        old_images = self.remote.files[article]["parents"][0]
        self.remote.files[article]["parents"] = ["elsewhere"]
        second = self.resolve_article(subfolders=("Ảnh", "Đã chọn"))
        self.assertNotEqual(second, first)
        self.assertEqual(self.remote.files[article]["parents"], ["elsewhere"])
        self.assertEqual(self.remote.files[nested]["parents"], [article])
        new_nested = self.remote.files[second]["parents"][0]
        new_article = self.remote.files[new_nested]["parents"][0]
        self.assertEqual(self.remote.files[new_article]["parents"], [old_images])
        self.assertEqual(self.remote.patches, [])

    def test_article_channel_move_replaces_full_branch(self):
        first = self.resolve_article()
        images = self.remote.files[first]["parents"][0]
        channel = self.remote.files[images]["parents"][0]
        self.remote.files[channel]["parents"] = ["elsewhere"]
        second = self.resolve_article()
        self.assertNotEqual(second, first)
        self.assertNotEqual(self.remote.files[second]["parents"], [images])
        self.assertEqual(self.remote.files[first]["parents"], [images])

    def test_article_rename_keeps_id_and_repairs_name_only(self):
        first = self.resolve_article(subfolders=("Ảnh",))
        self.remote.files[first]["name"] = "Renamed"
        self.assertEqual(self.resolve_article(subfolders=("Ảnh",)), first)
        self.assertEqual(self.remote.patches, [(first, {"name": "Ảnh"})])
        self.assertEqual(len(self.remote.created), 5)

    def test_article_matching_name_without_private_identity_is_not_adopted(self):
        roots = self.ensure()
        self.remote.files["unmanaged-album"] = {"id": "unmanaged-album", "name": "KL1_001",
            "mimeType": FOLDER_MIME, "parents": [roots["ANH"]], "trashed": False,
            "appProperties": {}}
        created = self.resolve_article()
        self.assertNotEqual(created, "unmanaged-album")
        self.assertEqual(self.remote.files["unmanaged-album"]["appProperties"], {})
        self.assertEqual(self.remote.patches, [])

    def test_article_trashed_or_deleted_ready_ids_get_replacements(self):
        first = self.resolve_article()
        self.remote.files[first]["trashed"] = True
        second = self.resolve_article()
        self.assertNotEqual(second, first)
        del self.remote.files[second]
        third = self.resolve_article()
        self.assertNotEqual(third, second)
        self.assertTrue(self.remote.files[first]["trashed"])
        self.assertEqual(len(self.remote.created), 6)

    def test_article_duplicate_recovery_fails_without_arbitrary_choice(self):
        first = self.resolve_article()
        duplicate = copy.deepcopy(self.remote.files[first])
        duplicate["id"] = "duplicate-article"
        self.remote.files[duplicate["id"]] = duplicate
        self.remote.files[first]["name"] = "Trigger repair"
        self.remote.page_size = 1
        with self.assertRaises(ApiError) as caught:
            self.resolve_article()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.remote.created), 4)
        self.assertEqual(self.remote.patches, [])

    def test_article_rejects_unsafe_paths_before_api(self):
        invalid = [("", ()), ("..", ()), ("A/B", ()), ("A\\B", ()),
                   ("A\nB", ()), ("A" * 201, ()), ("A", ("..",)),
                   ("A", "B"), ("A", ("B",) * 33),
                   ("A", ("Ả" * 200,) * 11)]
        for article, subfolders in invalid:
            with self.subTest(article=article, subfolders=subfolders):
                with self.assertRaises(ApiError) as caught:
                    self.resolve_article(article, subfolders)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.remote.calls, [])


if __name__ == "__main__":
    unittest.main()
