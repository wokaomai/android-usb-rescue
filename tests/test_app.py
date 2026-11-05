"""GUI and worker checks using local generated images and fake USB sessions."""
from __future__ import annotations

from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from android_usb_rescue import app
from android_usb_rescue.settings import load_config


class MappingTests(unittest.TestCase):
    def test_mapping_scales_and_rejects_margins(self):
        self.assertEqual(app.map_point(50, 100, (0, 0), (101, 201), (1080, 2340)),
                         (0.5, 0.5, 540, 1170))
        self.assertIsNone(app.map_point(9, 20, (10, 20), (100, 200), (1080, 2340)))
        self.assertIsNone(app.map_point(110, 20, (10, 20), (100, 200), (1080, 2340)))
        self.assertIsNone(app.map_point(float("nan"), 0, (0, 0), (100, 200), (1080, 2340)))


class FakeSession:
    hid_id = 10

    def __init__(self, calls):
        self.calls = calls

    def register(self):
        self.calls.append("keyboard")

    def register_touch(self):
        self.calls.append("touch")

    def key(self, usage, modifiers=0):
        self.calls.append(("key", usage, modifiers))

    def consumer_key(self, usage):
        self.calls.append(("consumer", usage))

    def contact(self, down, *point):
        self.calls.append(("contact", down))

    def send(self, *args, **kwargs):
        self.calls.append("release")

    def close(self):
        self.calls.append("closed")


def wait_event(events, kind, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        event = events.get(timeout=max(0.01, end - time.monotonic()))
        if event[0] == kind:
            return event[1]
    raise AssertionError("Expected worker event was not received")


class WorkerTests(unittest.TestCase):
    def test_refresh_uses_one_session_and_captures_before_reading(self):
        calls, events = [], queue.Queue()
        session = FakeSession(calls)

        def probe(given):
            self.assertIs(given, session)
            calls.append("probe")
            return {"ok": True, "storage_accessible": True}

        def newest(given, destination):
            self.assertIs(given, session)
            calls.append("newest")
            return {"ok": True, "screenshot_time": "2000-01-01T00:00:00"}

        worker = app.UsbWorker(events, {"serial": "TEST"}, Path("unused"),
                               session_factory=lambda **target: session)
        with patch("android_usb_rescue.mtp_reader.probe", probe), \
                patch("android_usb_rescue.mtp_reader.latest_screenshot", newest):
            worker.start()
            try:
                wait_event(events, "ready")
                worker.commands.put(("refresh", None, worker.generation))
                self.assertTrue(wait_event(events, "files")["ok"])
            finally:
                worker.stop_requested.set()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertLess(calls.index(("key", 0x46, 0)), calls.index("probe"))
        self.assertLess(calls.index("probe"), calls.index("newest"))
        self.assertEqual(calls.count("closed"), 1)

    def test_cancelled_touch_reports_are_not_sent(self):
        calls, events = [], queue.Queue()
        worker = app.UsbWorker(events, {"serial": "TEST"}, Path("unused"),
                               session_factory=lambda **target: FakeSession(calls))
        worker.commands.put(("down", (0.5, 0.5), worker.generation))
        worker.commands.put(("move", (0.7, 0.7), worker.generation))
        worker.cancel_input()
        with patch("android_usb_rescue.mtp_reader.probe",
                   return_value={"ok": True, "storage_accessible": False}):
            worker.start()
            try:
                wait_event(events, "ready")
                worker.commands.put(("files", None, worker.generation))
                wait_event(events, "files")
            finally:
                worker.stop_requested.set()
                worker.join(timeout=3)
        self.assertNotIn(("contact", True), calls)


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="android-usb-rescue-test-")
        self.data_dir = Path(self.folder.name).resolve()
        self.assertTrue(self.data_dir.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.folder.cleanup()
            self.skipTest("Tk display is not available")
        self.root.withdraw()
        self.application = app.ReferenceApp(
            self.root, dry_run=True, data_dir=self.data_dir,
            config=load_config(self.data_dir))
        self.root.update_idletasks()

    def tearDown(self):
        if hasattr(self, "application"):
            worker = self.application.worker
            if worker and isinstance(worker, threading.Thread):
                worker.stop_requested.set()
                worker.join(timeout=3)
            self.application.worker = None
            self.application.close()
            self.root.destroy()
        self.folder.cleanup()

    def make_image(self, name="portrait.png", size=(40, 80)):
        path = self.data_dir / name
        Image.new("RGB", size, "white").save(path)
        return path

    def test_empty_first_launch_does_not_connect_or_enable_input(self):
        state = self.application.status_data()
        self.assertIsNone(self.application.worker)
        self.assertIsNone(state["source"])
        self.assertFalse(state["connected"])
        self.assertFalse(state["control_enabled"])
        self.assertFalse(state["local_api_enabled"])
        self.assertTrue(self.application.canvas.find_all())
        self.assertFalse(self.application.connect())
        self.assertIsNone(self.application.worker)

    def test_invalid_image_keeps_previous_reference(self):
        good = self.make_image()
        self.assertTrue(self.application.try_load(good))
        bad = self.make_image("landscape.png", (80, 40))
        self.assertFalse(self.application.try_load(bad))
        self.assertEqual(self.application.image_path, good)
        self.assertEqual(self.application.image.size, (40, 80))

    def test_choose_image_does_not_claim_success_after_load_failure(self):
        good = self.make_image()
        self.application.try_load(good)
        bad = self.data_dir / "broken.png"
        bad.write_bytes(b"not an image")
        with patch.object(app.filedialog, "askopenfilename", return_value=str(bad)):
            self.application.choose_image()
        self.assertEqual(self.application.image_path, good)
        self.assertIn("无法加载图片", self.application.message.get())

    def test_sixteen_by_nine_portrait_is_supported(self):
        self.assertTrue(self.application.try_load(self.make_image(size=(108, 192))))

    def test_phone_timestamp_and_unchanged_capture_are_not_mislabelled(self):
        picture = self.make_image()
        report = {"ok": True, "capture_requested": True, "screenshot_path": str(picture),
                  "screenshot_time": "2000-01-02T03:04:05", "screenshot_object_handle": 3}
        self.application.events.put(("files", report))
        self.application.poll()
        self.assertIn("手机截图时间 2000-01-02 03:04:05", self.application.banner.get())
        self.application.events.put(("files", report))
        self.application.poll()
        self.assertIn("未检测到新生成的截图", self.application.message.get())

    def test_busy_refresh_pauses_input_and_disables_conflicting_controls(self):
        self.application.try_load(self.make_image())
        fake = DummyWorker()
        self.application.worker = fake
        self.application.ready = True
        self.application.enabled.set(True)
        self.assertTrue(self.application.refresh_screenshot())
        self.assertTrue(self.application.busy)
        self.assertFalse(self.application.enabled.get())
        self.assertEqual(str(self.application.choose_button.cget("state")), "disabled")
        self.assertEqual(str(self.application.refresh_button.cget("state")), "disabled")
        self.assertEqual(fake.commands.get_nowait()[0], "refresh")
        old = self.application.image_path
        self.application.events.put(("files", {"ok": False, "error": "temporary"}))
        self.application.poll()
        self.assertEqual(self.application.image_path, old)
        self.assertFalse(self.application.busy)
        self.assertFalse(self.application.enabled.get())

    def test_reconnect_waits_for_old_worker_to_exit(self):
        old = DummyWorker()
        self.application.worker = old
        self.application.ready = True
        self.application.serial.set("TEST")
        with patch.object(app, "UsbWorker", DummyWorker):
            self.assertTrue(self.application.connect())
            self.assertTrue(old.stop_requested.is_set())
            self.assertIs(self.application.worker, old)
            self.application.poll()
            self.assertIs(self.application.worker, old)
            old.alive = False
            self.application.poll()
            self.assertIsNot(self.application.worker, old)
            self.assertTrue(self.application.worker.started)
            self.assertFalse(self.application.pending_reconnect)


class DummyWorker:
    def __init__(self, *args, **kwargs):
        self.commands = queue.Queue(maxsize=128)
        self.stop_requested = threading.Event()
        self.generation = 0
        self.alive = True
        self.started = False

    def is_alive(self):
        return self.alive

    def cancel_input(self):
        self.generation += 1

    def start(self):
        self.started = True


if __name__ == "__main__":
    unittest.main()
