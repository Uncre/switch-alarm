# switch-alarm

Webブラウザから操作できる、自宅Linuxマシン用の目覚ましアラームです。
Pythonの標準ライブラリのみで動作し、外部ツールとして音声再生に `mpv`、音源取得に `yt-dlp`、音声出力基盤に `PipeWire` または `PulseAudio` を使用します。

## 主な特徴と仕様

**スケジュールと動作**

* **既定のスケジュール**: 平日 07:00 / 土日 09:00
* **停止するまでループ再生**: アラームは自動では止まりません。Web画面またはAPIから停止するまで、選択された曲をループ再生し続けます（`auto_stop_minutes` に0以外の値を設定すると自動停止も可能）。
* **日付指定（オーバーライド）**: 特定の日だけ「時刻を変える」「鳴らさない（スキップ）」「曲を変える」といった一時的な変更が可能です。翌日以降は既定の設定に戻ります。

**楽曲と再生モード**

* **曲の追加**: URLを指定すると `yt-dlp` で楽曲を取得・追加します。追加した曲はすべてランダム再生の候補に入ります。
* **再生モード**: 「ランダム再生」または「曲指定（1曲固定）」から選択できます。

**2系統の音量設定**
再生開始時に、それぞれの設定音量がシステムのデフォルト出力へ自動的に適用されます。

* **本番アラーム (`volume`)**: 既定値 100%（最大音量）
* **手動再生・音響テスト (`test_volume`)**: 既定値 25%（控えめな音量）

**Web画面でできること**

* 現在の状態と次回アラーム時刻の確認
* 各種設定の変更、日付指定の登録・解除
* アラームの停止、今すぐ再生、音響テスト
* 曲の追加・試聴（手動再生）・固定曲の指定・削除

## セットアップ

アラームを鳴らすLinuxマシン上でセットアップを行います。

**必要なパッケージ**

* `python3`
* `mpv`
* `ffmpeg`
* `pactl` / `paplay`（PipeWire または PulseAudio 環境）

**インストールと起動**

```sh
git clone https://github.com/Uncre/switch-alarm.git
cd switch-alarm
./install.sh

```

`install.sh` を実行すると、`~/.local/bin` への `yt-dlp` の配置、ユーザーの `crontab`（`@reboot`）への自動起動登録、および初回起動までが自動で行われます。

起動後、ブラウザで `http://<マシンのIP>:8767/` を開いてください（ポート番号は環境変数 `ALARM_PORT` で変更可能）。

**常駐とデータ保存について**

* **常駐の仕組み**: 既定ではユーザーの `crontab`（`@reboot`）と `start.sh` の再起動ループで動作するため、`sudo` 権限は不要です。`systemd --user` で管理したい場合は、同梱の `switch-alarm.service` も利用できます（要 `linger` 有効化）。
* **データ保存先**: 設定や楽曲データはすべて `data/` ディレクトリ以下（`config.json` / `tracks.json` / `state.json` / `music/`）に保存されます。このディレクトリは git の管理対象外です。

## API・CLI操作

**HTTP API エンドポイント**

| メソッド | パス | 説明 |
| --- | --- | --- |
| GET | `/api/status` | 設定・曲一覧・再生状態・次回アラーム時刻の取得 |
| POST | `/api/config` | 設定変更 `{weekday_time, weekend_time, volume, test_volume, mode, specific_track_id, auto_stop_minutes, enabled}` |
| POST | `/api/stop` | 再生の停止 |
| POST | `/api/test-sound` | 音響テスト（短いテスト音を再生） |
| POST | `/api/play` | 指定した曲を1回再生 `{track_id}` |
| POST | `/api/fire` | アラーム再生を今すぐ開始（ループ再生） |
| POST | `/api/tracks` | 曲の非同期追加（yt-dlp使用） `{url}` → `job id` を返却 |
| GET | `/api/jobs/<id>` | 曲追加ジョブの進捗確認 |
| DELETE | `/api/tracks/<id>` | 曲の削除 |
| POST | `/api/override` | 日付指定の登録 `{date, time, skip, mode, track_id}` |
| DELETE | `/api/override/<date>` | 日付指定の解除 |

**CLIラッパー (`alarmctl.py`)**

コマンドラインからも状態確認や操作が可能です。

```sh
python3 alarmctl.py status
python3 alarmctl.py override --date 2026-11-05 --time 06:30
python3 alarmctl.py override --date 2026-11-06 --skip
python3 alarmctl.py stop

```

## 注意事項

* **ネットワークとセキュリティ**: 認証機能はありません。自宅LANやTailscale内でのみ使用する前提のため、インターネットへ直接公開しないでください。
* **音声出力環境**: 音は PipeWire / PulseAudio のデフォルト出力（スピーカーなど）から再生されます。対象ユーザーのセッションで音声サーバーが起動している必要があります。
* **yt-dlp のバージョン管理**: YouTube側の仕様変更により古いバージョンが動作しなくなるのを防ぐため、`install.sh` は公式の単体ビルドを `~/.local/bin/yt-dlp` に配置し、アプリ側でもそちらを優先して使用します。
