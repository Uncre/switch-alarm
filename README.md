# switch-alarm

Webで操作できる、自宅Linux用の目覚ましアラームです。
Python標準ライブラリだけで動きます（再生に mpv、取得に yt-dlp、音声基盤に PipeWire / PulseAudio を使います）。

## 仕様

- 既定のスケジュール: **平日 07:00 / 土日 09:00**
- **アラームは自動で止まりません。** 選ばれた曲をループ再生し続け、Web画面かAPIで止めるまで鳴ります（`auto_stop_minutes` を 0 以外にすると自動停止も可能）
- 日付指定（オーバーライド）: 特定の日だけ時刻を変える / 鳴らさない / 曲を変える、ができます。翌日以降は既定に戻ります
- 曲は yt-dlp で URL 指定して追加します。**追加した曲はすべてランダム再生の候補**に入ります
- 再生モード: ランダム / 曲指定（1曲固定）
- 音量は2系統: **本番アラームは `volume`（既定100% = Max）**、曲の手動再生と音響テストは `test_volume`（既定25%）で小さく鳴ります。どちらの再生でも、開始時にその音量がデフォルト出力へセットされます
- Web画面でできること: 状態と次回アラームの確認、設定変更、停止、今すぐ再生、音響テスト、曲の追加・再生・指定・削除、日付指定

## セットアップ（アラームを鳴らすマシンで）

必要なもの: `python3`、`mpv`、`ffmpeg`、`pactl`/`paplay`（PipeWire か PulseAudio）

```sh
git clone https://github.com/Uncre/switch-alarm.git
cd switch-alarm
./install.sh   # yt-dlp を ~/.local/bin に入れ、@reboot の crontab 登録と起動まで行います
```

ブラウザで `http://<マシンのIP>:8767/` を開いてください（既定ポートは環境変数 `ALARM_PORT` で変更可）。

常駐はユーザーの crontab `@reboot` + `start.sh` の再起動ループで行います（sudo不要）。
systemd --user で動かしたい場合は `switch-alarm.service` も同梱しています（linger有効化が必要）。

データは `data/` 以下に置かれます（`config.json` / `tracks.json` / `state.json` / `music/`）。
このフォルダは git 管理外です。

## API

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/status` | 設定・曲一覧・再生状態・次回アラーム |
| POST | `/api/config` | 設定変更 `{weekday_time, weekend_time, volume（本番）, test_volume（テスト）, mode, specific_track_id, auto_stop_minutes, enabled}` |
| POST | `/api/stop` | 再生を止める |
| POST | `/api/test-sound` | 音響テスト（短いテスト音） |
| POST | `/api/play` | 曲を1回再生 `{track_id}` |
| POST | `/api/fire` | アラーム再生を今すぐ開始（ループ） |
| POST | `/api/tracks` | 曲追加（yt-dlp、非同期） `{url}` → job id |
| GET | `/api/jobs/<id>` | 追加ジョブの進捗 |
| DELETE | `/api/tracks/<id>` | 曲削除 |
| POST | `/api/override` | 日付指定 `{date, time, skip, mode, track_id}` |
| DELETE | `/api/override/<date>` | 日付指定の解除 |

CLI ラッパー `alarmctl.py` も使えます:

```sh
python3 alarmctl.py status
python3 alarmctl.py override --date 2026-11-05 --time 06:30
python3 alarmctl.py override --date 2026-11-06 --skip
python3 alarmctl.py stop
```

## 注意

- 認証はありません。自宅LAN / Tailscale 内だけで使う前提です。インターネットに直接公開しないでください
- 音が出るのは、PipeWire/PulseAudio のデフォルト出力（スピーカー等）です。ユーザーセッションで音声サーバが動いている必要があります
- yt-dlp は YouTube 側の仕様変更で古い版が動かなくなることがあるため、`install.sh` は公式の単体ビルドを `~/.local/bin/yt-dlp` に置き、アプリはそちらを優先します
