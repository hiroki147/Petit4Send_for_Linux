import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import p4core as c

FIX = os.environ.get("P4_FIXTURES", os.path.join(os.path.dirname(__file__), "Fixtures"))


class Core(unittest.TestCase):
    def test_crc(self):
        self.assertEqual(c.crc16(b"123456789"), 0x0B3A)
        self.assertEqual(c.crc16(b""), 0)

    def test_lz_literal_overlap(self):
        self.assertEqual(c.lzss_decompress(bytes([0x82, 0x02, 0xF0, 0x01]), 33), b"A" * 33)

    def test_lz_roundtrip(self):
        r = 1234; noise = bytearray()
        for _ in range(5000):
            r = (r * 1664525 + 1013904223) & 0xFFFFFFFF; noise.append((r >> 16) & 255)
        for d in [b"", b"\0", b"\xff", "日本語\nPRINT 123\r\n".encode(), b"*" * 5000, bytes(noise), bytes(noise) * 2]:
            z = c.lzss_compress(d)
            self.assertEqual(len(z) % 4, 0)
            self.assertEqual(c.lzss_decompress(z, len(d)), d)

    def test_lz_malformed(self):
        for args in [(b"", 1), (b"\x01", 1), (b"", c.MAXIMUM_SIZE + 1)]:
            with self.assertRaises(c.TransferError): c.lzss_decompress(*args)
        with self.assertRaises(c.TransferError): c.lzss_compress(b"1234", lambda: True)

    def test_header(self):
        b = bytes([0x41, 0, 0x42, 0])
        s = c.build_stream(b, "test", c.KIND_TEXT, c.COMPRESSION_NONE)
        self.assertEqual(s[0:16], bytes(16)); self.assertEqual(s[16:66], bytes([1]) * 50); self.assertEqual(s[66], 0)
        self.assertEqual(s[67:75], b"TXT:TEST")
        self.assertEqual(c.integer(s, 103, 4), 4); self.assertEqual(s[111], 0); self.assertEqual(c.integer(s, 112, 4), 4)
        self.assertEqual(s[116:120], b); self.assertEqual(c.integer(s, 120, 2), c.crc16(b))
        for bad in ["日本語", "ß", "", "A" * 33, "A:B"]:
            with self.assertRaises(c.TransferError): c.build_stream(b, bad, c.KIND_TEXT, c.COMPRESSION_NONE)
        self.assertEqual(c.build_stream(b, "i", c.KIND_TEXT, c.COMPRESSION_NONE)[67:72], b"TXT:I")

    def test_kind(self):
        self.assertEqual(c.infer_kind("txt"), c.KIND_TEXT); self.assertEqual(c.infer_kind("PRG"), c.KIND_TEXT)
        for e in ["font", "DAT", "cmt", "D88", "bin", "rom", "I80", "i88", "T88", "n80"]:
            self.assertEqual(c.infer_kind(e), c.KIND_DATA)
        for e in ["jpg", "PNG", "jpeg"]: self.assertEqual(c.infer_kind(e), c.KIND_GRAPHICS)
        self.assertIsNone(c.infer_kind("zip")); self.assertIsNone(c.infer_kind(""))

    def test_hid_against_switch_receiver(self):
        """Switch側(@RECEIVE)と同じ組み合わせ和でビット列を戻し、元のストリームに一致するか。"""
        data = bytes((i * 37) & 255 for i in range(1000))
        reports = list(c.HIDReports(data, 12))
        self.assertEqual(len(reports), len(c.HIDReports(data, 12)))
        bw = c.BitWriter(); mouse = 0
        for clock, p in enumerate(reports):
            self.assertEqual(len(p), 12); self.assertEqual(p[0], 50)
            idx = sorted(c.KEY_CODES.index(k) for k in p[1:7])
            self.assertEqual(len(set(idx)), 6)
            value = sum(c._CHOOSE[v][i + 1] for i, v in enumerate(idx))
            value |= ((p[7] >> 2) & 1) << 36
            self.assertEqual(((p[7] >> 3) & 1), clock & 1); self.assertEqual((p[7] >> 1) & 1, (clock >> 1) & 1)
            if clock & 1 == 0:
                m = p[8]; mouse = ((m & 16) >> 1) | ((m & 8) << 1) | (m & 7)
                bw.put(value, 37); bw.put(mouse, 5)
            else:
                self.assertEqual(p[8], reports[clock - 1][8]); bw.put(value, 37)
        self.assertEqual(bw.bytes[:len(data)], data)
        with self.assertRaises(c.TransferError): c.HIDReports(data, 25)
        self.assertEqual(c.HIDReports(data).command, 3)

    def test_screenshot_fixture(self):
        pages = [c.ScreenshotPage.load(os.path.join(FIX, f"page-{i}.png")) for i in (2, 1)]  # 順不同
        expected = open(os.path.join(FIX, "expected.bin"), "rb").read()
        self.assertEqual(c.assemble(pages), expected)
        self.assertEqual(c.missing_pages(pages[:1]), [1] if pages[0].index == 1 else [2])
        with self.assertRaises(c.TransferError): c.assemble(pages[:1])
        self.assertEqual(c.assemble(pages + pages), expected)  # 重複OK

    def test_text(self):
        self.assertEqual(c.text_to_utf16le("あA\n".encode("utf-8")), "あA\n".encode("utf-16-le"))
        self.assertEqual(c.text_to_utf16le(b"\xef\xbb\xbfhi"), "hi".encode("utf-16-le"))
        self.assertEqual(c.text_to_utf16le("あ".encode("cp932")), "あ".encode("utf-16-le"))
        self.assertEqual(c.text_to_utf16le("\ufeffあ".encode("utf-16")), "あ".encode("utf-16-le"))

    def test_sync_detection_order(self):
        calls = []
        c.sync_key_detection(lambda b, ch: calls.append((bytes(b), ch)), lambda d: calls.append(("pause", d)))
        self.assertEqual(calls, [(bytes([1]) + bytes(11), True), ("pause", 4), (bytes(12), False), (bytes(12), False)])
        calls.clear()
        def boom(b, ch):
            calls.append(ch)
            if len(calls) == 1: raise c.TransferError("x")
        with self.assertRaises(c.TransferError): c.sync_key_detection(boom, lambda d: None)
        self.assertEqual(calls, [True, False, False])

    def test_port_choice(self):
        self.assertEqual(c.choose_port([], ["/dev/ttyUSB0"], ""), "/dev/ttyUSB0")
        self.assertEqual(c.choose_port([], ["/dev/ttyUSB0", "/dev/ttyUSB1"], ""), "")
        self.assertEqual(c.choose_port(["/dev/ttyUSB0"], ["/dev/ttyUSB0", "/dev/ttyUSB1"], "/dev/ttyUSB0"), "/dev/ttyUSB0")
        self.assertEqual(c.choose_port(["/dev/ttyUSB0"], ["/dev/ttyUSB1"], "/dev/ttyUSB0"), "")

    def test_grp_roundtrip(self):
        from PIL import Image
        import tempfile
        im = Image.new("RGBA", (3, 2)); im.putdata([(10, 20, 30, 255), (1, 2, 3, 0)] + [(200, 100, 50, 128)] * 4)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.png"); im.save(p)
            r = c.Raster.load(p)
            bgra = r.bgra
            self.assertEqual(bgra[:4], bytes([30, 20, 10, 255])); self.assertEqual(bgra[4:8], bytes([0, 0, 0, 0]))
            stream_data = c.le(3, 2) + c.le(2, 2) + bgra
            # 画像として復元(スクリーンショットGRPと同じ4バイト寸法ヘッダー)できること
            page = c.ScreenshotPage("GRP:T.PNG", len(stream_data), c.crc16(stream_data), 1, 0, 0, stream_data)
            out = c.export([page], d)
            self.assertEqual(Image.open(out).size, (3, 2))

    def test_serial_loopback_pty(self):
        """擬似端末でArduino役を動かし、送信手順(ゼロ探査→4先行→ACK→キー解放)を通す。"""
        import pty, threading
        master, slave = pty.openpty()
        path = os.ttyname(slave)
        received = bytearray(); stop = threading.Event()
        def arduino():
            import select
            while not stop.is_set():
                if select.select([master], [], [], 0.05)[0]:
                    try: b = os.read(master, 64)
                    except OSError: return
                    received.extend(b)
                    for _ in b: os.write(master, b"\x06")  # 1バイトごとにACK(接続確認のゼロ1個も含む)
        t = threading.Thread(target=arduino, daemon=True); t.start()
        # ACKは受信バイト1個につき1個返るため、12バイトパケットだと12個返る → 実機の1パケット1ACKに合わせる
        stop.set(); t.join()
        received.clear(); stop.clear()
        def arduino2():
            import select
            buf = bytearray(); first = True
            while not stop.is_set():
                if select.select([master], [], [], 0.05)[0]:
                    try: b = os.read(master, 256)
                    except OSError: return
                    received.extend(b); buf.extend(b)
                    if first:
                        os.write(master, b"\x06"); first = False; buf.clear(); continue
                    while len(buf) >= 12:
                        del buf[:12]; os.write(master, b"\x06")
        t = threading.Thread(target=arduino2, daemon=True); t.start()
        data = b"HELLO" * 40
        stream = c.build_stream(data, "T.TXT", c.KIND_DATA, c.COMPRESSION_NONE)
        reps = c.HIDReports(stream, -1)
        prog = []
        c.send(path, reps, c.Cancellation(), prog.append)
        stop.set(); t.join()
        self.assertAlmostEqual(prog[-1], 1.0)
        body = bytes(received)
        n = len(reps)
        self.assertGreaterEqual(len(body), n * 12)
        self.assertTrue(body.endswith(bytes([3]) + bytes(6) + bytes([0]) + bytes(4)))  # 最後のキー解放(modifier 0)


if __name__ == "__main__":
    unittest.main()
