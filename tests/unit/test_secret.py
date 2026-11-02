"""C19 key material tests — resolution order, permissions, and the ephemeral default.

Importing the API must not create files, and a key that a roommate can read is not a
key. Both are easy to get wrong quietly, so both are tested.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest

from actionmesh.core.secret import ENV_FILE, ENV_KEY, KEY_BYTES, SecretError, load_key


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        for name in (ENV_KEY, ENV_FILE):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name


class TestResolutionOrder(_Env):
    def test_the_environment_value_wins_and_writes_nothing(self) -> None:
        os.environ[ENV_KEY] = "a-passphrase-that-is-not-the-key"
        key = load_key()
        self.assertTrue(key.durable)
        self.assertEqual(key.source, ENV_KEY)
        self.assertEqual(len(key.material), KEY_BYTES)
        self.assertEqual(os.listdir(self.dir), [])

    def test_the_same_passphrase_yields_the_same_key(self) -> None:
        os.environ[ENV_KEY] = "repeatable"
        self.assertEqual(load_key().material, load_key().material)

    def test_a_blank_passphrase_is_refused_rather_than_defaulted(self) -> None:
        os.environ[ENV_KEY] = "   "
        with self.assertRaises(SecretError):
            load_key()

    def test_with_nothing_configured_the_key_is_ephemeral_and_no_file_appears(self) -> None:
        key = load_key()
        self.assertFalse(key.durable)
        self.assertEqual(key.source, "ephemeral")
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(len(key.material), KEY_BYTES)

    def test_ephemeral_keys_differ_between_processes(self) -> None:
        # If the fallback were derived from the hostname or the clock, two fresh
        # installs would agree and a pairing would be portable between them.
        self.assertNotEqual(load_key().material, load_key().material)


class TestKeyFile(_Env):
    def _path(self, name: str = "secret.key") -> str:
        return os.path.join(self.dir, name)

    def test_a_file_is_created_once_with_private_permissions(self) -> None:
        path = self._path()
        first = load_key(path)
        self.assertTrue(first.durable)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(load_key(path).material, first.material)

    def test_the_parent_directory_is_made_private(self) -> None:
        path = os.path.join(self.dir, "nested", "secret.key")
        load_key(path)
        mode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        self.assertFalse(mode & 0o077)

    def test_a_group_readable_key_is_refused(self) -> None:
        path = self._path("shared.key")
        load_key(path)
        os.chmod(path, 0o640)
        with self.assertRaises(SecretError) as ctx:
            load_key(path)
        self.assertIn("group or other", str(ctx.exception))

    def test_a_world_readable_key_is_refused(self) -> None:
        path = self._path("public.key")
        load_key(path)
        os.chmod(path, 0o644)
        with self.assertRaises(SecretError):
            load_key(path)

    def test_a_truncated_key_is_refused_instead_of_regenerated(self) -> None:
        # Regenerating would silently unpair every device that ever paired, which is
        # a worse outcome than refusing to start.
        path = self._path("short.key")
        with open(path, "wb") as handle:
            handle.write(b"tiny")
        os.chmod(path, 0o600)
        with self.assertRaises(SecretError) as ctx:
            load_key(path)
        self.assertIn("shorter than", str(ctx.exception))

    def test_no_byte_value_is_trimmed_out_of_key_material(self) -> None:
        # A generated key is random bytes, so its first or last byte can be one that
        # `bytes.strip()` calls whitespace -- a newline, a space, a tab. When it was, the
        # file read back a byte short, "shorter than 32 bytes" was raised, and the server
        # could never start again on roughly one installation in twenty. Refusing to
        # regenerate is correct; refusing a *whole* key is the bug this pins out.
        filler = b"k" * (KEY_BYTES - 1)
        for value in range(256):
            for position, key in (
                ("first", bytes([value]) + filler),
                ("last", filler + bytes([value])),
            ):
                path = self._path(f"raw_{position}_{value}.key")
                with open(path, "wb") as handle:
                    handle.write(key)
                os.chmod(path, 0o600)
                with self.subTest(position=position, byte=value):
                    self.assertEqual(load_key(path).material, key)

    def test_a_hand_written_key_and_its_trailing_newline_are_a_whole_key(self) -> None:
        # The intent `strip()` was serving: a deployment that writes the key by hand
        # gets a newline at the end from its editor, and thirty-two characters plus
        # that newline still contains a full key.
        body = b"c" * KEY_BYTES
        path = self._path("handwritten.key")
        with open(path, "wb") as handle:
            handle.write(body + b"\n")
        os.chmod(path, 0o600)
        self.assertEqual(load_key(path).material, body)

    def test_the_file_variable_is_honoured(self) -> None:
        path = self._path("from-env.key")
        os.environ[ENV_FILE] = path
        key = load_key()
        self.assertTrue(key.durable)
        self.assertEqual(key.source, path)
        self.assertTrue(os.path.exists(path))

    def test_the_key_is_never_printed(self) -> None:
        key = load_key(self._path())
        self.assertNotIn(key.material.hex(), repr(key))
        self.assertIn(key.source, repr(key))


if __name__ == "__main__":
    unittest.main()
