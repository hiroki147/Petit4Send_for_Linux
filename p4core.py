"""Petit4Send for Linux - core (protocol, codec, serial, screenshot restore).

Swift版 Petit4SendCore の移植。標準ライブラリのみ（画像の読み書きだけ Pillow）。
プロトコルの詳細は PROTOCOL.md（Mac版に同梱）を参照。
"""
import glob
import os
import select
import struct
import termios
import threading
import time
import fcntl
import errno
from dataclasses import dataclass

MAXIMUM_SIZE = 64 * 1024 * 1024


class TransferError(Exception):
    """利用者に見せる失敗。メッセージはそのまま状態欄へ出す。"""


# ---------------------------------------------------------------- Codec
def crc16(data: bytes) -> int:
    """CRC-16/KOOPMAN (反射 0xC7ED, init/xorout 0xFFFF)。'123456789' -> 0x0B3A。"""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ (0xC7ED if c & 1 else 0)
    return c ^ 0xFFFF


class BitWriter:
    """LSBファーストのビット列(小さなアキュムレータでO(n))。"""
    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.nacc = 0
        self.position = 0

    def put(self, value: int, count: int):
        self.acc |= (value & ((1 << count) - 1)) << self.nacc
        self.nacc += count
        self.position += count
        while self.nacc >= 8:
            self.buf.append(self.acc & 0xFF)
            self.acc >>= 8
            self.nacc -= 8

    @property
    def bytes(self) -> bytes:
        out = bytes(self.buf)
        if self.nacc:
            out += bytes([self.acc & 0xFF])
        return out


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    def get(self, count: int) -> int:
        if self.position + count > len(self.data) * 8:
            raise TransferError("圧縮データが途中で切れています。")
        start = self.position // 8
        end = (self.position + count + 7) // 8
        chunk = int.from_bytes(self.data[start:end], "little")
        value = (chunk >> (self.position % 8)) & ((1 << count) - 1)
        self.position += count
        return value


def lzss_compress(data: bytes, should_cancel=lambda: False) -> bytes:
    """LSBファーストLZSS。一致3..32バイト、距離最大1024。出力は4バイト境界まで0埋め。"""
    if should_cancel():
        raise TransferError("中止しました。")
    bits = BitWriter()
    n = len(data)
    pos = 0
    heads = {}
    previous = [-1] * 1024
    while pos < n:
        if pos % 16384 == 0 and should_cancel():
            raise TransferError("中止しました。")
        length = distance = 0
        if pos + 2 < n:
            key = data[pos:pos + 3]
            start = heads.get(key, -1)
            while start >= 0 and pos - start <= 1024:
                m = 0
                while m < 32 and pos + m < n and data[start + m] == data[pos + m]:
                    m += 1
                if m > length:
                    length, distance = m, pos - start
                if length == 32:
                    break
                start = previous[start & 1023]
        if length >= 3:
            bits.put(1, 1)
            bits.put(distance - 1, 10)
            bits.put(length - 1, 5)
            advance = length
        else:
            bits.put(0, 1)
            bits.put(data[pos], 8)
            advance = 1
        for p in range(pos, pos + advance):
            if p + 2 < n:
                key = data[p:p + 3]
                previous[p & 1023] = heads.get(key, -1)
                heads[key] = p
        pos += advance
    out = bytearray(bits.bytes)
    while len(out) % 4:
        out.append(0)
    return bytes(out)


def lzss_decompress(data: bytes, size: int) -> bytes:
    """size バイトで止める。4バイト境界のパディングは内容に含めない。"""
    if not 0 <= size <= MAXIMUM_SIZE:
        raise TransferError("ファイルサイズが上限64 MiBを超えています。")
    bits = BitReader(data)
    out = bytearray()
    window = bytearray(1024)
    cursor = 0
    while len(out) < size:
        if bits.get(1) == 0:
            b = bits.get(8)
            out.append(b)
            window[cursor] = b
            cursor = (cursor + 1) & 1023
        else:
            distance = bits.get(10) + 1
            length = bits.get(5) + 1
            for _ in range(length):
                b = window[(cursor - distance + 1024) & 1023]
                if len(out) < size:
                    out.append(b)
                window[cursor] = b
                cursor = (cursor + 1) & 1023
    return bytes(out)


def le(value: int, count: int) -> bytes:
    return (value & ((1 << (8 * count)) - 1)).to_bytes(count, "little")


def integer(data, offset: int, count: int) -> int:
    return int.from_bytes(bytes(data[offset:offset + count]), "little")


# ------------------------------------------------------------ USB protocol
COMPRESSION_AUTO, COMPRESSION_LZSS, COMPRESSION_NONE = "自動", "LZSS", "無圧縮"
COMPRESSIONS = (COMPRESSION_AUTO, COMPRESSION_LZSS, COMPRESSION_NONE)
KIND_TEXT, KIND_DATA, KIND_GRAPHICS = "TXT:", "DAT:", "GRP:"
KINDS = (KIND_TEXT, KIND_DATA, KIND_GRAPHICS)

_DATA_EXT = {"FONT", "DAT", "CMT", "D88", "BIN", "ROM", "I80", "I88", "T88", "N80"}


def infer_kind(extension: str):
    """拡張子(ドット無し)から種別。該当しなければ None。"""
    e = ascii_upper(extension)
    if e in ("TXT", "PRG"):
        return KIND_TEXT
    if e in _DATA_EXT:
        return KIND_DATA
    if e in ("JPG", "PNG", "JPEG"):
        return KIND_GRAPHICS
    return None


def ascii_upper(name: str) -> str:
    """a-z だけを大文字にする(ロケール依存のstr.upper()は使わない)。"""
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in name)


SWITCH_NAME_RULE = "半角ASCII 1〜32文字（: / \\ は不可）"
SWITCH_NAME_ERROR = "送信先名は半角ASCII 1〜32文字で指定してください（: / \\ は不可）。"


def switch_name_error(name: str):
    raw = ascii_upper(name).encode("utf-8")
    if not raw or len(raw) > 32 or any(b < 32 or b >= 127 or b in (58, 47, 92) for b in raw):
        return SWITCH_NAME_ERROR
    return None


def build_stream(data: bytes, name: str, kind: str, compression: str = COMPRESSION_AUTO,
                 width: int = 0, height: int = 0, should_cancel=lambda: False) -> bytes:
    if len(data) > MAXIMUM_SIZE:
        raise TransferError("上限64 MiBを超えています。")
    err = switch_name_error(name)
    if err:
        raise TransferError(err)
    name_bytes = ascii_upper(name).encode("ascii")
    zipped = b"" if compression == COMPRESSION_NONE else lzss_compress(data, should_cancel)
    use_zip = compression == COMPRESSION_LZSS or (compression == COMPRESSION_AUTO and len(zipped) < len(data))
    body = zipped if use_zip else data
    out = bytearray(16) + bytearray([1] * 50) + bytearray([0])
    out += kind.encode("ascii") + name_bytes + bytes(32 - len(name_bytes))
    out += le(len(data), 4) + le(width, 2) + le(height, 2)
    out += bytes([1 if use_zip else 0]) + le(len(body), 4) + body
    out += le(crc16(data), 2) + bytes(16)
    return bytes(out)


KEY_CODES = bytes([
    4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,54,55,56,
    73,74,75,76,77,78,79,80,81,82,84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,99,100,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117,118,119,
    120,121,122,123,124,125,126,127,128,129,133,134,135,137,155,156,157,158,159,160,161,162,163,164,176,177,178,179,180,181,182,183,184,185,186,187,188,189,
    190,191,192,193,194,195,196,197,198,199,200,201,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216,217,218,219,220,221,165,166,167,168,169,170,
    171,172,173,174,175,232,233,234,235,236,237,238,239,240,241,242,243,244,245,246,247,248,249])
assert len(KEY_CODES) == 195

# C(n,k) 表 (n=0..194, k=0..6)。組み合わせ番号系のランク→キー添字変換に使う。
_CHOOSE = [[0] * 7 for _ in range(195)]
for _n in range(195):
    _CHOOSE[_n][0] = 1
    for _k in range(1, 7):
        _CHOOSE[_n][_k] = (_CHOOSE[_n - 1][_k - 1] + _CHOOSE[_n - 1][_k]) if _n else 0


class HIDReports:
    """ストリームを12バイトHIDレポートへ必要な分だけ展開する(巨大な配列を作らない)。"""

    def __init__(self, stream: bytes, sync_key: int = -1):
        if not -1 <= sync_key <= 24:
            raise TransferError("Sync Keyは-1〜24です。")
        self.stream = bytes(stream)
        self.command = 3 if sync_key < 0 else 2 + sync_key * 4

    def __len__(self):
        bits = len(self.stream) * 8
        r = bits % 79
        return bits // 79 * 2 + (0 if r == 0 else 1 if r <= 42 else 2)

    def __iter__(self):
        total = len(self.stream) * 8
        pos = 0
        clock = 0
        mouse = 0
        data = self.stream

        def take(n):
            nonlocal pos
            avail = min(n, total - pos)
            if avail <= 0:
                return 0
            start = pos // 8
            end = (pos + avail + 7) // 8
            v = (int.from_bytes(data[start:end], "little") >> (pos % 8)) & ((1 << avail) - 1)
            pos += avail
            return v

        while pos < total:
            value = take(37)
            remainder = value & 0xFFFFFFFFF
            indices = [0] * 6
            upper = 194
            for k in range(6, 0, -1):
                while _CHOOSE[upper][k] > remainder:
                    upper -= 1
                indices[6 - k] = upper
                remainder -= _CHOOSE[upper][k]
                upper -= 1
            report = bytearray(12)
            report[0] = self.command
            for i in range(6):
                report[i + 1] = KEY_CODES[indices[i]]
            report[7] = (((clock & 1) << 3) | (clock & 2)) | ((value >> 34) & 4)
            if clock & 1 == 0:
                m = take(5)
                mouse = ((m & 8) << 1) | ((m & 16) >> 1) | (m & 7)
            report[8] = mouse
            clock += 1
            yield bytes(report)


# ------------------------------------------------------------ source text
def text_to_utf16le(data: bytes) -> bytes:
    """UTF-16(BOM付き) → UTF-8 → シフトJIS(CP932) の順に判定し、BOMなしUTF-16LEにする。"""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            text = None
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp932")
            except UnicodeDecodeError:
                text = None
    if text is None:
        raise TransferError("TXTはUTF-8、BOM付きUTF-16、またはシフトJISで保存してください。")
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return text.encode("utf-16-le")
    except UnicodeEncodeError:
        raise TransferError("UTF-16LEへ変換できません。")


# ------------------------------------------------------------ raster (Pillow)
@dataclass
class Raster:
    width: int
    height: int
    rgba: bytes  # 非プリマルチプライ RGBA

    @staticmethod
    def load(path) -> "Raster":
        try:
            from PIL import Image
        except ImportError:
            raise TransferError("画像の処理には Pillow が必要です: pip install pillow")
        try:
            Image.MAX_IMAGE_PIXELS = MAXIMUM_SIZE // 4
            im = Image.open(path)
            im.load()
            im = im.convert("RGBA")
        except Exception:
            raise TransferError("画像を読み込めません（最大16Mピクセル）。")
        w, h = im.size
        if w <= 0 or h <= 0 or w * h > MAXIMUM_SIZE // 4:
            raise TransferError("画像を読み込めません（最大16Mピクセル）。")
        return Raster(w, h, im.tobytes())

    @property
    def bgra(self) -> bytes:
        """USB送信用の行優先BGRA。α=0の画素はRGBを0にする(Mac版と同じ)。"""
        from PIL import Image, ImageChops
        im = Image.frombytes("RGBA", (self.width, self.height), self.rgba)
        r, g, b, a = im.split()
        mask = a.point(lambda v: 255 if v else 0)
        r, g, b = (ImageChops.multiply(c, mask) for c in (r, g, b))
        return Image.merge("RGBA", (b, g, r, a)).tobytes()

    def png_bytes(self) -> bytes:
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.frombytes("RGBA", (self.width, self.height), self.rgba).save(buf, "PNG")
        return buf.getvalue()


# ------------------------------------------------------------ screenshot restore
_THRESHOLDS = (22, 64, 107, 149, 191, 234)
_LEVEL = bytes(sum(1 for t in _THRESHOLDS if g >= t) for g in range(256))


@dataclass
class ScreenshotPage:
    name: str
    file_size: int
    crc: int
    total: int
    index: int
    compression: int
    payload: bytes

    @property
    def group_key(self):
        return (self.name, self.file_size, self.crc, self.total, self.compression)

    @staticmethod
    def from_stream(stream: bytes) -> "ScreenshotPage":
        if len(stream) < 60 or integer(stream, 0, 4) != 0x505A5332:
            raise TransferError("P4SENDの画像ヘッダーが見つかりません。原寸のスクリーンショットを選択してください。")
        file_size = integer(stream, 4, 4)
        if file_size > MAXIMUM_SIZE or integer(stream, 8, 4) > 65535:
            raise TransferError("画像ヘッダーが破損しています。")
        crc = integer(stream, 8, 4)
        raw_name = stream[12:48].split(b"\0", 1)[0]
        try:
            name = raw_name.decode("ascii")
        except UnicodeDecodeError:
            name = ""
        if len(name) <= 4 or name[:4] not in KINDS:
            raise TransferError("画像のファイル名または種類が不正です。")
        total, index, compression = stream[48], stream[49], stream[51]
        length = integer(stream, 52, 4)
        if not (total > 0 and index < total and compression <= 1 and length <= 181380
                and length + 60 <= len(stream)
                and integer(stream, 56, 2) == 720 and integer(stream, 58, 2) == 720):
            raise TransferError("ページ番号・画像サイズ・データ長が不正です。")
        mask = stream[50]
        payload = bytes(b ^ mask for b in stream[60:60 + length])
        return ScreenshotPage(name, file_size, crc, total, index, compression, payload)

    @staticmethod
    def load(path) -> "ScreenshotPage":
        try:
            from PIL import Image
        except ImportError:
            raise TransferError("画像の処理には Pillow が必要です: pip install pillow")
        try:
            im = Image.open(path)
            im.load()
        except Exception:
            raise TransferError("画像を読み込めません（最大16Mピクセル）。")
        if im.size not in ((1280, 720), (720, 720)):
            raise TransferError("画像は1280×720（または左側を切り出した720×720）の原寸が必要です。")
        g = im.convert("RGBA").split()[1].crop((0, 0, 720, 720))
        gb = g.tobytes()  # 行優先 720x720
        levels = gb.translate(_LEVEL)
        # 列を左から、各列は上から5画素ずつ。digit0が最上段(下位桁)。
        acc = 0
        shift = 0
        bw = BitWriter()
        for x in range(720):
            col = levels[x::720]  # 列x(上から下へ720画素)
            for y in range(0, 720, 5):
                c = col[y:y + 5]
                value = c[0] + c[1] * 7 + c[2] * 49 + c[3] * 343 + c[4] * 2401
                bw.put(value & 0x3FFF, 14)
        return ScreenshotPage.from_stream(bw.bytes)


def missing_pages(pages) -> list:
    if not pages:
        return []
    total = pages[0].total
    present = {p.index for p in pages}
    return [i + 1 for i in range(total) if i not in present]


def assemble(pages) -> bytes:
    if not pages:
        raise TransferError("画像が選択されていません。")
    first = pages[0]
    if any(p.group_key != first.group_key for p in pages):
        raise TransferError("異なるファイルの画像が混在しています。")
    by_index = {}
    for p in pages:
        if p.index in by_index and by_index[p.index].payload != p.payload:
            raise TransferError("同じページ番号の内容が一致しません。")
        by_index[p.index] = p
    miss = missing_pages(pages)
    if miss:
        raise TransferError("不足ページ: " + ", ".join(map(str, miss)))
    stream = b"".join(by_index[i].payload for i in range(first.total))
    if first.compression == 1:
        data = lzss_decompress(stream, first.file_size)
    else:
        if len(stream) < first.file_size:
            raise TransferError("復元データがファイルサイズに足りません。")
        data = stream[:first.file_size]
    if crc16(data) != first.crc:
        raise TransferError("CRC不一致: 画像が劣化しているか、別の転送の画像が混在しています。保存を中止しました。")
    return data


def group_pages(pages) -> dict:
    groups = {}
    for p in pages:
        groups.setdefault(p.group_key, []).append(p)
    return groups


def export(pages, directory) -> str:
    """復元して保存する。既存ファイルは上書きせず ' (n)' を付ける。"""
    data = assemble(pages)
    name = pages[0].name
    filename = "".join("_" if c in "/\\:" or ord(c) < 32 else c for c in name[4:])
    if filename in (".", "..", ""):
        filename = "RECEIVED"
    kind = name[:4]
    if kind == KIND_TEXT:
        if len(data) % 2:
            raise TransferError("UTF-16テキストが不正です。")
        try:
            result = data.decode("utf-16-le").encode("utf-8")
        except UnicodeError:
            raise TransferError("UTF-16テキストが不正です。")
    elif kind == KIND_GRAPHICS:
        if len(data) < 4:
            raise TransferError("GRPヘッダーが不足しています。")
        w, h = integer(data, 0, 2), integer(data, 2, 2)
        if w <= 0 or h <= 0 or w * h * 4 != len(data) - 4:
            raise TransferError("GRPのサイズが一致しません。")
        from PIL import Image
        import io
        im = Image.frombuffer("RGBA", (w, h), bytes(data[4:]), "raw", "BGRA", 0, 1)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        result = buf.getvalue()
        filename += ".png"
    else:
        result = data
    stem, ext = os.path.splitext(filename)
    for suffix in range(10000):
        dest = os.path.join(directory, filename if suffix == 0 else f"{stem} ({suffix}){ext}")
        try:
            with open(dest, "xb") as f:
                f.write(result)
            return dest
        except FileExistsError:
            continue
    raise TransferError("保存先に同名ファイルが多すぎます。")


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def is_image(path) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


# ------------------------------------------------------------ serial (Linux, termios)
class Cancellation:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    @property
    def is_cancelled(self):
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise TransferError("送信を中止しました。Switch側もBボタンで受信を終了してください。")


def available_ports() -> list:
    """/dev/ttyUSB* と /dev/ttyACM* (USB-UART / Pro Micro系)。"""
    found = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return sorted(found, key=lambda p: (not is_usb_adapter(p), p.lower(), p))


def is_usb_adapter(path: str) -> bool:
    n = path.lower()
    return "ttyusb" in n or "ttyacm" in n


def choose_port(previous, current, selected: str) -> str:
    """抜き差しのあとの選択規則(Mac版と同じ)。"""
    if selected:
        return selected if selected in current else ""
    added = [p for p in current if p not in previous]
    return added[0] if len(added) == 1 else ""


def restored_port(saved: str, available) -> str:
    return saved if saved and saved in available else ""


def _run_serial(path, reports, cancellation, progress):
    if not path.startswith("/dev/"):
        raise TransferError("/dev/ttyUSB* や /dev/ttyACM* のシリアルポートを選択してください。")
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        hint = ""
        if e.errno == errno.EACCES:
            hint = "（dialout グループに入っていない可能性: sudo usermod -aG dialout $USER のあと再ログイン）"
        raise TransferError(f"ポートを開けません: {os.strerror(e.errno)}{hint}")
    try:
        try:
            original = termios.tcgetattr(fd)
        except termios.error:
            raise TransferError("シリアル設定を取得できません。")
        try:
            # 生モード 9600 8N1 RTS/CTSなし
            attrs = list(original)
            attrs[0] = 0                                  # iflag
            attrs[1] = 0                                  # oflag
            attrs[2] = termios.CS8 | termios.CLOCAL | termios.CREAD  # cflag
            attrs[3] = 0                                  # lflag
            attrs[4] = attrs[5] = termios.B9600
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            try:
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except termios.error:
                raise TransferError("9600bps 8N1の設定に失敗しました。")
            try:  # DTRアサート(多くのArduinoはこれでリセットされる)
                fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack("I", termios.TIOCM_DTR))
            except OSError:
                pass
            _protocol(fd, reports, cancellation, progress)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, original)
            except termios.error:
                pass
    finally:
        os.close(fd)


def _protocol(fd, reports, cancellation, progress):
    def wait(events, deadline, check=True):
        while time.monotonic() < deadline:
            if check:
                cancellation.check()
            p = select.poll()
            p.register(fd, events | select.POLLERR | select.POLLHUP | select.POLLNVAL)
            try:
                res = p.poll(50)
            except InterruptedError:
                continue
            for _, rev in res:
                if rev & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    raise TransferError("USB接続が切断されました。")
                if rev & events:
                    return
        raise TransferError("Arduinoから応答がありません。配線・ファームウェア・ポートを確認してください。")

    def write(data, check=True):
        deadline = time.monotonic() + 3
        offset = 0
        while offset < len(data):
            wait(select.POLLOUT, deadline, check)
            try:
                n = os.write(fd, data[offset:])
                offset += n
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                raise TransferError("シリアル書き込みに失敗しました。")

    def ack(deadline, check=True):
        while True:
            wait(select.POLLIN, deadline, check)
            try:
                if os.read(fd, 1):
                    return
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                raise TransferError("シリアル応答を読み取れません。")

    # リセット直後のゴミを捨て、ゼロバイト探査(DetectSerialStart)
    settle = time.monotonic() + 0.3
    while time.monotonic() < settle:
        cancellation.check()
        time.sleep(0.02)
    termios.tcflush(fd, termios.TCIOFLUSH)
    deadline = time.monotonic() + 5
    connected = False
    while time.monotonic() < deadline:
        cancellation.check()
        write(b"\0")
        p = select.poll()
        p.register(fd, select.POLLIN)
        if p.poll(100):
            ack(deadline)
            connected = True
            break
    if not connected:
        raise TransferError("Arduinoの初期応答がありません。")

    if reports is None:  # Sync Key検出
        def exchange(data, cancellable):
            write(data, cancellable)
            ack(time.monotonic() + 3, cancellable)

        def pause(duration):
            start = time.monotonic()
            while time.monotonic() - start < duration:
                cancellation.check()
                progress(min(1.0, (time.monotonic() - start) / duration))
                time.sleep(0.02)
            cancellation.check()
        sync_key_detection(exchange, pause)
        progress(1.0)
        return

    command = reports.command
    try:
        pending = 0
        count = max(1, len(reports))
        for i, report in enumerate(reports):
            cancellation.check()
            if pending >= 4:
                ack(time.monotonic() + 3)
                pending -= 1
            write(report)
            pending += 1
            if i % 16 == 0:
                progress(i / count)
        while pending > 0:
            ack(time.monotonic() + 3)
            pending -= 1
        progress(1.0)
    finally:
        # 正常・中止・失敗のどれでもキーを離す(切断時は0.25秒で打ち切るベストエフォート)
        release = bytearray(12)
        release[0] = command
        for modifier in (10, 2, 8, 0):
            release[7] = modifier
            try:
                write(bytes(release), False)
                ack(time.monotonic() + 0.25, False)
            except Exception:
                pass


def sync_key_detection(exchange, pause):
    """検出手順: 01+0*11 → 4秒待ち → 全0パケットを2回(中止を無視)。"""
    failure = None
    try:
        exchange(bytes([1]) + bytes(11), True)
        pause(4)
    except Exception as e:
        failure = e
    for _ in range(2):
        try:
            exchange(bytes(12), False)
        except Exception as e:
            if failure is None:
                failure = e
    if failure is not None:
        raise failure


def send(path, reports, cancellation, progress=lambda v: None):
    _run_serial(path, reports, cancellation, progress)


def detect_sync_key(path, cancellation, progress=lambda v: None):
    _run_serial(path, None, cancellation, progress)
