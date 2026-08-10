"""Conformance tests for the wire protocol, layout validation and action dispatch.

Runs with no third-party dependencies, so it works on a bare Python install:

    python tools/protocol_test.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))

from deckhost import protocol  # noqa: E402
from deckhost.actions import ActionRunner  # noqa: E402
from deckhost.assets import STAMP_FILE, asset_stamp, read_stamp, write_stamp  # noqa: E402
from deckhost.config import (  # noqa: E402
    ICON_NAMES,
    ConfigError,
    DeckConfig,
    is_device_local,
)
from deckhost.link import LinkError, SerialLink, SimulatedLink  # noqa: E402
from deckhost.main import DeckHost  # noqa: E402
from deckhost import main as deckhost_main  # noqa: E402
from deckhost.stats import StatsCollector, _find_lhm_cpu_temp  # noqa: E402


class FrameReaderTests(unittest.TestCase):
    def test_single_frame(self):
        reader = protocol.FrameReader()
        frames = list(reader.feed(b'{"t":"ping","seq":1}\n'))
        self.assertEqual(frames, [{"t": "ping", "seq": 1}])

    def test_split_across_reads(self):
        # Serial ports split writes constantly; the reader must not care where.
        reader = protocol.FrameReader()
        self.assertEqual(list(reader.feed(b'{"t":"pi')), [])
        self.assertEqual(list(reader.feed(b'ng","seq":7}\n')), [{"t": "ping", "seq": 7}])

    def test_multiple_frames_in_one_chunk(self):
        reader = protocol.FrameReader()
        frames = list(reader.feed(b'{"t":"a"}\n{"t":"b"}\n{"t":"c"}\n'))
        self.assertEqual([f["t"] for f in frames], ["a", "b", "c"])

    def test_garbage_line_is_skipped_not_fatal(self):
        reader = protocol.FrameReader()
        frames = list(reader.feed(b'not json\n{"t":"ok"}\n'))
        self.assertEqual(frames, [{"t": "ok"}])
        self.assertEqual(reader.dropped_lines, 1)

    def test_blank_lines_ignored(self):
        reader = protocol.FrameReader()
        frames = list(reader.feed(b'\n\n{"t":"ok"}\n\n'))
        self.assertEqual(frames, [{"t": "ok"}])

    def test_crlf_tolerated(self):
        reader = protocol.FrameReader()
        self.assertEqual(list(reader.feed(b'{"t":"ok"}\r\n')), [{"t": "ok"}])

    def test_oversized_line_resynchronises(self):
        # Without this guard a desynchronised stream grows the buffer until memory runs out.
        reader = protocol.FrameReader()
        list(reader.feed(b"x" * (protocol.MAX_LINE_BYTES + 10)))
        self.assertEqual(reader.dropped_lines, 1)
        self.assertEqual(list(reader.feed(b'{"t":"ok"}\n')), [{"t": "ok"}])

    def test_non_object_frame_rejected(self):
        reader = protocol.FrameReader()
        frames = list(reader.feed(b'[1,2,3]\n{"t":"ok"}\n'))
        self.assertEqual(frames, [{"t": "ok"}])

    def test_roundtrip(self):
        reader = protocol.FrameReader()
        original = protocol.stats({"cpu": 42.5, "mem": 61.0})
        decoded = list(reader.feed(protocol.encode(original)))
        self.assertEqual(decoded, [original])

    def test_encode_ends_with_newline(self):
        self.assertTrue(protocol.encode({"t": "x"}).endswith(b"\n"))


class ProtoVersionTests(unittest.TestCase):
    def test_matching_version_accepted(self):
        protocol.check_proto(protocol.hello())

    def test_mismatch_raises(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_proto({"t": "hello", "proto": protocol.PROTO_VERSION + 1})

    def test_missing_version_raises(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.check_proto({"t": "hello"})


class LocalityTests(unittest.TestCase):
    """These cases must agree with Action::isLocal() in deck_config.cpp."""

    def test_hid_is_local(self):
        self.assertTrue(is_device_local({"type": "hid", "keys": ["CTRL", "c"]}))

    def test_media_and_page_are_local(self):
        self.assertTrue(is_device_local({"type": "media", "key": "mute"}))
        self.assertTrue(is_device_local({"type": "page", "target": "numpad"}))

    def test_launch_is_not_local(self):
        self.assertFalse(is_device_local({"type": "launch", "target": "code"}))

    def test_all_local_sequence_is_local(self):
        action = {
            "type": "seq",
            "steps": [
                {"type": "hid", "keys": ["CTRL", "c"]},
                {"type": "delay", "ms": 50},
                {"type": "hid", "keys": ["CTRL", "v"]},
            ],
        }
        self.assertTrue(is_device_local(action))

    def test_mixed_sequence_is_not_local(self):
        # One agent step anywhere makes the whole button need the agent.
        action = {
            "type": "seq",
            "steps": [
                {"type": "hid", "keys": ["CTRL", "c"]},
                {"type": "launch", "target": "code"},
            ],
        }
        self.assertFalse(is_device_local(action))

    def test_unknown_type_is_not_local(self):
        self.assertFalse(is_device_local({"type": "nonsense"}))

    def test_theme_is_local(self):
        # Theme switching runs on the device so the deck can be restyled with the agent
        # closed, same as the ten-key. ActionType::Theme is in isLocal()'s local list.
        self.assertTrue(is_device_local({"type": "theme", "target": "next"}))
        self.assertTrue(is_device_local({"type": "theme", "target": "Midnight"}))


class ThemeParsingTests(unittest.TestCase):
    """Mirrors DeckConfig::parse()'s theme handling in deck_config.cpp."""

    def _load(self, tmp: Path, data: dict) -> DeckConfig:
        path = tmp / "deck.json"
        payload = {"rev": 1, "pages": [{"id": "p", "buttons": []}]}
        payload.update(data)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return DeckConfig.load(path)

    def test_legacy_single_theme_becomes_one_element_list(self):
        # Old deck.json files must keep working untouched — there is no migration step.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp), {"theme": {"bg": "#000000"}})
            self.assertEqual(config.theme_names(), ["Theme 1"])

    def test_unnamed_themes_get_positional_names(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(
                Path(tmp), {"themes": [{"bg": "#000000"}, {"name": "Ember"}]}
            )
            self.assertEqual(config.theme_names(), ["Theme 1", "Ember"])

    def test_no_theme_block_at_all_is_valid(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp), {})
            self.assertEqual(config.theme_names(), [])

    def test_theme_action_to_unknown_theme_rejected(self):
        # Same class of bug as a typo'd page target: a tile that looks fine and does nothing.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(
                json.dumps(
                    {
                        "rev": 1,
                        "themes": [{"name": "Midnight"}],
                        "pages": [
                            {
                                "id": "p",
                                "buttons": [
                                    {
                                        "id": "b",
                                        "action": {"type": "theme", "target": "Mignight"},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as ctx:
                DeckConfig.load(path)
            self.assertIn("unknown theme", str(ctx.exception))

    def test_theme_keywords_are_always_valid(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(
                json.dumps(
                    {
                        "rev": 1,
                        "themes": [{"name": "Midnight"}],
                        "pages": [
                            {
                                "id": "p",
                                "buttons": [
                                    {"id": "n", "action": {"type": "theme", "target": "next"}},
                                    {"id": "p2", "action": {"type": "theme", "target": "prev"}},
                                    {"id": "e", "action": {"type": "theme"}},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(len(DeckConfig.load(path).buttons), 3)

    def test_malformed_colour_rejected(self):
        # The firmware keeps its default for anything it cannot parse, so without this check a
        # typo looks exactly like "I changed the colour and nothing happened".
        import tempfile

        for bad in ("#40261731", "#c9a 488", "1b212", "rebeccapurple", 123):
            with self.subTest(value=bad), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ConfigError) as ctx:
                    self._load(Path(tmp), {"themes": [{"name": "T", "tile": bad}]})
                self.assertIn("six hex digits", str(ctx.exception))

    def test_colour_accepts_both_hash_forms(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(
                Path(tmp), {"themes": [{"name": "T", "bg": "#1b2129", "tile": "1B2129"}]}
            )
            self.assertEqual(config.theme_names(), ["T"])

    def test_wrong_type_for_numeric_token_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                self._load(Path(tmp), {"themes": [{"name": "T", "tile_opa": "70"}]})
            self.assertIn("whole number", str(ctx.exception))

    def test_out_of_range_numeric_token_allowed(self):
        # The firmware clamps rather than ignoring, so this is defined behaviour, not a typo.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(
                Path(tmp), {"themes": [{"name": "T", "tile_opa": 140, "radius": -3}]}
            )
            self.assertEqual(config.theme_names(), ["T"])

    def test_unknown_display_mode_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                self._load(Path(tmp), {"themes": [{"name": "T", "display": "icons"}]})
            self.assertIn("display", str(ctx.exception))

    def test_empty_display_is_the_written_form_of_unset(self):
        # So every theme can carry the same keys. Deleting the line was once the only way to
        # get the default, which left themes in a file looking like different kinds of object.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp), {"themes": [{"name": "T", "display": ""}]})
            self.assertEqual(config.theme_names(), ["T"])

    def test_start_theme_must_exist(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(
                json.dumps(
                    {
                        "rev": 1,
                        "themes": [{"name": "Midnight"}],
                        "settings": {"theme": "Nope"},
                        "pages": [{"id": "p", "buttons": []}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as ctx:
                DeckConfig.load(path)
            self.assertIn("settings.theme", str(ctx.exception))


class ConfigValidationTests(unittest.TestCase):
    def _write(self, tmp: Path, data: dict) -> Path:
        path = tmp / "deck.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_shipped_deck_json_is_valid(self):
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        self.assertGreater(len(config.buttons), 0)
        # Deliberately not asserting a specific rev: it increments every time the layout is
        # edited, so pinning it makes this test fail on ordinary use rather than on a defect.
        self.assertIsInstance(config.rev, int)
        self.assertGreater(config.rev, 0)

    def test_unknown_page_type_rejected(self):
        """A typo'd type silently becomes a grid on the device, so it must not reach it.

        `DeckConfig::parse()` ends its strcmp chain with an unconditional `PageType::Grid`, so
        "calender" builds and navigates as an empty grid page and nothing reports the problem.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                {"rev": 1, "pages": [{"id": "c", "type": "calender", "buttons": []}]},
            )
            with self.assertRaises(ConfigError) as caught:
                DeckConfig.load(path)
            self.assertIn("calender", str(caught.exception))

    def test_timing_settings_must_be_whole_seconds(self):
        """A string here is ignored by ArduinoJson's `| default`, so the edit vanishes silently."""
        import tempfile

        for key, bad in (
            ("idle_dim_s", "120"),
            ("idle_off_s", 12.5),
            ("sleep_clock_s", True),
        ):
            with self.subTest(key=key, bad=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    path = self._write(
                        Path(tmp), {"rev": 1, "settings": {key: bad}, "pages": []}
                    )
                    with self.assertRaises(ConfigError) as caught:
                        DeckConfig.load(path)
                    self.assertIn(key, str(caught.exception))

    def test_negative_timing_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp), {"rev": 1, "settings": {"sleep_clock_s": -5}, "pages": []}
            )
            with self.assertRaises(ConfigError) as caught:
                DeckConfig.load(path)
            self.assertIn("sleep_clock_s", str(caught.exception))

    def test_zero_timings_are_allowed(self):
        """Zero is the documented way to switch a stage off, not a mistake."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                {
                    "rev": 1,
                    "settings": {"idle_dim_s": 0, "idle_off_s": 0, "sleep_clock_s": 0},
                    "pages": [],
                },
            )
            DeckConfig.load(path)

    def test_off_below_dim_is_reported(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                {"rev": 1, "settings": {"idle_dim_s": 300, "idle_off_s": 60}, "pages": []},
            )
            with self.assertRaises(ConfigError) as caught:
                DeckConfig.load(path)
            self.assertIn("without ever dimming", str(caught.exception))

    def test_known_page_types_accepted(self):
        import tempfile

        for page_type in ("grid", "numpad", "stats", "calendar"):
            with tempfile.TemporaryDirectory() as tmp:
                path = self._write(
                    Path(tmp),
                    {"rev": 1, "pages": [{"id": "p", "type": page_type, "buttons": []}]},
                )
                DeckConfig.load(path)  # must not raise

    def test_page_without_a_type_is_a_grid(self):
        """Omitting `type` has always meant grid; validation must not make it an error."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), {"rev": 1, "pages": [{"id": "p", "buttons": []}]})
            DeckConfig.load(path)

    def test_duplicate_button_id_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                {
                    "rev": 1,
                    "pages": [
                        {
                            "id": "p",
                            "buttons": [
                                {"id": "dup", "action": {"type": "shell", "cmd": "x"}},
                                {"id": "dup", "action": {"type": "shell", "cmd": "y"}},
                            ],
                        }
                    ],
                },
            )
            with self.assertRaises(ConfigError) as ctx:
                DeckConfig.load(path)
            self.assertIn("duplicate", str(ctx.exception))

    def test_page_action_to_unknown_page_rejected(self):
        # This is the failure that otherwise looks like a working button doing nothing.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                {
                    "rev": 1,
                    "pages": [
                        {
                            "id": "p",
                            "buttons": [
                                {
                                    "id": "b",
                                    "action": {"type": "page", "target": "typo"},
                                }
                            ],
                        }
                    ],
                },
            )
            with self.assertRaises(ConfigError) as ctx:
                DeckConfig.load(path)
            self.assertIn("unknown page", str(ctx.exception))

    def test_action_without_type_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                {"rev": 1, "pages": [{"id": "p", "buttons": [{"id": "b", "action": {}}]}]},
            )
            with self.assertRaises(ConfigError):
                DeckConfig.load(path)

    def test_missing_file_reports_clearly(self):
        with self.assertRaises(ConfigError):
            DeckConfig.load(REPO / "does" / "not" / "exist.json")


class StatsTests(unittest.TestCase):
    def test_synthetic_sample_has_guaranteed_field(self):
        sample = StatsCollector(synthetic=True).sample()
        self.assertIn("cpu", sample)

    def test_lhm_tree_walk_finds_package_temp(self):
        tree = {
            "Children": [
                {
                    "Text": "Sensor",
                    "Children": [{"Text": "CPU Package", "Value": "48.5 °C"}],
                }
            ]
        }
        self.assertAlmostEqual(_find_lhm_cpu_temp(tree), 48.5)

    def test_lhm_tree_walk_returns_none_when_absent(self):
        self.assertIsNone(_find_lhm_cpu_temp({"Children": [{"Text": "Fan"}]}))


class SerialWriteSerialisationTests(unittest.IsolatedAsyncioTestCase):
    """Regression: concurrent writes to one pyserial handle raise 'Write timeout'.

    pyserial keeps a single OVERLAPPED structure per port on Windows, so two simultaneous
    write() calls corrupt each other's completion state and one of them fails on a healthy
    port. SerialLink must serialise writes.
    """

    async def test_writes_do_not_overlap(self):
        link = SerialLink()
        overlapping = False
        in_flight = 0

        class FakeSerial:
            def write(self, data):
                nonlocal overlapping, in_flight
                in_flight += 1
                if in_flight > 1:
                    overlapping = True
                time.sleep(0.01)  # hold the "port" so a racing write would collide
                in_flight -= 1
                return len(data)

            def close(self):
                pass

        link._serial = FakeSerial()
        link._open = True
        link._write_lock = asyncio.Lock()

        # The ping, stats and press paths all send independently.
        await asyncio.gather(*(link.write(b"frame\n") for _ in range(8)))

        self.assertFalse(overlapping, "writes overlapped; the lock is not holding")

    async def test_write_failure_raises_linkerror_and_marks_closed(self):
        link = SerialLink()

        class ExplodingSerial:
            def write(self, data):
                raise OSError("Write timeout")

            def close(self):
                pass

        link._serial = ExplodingSerial()
        link._open = True
        link._write_lock = asyncio.Lock()

        with self.assertRaises(LinkError):
            await link.write(b"x\n")

        # Marked closed so the supervisor reconnects rather than retrying a dead handle.
        self.assertFalse(link.is_open)


class DiskReloadTests(unittest.IsolatedAsyncioTestCase):
    """The agent re-reads deck.json when a session starts."""

    @staticmethod
    def _write(path: Path, rev: int) -> None:
        path.write_text(
            json.dumps(
                {
                    "rev": rev,
                    "pages": [
                        {
                            "id": "p",
                            "buttons": [
                                {"id": "b", "action": {"type": "hid", "keys": ["a"]}}
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    async def test_edit_while_disconnected_is_picked_up(self):
        # The papercut this exists to prevent: reflashing the device drops the link, and on
        # reconnect both sides still claim the pre-edit revision — so they agree, nothing is
        # pushed, and the edit never appears. It looks like the flash failed.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            self._write(path, 1)
            config = DeckConfig.load(path)

            self._write(path, 2)  # edited while nothing was connected

            link = SimulatedLink([], rev=1, step_delay=0.01)
            host = DeckHost(
                link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
            )
            await host.run(duration=0.4)

            self.assertEqual(host.config.rev, 2)
            self.assertIn("layout", [f["t"] for f in link.received])
            self.assertEqual(link.rev, 2)

    async def test_broken_file_keeps_the_running_layout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            self._write(path, 1)
            config = DeckConfig.load(path)

            path.write_text("{ not json", encoding="utf-8")

            link = SimulatedLink([], rev=1, step_delay=0.01)
            host = DeckHost(
                link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
            )
            await host.run(duration=0.4)

            # Still serving rev 1 rather than having thrown or blanked the deck.
            self.assertEqual(host.config.rev, 1)
            self.assertEqual(len(host.config.buttons), 1)


class AssetFormatTests(unittest.TestCase):
    """MDI1 must mean the same thing to make_assets.py and to firmware/assets.cpp.

    The two are compiled by different toolchains in different languages and can only disagree
    silently — a wrong stride or byte order renders as plausible-looking garbage, not an error.
    """

    HEADER_BYTES = 8
    MAGIC = b"MDI1"

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        sys.path.insert(0, str(REPO / "tools"))

    def _encode(self, image):
        import make_assets

        return make_assets.encode(image)

    def test_header_matches_the_firmware_reader(self):
        from PIL import Image

        blob = self._encode(Image.new("RGB", (7, 3), (0, 0, 0)))

        # Exactly the fields assets.cpp reads, at exactly the offsets it reads them from.
        self.assertEqual(blob[0:4], self.MAGIC)
        width = blob[4] | (blob[5] << 8)
        height = blob[6] | (blob[7] << 8)
        self.assertEqual((width, height), (7, 3))

        # assets.cpp rejects the file unless this holds exactly.
        self.assertEqual(len(blob) - self.HEADER_BYTES, width * height * 2)

    def test_pixel_packing_is_little_endian_rgb565(self):
        from PIL import Image

        # Pure red, green and blue, so a channel swap or a byte swap cannot hide.
        image = Image.new("RGB", (3, 1))
        image.putpixel((0, 0), (255, 0, 0))
        image.putpixel((1, 0), (0, 255, 0))
        image.putpixel((2, 0), (0, 0, 255))

        body = self._encode(image)[self.HEADER_BYTES :]
        words = [body[i] | (body[i + 1] << 8) for i in range(0, len(body), 2)]

        self.assertEqual(words, [0xF800, 0x07E0, 0x001F])

    def test_black_stays_black(self):
        # The bug that cost this project an evening was blue's top bit stuck high. If the
        # converter ever emits a non-zero blue for black, it must fail here rather than on a
        # bench with a camera.
        from PIL import Image

        body = self._encode(Image.new("RGB", (4, 4), (0, 0, 0)))[self.HEADER_BYTES :]
        self.assertEqual(set(body), {0})

    def test_cover_crop_fills_the_screen_without_letterboxing(self):
        from PIL import Image

        import make_assets

        for size in ((600, 1000), (2000, 400), (800, 480)):
            with self.subTest(size=size):
                cropped = make_assets.cover_crop(
                    Image.new("RGB", size, (10, 20, 30)), 800, 480, "centre"
                )
                self.assertEqual(cropped.size, (800, 480))

    def test_dim_darkens_without_clipping_to_black(self):
        from PIL import Image

        import make_assets

        dimmed = make_assets.dim(Image.new("RGB", (2, 2), (200, 100, 50)), 50)
        self.assertEqual(dimmed.getpixel((0, 0)), (100, 50, 25))


class IconNameTests(unittest.TestCase):
    """config.py's ICON_NAMES and firmware/icons.cpp's kIcons must be the same list.

    They cannot disagree loudly. A name the firmware knows but the agent does not is rejected
    as a typo you did not make; a name the agent knows but the firmware does not passes
    validation and then renders as text on the deck. Both look like the icon field being
    ignored, which is exactly the failure this validation was added to prevent.
    """

    def _firmware_names(self) -> set[str]:
        source = (REPO / "firmware" / "multi_deck" / "icons.cpp").read_text(encoding="utf-8")
        table = re.search(r"kIcons\[\]\s*=\s*\{(.*?)\n\};", source, re.S)
        self.assertIsNotNone(table, "could not find the kIcons table in icons.cpp")
        return set(re.findall(r'\{"([a-z0-9_]+)",\s*LV_SYMBOL_', table.group(1)))

    def test_lists_agree(self):
        firmware = self._firmware_names()
        self.assertTrue(firmware, "parsed no icon names out of icons.cpp")

        self.assertEqual(
            firmware,
            set(ICON_NAMES),
            "icons.cpp and config.py ICON_NAMES have drifted",
        )

    def test_names_follow_the_lowercase_lv_symbol_rule(self):
        # The rule that makes the two lists maintainable without a mapping table.
        for name in self._firmware_names():
            with self.subTest(name=name):
                self.assertEqual(name, name.lower())
                self.assertRegex(name, r"^[a-z][a-z0-9_]*$")


class HidTokenTests(unittest.TestCase):
    """config.py's key tables and hid.cpp's must be the same tables, for ICON_NAMES' reasons.

    The failure is worse here than for icons, though. An unknown icon name degrades to showing
    the tile's text label; an unknown key token makes sendCombo() reject the *whole chord* and
    return, so the tile types nothing at all. Both write a line to UART0, which in daily use is
    not attached to anything.
    """

    def _source(self) -> str:
        return (REPO / "firmware" / "multi_deck" / "hid.cpp").read_text(encoding="utf-8")

    def _table(self, name: str) -> set[str]:
        table = re.search(rf"{name}\[\]\s*=\s*\{{(.*?)\n\}};", self._source(), re.S)
        self.assertIsNotNone(table, f"could not find {name} in hid.cpp")
        return set(re.findall(r'\{"([A-Z0-9_]+)",', table.group(1)))

    def test_key_names_agree(self):
        from deckhost.config import HID_KEY_NAMES

        firmware = self._table("kNamedKeys")
        self.assertTrue(firmware, "parsed no key names out of hid.cpp")
        self.assertEqual(firmware, set(HID_KEY_NAMES), "hid.cpp and HID_KEY_NAMES have drifted")

    def test_modifiers_agree(self):
        from deckhost.config import HID_MODIFIERS

        firmware = self._table("kNamedModifiers")
        self.assertTrue(firmware, "parsed no modifiers out of hid.cpp")
        self.assertEqual(firmware, set(HID_MODIFIERS), "hid.cpp and HID_MODIFIERS have drifted")

    def test_media_keys_agree(self):
        from deckhost.config import MEDIA_KEYS

        # sendMedia is a strcmp chain rather than a table, so the keys are read from the
        # comparisons themselves.
        body = re.search(r"\bsendMedia\(const String.*?\n\}", self._source(), re.S)
        self.assertIsNotNone(body, "could not find sendMedia in hid.cpp")
        firmware = set(re.findall(r'key == "([a-z_]+)"', body.group(0)))

        self.assertTrue(firmware, "parsed no media keys out of hid.cpp")
        self.assertEqual(firmware, set(MEDIA_KEYS), "hid.cpp and MEDIA_KEYS have drifted")

    def test_the_report_limit_matches_the_firmware(self):
        from deckhost.config import HID_MAX_KEYS

        self.assertRegex(self._source(), rf"key_count < {HID_MAX_KEYS}\b")

    def test_single_characters_resolve_arithmetically(self):
        """The branch with no table behind it, and the one an editor has to explain.

        An upper-case letter implies SHIFT on the device, so ["A"] and ["a"] are different
        keystrokes — which is not visible from reading deck.json.
        """
        from deckhost.config import resolve_hid_token

        for token in ("a", "z", "A", "Z", "0", "9"):
            with self.subTest(token=token):
                self.assertEqual(resolve_hid_token(token), "key")

        self.assertIsNone(resolve_hid_token("ab"))
        self.assertIsNone(resolve_hid_token("é"))
        self.assertIsNone(resolve_hid_token(""))
        self.assertIsNone(resolve_hid_token(None))

    def test_matching_is_case_insensitive_like_the_firmware(self):
        from deckhost.config import resolve_hid_token

        self.assertEqual(resolve_hid_token("ctrl"), "modifier")
        self.assertEqual(resolve_hid_token("Ctrl"), "modifier")
        self.assertEqual(resolve_hid_token("PAGEUP"), "key")
        self.assertEqual(resolve_hid_token("pageup"), "key")


class ActionValidationTests(unittest.TestCase):
    """Every action field the firmware or the agent reads, checked before it ships.

    None of this was checked while buttons were written by hand: you copied a working one and
    changed the target. From a form, `{"type": "launch"}` with no target is two clicks — and
    every failure here is silent from where you are standing, so the validator has to come
    before the UI that makes them easy.
    """

    def _config(self, action, **button):
        return DeckConfig.from_raw(
            {
                "rev": 1,
                "themes": [{"name": "T"}],
                "settings": {},
                "pages": [
                    {"id": "home", "buttons": [{"id": "b", "action": action, **button}]}
                ],
            },
            validate=False,
        )

    def _problems(self, action, **button) -> str:
        return " | ".join(self._config(action, **button).problems())

    def test_an_action_missing_its_only_useful_field_is_caught(self):
        for action, field_name in (
            ({"type": "launch"}, "target"),
            ({"type": "shell"}, "cmd"),
            ({"type": "ahk"}, "fn"),
            ({"type": "hid"}, "keys"),
            ({"type": "hid_text"}, "text"),
            ({"type": "media"}, "key"),
            ({"type": "seq"}, "steps"),
        ):
            with self.subTest(type=action["type"]):
                self.assertIn(f"no {field_name}", self._problems(action))

    def test_an_empty_string_is_as_useless_as_an_absent_key(self):
        """Which is what an untouched form field produces."""
        self.assertIn("no target", self._problems({"type": "launch", "target": ""}))

    def test_a_theme_action_may_have_no_target(self):
        """Empty means "next", and that is a real thing to write."""
        self.assertEqual(self._problems({"type": "theme", "target": ""}), "")

    def test_an_unknown_action_type_is_named(self):
        self.assertIn("action type is 'lanch'", self._problems({"type": "lanch", "target": "x"}))

    def test_a_rejected_chord_is_caught_before_it_silently_types_nothing(self):
        self.assertIn(
            "not one the device knows",
            self._problems({"type": "hid", "keys": ["ctrl", "shfit", "c"]}),
        )
        self.assertEqual(self._problems({"type": "hid", "keys": ["ctrl", "SHIFT", "c"]}), "")

    def test_seven_keys_is_a_rejected_chord_not_a_truncated_one(self):
        keys = ["a", "b", "c", "d", "e", "f", "g"]
        self.assertIn("rejects the whole chord", self._problems({"type": "hid", "keys": keys}))
        self.assertEqual(self._problems({"type": "hid", "keys": keys[:6]}), "")

    def test_modifiers_alone_type_nothing(self):
        self.assertIn(
            "nothing is typed", self._problems({"type": "hid", "keys": ["ctrl", "shift"]})
        )

    def test_an_unknown_media_key_is_named(self):
        self.assertIn(
            "media key 'volume_up'", self._problems({"type": "media", "key": "volume_up"})
        )

    def test_a_delay_needs_a_number(self):
        self.assertIn("delay ms is '250'", self._problems({"type": "delay", "ms": "250"}))
        self.assertEqual(self._problems({"type": "delay", "ms": 250}), "")

    def test_nested_steps_are_checked_too(self):
        action = {"type": "seq", "steps": [{"type": "delay", "ms": 50}, {"type": "shell"}]}
        self.assertIn("no cmd", self._problems(action))

    def test_a_hold_is_validated_like_an_action(self):
        """The least visible thing on a deck: nothing about a tile says it has a long press."""
        problems = self._problems(
            {"type": "launch", "target": "notepad.exe"},
            hold={"type": "page", "target": "nowhere"},
        )
        self.assertIn("b (hold)", problems)
        self.assertIn("unknown page", problems)

    def test_duplicate_page_ids_are_caught(self):
        raw = {
            "rev": 1,
            "themes": [{"name": "T"}],
            "pages": [{"id": "home", "buttons": []}, {"id": "home", "buttons": []}],
        }
        problems = DeckConfig.from_raw(raw, validate=False).problems()
        self.assertIn("duplicate page id 'home'", " | ".join(problems))

    def test_a_page_with_no_id_cannot_be_navigated_to(self):
        raw = {"rev": 1, "themes": [{"name": "T"}], "pages": [{"buttons": []}]}
        problems = DeckConfig.from_raw(raw, validate=False).problems()
        self.assertIn("has no id", " | ".join(problems))

    def test_the_shipped_layout_passes_all_of_it(self):
        """The point of adding checks is to find real mistakes, not to invent them."""
        self.assertEqual(DeckConfig.load(REPO / "sdcard" / "deck.json").problems(), [])

    def test_an_unknown_ahk_function_warns_rather_than_refusing_to_start(self):
        """lib.ahk is a file you are meant to edit, so an unknown name may be one not yet written."""
        config = self._config({"type": "ahk", "fn": "NotAHelper"})
        self.assertEqual(config.problems(), [])
        self.assertIn("NotAHelper", " | ".join(config.warnings()))

    def test_the_shipped_ahk_functions_all_exist(self):
        self.assertEqual(DeckConfig.load(REPO / "sdcard" / "deck.json").warnings(), [])


class HoldDispatchTests(unittest.TestCase):
    """A long press the device cannot run itself arrives under a different id than it left.

    ui_builder.cpp sends `<id>.hold`, and the agent's index is keyed by the ids in deck.json —
    so every agent-side hold answered "Unknown button" and toasted it. A device-local hold ran
    fine, which is what kept this hidden: the ten-key's holds work, and those are the ones you
    press while testing.
    """

    def _config(self):
        return DeckConfig.from_raw(
            {
                "rev": 1,
                "themes": [{"name": "T"}],
                "pages": [
                    {
                        "id": "home",
                        "buttons": [
                            {
                                "id": "edit.paste",
                                "action": {"type": "hid", "keys": ["ctrl", "v"]},
                                "hold": {"type": "ahk", "fn": "PasteInto", "args": ["x"]},
                            }
                        ],
                    }
                ],
            },
            validate=False,
        )

    def test_the_suffix_matches_the_firmware(self):
        from deckhost.config import HOLD_SUFFIX

        source = (REPO / "firmware" / "multi_deck" / "ui_builder.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'button.id + "{HOLD_SUFFIX}"', source)

    def test_a_hold_press_resolves_to_the_hold_action(self):
        config = self._config()
        self.assertEqual(config.action_for("edit.paste.hold"), config.buttons["edit.paste"]["hold"])

    def test_a_plain_press_still_resolves_to_the_plain_action(self):
        self.assertEqual(self._config().action_for("edit.paste")["type"], "hid")

    def test_an_exact_id_wins_over_the_suffix_rule(self):
        """A button someone genuinely named `foo.hold` must not be shadowed."""
        raw = {
            "rev": 1,
            "themes": [{"name": "T"}],
            "pages": [
                {
                    "id": "home",
                    "buttons": [
                        {"id": "foo", "hold": {"type": "shell", "cmd": "wrong"}},
                        {"id": "foo.hold", "action": {"type": "shell", "cmd": "right"}},
                    ],
                }
            ],
        }
        config = DeckConfig.from_raw(raw, validate=False)
        self.assertEqual(config.action_for("foo.hold")["cmd"], "right")

    def test_an_unknown_hold_is_still_unknown(self):
        self.assertIsNone(self._config().action_for("nosuch.hold"))


class ConfigShapeTests(unittest.TestCase):
    """deck.json is a socket: objects of the same kind present the same keys.

    Optional-means-absent let that drift. Themes ended up in two shapes — some with `display`,
    some without, some with `tile_opa` — and an absent key told the reader nothing about whether
    the default was chosen or forgotten. Every field has a written form of unset now (`null`, or
    `""` for strings), so a gap is a gap rather than a statement.

    These tests read the firmware's own parser for the field list, which is what stops the file
    going stale: add `src["glow"]` to parseTheme and every theme here has to answer for it.
    """

    @staticmethod
    def _deck() -> dict:
        return json.loads((REPO / "sdcard" / "deck.json").read_text(encoding="utf-8"))

    @staticmethod
    def _source() -> str:
        return (REPO / "firmware" / "multi_deck" / "deck_config.cpp").read_text(encoding="utf-8")

    def _theme_fields(self) -> set[str]:
        body = re.search(r"Theme parseTheme\(JsonObjectConst src\) \{(.*?)\n\}", self._source(), re.S)
        self.assertIsNotNone(body, "could not find parseTheme() in deck_config.cpp")
        return set(re.findall(r'src\["(\w+)"\]', body.group(1)))

    def _settings_fields(self) -> set[str]:
        # Scoped to the settings block: `s` is a short name, and `pos["col"]` a few lines
        # further down matches a bare `s\["..."\]` just as well.
        block = re.search(
            r'JsonObjectConst s = root\["settings"\];(.*?)active_theme_', self._source(), re.S
        )
        self.assertIsNotNone(block, "could not find the settings block in deck_config.cpp")
        return set(re.findall(r's\["(\w+)"\]', block.group(1)))

    def test_every_theme_carries_every_field_the_firmware_reads(self):
        fields = self._theme_fields()
        self.assertIn("tile_opa", fields, "parsed no theme fields out of deck_config.cpp")

        for theme in self._deck()["themes"]:
            with self.subTest(theme=theme.get("name")):
                self.assertEqual(
                    set(theme),
                    fields,
                    "theme keys and parseTheme() have drifted — write the missing key with "
                    "null (or \"\") rather than leaving it out",
                )

    def test_themes_agree_on_key_order(self):
        """Same keys in the same order, so two themes diff against each other cleanly."""
        shapes = {t["name"]: tuple(t) for t in self._deck()["themes"]}
        reference = shapes["Midnight"]
        for name, shape in shapes.items():
            with self.subTest(theme=name):
                self.assertEqual(shape, reference)

    def test_settings_carries_every_field_the_firmware_reads(self):
        fields = self._settings_fields()
        self.assertIn("dim_pct", fields, "parsed no settings fields out of deck_config.cpp")
        self.assertEqual(set(self._deck()["settings"]), fields)

    def test_every_page_carries_every_field_the_firmware_reads(self):
        # To the end of the loop body, so `p["buttons"]` — which the inner loop iterates — is
        # inside the slice rather than the line that terminates it.
        block = re.search(
            r'for \(JsonObjectConst p : root\["pages"\](.*?)\n    pages\.push_back',
            self._source(),
            re.S,
        )
        self.assertIsNotNone(block, "could not find the page loop in deck_config.cpp")
        fields = set(re.findall(r'p\["(\w+)"\]', block.group(1)))
        self.assertIn("buttons", fields, "parsed no page fields out of deck_config.cpp")

        for page in self._deck()["pages"]:
            with self.subTest(page=page["id"]):
                self.assertEqual(set(page), fields)

    def test_every_button_carries_every_field_the_firmware_reads(self):
        block = re.search(
            r'for \(JsonObjectConst b : p\["buttons"\](.*?)\n      page\.buttons\.push_back',
            self._source(),
            re.S,
        )
        self.assertIsNotNone(block, "could not find the button loop in deck_config.cpp")
        fields = set(re.findall(r'b\["(\w+)"\]', block.group(1)))
        self.assertIn("hold", fields, "parsed no button fields out of deck_config.cpp")

        buttons = [b for page in self._deck()["pages"] for b in page["buttons"]]
        self.assertTrue(buttons, "the shipped deck has no buttons to check")
        for button in buttons:
            with self.subTest(button=button["id"]):
                self.assertEqual(set(button), fields)

    def test_buttons_agree_on_key_order(self):
        shapes = {
            b["id"]: tuple(b) for page in self._deck()["pages"] for b in page["buttons"]
        }
        reference = shapes["launch.vscode"]
        for button_id, shape in shapes.items():
            with self.subTest(button=button_id):
                self.assertEqual(shape, reference)

    def test_the_shipped_layout_still_fits_in_one_line(self):
        """Writing every default down costs bytes, and the line limit is a cliff.

        `layout` crosses as a single line, and the firmware drops any line at or over
        MD_LINK_RX_MAX (8192) with "[link] oversized line — resynchronising". Nothing checks
        this on the way out — MAX_LINE_BYTES guards the *inbound* reader only — so the failure
        is a tray reload that appears to do nothing while the deck keeps its cached SD copy.

        Filling in the shape took the frame from ~4.5KB to ~5.8KB. That is fine and this test
        says so out loud, because the next few pages are what would take it over.
        """
        from deckhost import protocol

        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        line = protocol.encode(protocol.layout(config.rev, config.raw))

        self.assertLess(
            len(line),
            protocol.MAX_LINE_BYTES,
            "the layout frame no longer fits in one line — the device would drop it",
        )
        # Margin, not just the limit: a deck this size should not be near the cliff at all.
        self.assertLess(
            len(line),
            protocol.MAX_LINE_BYTES * 0.8,
            f"the layout frame is {len(line)} bytes, over 80% of the {protocol.MAX_LINE_BYTES} "
            "line limit — raise MD_LINK_RX_MAX (and MAX_LINE_BYTES with it) before adding more",
        )

    def test_a_page_with_no_buttons_says_so(self):
        """numpad, stats and calendar build themselves — but the keys are still written.

        `"buttons": []` is a statement; a missing key is a shrug. `pos` and `hold` are null for
        the same reason: `JsonObjectConst::isNull()` is true for both null and absent, so the
        firmware reads them identically and only the reader gains anything.
        """
        by_id = {p["id"]: p for p in self._deck()["pages"]}
        for page_id in ("numpad", "stats", "calendar"):
            with self.subTest(page=page_id):
                self.assertEqual(by_id[page_id]["buttons"], [])
                self.assertIsNone(by_id[page_id]["grid"])

    def test_build_dependent_defaults_are_left_to_the_build(self):
        """`dim_opa` and `flip180` default from config.h, so the shipped themes say null.

        A literal here would override the build. `dim_opa` is the one with teeth: it is 0 with
        the PWM backlight rewire and 55 without, because the veil only has to supply darkness
        the backlight cannot. Writing this deck's 0 into the file would leave anyone building
        the unmodified board with a dim state that does nothing at all.
        """
        for theme in self._deck()["themes"]:
            with self.subTest(theme=theme["name"]):
                self.assertIsNone(theme["dim_opa"])
                self.assertIsNone(theme["flip180"])

    def test_null_is_accepted_wherever_a_default_lives(self):
        import tempfile

        theme = {
            "name": "T",
            "display": "",
            "wallpaper": "",
            "bg": None,
            "tile_opa": None,
            "border_opa": None,
            "radius": None,
            "dim_opa": None,
            "flip180": None,
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(
                json.dumps({"rev": 1, "themes": [theme], "pages": []}), encoding="utf-8"
            )
            self.assertEqual(DeckConfig.load(path).theme_names(), ["T"])

    def test_wrong_type_still_rejected_alongside_null(self):
        import tempfile

        for field, bad in (("tile_opa", "70"), ("radius", True), ("flip180", 1), ("bg", 123)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "deck.json"
                path.write_text(
                    json.dumps({"rev": 1, "themes": [{"name": "T", field: bad}], "pages": []}),
                    encoding="utf-8",
                )
                with self.assertRaises(ConfigError) as caught:
                    DeckConfig.load(path)
                self.assertIn(field, str(caught.exception))


class IconValidationTests(unittest.TestCase):
    """Presentation fields fail silently on the device, so they are checked here."""

    @staticmethod
    def _config(button: dict) -> dict:
        return {"rev": 1, "pages": [{"id": "p", "buttons": [button]}]}

    def _load(self, button: dict) -> DeckConfig:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(json.dumps(self._config(button)), encoding="utf-8")
            return DeckConfig.load(path)

    def _reject(self, button: dict, fragment: str) -> None:
        with self.assertRaises(ConfigError) as caught:
            self._load(button)
        self.assertIn(fragment, str(caught.exception))

    def test_known_symbol_accepted(self):
        # The icon is `play`; the media key is `play_pause`. They are not the same vocabulary,
        # which is easy to forget and is now caught.
        self._load({"id": "b", "icon": "play", "action": {"type": "media", "key": "play_pause"}})

    def test_sd_path_accepted_without_touching_the_card(self):
        self._load(
            {
                "id": "b",
                "icon": "/icons/code.bin",
                "action": {"type": "launch", "target": "code"},
            }
        )

    def test_typo_rejected(self):
        # "volume" is the natural thing to type; the symbol is volume_mid. Exactly the case
        # that would otherwise show a text label and look like the field did nothing.
        self._reject(
            {"id": "b", "icon": "volume", "action": {"type": "media", "key": "mute"}},
            "not a built-in symbol",
        )

    def test_unconverted_image_rejected(self):
        self._reject(
            {"id": "b", "icon": "/icons/code.png", "action": {"type": "launch"}},
            "not a .bin",
        )

    def test_unknown_display_mode_rejected(self):
        self._reject(
            {"id": "b", "display": "icon-text", "action": {"type": "page", "target": "p"}},
            "expected one of",
        )

    def test_no_icon_is_fine(self):
        self._load({"id": "b", "label": "Go", "action": {"type": "page", "target": "p"}})

    def test_empty_display_is_the_written_form_of_unset(self):
        self._load(
            {"id": "b", "display": "", "action": {"type": "page", "target": "p"}}
        )

    def test_bad_settings_display_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(
                json.dumps({"rev": 1, "pages": [], "settings": {"display": "icons"}}),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as caught:
                DeckConfig.load(path)
            self.assertIn("settings.display", str(caught.exception))

    def test_empty_settings_display_accepted(self):
        # Legal but inert: there is no level above `settings`, so a tile lands on the
        # firmware's own icon_text. Rejecting it would put the shape rule back.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(
                json.dumps({"rev": 1, "pages": [], "settings": {"display": ""}}),
                encoding="utf-8",
            )
            self.assertEqual(DeckConfig.load(path).rev, 1)

    def test_shipped_deck_has_a_display_baseline(self):
        # Only that a baseline exists, so a theme which says nothing cannot silently land on
        # text and look like the icons were never configured.
        #
        # Says nothing about what any theme's own `display` is. ConfigShapeTests requires the
        # key to be present; its value stays a per-theme choice the format is meant to support,
        # and `""` — inherit this baseline — is one of the real answers.
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")

        self.assertIn(
            "display",
            config.raw.get("settings", {}),
            "settings.display should carry the deck-wide baseline",
        )

    def test_a_theme_may_override_the_baseline(self):
        """Per-theme anatomy is a supported choice, not a legacy path."""
        import tempfile

        deck = {
            "rev": 1,
            "settings": {"display": "icon_text", "theme": "Kiosk"},
            "themes": [
                {"name": "Kiosk", "display": "icon"},
                {"name": "Reader", "display": "text"},
                {"name": "Default"},
            ],
            "pages": [
                {"id": "p", "buttons": [{"id": "b", "icon": "play", "action": {"type": "media", "key": "play_pause"}}]}
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_text(json.dumps(deck), encoding="utf-8")
            config = DeckConfig.load(path)

        themes = {t["name"]: t.get("display") for t in config.raw["themes"]}
        self.assertEqual(themes["Kiosk"], "icon")
        self.assertEqual(themes["Reader"], "text")
        self.assertIsNone(themes["Default"], "an unset theme inherits rather than defaulting")


class AssetStampTests(unittest.TestCase):
    """The stamp must change when the card would need rewriting, and not otherwise.

    Both halves matter. A stamp that misses a change is a check that does not work; a stamp
    that changes when nothing did is worse, because it trains you to ignore the warning.
    """

    def _tree(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / "wall").mkdir()
        (root / "wall" / "dusk.bin").write_bytes(b"pixels")
        (root / "deck.json").write_text("{}", encoding="utf-8")
        return root

    def test_empty_tree_has_no_stamp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # No assets means nothing to keep in sync, so a fresh checkout does not nag.
            self.assertEqual(asset_stamp(Path(tmp)), "")

    def test_content_change_moves_the_stamp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            before = asset_stamp(root)

            (root / "wall" / "dusk.bin").write_bytes(b"different")
            self.assertNotEqual(asset_stamp(root), before)

    def test_rename_moves_the_stamp(self):
        # Paths are hashed alongside contents: a theme points at "/wall/dusk.bin", so the same
        # bytes under a new name still means the card is wrong.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            before = asset_stamp(root)

            (root / "wall" / "dusk.bin").rename(root / "wall" / "dawn.bin")
            self.assertNotEqual(asset_stamp(root), before)

    def test_layout_edits_do_not_move_the_stamp(self):
        # deck.json travels over USB and has its own rev. If it counted, every colour tweak
        # would claim the images were stale.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            before = asset_stamp(root)

            (root / "deck.json").write_text('{"rev": 99}', encoding="utf-8")
            self.assertEqual(asset_stamp(root), before)

    def test_stamp_file_does_not_describe_itself(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            before = asset_stamp(root)

            written = write_stamp(root)
            self.assertEqual(written, before)
            # Writing it must not change the answer, or no two runs would ever agree.
            self.assertEqual(asset_stamp(root), before)
            self.assertEqual(read_stamp(root), before)

    def test_write_stamp_removes_a_stamp_with_nothing_left_to_describe(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            write_stamp(root)
            self.assertTrue((root / STAMP_FILE).exists())

            (root / "wall" / "dusk.bin").unlink()
            self.assertEqual(write_stamp(root), "")
            # A leftover stamp would claim a generation that no longer exists.
            self.assertFalse((root / STAMP_FILE).exists())

    def test_read_stamp_matches_how_the_firmware_reads_it(self):
        # assets.cpp does readStringUntil('\n') then trim(). Anything the writer emits that
        # this does not survive would produce a permanent phantom mismatch.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / STAMP_FILE).write_text("abc123def456\n", encoding="ascii")
            self.assertEqual(read_stamp(root), "abc123def456")

            # A truncated copy onto the card must read as "no stamp", not raise.
            (root / STAMP_FILE).write_text("", encoding="ascii")
            self.assertEqual(read_stamp(root), "")

    def test_shipped_card_is_stamped_and_current(self):
        root = REPO / "sdcard"
        expected = asset_stamp(root)
        if not expected:
            self.skipTest("no assets in sdcard/")

        # Catches the commit that adds a wallpaper and forgets to restamp — at which point
        # every connect would warn about a card that is actually fine.
        self.assertEqual(
            read_stamp(root),
            expected,
            f"sdcard/{STAMP_FILE} is out of date — run: python tools/make_assets.py stamp",
        )


class AssetSyncWarningTests(unittest.IsolatedAsyncioTestCase):
    """What the agent says on connect about the card's images."""

    ONE_ASSET = {"wall/dusk.bin": b"pixels"}

    async def _toasts_for(self, files: dict[str, bytes], hello_for) -> list[str]:
        """Builds a repo tree, then asks `hello_for(current_stamp)` for the frame to test.

        The callback exists so a test can say "the card agrees" without hardcoding a hash.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deck.json").write_text(
                json.dumps({"rev": 1, "pages": []}), encoding="utf-8"
            )
            for name, data in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

            config = DeckConfig.load(root / "deck.json")
            link = SimulatedLink([], rev=1, step_delay=0.01)
            host = DeckHost(
                link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
            )

            await host._check_assets(hello_for(asset_stamp(root)))
            return [f["msg"] for f in link.received if f["t"] == "toast"]

    async def test_matching_stamp_says_nothing(self):
        toasts = await self._toasts_for(
            self.ONE_ASSET, lambda current: protocol.hello(assets=current)
        )
        self.assertEqual(toasts, [])

    async def test_stale_stamp_warns(self):
        toasts = await self._toasts_for(
            self.ONE_ASSET, lambda _: protocol.hello(assets="000000000000")
        )
        self.assertEqual(len(toasts), 1)
        self.assertIn("stale", toasts[0].lower())

    async def test_unstamped_card_warns_differently(self):
        # A card written before stamps existed. Its images may well be fine, so the message
        # must not assert they are wrong.
        toasts = await self._toasts_for(
            self.ONE_ASSET, lambda _: protocol.hello(assets="")
        )
        self.assertEqual(len(toasts), 1)
        self.assertNotIn("stale", toasts[0].lower())

    async def test_device_without_a_card_says_nothing(self):
        # The field is absent, which is also what firmware older than the stamp sends. Neither
        # is evidence of anything, and a deck with no card already complains on screen.
        toasts = await self._toasts_for(self.ONE_ASSET, lambda _: protocol.hello())
        self.assertEqual(toasts, [])

    async def test_repo_without_assets_says_nothing(self):
        toasts = await self._toasts_for(
            {}, lambda _: protocol.hello(assets="000000000000")
        )
        self.assertEqual(toasts, [])


class LinkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The two ways a link dies without erroring.

    Both were invisible before: reads returning nothing and writes disappearing into a closed
    port are silent, so neither failure produced an exception for the agent to react to. They
    are caught by a clock, and these tests turn that clock down so the suite stays quick.
    """

    def setUp(self):
        self._patches = [
            unittest.mock.patch.object(deckhost_main, "HANDSHAKE_TIMEOUT_S", 0.3),
            unittest.mock.patch.object(deckhost_main, "SILENCE_TIMEOUT_S", 0.3),
            unittest.mock.patch.object(deckhost_main, "WATCHDOG_INTERVAL_S", 0.05),
            unittest.mock.patch.object(deckhost_main, "PING_INTERVAL_S", 0.05),
            unittest.mock.patch.object(deckhost_main, "RECONNECT_DELAY_S", 0.05),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _host(self, link):
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        return DeckHost(
            link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
        )

    async def test_port_that_opens_but_never_handshakes_is_reopened(self):
        """The sleep failure, exactly: COM comes back, the device does not answer on it.

        Reopening rather than merely retrying is the point — a fresh serial.Serial() re-asserts
        DTR, which is what prods the device into announcing itself. Before this the agent sent
        identify into the void indefinitely; one morning it sat there twelve minutes.
        """
        link = SimulatedLink([], rev=1, step_delay=0.01, handshake=False)
        host = self._host(link)

        await host.run(duration=1.2)

        self.assertFalse(host.session_up)
        self.assertGreater(link.opens, 1, "gave up and reopened at least once")

    async def test_session_that_goes_silent_is_torn_down(self):
        """A device that handshakes and then stops answering must not stay 'connected'."""
        link = SimulatedLink([], rev=1, step_delay=0.01, go_silent_after=2)
        host = self._host(link)

        await host.run(duration=1.2)

        self.assertGreater(link.opens, 1, "ended the dead session and started a new one")

    async def test_healthy_session_is_left_alone(self):
        """The watchdog must not be trigger-happy: a deck that answers is never interrupted.

        Asserted on reopen count rather than on session_up, because run() clears that flag on
        its way out whether the session ended well or badly — so it cannot tell the two apart.
        """
        link = SimulatedLink([], rev=1, step_delay=0.01)
        host = self._host(link)

        await host.run(duration=1.2)

        self.assertEqual(link.opens, 1, "a working link is never reopened")
        kinds = [f["t"] for f in link.received]
        self.assertIn("welcome", kinds, "the session really did come up")
        self.assertIn("ping", kinds, "and kept running long enough to be pinged")

    async def test_backoff_grows_then_resets_on_handshake(self):
        host = self._host(SimulatedLink([], rev=1, step_delay=0.01))

        first = host._retry_delay
        await host._pause_before_retry(None)
        await host._pause_before_retry(None)
        self.assertGreater(host._retry_delay, first)

        # A handshake is what proves the link works, so it is what clears the backoff. An open
        # port is not enough — the failure this exists for is a port that opens and does nothing.
        host._note_recovered()
        self.assertEqual(host._retry_delay, first)
        self.assertEqual(host._failures, 0)

    async def test_backoff_is_capped(self):
        host = self._host(SimulatedLink([], rev=1, step_delay=0.01))

        for _ in range(20):
            host._retry_delay = min(
                host._retry_delay * 2, deckhost_main.RECONNECT_DELAY_MAX_S
            )

        self.assertLessEqual(host._retry_delay, deckhost_main.RECONNECT_DELAY_MAX_S)

    async def test_wake_cuts_the_backoff_short(self):
        """A resume is the likeliest moment for the deck to return; don't sit out the timer."""
        host = self._host(SimulatedLink([], rev=1, step_delay=0.01))
        host._retry_delay = 30.0

        started = time.monotonic()
        waiter = asyncio.create_task(host._pause_before_retry(None))
        await asyncio.sleep(0.05)
        host.on_wake()
        await waiter

        self.assertLess(time.monotonic() - started, 1.0)


class TimeSyncTests(unittest.TestCase):
    """The deck has no RTC, so this frame is the only thing that makes its clock true."""

    def test_carries_epoch_and_offset(self):
        frame = protocol.time_sync()

        self.assertEqual(frame["t"], "time")
        self.assertGreater(frame["epoch"], 1_700_000_000)  # sometime after 2023
        self.assertIsInstance(frame["tz_min"], int)
        self.assertGreaterEqual(frame["tz_min"], -12 * 60)
        self.assertLessEqual(frame["tz_min"], 14 * 60)

    def test_offset_is_whole_minutes(self):
        """Some zones are on a half hour, none are on a fraction of a minute."""
        self.assertEqual(protocol.time_sync()["tz_min"] % 1, 0)

    def test_offset_recomputed_per_call(self):
        """Sent fresh every minute so a daylight-saving change carries across.

        The device has no timezone database and cannot work out that the clocks went forward;
        it only knows what it was last told, so the offset must not be cached here.
        """
        import time as _time

        january = protocol.time_sync(_time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, -1)))
        july = protocol.time_sync(_time.mktime((2026, 7, 15, 12, 0, 0, 0, 0, -1)))

        # Equal in a zone without DST, different in one with it — either is correct, so this
        # asserts only that each was derived from its own timestamp rather than from "now".
        self.assertNotEqual(january["epoch"], july["epoch"])

    def test_power_frame_shape(self):
        self.assertEqual(protocol.power("sleep"), {"t": "power", "state": "sleep"})
        self.assertEqual(protocol.power("wake"), {"t": "power", "state": "wake"})


class PowerEventTests(unittest.TestCase):
    """Edge detection, which is the whole job once the window exists.

    Windows announces the same transition more than one way — a display-off arrives, then a
    PBT_APMSUSPEND for the same sleep — so the monitor must report edges, not messages, or the
    deck gets told to sleep twice and the agent reconnects twice on the way back.
    """

    def _monitor(self):
        from deckhost.power import PowerMonitor

        events = []
        monitor = PowerMonitor(
            on_sleep=lambda: events.append("sleep"),
            on_wake=lambda: events.append("wake"),
        )
        return monitor, events

    def test_sleep_then_wake(self):
        monitor, events = self._monitor()
        monitor.on_display_state(0)
        monitor.on_display_state(1)
        self.assertEqual(events, ["sleep", "wake"])

    def test_repeated_signals_report_one_edge(self):
        from deckhost.power import PBT_APMSUSPEND

        monitor, events = self._monitor()
        monitor.on_display_state(0)
        monitor._on_broadcast(PBT_APMSUSPEND, 0, None)
        monitor.on_display_state(0)
        self.assertEqual(events, ["sleep"])

    def test_wake_without_sleep_is_ignored(self):
        monitor, events = self._monitor()
        monitor.on_display_state(1)
        self.assertEqual(events, [])

    def test_dimmed_display_is_not_sleep(self):
        """State 2 means the screen dimmed and the user is still there."""
        monitor, events = self._monitor()
        monitor.on_display_state(2)
        self.assertEqual(events, [])

    def test_apm_messages_are_a_backstop(self):
        from deckhost.power import PBT_APMRESUMEAUTOMATIC, PBT_APMSUSPEND

        monitor, events = self._monitor()
        monitor._on_broadcast(PBT_APMSUSPEND, 0, None)
        monitor._on_broadcast(PBT_APMRESUMEAUTOMATIC, 0, None)
        self.assertEqual(events, ["sleep", "wake"])

    def test_raising_callback_does_not_kill_the_pump(self):
        """A failed callback must not cost every later power event."""
        from deckhost.power import PowerMonitor

        def boom():
            raise RuntimeError("callback exploded")

        monitor = PowerMonitor(on_sleep=boom, on_wake=boom)
        with self.assertLogs("deckhost.power", level="ERROR"):
            monitor.on_display_state(0)

        self.assertTrue(monitor._asleep, "the edge still counted")


class PortDiscoveryTests(unittest.TestCase):
    def test_autodetected_port_is_rediscovered_each_open(self):
        """Caching the discovered port is what made a COM renumber unrecoverable.

        open() used to write its find back into self.port, so `self.port or self.discover()`
        short-circuited from then on. When Windows moved the deck after a suspend, the agent
        reopened a port that no longer existed until it was restarted — 1273 times in one
        overnight run.
        """
        link = SerialLink()
        self.assertFalse(link._pinned)

        link.port = "COM6"  # as if a previous open had found this one
        self.assertFalse(link._pinned, "discovery must stay live")

    def test_explicit_port_is_honoured(self):
        link = SerialLink("COM9")
        self.assertTrue(link._pinned)
        self.assertEqual(link.port, "COM9")


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_pushes_layout_on_rev_mismatch(self):
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        link = SimulatedLink([], rev=-1, step_delay=0.01)

        host = DeckHost(
            link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
        )
        await host.run(duration=0.4)

        kinds = [f["t"] for f in link.received]
        self.assertIn("welcome", kinds)
        self.assertIn("layout", kinds)
        self.assertEqual(link.rev, config.rev)

    @staticmethod
    def _mixed_sequence(config: DeckConfig) -> tuple[str, list[dict]]:
        """Finds a button whose action is a seq mixing agent-side and device-local steps.

        Looked up rather than named. This test hardcoded a button id and was broken twice by
        ordinary layout edits renaming it — a failure that says nothing about the behaviour
        under test, and trains you to ignore the suite.
        """
        for button_id, button in config.buttons.items():
            action = button.get("action") or {}
            if action.get("type") != "seq":
                continue

            steps = action.get("steps", [])
            has_local = any(
                s.get("type") != "delay" and is_device_local(s) for s in steps
            )
            has_agent = any(not is_device_local(s) for s in steps)
            if has_local and has_agent:
                return button_id, steps

        raise unittest.SkipTest("shipped deck.json has no mixed sequence to exercise")

    async def test_mixed_sequence_calls_back_for_local_steps(self):
        """The ordering guarantee: the agent sequences, the device performs local steps."""
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        button_id, steps = self._mixed_sequence(config)

        link = SimulatedLink([button_id], rev=1, step_delay=0.01)
        host = DeckHost(
            link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
        )

        # Wait out whatever delays the sequence actually contains, rather than a magic number
        # that silently becomes too short when someone lengthens a step.
        delay_s = sum(s.get("ms", 0) for s in steps if s.get("type") == "delay") / 1000
        await host.run(duration=delay_s + 0.7)

        local_steps = [s for s in steps if s.get("type") != "delay" and is_device_local(s)]
        self.assertEqual(len(link.hid_exec_calls), len(local_steps))
        self.assertEqual(
            [c["type"] for c in link.hid_exec_calls],
            [s["type"] for s in local_steps],
        )

    async def test_unknown_button_produces_toast_not_crash(self):
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        link = SimulatedLink(["no.such.button"], rev=1, step_delay=0.01)

        host = DeckHost(
            link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
        )
        await host.run(duration=0.4)

        toasts = [f for f in link.received if f["t"] == "toast"]
        self.assertEqual(len(toasts), 1)
        self.assertEqual(toasts[0]["lvl"], "error")

    async def test_stats_pushed_once_session_is_up(self):
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        link = SimulatedLink([], rev=1, step_delay=0.01)

        host = DeckHost(
            link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
        )
        await host.run(duration=1.4)

        stats_frames = [f for f in link.received if f["t"] == "stats"]
        self.assertGreaterEqual(len(stats_frames), 1)
        self.assertIn("cpu", stats_frames[0])

    async def test_protocol_mismatch_refuses_session(self):
        config = DeckConfig.load(REPO / "sdcard" / "deck.json")
        link = SimulatedLink([], rev=1, step_delay=0.01)

        host = DeckHost(
            link, config, ActionRunner(dry_run=True), StatsCollector(synthetic=True)
        )
        await host._on_frame({"t": "hello", "proto": 999, "fw": "x", "rev": 1})

        self.assertFalse(host.session_up)


class ConfigReuseTests(unittest.TestCase):
    """from_raw() and problems() exist so the editor validates without a temp file.

    Both are refactors of load()/validate() rather than new rules, so what is worth testing is
    that they still say exactly what the originals said — a second copy of the checks that
    drifts is worse than no editor validation at all, because it would report problems the
    agent does not have and miss the ones it does.
    """

    def _deck(self) -> dict:
        return json.loads((REPO / "sdcard" / "deck.json").read_text(encoding="utf-8"))

    def test_from_raw_matches_load(self):
        from_disk = DeckConfig.load(REPO / "sdcard" / "deck.json")
        in_memory = DeckConfig.from_raw(self._deck(), path=REPO / "sdcard" / "deck.json")

        self.assertEqual(in_memory.rev, from_disk.rev)
        self.assertEqual(set(in_memory.buttons), set(from_disk.buttons))
        self.assertEqual(in_memory.asset_root, from_disk.asset_root)

    def test_problems_is_empty_exactly_when_validate_passes(self):
        config = DeckConfig.from_raw(self._deck(), validate=False)
        self.assertEqual(config.problems(), [])
        config.validate()  # must not raise

    def test_problems_lists_what_validate_would_have_raised(self):
        broken = self._deck()
        broken["themes"][0]["accent"] = "nonsense"
        broken["pages"][0]["buttons"][0]["icon"] = "not_a_symbol"

        config = DeckConfig.from_raw(broken, validate=False)
        problems = config.problems()
        self.assertEqual(len(problems), 2)

        with self.assertRaises(ConfigError) as caught:
            config.validate()
        for problem in problems:
            self.assertIn(problem, str(caught.exception))

    def test_validate_still_raises_by_default(self):
        broken = self._deck()
        broken["themes"][0]["radius"] = "16"  # a string where the firmware wants an int
        with self.assertRaises(ConfigError):
            DeckConfig.from_raw(broken)


class Mdi1Tests(unittest.TestCase):
    """The container now has an inverse, which is the only reason it can be checked at all.

    encode() alone could only ever be tested against hand-written expectations. With decode()
    the format has to agree with itself, and the round trip catches a whole class of stride and
    byte-order mistakes that a fixed test vector walks straight past.
    """

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def test_round_trip_survives_rgb565_quantisation(self):
        from PIL import Image

        from deckhost import mdi1

        source = Image.new("RGB", (5, 3))
        for x in range(5):
            for y in range(3):
                source.putpixel((x, y), (x * 50, y * 80, 255 - x * 40))

        width, height, pixels = mdi1.decode(mdi1.encode(source))
        self.assertEqual((width, height), (5, 3))

        decoded = Image.frombytes("RGB", (width, height), pixels)
        for x in range(5):
            for y in range(3):
                for got, want in zip(decoded.getpixel((x, y)), source.getpixel((x, y))):
                    # 5 bits of red and blue, 6 of green: the error is bounded, not zero.
                    self.assertLessEqual(abs(got - want), 8)

    def test_encoding_a_decoded_image_is_stable(self):
        """Quantisation happens once. A file re-saved through the tools must not drift."""
        from PIL import Image

        from deckhost import mdi1

        source = Image.new("RGB", (4, 4), (137, 91, 200))
        once = mdi1.encode(source)
        width, height, pixels = mdi1.decode(once)
        twice = mdi1.encode(Image.frombytes("RGB", (width, height), pixels))
        self.assertEqual(once, twice)

    def test_extremes_are_exact(self):
        from PIL import Image

        from deckhost import mdi1

        image = Image.new("RGB", (2, 1))
        image.putpixel((0, 0), (0, 0, 0))
        image.putpixel((1, 0), (255, 255, 255))
        _w, _h, pixels = mdi1.decode(mdi1.encode(image))
        self.assertEqual(tuple(pixels), (0, 0, 0, 255, 255, 255))

    def test_the_shipped_wallpapers_decode(self):
        from deckhost import mdi1

        walls = sorted((REPO / "sdcard" / "wall").glob("*.bin"))
        self.assertTrue(walls, "no wallpapers to check")
        for wall in walls:
            with self.subTest(wall=wall.name):
                width, height, pixels = mdi1.decode(wall.read_bytes())
                self.assertEqual((width, height), (800, 480))
                self.assertEqual(len(pixels), 800 * 480 * 3)

    def test_the_three_header_checks_are_the_firmware_s(self):
        from deckhost import mdi1

        with self.assertRaises(mdi1.Mdi1Error):
            mdi1.decode(b"MDI")  # too short for a header
        with self.assertRaises(mdi1.Mdi1Error):
            mdi1.decode(b"PNG\x00" + bytes(8))  # wrong magic
        with self.assertRaises(mdi1.Mdi1Error):
            mdi1.decode(b"MDI1" + bytes([4, 0, 4, 0]) + bytes(10))  # short body

    def test_make_assets_still_exports_the_format(self):
        """tools/make_assets.py re-exports these, and AssetFormatTests calls them by that name."""
        sys.path.insert(0, str(REPO / "tools"))
        import make_assets

        from deckhost import mdi1

        self.assertIs(make_assets.encode, mdi1.encode)
        self.assertIs(make_assets.rgb_to_rgb565, mdi1.rgb_to_rgb565)
        self.assertEqual(make_assets.MAGIC, mdi1.MAGIC)


class ImagePipelineTests(unittest.TestCase):
    """The wallpaper pipeline has two callers, and only one of them has a Python to run.

    tools/make_assets.py drives it from a command line; the theme builder calls it in-process.
    The builder used to shell out to the script instead, which worked from a checkout and could
    not possibly work from the packaged exe — sys.executable there is the builder itself, and
    app.py is not a file on disk at all once PyInstaller has folded it into the archive. The
    failure was a button that reported "not next to this build" on the one install where the
    button was the only way to convert anything.

    So the pipeline moved into the package and the script imports it. These tests hold that
    shape: one implementation, and no assumption that an interpreter is lying around.
    """

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        sys.path.insert(0, str(REPO / "tools"))

    def test_make_assets_delegates_rather_than_reimplementing(self):
        import make_assets

        from deckhost import images

        for name in ("cover_crop", "dim", "blur", "SCREEN_W", "SCREEN_H", "DEFAULT_ICON_BG"):
            with self.subTest(name=name):
                self.assertIs(getattr(make_assets, name), getattr(images, name))

    def test_the_builder_never_needs_an_interpreter(self):
        """A frozen app has no Python to shell out to, so it must not try.

        Checked against the source rather than by running it, because the failure only appears
        in the packaged build and the test suite does not produce one. Parsed rather than
        grepped: the module docstring names make_assets.py on purpose, to say where the shared
        pipeline is driven from, and a text search cannot tell prose from a call.
        """
        import ast

        tree = ast.parse((REPO / "agent" / "deckbuilder" / "app.py").read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name.split(".")[0], "subprocess",
                        "app.py imports subprocess — the exe has no interpreter to spawn",
                    )
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(
                    (node.module or "").split(".")[0], "subprocess",
                    "app.py imports subprocess — the exe has no interpreter to spawn",
                )
            elif isinstance(node, ast.Attribute):
                self.assertFalse(
                    node.attr == "executable"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "sys",
                    "app.py reads sys.executable, which in a frozen build is the builder itself",
                )

    def test_every_check_the_save_button_is_given_is_a_check_it_makes(self):
        """`_can_save(self, problems)` took the validator's list and never looked at it.

        It read as though the check existed, so the save button stayed lit for a layout the
        agent refuses — which makes the agent exit 2 at logon, and the deck keeps whatever it
        had. Static, because reaching this through tkinter means standing up a window.
        """
        import ast

        tree = ast.parse((REPO / "agent" / "deckbuilder" / "app.py").read_text(encoding="utf-8"))
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_can_save"
        )

        parameters = [arg.arg for arg in target.args.args if arg.arg != "self"]
        used = {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}
        for name in parameters:
            with self.subTest(parameter=name):
                self.assertIn(name, used, f"_can_save takes {name!r} and ignores it")

    def test_wallpaper_fills_the_panel_from_any_aspect_ratio(self):
        import tempfile

        from PIL import Image

        from deckhost import images, mdi1

        with tempfile.TemporaryDirectory() as tmp:
            for size in ((600, 1000), (2000, 400), (800, 480)):
                with self.subTest(size=size):
                    src = Path(tmp) / f"{size[0]}x{size[1]}.png"
                    Image.new("RGB", size, (10, 120, 200)).save(src)
                    dst = Path(tmp) / f"{size[0]}.bin"

                    self.assertEqual(images.wallpaper(src, dst), (800, 480))
                    width, height, _pixels = mdi1.decode(dst.read_bytes())
                    self.assertEqual((width, height), (800, 480))

    def test_wallpaper_creates_the_folder_it_writes_into(self):
        """The builder points this at sdcard/wall/, which need not exist on a fresh card tree."""
        import tempfile

        from PIL import Image

        from deckhost import images

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            Image.new("RGB", (100, 100), (1, 2, 3)).save(src)
            dst = Path(tmp) / "wall" / "new.bin"
            images.wallpaper(src, dst)
            self.assertTrue(dst.is_file())

    def test_converting_restamps_so_the_card_check_stays_honest(self):
        """make_assets restamps on every run; the builder has to do the same by hand."""
        import tempfile

        from PIL import Image

        from deckhost import images
        from deckhost.assets import asset_stamp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deck.json").write_text("{}", encoding="utf-8")
            before = asset_stamp(root)

            src = root / "src.png"
            Image.new("RGB", (60, 60), (9, 9, 9)).save(src)
            images.wallpaper(src, root / "wall" / "x.bin")
            src.unlink()  # the source is not part of the card

            self.assertNotEqual(asset_stamp(root), before)
            write_stamp(root)
            self.assertEqual(read_stamp(root), asset_stamp(root))

    def test_a_filename_from_a_photo_becomes_addressable(self):
        from deckbuilder.app import _safe_stem

        self.assertEqual(_safe_stem("Holiday 2024 (1)"), "holiday-2024-1")
        self.assertEqual(_safe_stem("DSC_0042"), "dsc_0042")
        self.assertEqual(_safe_stem("...."), "wallpaper")


class DeckFileCanonicalTests(unittest.TestCase):
    """sdcard/deck.json has to stay in the shape a serialiser writes.

    This is the property that lets the editor own the whole file. It used to splice, which meant
    hand-formatting elsewhere in the file was safe; now a save renders everything, so a file that
    has drifted out of canonical form gets silently reflowed on the next save and the diff is
    unreadable. The check belongs here rather than in the editor, because the thing that breaks
    it is a hand edit committed to the repo, not anything the editor does.
    """

    def _text(self) -> str:
        return (REPO / "sdcard" / "deck.json").read_bytes().decode("utf-8")

    def test_the_shipped_file_is_canonical(self):
        from deckbuilder import writer

        self.assertTrue(
            writer.is_canonical(self._text()),
            "sdcard/deck.json has drifted from json.dumps(indent=2); the next editor save "
            "will reformat it. Run the normalisation from the v2 plan and commit that alone.",
        )

    def test_formatting_costs_nothing_on_the_wire(self):
        """Why the normalisation was safe to do: the deck never sees the indentation."""
        from deckbuilder import budget, writer

        raw = json.loads(self._text())
        compacted = json.loads(json.dumps(raw, separators=(",", ":")))
        self.assertEqual(
            budget.frame_bytes(raw["rev"], raw),
            budget.frame_bytes(raw["rev"], json.loads(writer.canonical(compacted))),
        )

    def test_the_layout_is_pure_ascii(self):
        """Not style. The meter measures with ensure_ascii, and the file must agree.

        A label containing `→` weighs six bytes on the wire and prints as one character here, so
        the day this stops being true is the day the byte meter starts under-reporting — and
        under-reporting is the failure mode the meter exists to prevent.
        """
        self._text().encode("ascii")  # raises, with the offending character, if it ever is not


class DeckWriterTests(unittest.TestCase):
    """The theme builder rewrites deck.json, so it has to leave the rest of it alone.

    The writer used to splice, carrying everything from `"pages":` onward across byte for byte,
    and these tests were the argument that the splice was safe. It owns pages now, so the
    argument has changed shape but not subject: the file is rendered whole, and what has to be
    proved is that rewriting an unchanged file changes nothing at all, and that a save cannot
    touch a top-level key the writer does not own.
    """

    def _text(self) -> str:
        # Bytes, not read_text(): this file has CRLF endings and universal newline translation
        # would hand the writer something that does not match the disk.
        return (REPO / "sdcard" / "deck.json").read_bytes().decode("utf-8")

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(REPO / "sdcard" / "deck.json")

    def _build(self, original: str, **changes):
        """Rebuilds the file from its own contents, with `changes` applied."""
        from deckbuilder import writer

        data = json.loads(original)
        args = {key: json.loads(json.dumps(data[key])) for key in writer.OWNED}
        args.update(changes)
        return writer.build(original, **args)

    def test_rewriting_an_unchanged_file_is_byte_identical(self):
        """The acceptance test for the whole module: open, save, `git diff` is empty."""
        original = self._text()
        result, reformatted = self._build(original)

        self.assertFalse(reformatted, "the shipped file is not in canonical form")
        self.assertEqual(result, original)

    def test_changing_a_colour_leaves_pages_untouched(self):
        original = self._text()
        data = json.loads(original)
        themes = json.loads(json.dumps(data["themes"]))
        themes[0]["accent"] = "#ff0000"

        result, reformatted = self._build(original, themes=themes, rev=data["rev"] + 1)
        self.assertFalse(reformatted)
        # Semantic, not textual. The old writer proved this by comparing the tail of the file
        # byte for byte, which it could only do while pages were a contiguous suffix it never
        # rendered. Now they are rendered like everything else, so the claim worth making is
        # that they still mean exactly what they meant.
        self.assertEqual(json.loads(result)["pages"], data["pages"])
        self.assertEqual(json.loads(result)["themes"][0]["accent"], "#ff0000")

    def test_the_diff_is_only_the_lines_that_changed(self):
        """Why the file was normalised: a one-colour save is still a two-line diff."""
        original = self._text()
        data = json.loads(original)
        themes = json.loads(json.dumps(data["themes"]))
        themes[0]["accent"] = "#ff0000"

        result, _ = self._build(original, themes=themes, rev=data["rev"] + 1)
        changed = [
            (a, b)
            for a, b in zip(original.splitlines(), result.splitlines())
            if a != b
        ]
        self.assertEqual(len(changed), 2, changed)  # rev and the colour, nothing else

    def test_crlf_survives_a_save(self):
        from deckbuilder import writer

        original = self._text()
        self.assertEqual(writer.newline_style(original), "\r\n", "fixture is no longer CRLF")

        result, _ = self._build(original, rev=99)
        self.assertEqual(writer.newline_style(result), "\r\n")
        # Every LF belongs to a CRLF — no line was left half-converted.
        self.assertNotIn("\n", result.replace("\r\n", ""))

    def test_lf_files_stay_lf(self):
        original = self._text().replace("\r\n", "\n")
        result, reformatted = self._build(original)
        self.assertFalse(reformatted)
        self.assertEqual(result, original)

    def test_a_file_that_was_formatted_by_hand_says_so_before_the_diff_does(self):
        """Saving normalises it. That is allowed, but it must never be a surprise."""
        from deckbuilder import writer

        original = json.dumps(json.loads(self._text()), indent=4) + "\n"
        data = json.loads(original)
        result, reformatted = self._build(original, rev=42)

        self.assertTrue(reformatted, "reformatted the file without admitting it")
        self.assertIn("reformatted", writer.SaveResult(Path("x"), 42, True).warning or "")
        self.assertEqual(json.loads(result)["rev"], 42)
        self.assertEqual(json.loads(result)["pages"], data["pages"])

    def test_mixed_line_endings_are_never_claimed_to_survive(self):
        from deckbuilder import writer

        original = self._text().replace("\r\n", "\n", 5)
        self.assertIsNone(writer.newline_style(original))
        _result, reformatted = self._build(original)
        self.assertTrue(reformatted)

    def test_a_key_the_writer_does_not_own_comes_through_untouched(self):
        """The honest successor to the old "the tail is unchanged" check.

        A future firmware key has to survive a save it knows nothing about, wherever in the file
        it sits — including before `pages`, which the splice could never have protected.
        """
        from deckbuilder import writer

        data = json.loads(self._text())
        spiked = {"schema": {"note": "not ours"}, **data, "trailing": [1, 2, 3]}
        original = writer.canonical(spiked)

        result, _ = self._build(original, rev=data["rev"] + 1)
        written = json.loads(result)
        self.assertEqual(written["schema"], {"note": "not ours"})
        self.assertEqual(written["trailing"], [1, 2, 3])
        self.assertEqual(list(written), list(spiked), "top-level keys were reordered")

    def test_the_scope_guard_catches_a_writer_that_overreaches(self):
        from deckbuilder import writer

        before = {"rev": 1, "nav": {"macros": "x"}, "themes": [], "settings": {}, "pages": []}
        writer.check_scope(before, dict(before, rev=2, themes=[{"name": "a"}]))

        with self.assertRaises(writer.WriteError):
            writer.check_scope(before, dict(before, nav={"macros": "y"}))
        with self.assertRaises(writer.WriteError):
            writer.check_scope(before, {k: v for k, v in before.items() if k != "nav"})
        with self.assertRaises(writer.WriteError):
            writer.check_scope(before, dict(sorted(before.items())))

    def test_an_owned_key_the_file_never_had_may_be_appended(self):
        """A deck.json with no pages at all gains one the first time a page is added."""
        from deckbuilder import writer

        before = {"rev": 1, "themes": [], "settings": {}}
        writer.check_scope(before, dict(before, pages=[{"id": "home"}]))

    def test_the_written_file_still_loads(self):
        import tempfile

        from deckbuilder import writer

        doc = self._doc()
        doc.new_theme("Scratch")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.json"
            path.write_bytes(self._text().encode("utf-8"))
            writer.write(
                path, themes=doc.themes, settings=doc.settings, pages=doc.pages,
                rev=doc.next_rev(),
            )

            reloaded = DeckConfig.load(path)  # validates, and raises if it cannot
            self.assertEqual(reloaded.rev, doc.next_rev())
            self.assertEqual(len(reloaded.raw["themes"]), len(doc.themes))

    def test_writing_leaves_nothing_behind_in_the_folder(self):
        """Anything left under sdcard/ would change the card's asset stamp for good."""
        import tempfile

        from deckbuilder import writer

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "deck.json"
            path.write_bytes(self._text().encode("utf-8"))

            doc = self._doc()
            writer.write(
                path, themes=doc.themes, settings=doc.settings, pages=doc.pages, rev=77,
                backup_dir=folder / "elsewhere",
            )

            self.assertEqual(
                sorted(p.name for p in folder.iterdir()), ["deck.json", "elsewhere"]
            )

    def test_a_stale_temp_file_is_swept(self):
        import tempfile

        from deckbuilder import writer

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / ".deck.json.999.tmp").write_text("left by a crash", encoding="utf-8")
            writer.sweep_temp_files(folder)
            self.assertEqual(list(folder.iterdir()), [])


class ThemeShapeTests(unittest.TestCase):
    """Themes the editor builds must be the same shape as the ones already in the file.

    ConfigShapeTests enforces this on what is committed. These enforce it on what the editor
    would produce, which is the only way the two can be guaranteed to keep agreeing once themes
    stop being written by hand.
    """

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(REPO / "sdcard" / "deck.json")

    def _reference(self) -> tuple:
        deck = json.loads((REPO / "sdcard" / "deck.json").read_text(encoding="utf-8"))
        return tuple(deck["themes"][0])

    def test_the_hardcoded_field_order_matches_the_shipped_file(self):
        """The literal is only a fallback for an empty file, and it must not drift."""
        from deckbuilder.model import THEME_FIELD_ORDER

        self.assertEqual(THEME_FIELD_ORDER, self._reference())

    def test_the_template_covers_every_field(self):
        from deckbuilder.model import THEME_FIELD_ORDER, THEME_TEMPLATE

        self.assertEqual(set(THEME_TEMPLATE), set(THEME_FIELD_ORDER))

    def test_a_new_theme_keeps_the_canonical_order(self):
        doc = self._doc()
        doc.new_theme("Fresh")
        self.assertEqual(tuple(doc.themes[-1]), self._reference())

    def test_a_duplicated_theme_keeps_the_canonical_order(self):
        doc = self._doc()
        doc.duplicate(0)
        self.assertEqual(tuple(doc.themes[1]), self._reference())

    def test_a_new_theme_writes_its_defaults_down(self):
        """"Unset" is a value in this format, not an absent key."""
        doc = self._doc()
        doc.new_theme("Fresh")
        theme = doc.themes[-1]

        self.assertIsNone(theme["dim_opa"], "dim_opa must defer to config.h")
        self.assertIsNone(theme["flip180"], "flip180 must defer to config.h")
        self.assertEqual(theme["display"], "")
        self.assertEqual(theme["wallpaper"], "")

    def test_a_new_theme_gets_a_name_that_is_not_taken(self):
        doc = self._doc()
        doc.new_theme("Midnight")
        self.assertNotEqual(doc.themes[-1]["name"], "Midnight")

    def test_a_drifted_theme_is_refused_before_it_reaches_the_writer(self):
        """The splice used to catch this by failing to regenerate the block.

        Nothing structural catches it now — a dump will happily write a theme with a missing
        key — so the guard has to be explicit, and it has to be the thing the save button reads.
        """
        doc = self._doc()
        del doc.themes[0]["idle"]
        self.assertTrue(doc.shape_problems())

    def test_renaming_follows_the_references(self):
        """A theme is referenced by name, and a stale name fails silently on the device."""
        doc = self._doc()
        doc.settings["theme"] = doc.themes[1]["name"]
        doc.rename(1, "Renamed")

        self.assertEqual(doc.settings["theme"], "Renamed")
        self.assertEqual(doc.problems(), [])

    def test_renaming_follows_theme_actions_including_nested_ones(self):
        """Poked through `doc.pages`, which is the copy a save actually writes.

        This test used to reach into `doc.raw["pages"]`, and would have gone on passing while
        renaming quietly stopped following references — because `rename()` walked the same raw
        dict the test was inspecting, rather than the list the document owns.
        """
        doc = self._doc()
        page = next(p for p in doc.pages if p.get("buttons"))
        page["buttons"][0]["action"] = {
            "type": "seq",
            "steps": [{"type": "theme", "target": doc.themes[0]["name"]}],
        }
        doc.rename(0, "Elsewhere")

        self.assertEqual(page["buttons"][0]["action"]["steps"][0]["target"], "Elsewhere")
        saved = doc.candidate_raw()["pages"]
        self.assertEqual(saved[doc.pages.index(page)]["buttons"][0]["action"]["steps"][0]
                         ["target"], "Elsewhere")

    def test_rev_bumps_only_when_something_changed(self):
        doc = self._doc()
        self.assertFalse(doc.dirty)
        self.assertEqual(doc.next_rev(), doc.rev)

        doc.themes[0]["accent"] = "#123456"
        self.assertTrue(doc.dirty)
        self.assertEqual(doc.next_rev(), doc.rev + 1)

    def test_the_last_theme_cannot_be_deleted(self):
        from deckbuilder.model import ModelError

        doc = self._doc()
        while len(doc.themes) > 1:
            doc.delete(0)
        with self.assertRaises(ModelError):
            doc.delete(0)


class LayoutShapeTests(unittest.TestCase):
    """Pages and buttons follow the rule themes already followed: one key set, one order.

    ConfigShapeTests enforces it on what is committed; these enforce it on what the editor would
    produce. The editor is about to start writing both, and the writer will no longer catch a
    drifted shape structurally — a dump writes whatever it is handed.
    """

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(REPO / "sdcard" / "deck.json")

    def _deck(self) -> dict:
        return json.loads((REPO / "sdcard" / "deck.json").read_bytes().decode("utf-8"))

    def test_the_hardcoded_orders_match_the_shipped_file(self):
        from deckbuilder.model import BUTTON_FIELD_ORDER, PAGE_FIELD_ORDER

        deck = self._deck()
        first_button = next(
            b for p in deck["pages"] for b in (p.get("buttons") or [])
        )
        self.assertEqual(PAGE_FIELD_ORDER, tuple(deck["pages"][0]))
        self.assertEqual(BUTTON_FIELD_ORDER, tuple(first_button))

    def test_the_templates_cover_every_field(self):
        from deckbuilder.model import (
            BUTTON_FIELD_ORDER,
            BUTTON_TEMPLATE,
            PAGE_FIELD_ORDER,
            PAGE_TEMPLATE,
        )

        self.assertEqual(set(PAGE_TEMPLATE), set(PAGE_FIELD_ORDER))
        self.assertEqual(set(BUTTON_TEMPLATE), set(BUTTON_FIELD_ORDER))

    def test_the_shipped_file_has_no_shape_problems(self):
        self.assertEqual(self._doc().shape_problems(), [])
        self.assertEqual(self._doc().notices(), [])

    def test_the_button_shape_comes_from_the_first_button_anywhere(self):
        """A deck whose first page is the ten-key has no buttons on pages[0]."""
        from deckbuilder.model import BUTTON_FIELD_ORDER, DeckDoc

        deck = self._deck()
        deck["pages"] = [p for p in deck["pages"] if p["id"] == "numpad"] + [
            p for p in deck["pages"] if p["id"] != "numpad"
        ]
        doc = DeckDoc(
            path=REPO / "sdcard" / "deck.json", original="", raw=deck,
            themes=deck["themes"], settings=deck["settings"], pages=deck["pages"],
            field_order=tuple(deck["themes"][0]),
            page_order=tuple(deck["pages"][0]),
            button_order=DeckDoc._order_of(DeckDoc._first_button(deck["pages"]), ()),
        )
        self.assertEqual(doc.button_order, BUTTON_FIELD_ORDER)

    def test_a_pos_is_null_or_all_four_in_order(self):
        doc = self._doc()
        button = doc.pages[0]["buttons"][0]

        button["pos"] = {"col": 1, "row": 0, "w": 1, "h": 1}
        self.assertEqual(doc.shape_problems(), [])

        button["pos"] = {"col": 1, "row": 0}
        self.assertIn("pos must be null", " | ".join(doc.shape_problems()))

        button["pos"] = {"row": 0, "col": 1, "w": 1, "h": 1}
        self.assertIn("in that order", " | ".join(doc.shape_problems()))

        button["pos"] = {"col": "1", "row": 0, "w": 1, "h": 1}
        self.assertIn("pos.col is '1'", " | ".join(doc.shape_problems()))

    def test_a_firmware_page_carrying_a_grid_or_buttons_is_caught(self):
        """Both are parsed, ignored, and paid for in wire bytes forever."""
        doc = self._doc()
        numpad = next(p for p in doc.pages if p["type"] == "numpad")

        numpad["grid"] = {"cols": 4, "rows": 3}
        self.assertIn("grid should be null", " | ".join(doc.shape_problems()))

        numpad["grid"] = None
        numpad["buttons"] = [json.loads(json.dumps(doc.pages[0]["buttons"][0]))]
        self.assertIn("never built", " | ".join(doc.shape_problems()))

    def test_a_drifted_first_button_is_reported_rather_than_adopted_in_silence(self):
        """The hole in shape_problems(): it compares against the order derived from the file."""
        from deckbuilder.model import DeckDoc

        deck = self._deck()
        for page in deck["pages"]:
            for button in page.get("buttons") or []:
                button["stat"] = None

        doc = DeckDoc(
            path=REPO / "sdcard" / "deck.json", original="", raw=deck,
            themes=deck["themes"], settings=deck["settings"], pages=deck["pages"],
            field_order=tuple(deck["themes"][0]),
            page_order=tuple(deck["pages"][0]),
            button_order=DeckDoc._order_of(DeckDoc._first_button(deck["pages"]), ()),
        )

        # Adopted, so a new firmware field needs no change here...
        self.assertEqual(doc.shape_problems(), [])
        # ...but said out loud, because the other way to get here is a bad hand edit.
        self.assertIn("extra: stat", " | ".join(doc.notices()))


class GridGeometryTests(unittest.TestCase):
    """The preview has to place tiles exactly where ui_builder.cpp would, including the edges.

    `col >= cols` has no firmware log at all — the tile is created at its computed x and simply
    extends past the 800px edge — so a hole in this arithmetic shows up on the deck as a tile
    that is not there, with nothing anywhere saying why.
    """

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def _deck(self) -> dict:
        return json.loads((REPO / "sdcard" / "deck.json").read_bytes().decode("utf-8"))

    def test_the_grid_fallback_matches_the_firmware(self):
        from deckbuilder.render import grid_size

        source = (REPO / "firmware" / "multi_deck" / "ui_builder.cpp").read_text(
            encoding="utf-8"
        )
        self.assertRegex(source, r"cols\s*>\s*0\s*\?\s*.*?cols\s*:\s*4")
        self.assertRegex(source, r"rows\s*>\s*0\s*\?\s*.*?rows\s*:\s*3")

        self.assertEqual(grid_size({"cols": 5, "rows": 4}), (5, 4))
        self.assertEqual(grid_size({"cols": 0, "rows": 0}), (4, 3))
        self.assertEqual(grid_size({"cols": -2, "rows": -1}), (4, 3))
        self.assertEqual(grid_size({"cols": None, "rows": None}), (4, 3))
        self.assertEqual(grid_size(None), (4, 3))

    def test_a_null_pos_with_a_span_renders(self):
        """Legal on the device — ArduinoJson's `|` treats null and absent alike — and it used
        to crash the preview with `None < 0`."""
        from deckbuilder import render

        deck = self._deck()
        deck["pages"][0]["buttons"][0]["pos"] = {"col": None, "row": None, "w": 2, "h": 1}
        preview = render.render_page(
            deck, deck["themes"][0], "launch", asset_root=REPO / "sdcard"
        )
        self.assertEqual(preview.warnings, [])

    def test_the_pos_fields_are_read_the_way_arduinojson_reads_them(self):
        from deckbuilder.render import _int_or

        source = (REPO / "firmware" / "multi_deck" / "deck_config.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn('pos["col"] | -1', source)
        self.assertIn('pos["w"] | 1', source)

        self.assertEqual(_int_or(None, -1), -1)
        self.assertEqual(_int_or("2", -1), -1)
        self.assertEqual(_int_or(True, -1), -1)
        self.assertEqual(_int_or(0, -1), 0)
        self.assertEqual(_int_or(3, -1), 3)

    def test_a_tile_that_spans_off_the_grid_is_reported(self):
        from deckbuilder import render

        deck = self._deck()
        deck["pages"][0]["buttons"][0]["pos"] = {"col": 3, "row": 0, "w": 2, "h": 1}
        preview = render.render_page(
            deck, deck["themes"][0], "launch", asset_root=REPO / "sdcard"
        )
        self.assertIn("past the 4-column grid", " | ".join(preview.warnings))

    def test_the_nav_bar_draws_what_the_firmware_draws(self):
        """The firmware creates every tab and lets the container clip it; this used to break.

        Which made the preview look clean at exactly the point the deck becomes unusable.
        """
        from deckbuilder import render

        source = (REPO / "firmware" / "multi_deck" / "ui_builder.cpp").read_text(
            encoding="utf-8"
        )
        nav = re.search(r"void buildNav.*?\n\}", source, re.S)
        self.assertIsNotNone(nav, "could not find buildNav in ui_builder.cpp")
        self.assertNotIn("break", nav.group(0), "the firmware now stops early; so should we")

        deck = self._deck()
        spare = json.loads(json.dumps(deck["pages"][-1]))
        for index in range(2):
            extra = json.loads(json.dumps(spare))
            extra["id"], extra["title"] = f"extra{index}", f"Extra {index}"
            deck["pages"].append(extra)

        preview = render.render_page(
            deck, deck["themes"][0], "launch", asset_root=REPO / "sdcard"
        )
        self.assertIn("cut off at the edge", " | ".join(preview.warnings))

    def test_six_tabs_fit_and_the_seventh_does_not(self):
        from deckbuilder import render

        fits = [
            index
            for index in range(10)
            if render.PAD + index * render.TAB_STEP + render.TAB_W <= render.SCREEN_W
        ]
        self.assertEqual(len(fits), 6)


class AutoFlowTests(unittest.TestCase):
    """Where tiles land, and the one line of it that surprises everybody.

    `flow++` fires only in the auto branch (ui_builder.cpp:409-413), so a pinned tile consumes no
    slot and every auto tile after it shifts back one. That is why the editor pins a whole page at
    a time and never a single tile — and the strongest statement available about pin_all is that
    it changes no placement at all.
    """

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(REPO / "sdcard" / "deck.json")

    def test_pinning_a_page_moves_nothing(self):
        from deckbuilder import geometry

        doc = self._doc()
        grids = [i for i, p in enumerate(doc.pages) if p.get("type") == "grid"]
        before = {p["id"]: geometry.auto_flow(p) for p in doc.pages}

        for index in grids:
            doc.pin_all(index)

        self.assertEqual({p["id"]: geometry.auto_flow(p) for p in doc.pages}, before)
        for index in grids:
            self.assertTrue(doc.is_pinned(index))

    def test_unpinning_returns_the_file_to_where_it_started(self):
        doc = self._doc()
        before = json.dumps(doc.pages)
        doc.pin_all(0)
        doc.unpin_all(0)
        self.assertEqual(json.dumps(doc.pages), before)

    def test_pinning_one_tile_in_place_moves_the_others(self):
        """The measurement the whole Auto/Fixed design falls out of."""
        from deckbuilder import geometry

        doc = self._doc()
        page = doc.pages[0]
        before = geometry.auto_flow(page)

        # Pin the fourth tile exactly where auto-flow already put it.
        _id, col, row, _w, _h = before[3]
        page["buttons"][3]["pos"] = {"col": col, "row": row, "w": 1, "h": 1}

        after = geometry.auto_flow(page)
        moved = [b for b, a in zip(before, after) if b != a]
        self.assertEqual(len(moved), len(before) - 4, moved)

    def test_pinning_a_page_costs_what_the_plan_measured(self):
        from deckbuilder import budget

        doc = self._doc()
        before = budget.frame_bytes(doc.rev, doc.candidate_raw())
        for index, page in enumerate(doc.pages):
            if page.get("type") == "grid":
                doc.pin_all(index)

        after = budget.frame_bytes(doc.rev, doc.candidate_raw())
        # Not pinned to 600 exactly — the layout is allowed to grow. Pinned to the shape of the
        # trade: it is hundreds of bytes, which is why it is opt-in and states its price first.
        self.assertGreater(after - before, 400)
        self.assertLess(after, budget.LIMIT)

    def test_the_inverse_of_the_grid_is_exactly_the_grid(self):
        """Every cell of every plausible grid, because a hole here is a tile that is not there."""
        from deckbuilder import geometry

        for cols in range(1, 9):
            for rows in range(1, 7):
                cell_w, cell_h = geometry.cells(cols, rows)
                if cell_w <= 0 or cell_h <= 0:
                    continue
                for col in range(cols):
                    for row in range(rows):
                        x0, y0, x1, y1 = geometry.tile_box(col, row, 1, 1, cell_w, cell_h)
                        with self.subTest(grid=f"{cols}x{rows}", cell=f"{col},{row}"):
                            self.assertEqual(geometry.cell_at(x0, y0, cols, rows), (col, row))
                            self.assertEqual(
                                geometry.cell_at(x1 - 1, y1 - 1, cols, rows), (col, row)
                            )

    def test_the_gutters_belong_to_no_cell(self):
        """A drag released in the padding reports nothing rather than guessing at the near side."""
        from deckbuilder import geometry

        cols, rows = 4, 3
        cell_w, cell_h = geometry.cells(cols, rows)
        x0, y0, x1, _y1 = geometry.tile_box(0, 0, 1, 1, cell_w, cell_h)

        self.assertIsNone(geometry.cell_at(x0 - 1, y0, cols, rows))
        self.assertIsNone(geometry.cell_at(x1, y0, cols, rows))  # first pixel of the gutter
        self.assertIsNone(geometry.cell_at(x0, geometry.NAV_H // 2, cols, rows))  # the nav bar
        self.assertIsNone(geometry.cell_at(geometry.SCREEN_W - 1, y0, cols, rows))

    def test_the_nav_bar_capacity_is_six(self):
        from deckbuilder import geometry

        self.assertEqual(geometry.nav_capacity(), 6)
        # The seventh starts inside the screen and ends past it, which is the case worth naming:
        # it is drawn, clipped, and cannot be pressed.
        seventh = geometry.PAD + 6 * geometry.TAB_STEP
        self.assertLess(seventh, geometry.SCREEN_W)
        self.assertGreater(seventh + geometry.TAB_W, geometry.SCREEN_W)


class PreviewFeedbackTests(unittest.TestCase):
    """The preview reports what it drew, so a click can be turned back into a tile."""

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def _deck(self) -> dict:
        return json.loads((REPO / "sdcard" / "deck.json").read_bytes().decode("utf-8"))

    def _render(self, deck: dict, page_id: str = "launch"):
        from deckbuilder import render

        return render.render_page(
            deck, deck["themes"][0], page_id, asset_root=REPO / "sdcard"
        )

    def test_every_drawn_tile_is_reported_with_its_box(self):
        deck = self._deck()
        page = next(p for p in deck["pages"] if p["id"] == "launch")
        preview = self._render(deck)

        self.assertEqual(list(preview.boxes), [b["id"] for b in page["buttons"]])
        self.assertEqual(preview.cell, (4, 3))

    def test_a_hit_test_finds_the_tile_under_the_point(self):
        deck = self._deck()
        preview = self._render(deck)

        for button_id, (x0, y0, x1, y1) in preview.boxes.items():
            with self.subTest(button=button_id):
                self.assertEqual(preview.at((x0 + x1) // 2, (y0 + y1) // 2), button_id)

        self.assertIsNone(preview.at(2, 2), "the nav bar is not a tile")

    def test_the_topmost_tile_wins_an_overlap(self):
        """LVGL z-order is creation order, so the later tile is the one you would press."""
        deck = self._deck()
        page = next(p for p in deck["pages"] if p["id"] == "launch")

        # The last cell of the 4x3 grid, which auto-flow does not reach: pinning these two frees
        # their slots, so the ten remaining tiles fill (0,0) through (1,2) and stop.
        page["buttons"][0]["pos"] = {"col": 3, "row": 2, "w": 1, "h": 1}
        page["buttons"][1]["pos"] = {"col": 3, "row": 2, "w": 1, "h": 1}

        preview = self._render(deck)
        box = preview.boxes[page["buttons"][1]["id"]]
        self.assertEqual(
            preview.at((box[0] + box[2]) // 2, (box[1] + box[3]) // 2),
            page["buttons"][1]["id"],
        )

    def test_placement_says_where_auto_flow_actually_put_things(self):
        from deckbuilder import geometry

        deck = self._deck()
        page = next(p for p in deck["pages"] if p["id"] == "launch")
        preview = self._render(deck)

        self.assertEqual(
            [(k, *v) for k, v in preview.placed.items()], geometry.auto_flow(page)
        )

    def test_a_tile_the_firmware_would_not_build_is_named_rather_than_missing(self):
        deck = self._deck()
        page = next(p for p in deck["pages"] if p["id"] == "launch")
        page["buttons"][2]["pos"] = {"col": 0, "row": 9, "w": 1, "h": 1}

        preview = self._render(deck)
        self.assertEqual(preview.skipped, [page["buttons"][2]["id"]])
        self.assertNotIn(page["buttons"][2]["id"], preview.boxes)

    def test_a_page_that_is_not_a_grid_reports_no_cells(self):
        preview = self._render(self._deck(), "numpad")
        self.assertIsNone(preview.cell)
        self.assertEqual(preview.boxes, {})


class ButtonFormTests(unittest.TestCase):
    """The action editor must never quietly drop a key it does not understand."""

    def test_every_action_type_is_either_modelled_or_deliberately_not(self):
        from deckbuilder.button_form import KNOWN_KEYS, SIMPLE_FIELDS
        from deckhost.config import ACTION_TYPES

        self.assertEqual(set(KNOWN_KEYS), set(ACTION_TYPES))
        # seq is the one type with no simple row: its payload is a list of actions, and a
        # nested list-of-forms would be most of the work of the whole panel.
        self.assertEqual(set(ACTION_TYPES) - set(SIMPLE_FIELDS), {"seq"})

    def test_the_required_field_is_the_one_the_row_edits(self):
        """Otherwise the form can build an action the validator immediately rejects."""
        from deckbuilder.button_form import SIMPLE_FIELDS
        from deckhost.config import ACTION_REQUIRED

        for kind, required in ACTION_REQUIRED.items():
            if kind not in SIMPLE_FIELDS:
                continue
            with self.subTest(type=kind):
                self.assertEqual(SIMPLE_FIELDS[kind][0], required)

    def test_the_known_keys_cover_what_the_firmware_and_agent_read(self):
        source = (REPO / "firmware" / "multi_deck" / "deck_config.cpp").read_text(
            encoding="utf-8"
        )
        from deckbuilder.button_form import KNOWN_KEYS

        # Scoped to parseActionJson: parseTheme reads `src["bg"]` and friends out of a different
        # kind of object entirely, and the whole file would drag all seventeen theme tokens in.
        body = re.search(r"void parseActionJson\(.*?\n\}", source, re.S)
        self.assertIsNotNone(body, "could not find parseActionJson in deck_config.cpp")
        parsed = set(re.findall(r'src\["(\w+)"\]', body.group(0)))
        self.assertIn("keys", parsed, "the regex stopped matching what the firmware reads")
        modelled = set().union(*KNOWN_KEYS.values())
        self.assertTrue(
            parsed <= modelled,
            f"parseActionJson reads {sorted(parsed - modelled)}, which no form row models",
        )


class WindowTests(unittest.TestCase):
    """The two things that only go wrong once a real event loop is running.

    Every other test in this file drives the editor's objects directly, which is fast and covers
    the logic — and is exactly why both of these shipped. One needs Tk to deliver a queued
    virtual event; the other needs a font to be measured.
    """

    def setUp(self):
        try:
            import tkinter as tk

            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("tkinter or Pillow not installed")

        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display")

        self.root.withdraw()
        self.addCleanup(self.root.destroy)

        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.deck = Path(tmp) / "sdcard"
        shutil.copytree(REPO / "sdcard", self.deck)

    def _app(self):
        from deckbuilder.app import BuilderApp

        return BuilderApp(self.root, self.deck / "deck.json")

    def test_rows_are_tall_enough_for_the_text_in_them(self):
        """ttk's rowheight does not follow its font: it defaults to 20 and stays there.

        At any readable point size that clips the text into a smear. It is also the whole of the
        hit-testing bug that came with it — ttk lays rows out *and* identifies them from
        rowheight, so while the two disagreed, the row you clicked was not the row you could see.
        Asserted here rather than by clicking, because a click needs a mapped window and a test
        suite that flashes windows is one you stop running.
        """
        import tkinter.font as tkfont
        from tkinter import ttk

        app = self._app()
        style = ttk.Style()

        for points in (11, 12, 14, 18):
            with self.subTest(points=points):
                app._style(points)
                line = tkfont.Font(font=("Segoe UI", points)).metrics("linespace")
                self.assertGreaterEqual(int(style.lookup("Treeview", "rowheight")), line)

    def test_selecting_a_row_settles_instead_of_refreshing_forever(self):
        """<<TreeviewSelect>> arrives queued, not only synchronously.

        Rebuilding the tree restores the selection, which fires the event *after* the rebuild
        has returned — so a flag cleared at the end of the rebuild does not stop it, and the
        result is an endless refresh loop with a stack eight frames deep. It pins the CPU rather
        than raising, and looks like a crash from the outside.
        """
        app = self._app()
        panel = app.layout_panel

        calls = []
        original = app.refresh
        app.refresh = lambda **kw: (calls.append(1), original(**kw))[1]

        panel.tree.selection_set("button:0:1")
        panel.tree.focus("button:0:1")

        # Each update() drains the queue once. A live loop re-arms it every time, so the count
        # keeps climbing; a settled one stops. Bounded either way, so the test cannot hang.
        for _ in range(6):
            self.root.update()
        settled = len(calls)
        for _ in range(6):
            self.root.update()

        self.assertEqual(len(calls), settled, "selecting a row keeps re-refreshing the window")
        self.assertLess(settled, 4, f"one selection caused {settled} refreshes")

    def test_a_selection_reaches_the_panels_that_follow_it(self):
        app = self._app()
        panel = app.layout_panel

        panel.select_button(0, 3)
        self.root.update()

        self.assertEqual(panel.selection(), ("button", 0, 3))
        self.assertIs(panel.button_form.button, app.doc.pages[0]["buttons"][3])
        self.assertEqual(app.page_var.get(), app.doc.pages[0]["title"])


class PageOperationTests(unittest.TestCase):
    """Pages and buttons are references as much as objects, so editing one edits the others."""

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(REPO / "sdcard" / "deck.json")

    def test_renaming_a_page_follows_every_nav_button(self):
        doc = self._doc()
        index = doc.page_index("macros")
        self.assertTrue(doc.page_referrers("macros"))

        doc.rename_page_id(index, "macro.pad")
        self.assertEqual(doc.page_referrers("macros"), [])
        self.assertTrue(doc.page_referrers("macro.pad"))
        self.assertEqual(doc.problems(), [])

    def test_deleting_a_referenced_page_refuses_and_names_the_referrers(self):
        """Silently repointing somebody's nav tile would lose the only clue about its purpose."""
        from deckbuilder.model import ModelError

        doc = self._doc()
        with self.assertRaises(ModelError) as caught:
            doc.delete_page(doc.page_index("macros"))
        self.assertIn("nav.macros", str(caught.exception))

    def test_a_duplicated_page_points_at_itself(self):
        doc = self._doc()
        source = doc.pages[0]["id"]
        doc.pages[0]["buttons"][0]["action"] = {"type": "page", "target": source}

        index = doc.duplicate_page(0)
        copied = doc.pages[index]
        self.assertNotEqual(copied["id"], source)
        self.assertEqual(copied["buttons"][0]["action"]["target"], copied["id"])
        self.assertEqual(doc.shape_problems(), [])
        self.assertEqual(doc.problems(), [])

    def test_new_ids_are_unique_and_carry_no_spaces(self):
        doc = self._doc()
        doc.new_page("Launch")
        doc.new_button(0, "VS Code")

        ids = [p["id"] for p in doc.pages] + sorted(doc.button_ids())
        self.assertEqual(len(ids), len(set(ids)))
        for value in ids:
            with self.subTest(id=value):
                self.assertNotIn(" ", value)

    def test_moving_a_button_between_pages_drops_its_position(self):
        """A pin is page-local: (3,2) on a 4x3 grid is off the edge of a 3x2 one."""
        doc = self._doc()
        doc.pin_all(0)
        moved = doc.pages[0]["buttons"][5]["id"]

        doc.move_button_to_page(0, 5, 1)
        landed = doc.pages[1]["buttons"][-1]
        self.assertEqual(landed["id"], moved)
        self.assertIsNone(landed["pos"])

    def test_a_firmware_page_refuses_buttons_and_a_grid(self):
        from deckbuilder.model import ModelError

        doc = self._doc()
        numpad = doc.page_index("numpad")
        with self.assertRaises(ModelError):
            doc.set_grid(numpad, 4, 3)
        with self.assertRaises(ModelError):
            doc.move_button_to_page(0, 0, numpad)

    def test_the_shipped_layout_has_no_geometry_problems(self):
        self.assertEqual(self._doc().layout_problems(), [])

    def test_overlapping_pins_are_caught(self):
        doc = self._doc()
        doc.pin_all(0)
        doc.pages[0]["buttons"][1]["pos"] = dict(doc.pages[0]["buttons"][0]["pos"])
        self.assertIn("overlaps", " | ".join(doc.layout_problems()))

    def test_a_grid_too_fine_to_press_is_caught(self):
        from deckbuilder import geometry
        from deckbuilder.model import MIN_TILE_PX

        # 8x6 is 91x61px, which squeaks past — the threshold is drawn at roughly a fingertip on
        # a 7" panel, and being close to it is not the same as being over it.
        self.assertGreaterEqual(min(geometry.cells(8, 6)), MIN_TILE_PX)

        doc = self._doc()
        doc.set_grid(0, 8, 7)
        self.assertIn("fingertip", " | ".join(doc.layout_problems()))

    def test_more_pages_than_the_nav_holds_is_caught(self):
        doc = self._doc()
        while len(doc.pages) <= 6:
            doc.new_page(f"Spare {len(doc.pages)}")
        self.assertIn("cannot be reached", " | ".join(doc.layout_problems()))

    def test_the_boot_page_is_named_so_a_reorder_can_mention_it(self):
        doc = self._doc()
        self.assertEqual(doc.boot_page, doc.pages[0]["id"])
        doc.move_page(1, -1)
        self.assertEqual(doc.boot_page, doc.pages[0]["id"])


class LibraryTests(unittest.TestCase):
    """Parking something has to be reversible, or nobody will use it on anything they care about."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        import shutil

        self.root = self.tmp / "sdcard"
        shutil.copytree(REPO / "sdcard", self.root)

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(self.root / "deck.json")

    def test_a_parked_page_comes_back_identical(self):
        from deckbuilder import library

        doc = self._doc()
        before = json.loads(json.dumps(doc.pages[doc.page_index("calendar")]))

        path, freed = library.park(
            doc, "page", [doc.page_index("calendar")], self.tmp / "calendar.mdpart.json"
        )
        self.assertGreater(freed, 0)
        self.assertNotIn("calendar", [p["id"] for p in doc.pages])

        plan = library.plan_import(doc, library.read(path))
        self.assertEqual(plan.problems, [])
        library.apply(doc, plan)

        self.assertEqual(doc.pages[doc.page_index("calendar")], before)
        self.assertEqual(doc.shape_problems(), [])
        self.assertEqual(doc.problems(), [])

    def test_nothing_is_removed_until_the_file_reads_back(self):
        """The ordering that makes park safe to use on something you cannot rebuild."""
        import ast

        source = (REPO / "agent" / "deckbuilder" / "library.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        park = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "park"
        )
        calls = [
            node.func.id
            for node in ast.walk(park)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertLess(calls.index("export"), calls.index("read"))
        self.assertLess(calls.index("read"), calls.index("_remove"))

    def test_parking_a_referenced_page_refuses_and_keeps_the_file(self):
        from deckbuilder import library

        doc = self._doc()
        path = self.tmp / "macros.mdpart.json"
        with self.assertRaises(library.LibraryError) as caught:
            library.park(doc, "page", [doc.page_index("macros")], path)

        self.assertIn("nothing was removed", str(caught.exception))
        self.assertTrue(path.is_file(), "the copy was written and must not be thrown away")
        self.assertIn("macros", [p["id"] for p in doc.pages])

    def test_importing_the_same_thing_twice_uniques_rather_than_collides(self):
        from deckbuilder import library

        doc = self._doc()
        path = library.export(doc, "theme", [0], self.tmp / "one.mdpart.json")
        fragment = library.read(path)

        for _ in range(2):
            plan = library.plan_import(doc, fragment)
            library.apply(doc, plan)

        names = [t["name"] for t in doc.themes]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(doc.problems(), [])

    def test_a_whole_deck_is_a_valid_source(self):
        """Which is what makes the backup folder an undo-across-sessions."""
        from deckbuilder import library

        fragment = library.read(self.root / "deck.json")
        self.assertEqual(fragment.source, "deck")
        self.assertEqual(fragment.kind, "theme")

        raw = json.loads((self.root / "deck.json").read_bytes().decode("utf-8"))
        as_pages = library.pick(fragment, "page", raw)
        self.assertEqual([p["id"] for p in as_pages.items], [p["id"] for p in raw["pages"]])

    def test_a_newer_item_is_refused_rather_than_quietly_trimmed(self):
        """Dropping a key the firmware reads changes behaviour, silently, on the deck."""
        from deckbuilder import library

        doc = self._doc()
        path = library.export(doc, "theme", [0], self.tmp / "one.mdpart.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["items"][0]["glow"] = 40
        path.write_text(json.dumps(data), encoding="utf-8")

        plan = library.plan_import(doc, library.read(path))
        self.assertFalse(plan.ok)
        self.assertIn("glow", " | ".join(plan.problems))

    def test_an_older_item_is_filled_in_and_said_so(self):
        from deckbuilder import library

        doc = self._doc()
        path = library.export(doc, "theme", [0], self.tmp / "one.mdpart.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["items"][0]["dim_opa"]
        path.write_text(json.dumps(data), encoding="utf-8")

        plan = library.plan_import(doc, library.read(path))
        self.assertTrue(plan.ok)
        self.assertIn("dim_opa", " | ".join(plan.notes))
        self.assertIsNone(plan.items[0]["dim_opa"], "must default to config.h, not to a literal")
        self.assertEqual(tuple(plan.items[0]), doc.field_order)

    def test_the_cost_is_known_before_anything_is_committed(self):
        """Import is where the budget actually gets blown."""
        from deckbuilder import budget, library

        doc = self._doc()
        path = library.export(doc, "page", [0], self.tmp / "page.mdpart.json")
        plan = library.plan_import(doc, library.read(path))

        before = budget.frame_bytes(doc.next_rev(), doc.candidate_raw())
        library.apply(doc, plan)
        after = budget.frame_bytes(doc.next_rev(), doc.candidate_raw())

        self.assertEqual(plan.bytes_delta, after - before)
        self.assertEqual(plan.used_after, after)

    def test_a_missing_wallpaper_is_reported_before_the_import_lands(self):
        from deckbuilder import library

        doc = self._doc()
        fragment = library.read(self.root / "deck.json")
        fragment.items = [dict(fragment.items[0], name="Ghost", wallpaper="/wall/absent.bin")]

        plan = library.plan_import(doc, fragment)
        self.assertEqual(plan.missing_assets, ["/wall/absent.bin"])

    def test_the_manifest_describes_assets_rather_than_carrying_them(self):
        """A wallpaper is 768KB, and the file's virtue is that you can open and read it."""
        from deckbuilder import library

        doc = self._doc()
        themed = next(
            (i for i, t in enumerate(doc.themes) if (t.get("wallpaper") or "").startswith("/")),
            None,
        )
        if themed is None:
            self.skipTest("no theme in the shipped deck uses a wallpaper")

        path = library.export(doc, "theme", [themed], self.tmp / "wall.mdpart.json")
        data = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(data["assets"])
        entry = data["assets"][0]
        self.assertEqual(set(entry), {"path", "sha256", "bytes"})
        self.assertLess(path.stat().st_size, 32_000, "the manifest turned into a payload")

    def test_a_button_loses_a_pin_it_cannot_honour(self):
        from deckbuilder import library

        doc = self._doc()
        doc.pin_all(0)
        path = library.export(doc, "button", [3], self.tmp / "one.mdpart.json", page_index=0)

        plan = library.plan_import(doc, library.read(path), into_page=1)
        self.assertIsNone(plan.items[0]["pos"])
        self.assertIn("dropped its fixed position", " | ".join(plan.notes))

    def test_a_page_keeps_pins_that_came_with_their_grid(self):
        from deckbuilder import library

        doc = self._doc()
        doc.pin_all(0)
        expected = json.loads(json.dumps(doc.pages[0]["buttons"][0]["pos"]))

        path = library.export(doc, "page", [0], self.tmp / "page.mdpart.json")
        plan = library.plan_import(doc, library.read(path))
        self.assertEqual(plan.items[0]["buttons"][0]["pos"], expected)

    def test_a_dangling_nav_target_is_named_at_import_time(self):
        from deckbuilder import library

        doc = self._doc()
        path = library.export(doc, "page", [0], self.tmp / "page.mdpart.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["items"][0]["buttons"][0]["action"] = {"type": "page", "target": "nowhere"}
        path.write_text(json.dumps(data), encoding="utf-8")

        plan = library.plan_import(doc, library.read(path))
        self.assertIn("nowhere", " | ".join(plan.notes))

    def test_a_file_that_is_neither_is_refused_by_name(self):
        from deckbuilder import library

        path = self.tmp / "random.json"
        path.write_text('{"hello": 1}', encoding="utf-8")
        with self.assertRaises(library.LibraryError):
            library.read(path)

    def test_a_future_format_version_is_refused(self):
        from deckbuilder import library

        doc = self._doc()
        path = library.export(doc, "theme", [0], self.tmp / "one.mdpart.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["multi_deck"] = 99
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(library.LibraryError):
            library.read(path)

    def test_a_parked_file_says_where_it_came_from(self):
        from deckbuilder import library

        doc = self._doc()
        path = library.export(doc, "button", [2], self.tmp / "one.mdpart.json", page_index=0)
        data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["from"]["rev"], doc.rev)
        self.assertEqual(data["origin"]["page"], doc.pages[0]["id"])
        self.assertEqual(data["kind"], "button")


class ByteBudgetTests(unittest.TestCase):
    """The meter has to measure the thing that actually fails, not an approximation of it."""

    def _raw(self) -> dict:
        return json.loads((REPO / "sdcard" / "deck.json").read_text(encoding="utf-8"))

    def test_the_budget_is_the_wire_size(self):
        from deckbuilder import budget

        raw = self._raw()
        self.assertEqual(
            budget.frame_bytes(raw["rev"], raw),
            len(protocol.encode(protocol.layout(raw["rev"], raw))),
        )

    def test_the_warning_line_is_where_the_test_suite_fails(self):
        """If these drift, the meter goes green right up until the build breaks."""
        from deckbuilder import budget

        self.assertEqual(budget.WARN_FRACTION, 0.8)
        self.assertEqual(budget.LIMIT, protocol.MAX_LINE_BYTES)

    def test_adding_a_theme_is_predicted_before_it_lands(self):
        from deckbuilder import budget

        raw = self._raw()
        before = budget.frame_bytes(raw["rev"], raw)
        predicted = before + budget.theme_cost(raw["themes"][0])

        raw["themes"].append(json.loads(json.dumps(raw["themes"][0])))
        actual = budget.frame_bytes(raw["rev"], raw)

        self.assertEqual(predicted, actual)

    def test_headroom_agrees_with_actually_adding_themes(self):
        from deckbuilder import budget

        raw = self._raw()
        template = raw["themes"][0]
        to_warning, _to_limit = budget.headroom_in_themes(raw["rev"], raw, template)

        for _ in range(to_warning):
            raw["themes"].append(json.loads(json.dumps(template)))
        self.assertFalse(budget.report(raw["rev"], raw).over_warning)

        raw["themes"].append(json.loads(json.dumps(template)))
        self.assertTrue(budget.report(raw["rev"], raw).over_warning)

    def test_the_shipped_layout_is_not_already_warning(self):
        from deckbuilder import budget

        # The property, not the enum it currently produces. Asserting "near" would start failing
        # for a layout that got *smaller*, which is not a regression in anything.
        raw = self._raw()
        report = budget.report(raw["rev"], raw)
        self.assertLess(report.used, report.warn_at)

    def test_the_meter_measures_the_same_way_the_encoder_does(self):
        """The mismatch that made this under-report: ensure_ascii, set two different ways."""
        from deckbuilder import budget

        raw = self._raw()
        before = budget.frame_bytes(raw["rev"], raw)
        theme = json.loads(json.dumps(raw["themes"][0]))
        theme["name"] = "Café"

        raw["themes"].append(theme)
        self.assertEqual(before + budget.item_cost(theme), budget.frame_bytes(raw["rev"], raw))

    def test_a_page_costs_what_the_meter_says_it_costs(self):
        from deckbuilder import budget

        raw = self._raw()
        before = budget.frame_bytes(raw["rev"], raw)
        page = json.loads(json.dumps(raw["pages"][0]))
        page["id"] = "copy"

        raw["pages"].append(page)
        self.assertEqual(before + budget.page_cost(page), budget.frame_bytes(raw["rev"], raw))

    def test_removing_something_is_measured_rather_than_estimated(self):
        """The number that appears next to a button someone is about to press."""
        from deckbuilder import budget

        raw = self._raw()
        freed = budget.removal_cost(raw["rev"], raw, lambda after: after["pages"].pop(0))

        after = json.loads(json.dumps(raw))
        after["pages"].pop(0)
        self.assertEqual(
            freed, budget.frame_bytes(raw["rev"], raw) - budget.frame_bytes(raw["rev"], after)
        )
        self.assertGreater(freed, 0)

    def test_removal_cost_does_not_disturb_the_layout_it_measures(self):
        from deckbuilder import budget

        raw = self._raw()
        untouched = json.dumps(raw)
        budget.removal_cost(raw["rev"], raw, lambda after: after["pages"].clear())
        self.assertEqual(json.dumps(raw), untouched)


class BuilderIconTests(unittest.TestCase):
    """Every icon the firmware can draw needs a stand-in, for the same reason ICON_NAMES does.

    A name with no entry would render as a blank tile in the preview, which reads as a layout
    bug you do not have — the preview inventing problems is worse than it admitting the glyph
    is an approximation.
    """

    def test_every_firmware_icon_has_a_preview_glyph(self):
        from deckbuilder.icons import PREVIEW_GLYPHS

        self.assertEqual(set(PREVIEW_GLYPHS), set(ICON_NAMES))

    def test_glyphs_are_single_characters(self):
        from deckbuilder.icons import PREVIEW_GLYPHS

        for name, glyph in PREVIEW_GLYPHS.items():
            with self.subTest(icon=name):
                self.assertEqual(len(glyph), 1)

    def test_unknown_names_get_no_glyph_rather_than_a_wrong_one(self):
        from deckbuilder import icons

        self.assertIsNone(icons.glyph_for("not_a_symbol"))


class PreviewRenderTests(unittest.TestCase):
    """The preview is the only feedback loop there is, so it has to survive every theme."""

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def _doc(self):
        from deckbuilder.model import DeckDoc

        return DeckDoc.load(REPO / "sdcard" / "deck.json")

    def test_every_shipped_theme_and_page_renders(self):
        from deckbuilder import render

        doc = self._doc()
        for theme in doc.themes:
            for page in doc.pages:
                with self.subTest(theme=theme["name"], page=page["id"]):
                    preview = render.render_page(
                        doc.candidate_raw(), theme, page["id"],
                        asset_root=REPO / "sdcard",
                    )
                    self.assertEqual(preview.image.size, (render.SCREEN_W, render.SCREEN_H))
                    self.assertEqual(preview.warnings, [])

    def test_every_tile_state_renders(self):
        from deckbuilder import render

        doc = self._doc()
        for state in ("normal", "pressed", "disabled"):
            with self.subTest(state=state):
                preview = render.render_page(
                    doc.candidate_raw(), doc.themes[0], "launch",
                    asset_root=REPO / "sdcard", state=state,
                )
                self.assertEqual(preview.image.size, (render.SCREEN_W, render.SCREEN_H))

    def test_a_missing_wallpaper_degrades_to_the_background(self):
        """The device paints `bg` and toasts the reason; the preview must not simply crash."""
        from deckbuilder import render

        doc = self._doc()
        theme = dict(doc.themes[0], wallpaper="/wall/does-not-exist.bin", tile_opa=0)
        preview = render.render_page(
            doc.candidate_raw(), theme, "launch", asset_root=REPO / "sdcard"
        )

        self.assertTrue(preview.warnings)
        self.assertEqual(preview.image.getpixel((400, 300)), render.parse_color("#0d1117", 0))

    def test_geometry_matches_the_firmware(self):
        """The numbers the firmware computes with C integer division, computed the same way."""
        from deckbuilder import render

        # (800 - 8*5)//4 = 190, and (424 - 8*4)//3 = 130 — not 130.67. The 10px left over is a
        # real gap at the bottom of the panel, and rounding it away here would make the preview
        # wrong in the dimension people actually notice.
        self.assertEqual(render._cells(4, 3), (190, 130))
        # The ten-key: (424 - 8*6)//5 = 75.
        self.assertEqual(render._cells(4, 5), (190, 75))

    def test_the_caption_names_the_substitutions(self):
        from deckbuilder import render

        doc = self._doc()
        preview = render.render_page(
            doc.candidate_raw(), doc.themes[0], "launch", asset_root=REPO / "sdcard"
        )
        self.assertIn("Montserrat", preview.font_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
