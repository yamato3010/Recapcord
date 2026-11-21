# Recapcord

Discordのボイスチャンネルで話した内容を、Botを動かすPC上の Whisper で文字起こしします。録音終了後、話者のDiscord表示名と録音開始からの経過時刻を付けて `transcripts/` にMarkdownを保存します。

## Discordの設定

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリを作成し、**Bot** でトークンを取得します。
2. **Installation** の **Guild Install** で `bot` と `applications.commands` を選び、サーバーにBotを追加します。
3. 対象のボイスチャンネルでBotに **チャンネルを見る**、**接続**、**メッセージを送信** の権限を付けます。録音開始のお知らせはボイスチャンネル付属のテキストチャットに投稿されます。

## Docker Composeで起動

Dockerが起動している状態で、このフォルダから実行します。

```sh
cp -n .env.example .env
mkdir -p transcripts
```

`.env` の `DISCORD_TOKEN` にBotトークンを設定します。`DISCORD_GUILD_ID` にテスト用サーバーのIDを設定すると、そのサーバーにコマンドが登録されます。

ヤッチョ風のセリフを使うには、`.env` に `YACHIYO_MODE=true` を設定します。未設定または `false` の場合は通常の案内文になります。設定変更後は `docker compose up -d` で反映します。

```sh
docker compose up -d --build
docker compose logs -f bot
```

ログに `Logged in as` が表示されたらDiscordからコマンドを使えます。文字起こしはホストの `transcripts/` に保存されます。WhisperのモデルはDockerボリュームにキャッシュされ、コンテナを作り直しても再利用されます。ファイル名の日付と時刻には `.env` の `TZ` が使われます。

停止する場合はDiscordで `/recap_stop` を実行し、保存完了を待ってから `docker compose down` を実行してください。

## 使い方

1. ボイスチャンネルに入り、参加者に録音することを伝えます。
2. Discordで `/recap_start` を実行します。Botが参加し、ボイスチャンネルのチャットに録音開始を投稿します。
3. `/recap_stop` を実行します。録音を開始した人と「チャンネルの管理」権限を持つ人が停止できます。Botが切断された場合も、その時点までの音声を文字起こしします。
4. 処理完了後、`/recap_stop` を実行した人への返信にMarkdownファイルが添付されます。返信に失敗した場合はその人へのDMを試みます。Botが切断された場合は録音を開始した人にDMします。ファイルはホストの `transcripts/` にも保存されます。

```markdown
# トランスクリプト

録音開始日時: 2026-11-21 18:30:00+09:00

- `00:00:03` **山田** — じゃあ、今週の進捗から確認しましょう。
- `00:00:08` **佐藤** — テスト環境の準備が終わりました。
```

最初の文字起こし時にモデルをダウンロードします。音声はPC上で処理し、録音中のWAVはホストの `transcripts/.pending/` に保存します。Markdownの保存に成功すると、対応するWAVを削除します。

文字起こしに失敗した場合は、通知または起動ログに出る録音IDを使って、同じサーバーで `/recap_retry recording_id:<録音ID>` を実行できます。録音を開始した人と「チャンネルの管理」権限を持つ人が再試行できます。停止済みの録音はBotの再起動後も再試行できます。失敗した録音の音声は再試行に成功するまでディスクに残ります。録音の途中でBotを終了すると再試行用の情報が作成されないため、終了前に `/recap_stop` を実行してください。

標準設定は日本語の `small` モデルで、CPUを使います。`WHISPER_LANGUAGE=` で言語を自動判定し、`WHISPER_MODEL=base` で軽量モデルに切り替えられます。

DiscordのDAVE暗号化音声を受信するため、[Pycordの音声受信対応PR](https://github.com/Pycord-Development/pycord/pull/3159) のコミットを固定しています。
