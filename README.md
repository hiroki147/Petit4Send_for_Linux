# Petit4Send for Linux

プチコン4(Nintendo Switch)とPCのあいだでファイルをやり取りする **Petit4Send** の Linux 版です。
Python 3(Tkinter)で書かれており、GUI と CLI の両方で使えます。

## このソフトの成り立ち

1. **Windows版(原作)** — Rei HOBARA / REI SOFTWARE 作の Petit4Send 1.2.2(.NET)。一次配布サイト: http://rei.to/petit4send.html
   同梱の説明書が `readme_ja.txt`(このフォルダに原文のまま入れてあります)。
2. **Mac版** — 原作を Swift / SwiftUI で再実装した Petit4Send for Mac。https://github.com/kazenif/Petit4Send_for_mac
3. **Linux版(これ)** — Mac版のソースを読み、同じ仕様で Python に移植したもの。

原作者・Mac版の作者のどちらの公式版でもありません。`readme_ja.txt` は二次配布・商用利用・改造・リバースエンジニアリングを自由としています。
その表記と原作者の著作権表示は保持してください。Linux版も同様に、改造・再配布は自由です。

## できること

- **Linux → プチコン4**(USB送信): TXT / DAT / GRP(画像)を、Pro Micro 経由のキーボード/マウス信号として送ります。最大約296バイト/秒(2370bps)。
  圧縮は LZSS / 無圧縮 / 自動、誤り検出は CRC16(Koopman)。
- **プチコン4 → Linux**(画像から復元): プチコン4の `SCREENSHOT SEND` が画面に出す模様をスクリーンショットで保存し、そこからファイルを復元します。1枚あたり約181kバイト(無圧縮時)。
- Sync Key の検出(同期が合わない環境向け)。

対象外(Windows版にあるが未移植): キーボード/マウスの常時エミュレーション、タイピング送信、ファームウェア書き込み機能。

## 必要なもの

ハード(原作 `readme_ja.txt` より):
- Pro Micro(SparkFun 5V/16MHz。USBゲスト機能とハードウェアシリアルがあるArduino Unoなども可)
- 2台構成(Switch側とPC側にPro Micro各1台)、または Pro Micro 1台 + USB-UART。
  **このLinux版は、Mac版と同じく USB-UART(または PC側Pro Micro)をPCにつなぎ、Switch側のPro MicroをSwitchにつなぐ構成が前提**です。
- Pro Micro には原作配布物のファームウェア(`Petit4Send.ino.promicro.hex`、PC側は `USBUART.ino.promicro.hex`)を書き込んでおきます。
  これらは原作の配布パッケージに入っており、このzipには含みません。Windows版の「Write Firmware」機能は移植していないので、
  Linuxでは avrdude など別の書き込みツールを使います(コマンド例は未検証)。
- Nintendo Switch、プチコン4、microSDカード。プチコン4側では `P4SEND122.PRG`(原作配布物)を使います。

ソフト:

    sudo apt install python3-tk python3-pil        # Pillow は画像(GRP送信・画像復元)で使用
    sudo usermod -aG dialout $USER                 # シリアルポートの権限。反映には再ログイン(Arch系は uucp)

ModemManager が `/dev/ttyACM*` を掴むと開けないことがあります。その場合は `sudo systemctl stop ModemManager` を試してください。

## 使い方

    python3 p4send.py                  # GUI(「USB送信」と「画像から復元」の2タブ)
    python3 p4send.py ports            # シリアルポート一覧
    python3 p4send.py send FILE -p /dev/ttyUSB0 [-n 名前] [-k txt|dat|grp] [-c auto|lzss|none] [-s -1..24]
    python3 p4send.py detect -p /dev/ttyUSB0
    python3 p4send.py restore page-1.png page-2.png -o 出力フォルダ

### Linux → プチコン4

1. USB-UART(または PC側Pro Micro)をPCに、Switch側Pro MicroをSwitchにつなぎます。
2. プチコン4で `P4SEND122.PRG` → `USB RECEIVE` を開き、`WAITING FILE...` にします。
3. 送るファイルを選びます。種類(TXT/DAT/GRP)は拡張子から自動で入ります。シリアルポートも選びます。
4. 「送信できます」と出たら「Switchへ送信」を押します(CLIなら `send`)。圧縮は「自動」、Sync Keyは「自動(-1)」が通常です。
5. プチコン4側でファイル名・サイズ・種類を確認して待ち、完了したら **A ボタン**で保存します。
   Linux側が分かるのは Arduino までの受信確認で、Switch 側の保存の成否は分かりません。
   中止したときは、プチコン4側も B ボタンで受信を終えてください。

### 「Switch側の名前」とは

送ったファイルを**プチコン4(Switch)側でどんな名前で受け取って保存するか**、という名前です(Linux上のファイル名ではありません)。
プチコン4の受信画面に表示され、保存するときのファイル名になります(プチコン4上では `TXT:名前` のように種類付きで扱われます)。

- 初期値は、選んだファイル名を**大文字にした先頭32文字**です。必要なら手で直せます。
- **半角ASCII 1〜32文字**で、`:` `/` `\` は使えません。条件を満たさないと「Switchへ送信」は押せません。
- 日本語名などは使えないので、英数字の名前に直してください。

### 種類

| 種類 | 拡張子の例 | 送り方 |
|---|---|---|
| TXT | `.TXT` `.PRG` | UTF-8 / BOM付きUTF-16 / シフトJISを読み、BOMなしUTF-16LEで送信 |
| DAT | `.FONT` `.DAT` `.CMT` `.D88` `.BIN` `.ROM` `.I80` `.I88` `.T88` `.N80` | バイナリのまま。Switch側は4バイト単位で保存するため、4の倍数でないと末尾にゼロが付くことがあります |
| GRP | `.JPG` `.PNG` `.JPEG` | 行順のBGRA 32bit画素で送信 |

ファイルを選んだ直後だけ拡張子で種類を決め、その後に手で変えた種類は次のファイルを選ぶまで保持します。
空ファイルは送れません。入力・展開後サイズの上限は64 MiBです(296バイト/秒なので大きいファイルは非常に時間がかかります)。

### Sync Key を検出する

送信がうまく同期しないときだけ使います。

1. プチコン4で `P4SEND122.PRG` → `DETECT SYNC KEY` を開き、`WAITING SYNC KEY...` にします。
2. 「Detect Sync Key」(CLIなら `detect`)を実行すると約4秒の検出信号が出て、自動で止まります。
3. プチコン4に表示された `SYNC KEY: 0〜24` の値を、Sync Key(CLIなら `-s`)に設定します。値はPCには自動で返ってきません。
4. プチコン4で B ボタンで戻り、`USB RECEIVE` を選んで送信します。

### プチコン4 → Linux

1. プチコン4の `SCREENSHOT SEND` で対象ファイルを選び、表示された**全ページ**をスクリーンショットで保存します。
2. 画像をPCに取り込み、「画像から復元」タブの「画像を追加…」で全ページを選びます(順不同でOK)。
3. 「復元して保存…」で保存先フォルダを選びます(CLIなら `restore`)。

1280×720の原寸、または左側720×720を原寸で切り出した画像に対応します。拡大縮小・SNS経由の再圧縮・カメラ撮影には対応しません。
ページが足りない、同じページの内容が違う、CRCが合わない場合は保存しません。TXTはUTF-8、DATは生バイナリ、GRPはPNGで保存し、既存ファイルは上書きせず ` (1)` のような連番を付けます。

## Mac版との違い

- シリアルポートは `/dev/ttyUSB*` と `/dev/ttyACM*`(Macの `/dev/cu.*` 相当)。一覧は2秒ごとに再列挙し、抜き差しのときの選択規則はMac版と同じです。
- 最後に選んだポートは `~/.config/petit4send.json` に記憶します。
- 通信は termios で 9600bps 8N1・DTRアサート。接続確認、未ACK 4個まで先行、終了時のキー解放、Sync Key検出の停止パケット2回はMac版どおりです。
- 送信中にウィンドウを閉じても、キー解放が済んでから終了します。
- ドラッグ&ドロップによる画像追加は未実装です(「画像を追加…」を使います)。

## プロトコルの資料

通信仕様の詳細(USBバイト列、HIDレポート、LZSS/CRC、スクリーンショットの形式)は、Mac版に同梱の `PROTOCOL.md` がそのまま当てはまります。

## テスト

    python3 -m unittest tests.test_core

CRC基準値、LZSSの往復、HIDレポートをSwitch側と同じ手順で戻す検証、2ページ画像フィクスチャ(`tests/Fixtures`、Mac版から複製)での復元、
擬似端末(pty)でArduino役を動かした送信手順などを含みます。別の場所のフィクスチャを使うときは `P4_FIXTURES` で指定してください。

## 検証状況

- ソフトウェアテストは通っています。
- Linux版の動作確認ができました(利用者による確認)。
