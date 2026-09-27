"""Tests for attachment-root resolution after the NAS container migration.

The bot used to read attachments through a CIFS mount at /mnt/media and to
store that host path in message_logs. Inside the NAS compose network the same
tree is bind-mounted at /data/message_logs, so persisted values must be
re-anchored and a missing root must fail loudly instead of silently falling
back to container-local storage.
"""

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from igngbot_v3.storage import StorageHandler


class StoragePathResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp_dir, "message_logs")
        os.makedirs(os.path.join(self.root, "123456", "2026-09-19"), exist_ok=True)
        self.local_dir = os.path.join(self.tmp_dir, "runtime")
        os.makedirs(self.local_dir, exist_ok=True)
        self.sample = os.path.join(self.root, "123456", "2026-09-19", "a.webp")
        with open(self.sample, "wb") as handle:
            handle.write(b"webp")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _handler(self, **overrides):
        values = {
            "MESSAGE_ROOT": self.root,
            "LEGACY_PATH_PREFIXES": (
                "/mnt/media/message_logs",
                "/vol1/1000/IGNGbot/message_logs",
                "/data/message_logs",
            ),
            "STORAGE_REQUIRE_MOUNT": False,
            "LOCAL_STORAGE": self.local_dir,
            "MAX_FILE_SIZE": 50 * 1024 * 1024,
        }
        values.update(overrides)
        return StorageHandler(SimpleNamespace(**values))

    def test_current_container_path_is_used_as_is(self):
        storage = self._handler()
        storage.check_available()
        self.assertEqual(storage.resolve_path(self.sample), self.sample)

    def test_legacy_cifs_path_is_reanchored(self):
        storage = self._handler()
        storage.check_available()
        legacy = "/mnt/media/message_logs/123456/2026-09-19/a.webp"
        self.assertEqual(storage.resolve_path(legacy), self.sample)

    def test_legacy_nas_host_path_is_reanchored(self):
        storage = self._handler()
        storage.check_available()
        legacy = "/vol1/1000/IGNGbot/message_logs/123456/2026-09-19/a.webp"
        self.assertEqual(storage.resolve_path(legacy), self.sample)

    def test_relative_path_is_anchored_to_root(self):
        storage = self._handler()
        storage.check_available()
        self.assertEqual(storage.resolve_path("123456/2026-09-19/a.webp"), self.sample)

    def test_missing_legacy_path_still_reanchors(self):
        # A file pruned from disk must still map to the canonical location so
        # callers see a consistent path rather than the stale host prefix.
        storage = self._handler()
        storage.check_available()
        legacy = "/mnt/media/message_logs/123456/2026-09-19/gone.webp"
        self.assertEqual(
            storage.resolve_path(legacy),
            os.path.join(self.root, "123456", "2026-09-19", "gone.webp"),
        )

    def test_empty_path_returns_empty_string(self):
        storage = self._handler()
        storage.check_available()
        self.assertEqual(storage.resolve_path(None), "")
        self.assertEqual(storage.resolve_path("   "), "")

    def test_to_stored_path_keeps_message_logs_marker(self):
        # The IGNG site slices on the /message_logs/ marker to build media URLs,
        # so the persisted value must always carry it.
        storage = self._handler()
        storage.check_available()
        stored = storage.to_stored_path(self.sample)
        self.assertEqual(stored, self.sample)
        self.assertIn("/message_logs/", stored)

    def test_to_stored_path_normalises_legacy_and_relative(self):
        storage = self._handler()
        storage.check_available()
        self.assertEqual(
            storage.to_stored_path("/mnt/media/message_logs/123456/2026-09-19/a.webp"),
            self.sample,
        )
        self.assertEqual(
            storage.to_stored_path("123456/2026-09-19/a.webp"),
            self.sample,
        )

    def test_check_available_is_idempotent(self):
        storage = self._handler()
        self.assertTrue(storage.check_available())
        root_before = storage._storage_root
        self.assertTrue(storage.check_available())
        self.assertEqual(storage._storage_root, root_before)

    def test_missing_root_raises_when_mount_required(self):
        missing = os.path.join(self.tmp_dir, "does_not_exist")
        storage = self._handler(MESSAGE_ROOT=missing, STORAGE_REQUIRE_MOUNT=True)
        with self.assertRaises(RuntimeError):
            storage.check_available()

    def test_missing_root_falls_back_when_allowed(self):
        missing = os.path.join(self.tmp_dir, "does_not_exist")
        storage = self._handler(MESSAGE_ROOT=missing, STORAGE_REQUIRE_MOUNT=False)
        self.assertTrue(storage.check_available())
        self.assertEqual(
            storage._storage_root, os.path.join(self.local_dir, "attachments")
        )

    def test_legacy_nas_mount_path_attribute_still_accepted(self):
        # Deployment-time compatibility: an old .env only provides NAS_MOUNT_PATH.
        config = SimpleNamespace(
            NAS_MOUNT_PATH=self.root,
            LEGACY_PATH_PREFIXES=("/mnt/media/message_logs",),
            STORAGE_REQUIRE_MOUNT=False,
            LOCAL_STORAGE=self.local_dir,
            MAX_FILE_SIZE=50 * 1024 * 1024,
        )
        storage = StorageHandler(config)
        self.assertTrue(storage.check_available())
        self.assertEqual(storage._storage_root, self.root)


if __name__ == "__main__":
    unittest.main()
