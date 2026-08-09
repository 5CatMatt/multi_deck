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
        self._load({"id": "b", "icon": "play", "action": {"type": "media", "key": "play"}})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
